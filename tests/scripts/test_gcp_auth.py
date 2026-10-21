"""Tests for scripts/gcp_auth.py.

The metadata server, the file system and google-auth are mocked, so
no test needs GCP or a network.
"""

import json
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import gcp_auth as module
import pytest
from gcp_auth import (
    GCPAuthSession,
    _auth_from_json,
    _detect_environment,
    _get_adc_path,
    gcp_auth,
    gcp_billing,
    gcp_is_authenticated,
    gcp_project,
    gcp_session_info,
    gcp_token,
)


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh session.

    gcp_auth() keeps its state in a module global, so without this a
    test could pass or fail depending on the one before it.

    Args:
        monkeypatch: Swaps the module global.
    """
    monkeypatch.setattr(module, "_session", GCPAuthSession())


def _write_json(path: Path, content: dict[str, str]) -> str:
    """Write a credentials file.

    Args:
        path: Where to write it.
        content: The JSON object to write.

    Returns:
        The path, as a string.
    """
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


class TestDetectEnvironment:
    """_detect_environment() picks gce, adc or interactive."""

    def test_returns_gce_when_metadata_server_responds(self) -> None:
        """A 200 from the metadata server means GCP."""
        response = Mock(status=200)

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            assert _detect_environment() == "gce"

        urlopen.assert_called_once()
        assert urlopen.call_args.kwargs == {"timeout": 1}

    def test_sends_metadata_flavor_header_on_the_request(self) -> None:
        """The header rides on a Request object.

        urlopen() has no headers parameter. Passing one raises a
        TypeError that the broad except would swallow, so GCP would
        never be detected.
        """
        response = Mock(status=200)

        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            _detect_environment()

        assert "headers" not in urlopen.call_args.kwargs
        sent = urlopen.call_args.args[0]
        assert isinstance(sent, urllib.request.Request)
        assert sent.get_header("Metadata-flavor") == "Google"
        assert sent.full_url == module.METADATA_URL

    def test_falls_back_to_adc_when_metadata_unreachable(self) -> None:
        """Off GCP, a credentials file means adc."""
        with (
            patch("urllib.request.urlopen", side_effect=OSError("down")),
            patch("gcp_auth._get_adc_path", return_value="/fake/adc.json"),
        ):
            assert _detect_environment() == "adc"

    def test_falls_back_to_interactive_when_nothing_found(self) -> None:
        """With no metadata server and no file, it reports interactive.

        It does not raise: gcp_auth() decides whether that is fatal.
        """
        with (
            patch("urllib.request.urlopen", side_effect=OSError("down")),
            patch(
                "gcp_auth._get_adc_path",
                side_effect=FileNotFoundError("no creds"),
            ),
        ):
            assert _detect_environment() == "interactive"

    def test_non_200_from_metadata_server_is_not_gce(self) -> None:
        """Only a 200 counts as being on GCP."""
        with (
            patch("urllib.request.urlopen", return_value=Mock(status=404)),
            patch("gcp_auth._get_adc_path", return_value="/fake/adc.json"),
        ):
            assert _detect_environment() == "adc"


class TestGetAdcPath:
    """_get_adc_path() finds the credentials file."""

    def test_finds_credentials_from_env_var(self) -> None:
        """GOOGLE_APPLICATION_CREDENTIALS wins when its file exists."""
        with (
            patch("gcp_auth.os.getenv", return_value="C:/keys/creds.json"),
            patch("gcp_auth.os.path.exists", return_value=True),
        ):
            assert _get_adc_path() == "C:/keys/creds.json"

    def test_finds_gcloud_file_under_appdata_on_windows(self) -> None:
        """On Windows gcloud keeps the file under %APPDATA%/gcloud."""

        def fake_getenv(key: str, default: str = "") -> str:
            """Return a Windows APPDATA and nothing else.

            Args:
                key: The variable's name.
                default: Returned for any other variable.

            Returns:
                The fake value.
            """
            return {"APPDATA": "C:/Users/test/AppData/Roaming"}.get(
                key, default
            )

        with (
            patch("gcp_auth.os.getenv", side_effect=fake_getenv),
            patch("gcp_auth.os.path.exists", return_value=False),
            patch("gcp_auth.sys.platform", "win32"),
            patch("gcp_auth.Path.exists", return_value=True),
        ):
            result = Path(_get_adc_path())

        assert result.parts[-2:] == ("gcloud", module.ADC_FILE)

    def test_finds_gcloud_file_under_config_elsewhere(self) -> None:
        """On Mac and Linux the file is under ~/.config/gcloud."""
        with (
            patch("gcp_auth.os.getenv", return_value=""),
            patch("gcp_auth.sys.platform", "linux"),
            patch("gcp_auth.Path.exists", return_value=True),
        ):
            result = Path(_get_adc_path())

        assert result.parts[-3:] == (".config", "gcloud", module.ADC_FILE)

    def test_raises_when_no_credentials_found(self) -> None:
        """No env var and no default file is a FileNotFoundError."""
        with (
            patch("gcp_auth.os.getenv", return_value=""),
            patch("gcp_auth.os.path.exists", return_value=False),
            patch("gcp_auth.Path.exists", return_value=False),
        ):
            with pytest.raises(FileNotFoundError, match="No credentials"):
                _get_adc_path()


class TestAuthFromJson:
    """_auth_from_json() loads both kinds of credentials file."""

    def test_loads_service_account_key(self, tmp_path: Path) -> None:
        """A service account key goes to from_service_account_file.

        Args:
            tmp_path: Folder for the key file.
        """
        path = _write_json(tmp_path / "sa.json", {"type": "service_account"})
        creds = MagicMock()

        with patch(
            "gcp_auth.Credentials.from_service_account_file",
            return_value=creds,
        ) as from_file:
            assert _auth_from_json(path) is creds

        from_file.assert_called_once_with(path)

    def test_refreshes_user_credentials_directly(self, tmp_path: Path) -> None:
        """User credentials get an access token from Google's endpoint.

        Args:
            tmp_path: Folder for the credentials file.
        """
        path = _write_json(
            tmp_path / "user.json",
            {
                "type": "authorized_user",
                "client_id": "id",
                "client_secret": "not-a-secret",
                "refresh_token": "refresh",
            },
        )
        response = Mock(status_code=200)
        response.json.return_value = {"access_token": "access"}

        with patch("requests.post", return_value=response) as post:
            creds = _auth_from_json(path)

        assert post.call_args.args[0] == module.TOKEN_URI
        assert post.call_args.kwargs["data"]["grant_type"] == ("refresh_token")
        assert creds.token == "access"

    def test_refused_refresh_says_how_to_log_in_again(
        self, tmp_path: Path
    ) -> None:
        """A failed refresh names Google's error and the fix.

        Args:
            tmp_path: Folder for the credentials file.
        """
        path = _write_json(tmp_path / "user.json", {"type": "authorized_user"})
        response = Mock(status_code=400)
        response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        }

        with patch("requests.post", return_value=response):
            with pytest.raises(ValueError, match="invalid_grant") as err:
                _auth_from_json(path)

        assert "application-default login" in str(err.value)

    def test_raises_on_invalid_json(self, tmp_path: Path) -> None:
        """Malformed JSON raises rather than being swallowed.

        Args:
            tmp_path: Folder for the broken file.
        """
        path = tmp_path / "broken.json"
        path.write_text("{not valid json", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            _auth_from_json(str(path))

    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        """A path that doesn't exist is a FileNotFoundError.

        Args:
            tmp_path: Folder that does not hold the file.
        """
        with pytest.raises(FileNotFoundError, match="not found"):
            _auth_from_json(str(tmp_path / "absent.json"))

    def test_raises_when_type_missing(self, tmp_path: Path) -> None:
        """A file with no "type" is reported as malformed.

        Args:
            tmp_path: Folder for the credentials file.
        """
        path = _write_json(tmp_path / "x.json", {"project_id": "p"})

        with pytest.raises(ValueError, match="malformed"):
            _auth_from_json(path)

    def test_raises_on_unsupported_type(self, tmp_path: Path) -> None:
        """Only service_account and authorized_user are supported.

        Args:
            tmp_path: Folder for the credentials file.
        """
        path = _write_json(tmp_path / "x.json", {"type": "external_account"})

        with pytest.raises(ValueError, match="Unsupported credential"):
            _auth_from_json(path)


class TestGcpAuth:
    """gcp_auth() authenticates and fills the session."""

    def test_auto_detects_and_authenticates_via_adc(self) -> None:
        """The everyday laptop path ends with a usable session."""
        creds = MagicMock()

        with (
            patch("gcp_auth._detect_environment", return_value="adc"),
            patch("gcp_auth._get_adc_path", return_value="/fake/adc.json"),
            patch("gcp_auth._auth_from_json", return_value=creds),
            patch("gcp_auth.Request"),
        ):
            gcp_auth(project="my-project")

        assert gcp_is_authenticated() is True
        assert gcp_project() == "my-project"
        assert gcp_billing() == "my-project"
        assert gcp_token() is creds
        creds.refresh.assert_called_once()

    def test_uses_google_default_on_gcp(self) -> None:
        """On GCP the project comes from google.auth.default()."""
        creds = MagicMock()

        with (
            patch("gcp_auth._detect_environment", return_value="gce"),
            patch(
                "gcp_auth.google.auth.default",
                return_value=(creds, "gcp-project"),
            ),
            patch("gcp_auth.Request"),
        ):
            gcp_auth(project="ignored")

        assert gcp_project() == "gcp-project"
        assert module._session.method == "gce"

    def test_json_path_forces_service_account(self, tmp_path: Path) -> None:
        """Giving a key file skips detection.

        Args:
            tmp_path: Folder for the key file.
        """
        path = _write_json(tmp_path / "sa.json", {"type": "service_account"})
        creds = MagicMock()

        with (
            patch("gcp_auth._detect_environment") as detect,
            patch("gcp_auth._auth_from_json", return_value=creds),
            patch("gcp_auth.Request"),
        ):
            gcp_auth(project="my-project", json_path=path)

        detect.assert_not_called()
        assert module._session.method == "service_account"

    def test_service_account_needs_an_existing_key(self) -> None:
        """method="service_account" without a key file fails."""
        with pytest.raises(ValueError, match="json_path"):
            gcp_auth(project="my-project", method="service_account")

    def test_unknown_method_is_rejected(self) -> None:
        """An unknown method names the supported ones."""
        with pytest.raises(ValueError, match="Unsupported method"):
            gcp_auth(project="my-project", method="oauth")

        assert gcp_is_authenticated() is False

    def test_raises_when_no_gce_and_no_adc_credentials(self) -> None:
        """Interactive falls back to ADC, whose error is surfaced."""
        with (
            patch("gcp_auth._detect_environment", return_value="interactive"),
            patch(
                "gcp_auth._get_adc_path",
                side_effect=FileNotFoundError("no creds"),
            ),
        ):
            with pytest.raises(ValueError, match="Authentication failed"):
                gcp_auth(project="my-project")


class TestSessionAccessors:
    """The accessors refuse to answer before gcp_auth() succeeds."""

    def test_token_before_auth_raises(self) -> None:
        """gcp_token() needs a successful gcp_auth()."""
        with pytest.raises(ValueError, match="Not authenticated"):
            gcp_token()

    def test_project_before_auth_raises(self) -> None:
        """gcp_project() needs a project."""
        with pytest.raises(ValueError, match="No project set"):
            gcp_project()

    def test_billing_falls_back_to_project(self) -> None:
        """With no billing project, the session's project is used."""
        module._session.project = "my-project"

        assert gcp_billing() == "my-project"

    def test_session_info_prints_state(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """gcp_session_info() shows unset values plainly.

        Args:
            capsys: Captures what is printed.
        """
        gcp_session_info()

        out = capsys.readouterr().out
        assert "Authenticated: False" in out
        assert "Project:       not set" in out
