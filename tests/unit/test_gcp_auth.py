"""Unit tests for the gcp_auth module.

Tests cover environment detection, ADC path resolution, and
credential loading without requiring a live GCP environment. All
external dependencies (metadata server, filesystem, google-auth) are
mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test name describes what is being verified
- Transparent: No hardcoded secrets; mocks are explicit
"""

import json
import urllib.request

import pytest
from unittest.mock import MagicMock, Mock, patch

from py_common.gcp_auth import (
    GCPAuthSession,
    _auth_from_json,
    _detect_environment,
    _get_adc_path,
    gcp_auth,
    gcp_is_authenticated,
    gcp_project,
    gcp_token,
)


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch):
    """Give every test a fresh, isolated GCPAuthSession.

    gcp_auth() stores state on a module-level singleton, so without
    this reset, authentication state from one test would leak into
    the next depending on run order.

    Args:
        monkeypatch: pytest fixture used to swap the module global.

    Returns:
        None
    """
    monkeypatch.setattr("py_common.gcp_auth._session", GCPAuthSession())


class TestDetectEnvironment:
    """Test _detect_environment()."""

    def test_returns_gce_when_metadata_server_responds(self):
        """Metadata server reachable and returns 200 → "gce".

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If environment is not detected as "gce"
        """
        mock_response = Mock()
        mock_response.status = 200

        with patch(
            "urllib.request.urlopen", return_value=mock_response
        ) as mock_urlopen:
            assert _detect_environment() == "gce"

        mock_urlopen.assert_called_once()
        assert mock_urlopen.call_args.kwargs == {"timeout": 1}

    def test_sends_metadata_flavor_header_via_request_object(self):
        """Metadata header is sent on a Request, not a urlopen kwarg.

        urlopen() has no `headers` parameter, so passing
        headers=... directly to it raises TypeError in real usage
        (silently swallowed by the surrounding except Exception,
        which would always skip GCE detection). The header must
        instead be attached to a urllib.request.Request object that
        is passed as urlopen's first argument.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If urlopen is not called with a Request
                carrying the Metadata-Flavor header, or if it is
                called with a headers keyword argument instead
        """
        mock_response = Mock()
        mock_response.status = 200

        with patch(
            "urllib.request.urlopen", return_value=mock_response
        ) as mock_urlopen:
            _detect_environment()

        assert "headers" not in mock_urlopen.call_args.kwargs

        sent_request = mock_urlopen.call_args.args[0]
        assert isinstance(sent_request, urllib.request.Request)
        assert sent_request.get_header("Metadata-flavor") == "Google"
        assert sent_request.full_url == (
            "http://metadata.google.internal/computeMetadata/v1/" "instance/id"
        )

    def test_falls_back_to_adc_when_metadata_server_unreachable(self):
        """Metadata server unreachable, ADC file present → "adc".

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If environment is not detected as "adc"
        """
        with (
            patch(
                "urllib.request.urlopen", side_effect=OSError("unreachable")
            ),
            patch(
                "py_common.gcp_auth._get_adc_path",
                return_value="/fake/adc.json",
            ),
        ):
            assert _detect_environment() == "adc"

    def test_falls_back_to_interactive_when_both_unavailable(self):
        """Metadata server and ADC both unavailable → "interactive".

        _detect_environment() does not raise even when nothing is
        found; it reports "interactive" and leaves the decision of
        whether that is fatal to the caller (see gcp_auth(), which
        maps "interactive" to method "adc" and raises there instead).

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If environment is not "interactive", or
                if a FileNotFoundError propagates out of this call
        """
        with (
            patch(
                "urllib.request.urlopen", side_effect=OSError("unreachable")
            ),
            patch(
                "py_common.gcp_auth._get_adc_path",
                side_effect=FileNotFoundError("no creds"),
            ),
        ):
            assert _detect_environment() == "interactive"


