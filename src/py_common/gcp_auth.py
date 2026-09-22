"""GCP authentication module — reusable by any GCP utility.

Equivalent to R's gcp_auth.R. Auto-detects environment and handles:
- GCE (Cloud Run, GKE) → metadata server
- Local + ADC → cached credentials from GOOGLE_APPLICATION_CREDENTIALS
- Local + service account key → JSON key file
- Corporate proxy → direct token refresh (no gcloud CLI needed)

USAGE:
    from py_common.gcp_auth import gcp_auth, gcp_token, gcp_project

    # Authenticate once per session
    gcp_auth(project="phw-eng-dev", method="auto")

    # Pass token to GCP clients
    from google.cloud import bigquery
    bq_client = bigquery.Client(
        project=gcp_project(),
        credentials=gcp_token()
    )

ENVIRONMENT SETUP (one-time):
    Windows:
        [System.Environment]::SetEnvironmentVariable(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "C:\\path\\to\\key.json",
            "User"
        )

    Mac/Linux:
        export GOOGLE_APPLICATION_CREDENTIALS="/path/to/key.json"
"""

import os
import json
import logging
from typing import Optional
from pathlib import Path

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

logger = logging.getLogger("py_common.gcp_auth")


class GCPAuthSession:
    """Shared GCP authentication state across modules.

    Stores token, project, method once per session. Equivalent to
    .state environment in R's gcp_auth.R.

    Attributes:
        token: google.auth.credentials.Credentials
        project (str): GCP project ID
        billing (str): Billing project (defaults to project)
        method (str): Auth method used
        authenticated (bool): Whether auth succeeded
    """

    def __init__(self) -> None:
        """Initialize empty session state.

        Args:
            None

        Returns:
            None
        """
        self.token = None
        self.project = None
        self.billing = None
        self.method = None
        self.authenticated = False


# Global session state (shared across imports)
_session = GCPAuthSession()


def _get_adc_path() -> str:
    """Resolve Application Default Credentials path.

    Checks:
    1. GOOGLE_APPLICATION_CREDENTIALS env var
    2. Default ADC location (OS-aware)

    Args:
        None

    Returns:
        str: Path to credentials file

    Raises:
        FileNotFoundError: No credentials found
    """
    # 1. Check env var first (service account or explicit ADC)
    env_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if env_path and os.path.exists(env_path):
        logger.debug(f"Found GOOGLE_APPLICATION_CREDENTIALS: {env_path}")
        return env_path

    # 2. Fallback to default ADC location (OS-aware)
    if os.name == "nt":  # Windows
        default_path = Path(
            os.getenv("APPDATA"),
            "gcloud",
            "application_default_credentials.json",
        )
    else:  # Mac/Linux
        default_path = (
            Path.home()
            / ".config"
            / "gcloud"
            / "application_default_credentials.json"
        )

    if default_path.exists():
        logger.debug(f"Found default ADC: {default_path}")
        return str(default_path)

    # 3. Fail with helpful message
    raise FileNotFoundError(
        "[GCP] No credentials found.\n"
        "Options:\n"
        "  1. Set GOOGLE_APPLICATION_CREDENTIALS to a service account key\n"
        "     Windows  : [System.Environment]::SetEnvironmentVariable(\n"
        '        "GOOGLE_APPLICATION_CREDENTIALS", "C:\\keys\\key.json", '
        '"User")\n'
        "     Mac/Linux: export GOOGLE_APPLICATION_CREDENTIALS="
        '"/path/to/key.json"\n'
        "  2. For interactive auth: gcloud auth application-default "
        "login\n"
    )


def _detect_environment() -> str:
    """Detect where code is running.

    Args:
        None

    Returns:
        str: "gce" (on GCP VM), "adc" (local with credentials), or
            "interactive" (local, no credentials)
    """
    # Check if running on GCE by pinging metadata server
    try:
        import urllib.request

        request = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/id",
            headers={"Metadata-Flavor": "Google"},
        )
        response = urllib.request.urlopen(request, timeout=1)
        if response.status == 200:
            return "gce"
    except Exception:
        pass

    # Check for ADC file
    try:
        _get_adc_path()
        return "adc"
    except FileNotFoundError:
        return "interactive"


