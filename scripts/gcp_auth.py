r"""Authenticate to GCP once and share the credentials.

A Python port of the R helper gcp_auth.R. It is kept in scripts/
rather than the library: py-common's adapters take credentials from
the Google client libraries directly.

It picks a method from where the code is running:

- On GCP (Compute Engine, Cloud Run, GKE): the attached service
  account, through the metadata server.
- Locally with Application Default Credentials: the cached
  credentials file.
- Locally with a service account key: the JSON key file.

Behind a corporate proxy, user credentials are refreshed with a
direct call to Google's token endpoint, so gcloud is not needed.

Example:
    From the repository root, with scripts/ on sys.path::

        from gcp_auth import gcp_auth, gcp_project, gcp_token
        from google.cloud import bigquery

        gcp_auth(project="my-project")
        client = bigquery.Client(
            project=gcp_project(), credentials=gcp_token()
        )

To point it at a key file once, in PowerShell::

    [System.Environment]::SetEnvironmentVariable(
        "GOOGLE_APPLICATION_CREDENTIALS", "C:\keys\key.json", "User"
    )

or in a Mac or Linux shell::

    export GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json"
"""

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

import google.auth
from google.auth.credentials import Credentials as BaseCredentials
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

logger = logging.getLogger("gcp_auth")

TOKEN_URI = "https://oauth2.googleapis.com/token"
METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/id"
ADC_FILE = "application_default_credentials.json"


class GCPAuthSession:
    """Credentials and project shared by everything in one process.

    Attributes:
        token: The credentials, or None before gcp_auth() runs.
        project: The GCP project ID.
        billing: The project queries are billed to.
        method: The method that authenticated: "gce", "adc" or
            "service_account".
        authenticated: Whether gcp_auth() has succeeded.
    """

    def __init__(self) -> None:
        """Start with nothing authenticated."""
        self.token: BaseCredentials | None = None
        self.project: str | None = None
        self.billing: str | None = None
        self.method: str | None = None
        self.authenticated = False


# One session per process, so a caller authenticates once and every
# later gcp_token() call gets the same credentials.
_session = GCPAuthSession()


def _get_adc_path() -> str:
    """Find the Application Default Credentials file.

    GOOGLE_APPLICATION_CREDENTIALS is checked first, then the file
    `gcloud auth application-default login` writes.

    Returns:
        The path of the credentials file.

    Raises:
        FileNotFoundError: Neither location has a file.
    """
    env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if env_path and os.path.exists(env_path):
        logger.debug("Using GOOGLE_APPLICATION_CREDENTIALS: %s", env_path)
        return env_path

    if sys.platform == "win32":
        appdata = os.getenv("APPDATA") or str(
            Path.home() / "AppData" / "Roaming"
        )
        default_path = Path(appdata, "gcloud", ADC_FILE)
    else:
        default_path = Path.home() / ".config" / "gcloud" / ADC_FILE

    if default_path.exists():
        logger.debug("Using default ADC file: %s", default_path)
        return str(default_path)

    raise FileNotFoundError(
        "No credentials found. Either:\n"
        "  1. Set GOOGLE_APPLICATION_CREDENTIALS to a key file, or\n"
        "  2. Run: gcloud auth application-default login"
    )


def _detect_environment() -> str:
    """Work out where the code is running.

    Returns:
        "gce" when the GCP metadata server answers, "adc" when a local
        credentials file exists, otherwise "interactive".
    """
    # The metadata server only exists on GCP. A one-second timeout
    # keeps the check quick on a laptop, where the name won't resolve.
    request = urllib.request.Request(
        METADATA_URL, headers={"Metadata-Flavor": "Google"}
    )
    try:
        response = urllib.request.urlopen(request, timeout=1)
        if response.status == 200:
            return "gce"
    except Exception:
        # Any failure (DNS, timeout, refused) means "not on GCP".
        pass

    try:
        _get_adc_path()
        return "adc"
    except FileNotFoundError:
        return "interactive"