class TestGetAdcPath:
    """Test _get_adc_path()."""

    def test_finds_credentials_from_env_var(self):
        """GOOGLE_APPLICATION_CREDENTIALS points at an existing file.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If the env var path is not returned
        """
        with (
            patch(
                "py_common.gcp_auth.os.getenv",
                return_value="C:/keys/creds.json",
            ),
            patch("py_common.gcp_auth.os.path.exists", return_value=True),
        ):
            assert _get_adc_path() == "C:/keys/creds.json"

    def test_finds_default_adc_location_when_no_env_var(self):
        """No env var set, but the default gcloud ADC file exists.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If the default ADC path is not returned
        """

        def fake_getenv(key, default=""):
            return {"APPDATA": "C:/Users/test/AppData/Roaming"}.get(
                key, default
            )

        with (
            patch("py_common.gcp_auth.os.getenv", side_effect=fake_getenv),
            patch("py_common.gcp_auth.os.path.exists", return_value=False),
            patch("py_common.gcp_auth.os.name", "nt"),
            patch("py_common.gcp_auth.Path.exists", return_value=True),
        ):
            result = _get_adc_path()

        assert result.endswith("application_default_credentials.json")

    def test_raises_when_no_credentials_found_anywhere(self):
        """Neither env var nor default ADC location has a file.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If FileNotFoundError is not raised
        """
        with (
            patch("py_common.gcp_auth.os.getenv", return_value=""),
            patch("py_common.gcp_auth.os.path.exists", return_value=False),
            patch("py_common.gcp_auth.Path.exists", return_value=False),
        ):
            with pytest.raises(FileNotFoundError, match="No credentials"):
                _get_adc_path()


class TestAuthFromJson:
    """Test _auth_from_json()."""

    def test_parses_service_account_json(self, tmp_path):
        """service_account credential type loads via Credentials.

        Args:
            tmp_path: pytest fixture giving a per-test temp directory

        Returns:
            None

        Raises:
            AssertionError: If credentials are not loaded and
                returned
        """
        cred_file = tmp_path / "service_account.json"
        cred_file.write_text(
            json.dumps({"type": "service_account", "project_id": "x"})
        )
        mock_creds = MagicMock()

        with patch(
            "py_common.gcp_auth.Credentials.from_service_account_file",
            return_value=mock_creds,
        ) as mock_from_file:
            result = _auth_from_json(str(cred_file))

        assert result is mock_creds
        mock_from_file.assert_called_once_with(str(cred_file))

    def test_raises_on_invalid_json(self, tmp_path):
        """Malformed JSON content raises rather than being swallowed.

        Args:
            tmp_path: pytest fixture giving a per-test temp directory

        Returns:
            None

        Raises:
            AssertionError: If no exception is raised for bad JSON
        """
        cred_file = tmp_path / "broken.json"
        cred_file.write_text("{not valid json")

        with pytest.raises(json.JSONDecodeError):
            _auth_from_json(str(cred_file))


class TestGcpAuth:
    """Test gcp_auth() — the main entry point."""

    def test_auto_detects_and_authenticates_via_adc(self):
        """method="auto" + local ADC creds → session is authenticated.

        Exercises the everyday local-dev path: environment detection
        picks "adc", the ADC file is located and parsed, credentials
        are refreshed, and the resulting session state is readable
        via gcp_token()/gcp_project()/gcp_is_authenticated().

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If session state is not updated correctly
        """
        mock_creds = MagicMock()

        with (
            patch(
                "py_common.gcp_auth._detect_environment", return_value="adc"
            ),
            patch(
                "py_common.gcp_auth._get_adc_path",
                return_value="/fake/adc.json",
            ),
            patch(
                "py_common.gcp_auth._auth_from_json", return_value=mock_creds
            ),
            patch("py_common.gcp_auth.Request"),
        ):
            gcp_auth(project="phw-eng-dev")

        assert gcp_is_authenticated() is True
        assert gcp_project() == "phw-eng-dev"
        assert gcp_token() is mock_creds
        mock_creds.refresh.assert_called_once()

    def test_raises_when_no_gce_and_no_adc_credentials(self):
        """auto-detection lands on "interactive" with no ADC file.

        _detect_environment() itself does not raise (see
        TestDetectEnvironment), but gcp_auth() maps "interactive" to
        method "adc", retries _get_adc_path(), and surfaces that
        failure as a ValueError.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If ValueError is not raised
        """
        with (
            patch(
                "py_common.gcp_auth._detect_environment",
                return_value="interactive",
            ),
            patch(
                "py_common.gcp_auth._get_adc_path",
                side_effect=FileNotFoundError("no creds"),
            ),
        ):
            with pytest.raises(ValueError, match="Authentication failed"):
                gcp_auth(project="phw-eng-dev")