def _auth_from_json(json_path: str) -> google.auth.credentials.Credentials:
    """Load credentials directly from JSON file.

    Handles service_account and authorized_user credential types.
    Works with corporate proxy (no browser, no gcloud CLI needed).

    Args:
        json_path (str): Path to credential JSON file

    Returns:
        google.auth.credentials.Credentials: Loaded credentials

    Raises:
        FileNotFoundError: File not found
        ValueError: Unsupported credential type
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Credential file not found: {json_path}")

    with open(json_path, "r") as f:
        cred_dict = json.load(f)

    cred_type = cred_dict.get("type")
    if not cred_type:
        raise ValueError(
            f"Cannot read credential type from {json_path}. "
            "File may be malformed."
        )

    logger.debug(f"Credential type: {cred_type}")

    if cred_type == "service_account":
        # Service account: direct from JSON
        creds = Credentials.from_service_account_file(json_path)
        return creds

    elif cred_type == "authorized_user":
        # Authorized user: exchange refresh_token for access_token
        # No browser needed, works with corporate proxy
        import requests

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
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
                f"Token refresh failed: {err.get('error')} — "
                f"{err.get('error_description')}. "
                "Re-run: gcloud auth application-default login"
            )

        tokens = resp.json()

        # Build credentials from tokens
        from google.oauth2.credentials import Credentials as OAuth2Credentials

        creds = OAuth2Credentials(
            token=tokens.get("access_token"),
            refresh_token=cred_dict.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cred_dict.get("client_id"),
            client_secret=cred_dict.get("client_secret"),
        )
        return creds

    else:
        raise ValueError(
            f"Unsupported credential type: {cred_type}. "
            "Supported: service_account, authorized_user"
        )


def gcp_auth(
    project: str,
    method: str = "auto",
    json_path: Optional[str] = None,
) -> None:
    """Authenticate with GCP — auto-detects environment.

    Fetches and stores credentials that downstream GCP clients
    (BigQuery, GCS, Pub/Sub) can retrieve via gcp_token().

    Auth strategy by environment:
    - GCE / Cloud Run / GKE  → VM's attached service account
    - Local + ADC credentials → cached credentials file
    - Local + service account → JSON key file
    - Corporate proxy → direct token refresh (no gcloud CLI)

    Args:
        project (str): GCP project ID
        method (str): "auto" (detect), "gce", "adc", "service_account"
            or "interactive". Defaults to "auto".
        json_path (str, optional): Path to service account JSON key.
            If provided, forces method="service_account".

    Returns:
        None

    Raises:
        ValueError: Authentication failed or unsupported method
    """
    global _session

    if json_path:
        method = "service_account"

    if method == "auto":
        env = _detect_environment()
        method_map = {
            "gce": "gce",
            "adc": "adc",
            "interactive": "adc",  # Fall back to ADC on local
        }
        method = method_map[env]
        logger.info(
            f"[GCP] Environment detected: {env} → using method: {method}"
        )

    try:
        if method == "gce":
            # On GCE, use metadata server
            creds, project_id = google.auth.default(
                scopes=[
                    "https://www.googleapis.com/auth/bigquery",
                    "https://www.googleapis.com/auth/devstorage" ".read_write",
                ]
            )

        elif method == "adc":
            # Read credential file directly (handles proxy issues)
            path = _get_adc_path()
            creds = _auth_from_json(path)
            project_id = project

        elif method == "service_account":
            # Service account key file
            if not json_path or not os.path.exists(json_path):
                raise ValueError(
                    "json_path must point to valid service account "
                    "JSON key file."
                )
            creds = _auth_from_json(json_path)
            project_id = project

        else:
            raise ValueError(
                f"Unsupported method: {method}. "
                "Supported: gce, adc, service_account"
            )

        # Refresh credentials to ensure they're valid
        if hasattr(creds, "refresh"):
            creds.refresh(Request())

        # Store in session state
        _session.token = creds
        _session.project = project_id
        _session.billing = project_id
        _session.method = method
        _session.authenticated = True

        logger.info(
            f"[GCP] Authenticated via: {method} | project: {project_id}"
        )

    except Exception as e:
        raise ValueError(f"[GCP] Authentication failed: {e}") from e


def gcp_token() -> google.auth.credentials.Credentials:
    """Retrieve the shared GCP token.

    Called by downstream utilities to get token without
    re-authenticating.

    Args:
        None

    Returns:
        google.auth.credentials.Credentials: Stored token

    Raises:
        ValueError: Not authenticated yet
    """
    if not _session.authenticated or _session.token is None:
        raise ValueError("[GCP] Not authenticated. Call gcp_auth() first.")
    return _session.token


def gcp_project() -> str:
    """Get the session GCP project ID.

    Args:
        None

    Returns:
        str: Project ID

    Raises:
        ValueError: No project set
    """
    if _session.project is None:
        raise ValueError(
            "[GCP] No project set. Pass project= in " "gcp_auth()."
        )
    return _session.project


def gcp_billing() -> str:
    """Get the session billing project.

    Args:
        None

    Returns:
        str: Billing project ID
    """
    return _session.billing or gcp_project()


def gcp_is_authenticated() -> bool:
    """Check if GCP auth has been completed.

    Args:
        None

    Returns:
        bool: True if authenticated, False otherwise
    """
    return _session.authenticated


def gcp_session_info() -> None:
    """Print current auth session state.

    Args:
        None

    Returns:
        None
    """
    print(
        "── GCP Auth Session ────────────────────\n"
        f" Authenticated : {_session.authenticated}\n"
        f" Method        : {_session.method or 'none'}\n"
        f" Project       : {_session.project or 'not set'}\n"
        f" Billing       : {_session.billing or 'not set'}\n"
        "────────────────────────────────────────\n"
    )
