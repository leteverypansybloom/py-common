"""SharePoint Online adapter via Microsoft Graph API.

Implements Source protocol: Discover and download Excel files
from a SharePoint document library.

Configuration (via environment variables):
- SHAREPOINT_TENANT_ID: Azure tenant ID
- SHAREPOINT_CLIENT_ID: Entra application ID
- SHAREPOINT_CLIENT_SECRET_NAME: Secret Manager reference
- SHAREPOINT_HOSTNAME: e.g., "contoso.sharepoint.com"
- SHAREPOINT_SITE_PATH: e.g., "/sites/MyDataSite"
- SHAREPOINT_LIBRARY: e.g., "Shared Documents"
- SHAREPOINT_FOLDER: e.g., "Uploads" (optional, defaults to root)
"""

import logging

from typing import Any
logger = logging.getLogger("py_common.adapters.sharepoint")


class SharePointSource:
    """Discover and download files from SharePoint Online.

    Uses Microsoft Graph API with delegated access via
    Azure AD application.

    Not yet implemented. Requires:
    1. Microsoft Graph client (msgraph-core)
    2. Azure identity provider (azure-identity)
    3. Application to have Sites.Selected permission on library
    """

    def __init__(self, **config: Any) -> None:
        """Initialize SharePoint adapter.

        Args:
            tenant_id: Azure tenant ID
            client_id: Entra app ID
            client_secret: OAuth token (from SecretStore)
            hostname: SharePoint domain
            site_path: Site collection path
            library: Document library name
            folder: Folder within library (optional)
        """
        raise NotImplementedError(
            "SharePointSource will be implemented in next phase. "
            "For now, use LocalSource with test fixtures."
        )

    def items(self) -> list[Any]:
        """List Excel files in SharePoint folder."""
        raise NotImplementedError()

    def download(self, item: Any) -> bytes:
        """Download file bytes from SharePoint."""
        raise NotImplementedError()