def _auth_from_json(json_path: str) -> BaseCredentials:
    """Load credentials from a service account or user JSON file.

    Args:
        json_path: The credentials file.

    Returns:
        The credentials. User credentials already hold a fresh access
        token.

    Raises:
        FileNotFoundError: The file does not exist.
        ValueError: The file has no "type", an unsupported type, or
            Google refused to refresh the user token.
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Credential file not found: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        cred_dict = json.load(f)

    cred_type = cred_dict.get("type")
    if not cred_type:
        raise ValueError(
            f"Cannot read the credential type from {json_path}; the "
            "file may be malformed."
        )
    logger.debug("Credential type: %s", cred_type)

    if cred_type == "service_account":
        load = Credentials.from_service_account_file
        creds: BaseCredentials = load(json_path)  # type: ignore[no-untyped-call]
        return creds

    if cred_type == "authorized_user":
        # Swap the refresh token for an access token directly, which
        # works behind the proxy without a browser or gcloud.
        import requests
        from google.oauth2.credentials import Credentials as UserCredentials

        resp = requests.post(
            TOKEN_URI,
            data={
                "client_id": cred_dict.get("client_id"),
                "client_secret": cred_dict.get("client_secret"),
                "refresh_token": cred_dict.get("refresh_token"),
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            err = resp.json()
            raise ValueError(
                f"Token refresh failed: {err.get('error')}: "
                f"{err.get('error_description')}. "
                "Re-run: gcloud auth application-default login"
            )

        user_creds: BaseCredentials = UserCredentials(  # type: ignore[no-untyped-call]
            token=resp.json().get("access_token"),
            refresh_token=cred_dict.get("refresh_token"),
            token_uri=TOKEN_URI,
            client_id=cred_dict.get("client_id"),
            client_secret=cred_dict.get("client_secret"),
        )
        return user_creds

    raise ValueError(
        f"Unsupported credential type: {cred_type}. "
        "Supported: service_account, authorized_user"
    )


def gcp_auth(
    project: str,
    method: str = "auto",
    json_path: str | None = None,
) -> None:
    """Authenticate and keep the credentials for gcp_token().

    Args:
        project: The GCP project ID.
        method: "auto" to choose from the environment, or one of
            "gce", "adc" and "service_account".
        json_path: A service account key file. Giving one forces
            method "service_account".

    Raises:
        ValueError: Authentication failed or the method is unknown.
            The original error is chained.
    """
    if json_path:
        method = "service_account"

    if method == "auto":
        env = _detect_environment()
        # With no credentials found, try ADC anyway so the error
        # message says how to create them.
        method = "gce" if env == "gce" else "adc"
        logger.info("Environment %s, using method %s", env, method)

    try:
        if method == "gce":
            creds, project_id = google.auth.default(
                scopes=[
                    "https://www.googleapis.com/auth/bigquery",
                    "https://www.googleapis.com/auth/devstorage.read_write",
                ]
            )
        elif method == "adc":
            creds = _auth_from_json(_get_adc_path())
            project_id = project
        elif method == "service_account":
            if not json_path or not os.path.exists(json_path):
                raise ValueError(
                    "json_path must point to a service account key file."
                )
            creds = _auth_from_json(json_path)
            project_id = project
        else:
            raise ValueError(
                f"Unsupported method: {method}. "
                "Supported: gce, adc, service_account"
            )

        # Refresh now so bad credentials fail here, not on first use.
        if hasattr(creds, "refresh"):
            creds.refresh(Request())  # type: ignore[no-untyped-call]

        _session.token = creds
        _session.project = project_id
        _session.billing = project_id
        _session.method = method
        _session.authenticated = True
        logger.info("Authenticated via %s, project %s", method, project_id)

    except Exception as e:
        raise ValueError(f"Authentication failed: {e}") from e


def gcp_token() -> BaseCredentials:
    """Return the credentials gcp_auth() stored.

    Returns:
        The session's credentials.

    Raises:
        ValueError: gcp_auth() has not succeeded yet.
    """
    if not _session.authenticated or _session.token is None:
        raise ValueError("Not authenticated. Call gcp_auth() first.")
    return _session.token


def gcp_project() -> str:
    """Return the session's project ID.

    Returns:
        The project ID.

    Raises:
        ValueError: No project has been set.
    """
    if _session.project is None:
        raise ValueError("No project set. Pass project= to gcp_auth().")
    return _session.project


def gcp_billing() -> str:
    """Return the project queries are billed to.

    Returns:
        The billing project, which defaults to the session's project.

    Raises:
        ValueError: No billing project or project has been set.
    """
    return _session.billing or gcp_project()


def gcp_is_authenticated() -> bool:
    """Say whether gcp_auth() has succeeded.

    Returns:
        True once authenticated, otherwise False.
    """
    return _session.authenticated


def gcp_session_info() -> None:
    """Print the session's state."""
    print(
        "GCP auth session\n"
        f"  Authenticated: {_session.authenticated}\n"
        f"  Method:        {_session.method or 'none'}\n"
        f"  Project:       {_session.project or 'not set'}\n"
        f"  Billing:       {_session.billing or 'not set'}"
    )
