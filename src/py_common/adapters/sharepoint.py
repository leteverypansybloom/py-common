"""SharePoint Online adapter via Microsoft Graph.

Implements the Source protocol: discover Excel files in one folder of
a SharePoint document library, and download the exact version that
was discovered.

Configuration holds *where* the files are (hostname, site path,
library, folder) and nothing about *who* is asking. Authentication is
injected as a token provider - any zero-argument callable returning a
Microsoft Graph access token - so the credential flow IT approves can
be plugged in later without changing this module. Example, once a
flow is chosen::

    source = SharePointSource(
        SharePointConfig.from_mapping(config["sharepoint"]),
        token_provider=my_approved_token_function,
    )

Graph calls used (all read-only GETs):

- ``/sites/{hostname}:{site_path}`` - resolve the site ID
- ``/sites/{site-id}/drives`` - resolve the library's drive ID
- ``/drives/{drive-id}/root:/{folder}:/children`` - list the folder
- ``/drives/{drive-id}/items/{item-id}`` - check the current eTag
- ``/drives/{drive-id}/items/{item-id}/content`` - download bytes

The minimum Graph application permission is ``Sites.Selected`` with
read access granted on the one site.

RAP Compliance:
- Reproducible: Items are returned sorted; download refuses any
  version other than the one discovered
- Auditable: Discovery, retries and downloads are logged; the access
  token never is
- Transparent: No credentials in code or configuration
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import requests

from py_common.config import ConfigError, interpolate, require_keys
from py_common.errors import NotFound, ServiceError, VersionMismatch
from py_common.model import SourceItem

logger = logging.getLogger("py_common.adapters.sharepoint")

GRAPH_URL = "https://graph.microsoft.com/v1.0"

#: Statuses worth retrying: throttling and transient server errors.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Longest single wait, whatever Retry-After asks for.
MAX_WAIT_SECONDS = 60.0

TokenProvider = Callable[[], str]
"""Returns a Microsoft Graph access token (no "Bearer " prefix).

Called before every request, so the provider owns caching and
refresh. Supplied by whichever credential flow IT approves.
"""


class HttpResponse(Protocol):
    """The parts of requests.Response this adapter uses."""

    @property
    def status_code(self) -> int:
        """HTTP status code."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Response headers."""
        ...

    @property
    def content(self) -> bytes:
        """Raw response body."""
        ...

    def json(self) -> Any:
        """Parsed JSON body; raises ValueError if not JSON."""
        ...


class HttpSession(Protocol):
    """The parts of requests.Session this adapter uses."""

    def get(
        self, url: str, *, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        """Send a GET request (following redirects)."""
        ...


@dataclass(frozen=True)
class SharePointConfig:
    """Where the source files live. Contains no credentials.

    Attributes:
        hostname: SharePoint host, e.g. "contoso.sharepoint.com".
        site_path: Server-relative site path, e.g. "/sites/DataSite".
        library: Document library name as shown in SharePoint,
            e.g. "Documents" (Graph's name for "Shared Documents").
        folder: Folder path inside the library, e.g. "Incoming/2026".
            Empty means the library root. Sub-folders are not read.
        extensions: File extensions to pick up (lower case, with dot).
    """

    hostname: str
    site_path: str
    library: str
    folder: str = ""
    extensions: tuple[str, ...] = (".xlsx", ".xls")

    _REQUIRED = ("hostname", "site_path", "library")
    _OPTIONAL = ("folder", "extensions")

    def __post_init__(self) -> None:
        """Validate and normalise settings.

        Raises:
            ConfigError: A setting is empty or malformed.
        """
        host = self.hostname.strip()
        if not host or "://" in host or "/" in host:
            raise ConfigError(
                f"SharePoint hostname must be a bare host such as "
                f"'contoso.sharepoint.com', got {self.hostname!r}"
            )
        site_path = self.site_path.strip().rstrip("/")
        if not site_path.startswith("/") or site_path == "":
            raise ConfigError(
                f"SharePoint site_path must be server-relative, such as "
                f"'/sites/DataSite', got {self.site_path!r}"
            )
        library = self.library.strip()
        if not library:
            raise ConfigError("SharePoint library must not be empty")
        folder = self.folder.replace("\\", "/").strip().strip("/")
        if ".." in folder.split("/"):
            raise ConfigError(
                f"SharePoint folder must not contain '..': {self.folder!r}"
            )
        extensions = tuple(e.strip().lower() for e in self.extensions)
        if not extensions or not all(
            e.startswith(".") and len(e) > 1 for e in extensions
        ):
            raise ConfigError(
                f"SharePoint extensions must look like '.xlsx', "
                f"got {self.extensions!r}"
            )
        object.__setattr__(self, "hostname", host)
        object.__setattr__(self, "site_path", site_path)
        object.__setattr__(self, "library", library)
        object.__setattr__(self, "folder", folder)
        object.__setattr__(self, "extensions", extensions)

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any]) -> SharePointConfig:
        """Build from a configuration section (e.g. YAML).

        String values may use ${VAR} placeholders, resolved from the
        environment here. Unknown keys are refused so typos - and
        credentials, which don't belong in this section - fail loudly.

        Args:
            settings: Mapping with hostname, site_path, library and
                optionally folder and extensions.

        Returns:
            Validated SharePointConfig.

        Raises:
            ConfigError: Missing, unknown or invalid settings, or an
                unset environment variable.
        """
        unknown = sorted(set(settings) - set(cls._REQUIRED + cls._OPTIONAL))
        if unknown:
            raise ConfigError(
                f"Unknown SharePoint settings: {', '.join(unknown)}. "
                "Allowed: "
                f"{', '.join(cls._REQUIRED + cls._OPTIONAL)}. Credentials "
                "are not configured here; pass a token_provider instead."
            )
        require_keys(dict(settings), list(cls._REQUIRED))

        def text(key: str) -> str:
            return str(interpolate(str(settings[key])))

        extensions = settings.get("extensions", cls.extensions)
        if isinstance(extensions, str):
            extensions = [extensions]
        return cls(
            hostname=text("hostname"),
            site_path=text("site_path"),
            library=text("library"),
            folder=text("folder") if "folder" in settings else "",
            extensions=tuple(str(interpolate(str(e))) for e in extensions),
        )

    def describe(self) -> str:
        """Human-readable location for logs and errors.

        Returns:
            e.g. "contoso.sharepoint.com/sites/DataSite/Documents/In".
        """
        parts = [self.hostname + self.site_path, self.library, self.folder]
        return "/".join(p for p in parts if p)


class SharePointSource:
    """Discover and download files from a SharePoint folder via Graph.

    Implements py_common.ports.Source.

    Only files directly in the configured folder are listed (no
    recursion). Each file's Graph item ID is its identity - stable
    across renames - and its eTag is its version.

    Throttling (429) and transient server errors (500/502/503/504)
    are retried, honouring Retry-After when given and otherwise
    waiting 1s, 2s, 4s... Waits are capped at 60 seconds. No random
    jitter is added: this runs as a single scheduled job, so there is
    no herd of clients to spread out, and fixed waits keep logs and
    tests reproducible.
    """

    def __init__(
        self,
        config: SharePointConfig,
        token_provider: TokenProvider,
        *,
        session: HttpSession | None = None,
        max_attempts: int = 4,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        graph_url: str = GRAPH_URL,
    ) -> None:
        """Initialise the adapter. Makes no network calls.

        Args:
            config: Where the files live.
            token_provider: Returns a Graph access token; called
                before every request.
            session: HTTP session; defaults to requests.Session().
                Tests pass a fake.
            max_attempts: Tries per request, including the first.
            timeout_seconds: Per-request timeout.
            sleep: Wait function used between retries.
            graph_url: Graph base URL (national clouds differ).

        Raises:
            ValueError: max_attempts < 1 or timeout_seconds <= 0.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.config = config
        self._token_provider = token_provider
        self._session: HttpSession = session or requests.Session()
        self._max_attempts = max_attempts
        self._timeout = timeout_seconds
        self._sleep = sleep
        self._graph_url = graph_url.rstrip("/")
        self._site_id: str | None = None
        self._drive_id: str | None = None

    # ---------------------------------------------------------------
    # Source protocol
    # ---------------------------------------------------------------

    def items(self) -> list[SourceItem]:
        """List matching files in the configured folder.

        The whole listing (all pages) is fetched before returning, so
        the pipeline works from one snapshot.

        Returns:
            SourceItems sorted by name (case-insensitive), then ID.

        Raises:
            ConfigError: Site, library or folder not found.
            ServiceError: Graph failed or returned something invalid.
        """
        drive_id = self._resolve_drive_id()
        if self.config.folder:
            folder = quote(self.config.folder, safe="/")
            url = (
                f"{self._graph_url}/drives/{drive_id}/root:/{folder}:/children"
            )
        else:
            url = f"{self._graph_url}/drives/{drive_id}/root/children"

        try:
            entries = self._get_all(url)
        except NotFound:
            raise ConfigError(
                f"SharePoint folder '{self.config.folder}' not found in "
                f"{self.config.describe()} (check the folder setting and "
                "that the app has access)"
            ) from None

        found = [self._to_source_item(e) for e in entries if self._wanted(e)]
        found.sort(key=lambda i: (i.name.casefold(), i.identity))
        logger.info(
            "Discovered %d file(s) in %s (%d entries listed)",
            len(found),
            self.config.describe(),
            len(entries),
        )
        return found

    def download(self, item: SourceItem) -> bytes:
        """Fetch the bytes of exactly the discovered version.

        The eTag is checked before and after the download, so a file
        saved between discovery and download, or while downloading,
        is refused rather than processed half-written.

        Args:
            item: SourceItem from a prior items() call.

        Returns:
            Complete file bytes.

        Raises:
            VersionMismatch: The file changed since discovery.
            NotFound: The file no longer exists.
            ServiceError: Graph failed or the download was incomplete.
        """
        item_url = (
            f"{self._graph_url}/drives/{self._resolve_drive_id()}"
            f"/items/{item.identity}"
        )
        self._check_version(item, item_url, "before download")
        data = self._request(f"{item_url}/content").content
        self._check_version(item, item_url, "during download")

        if len(data) != item.size_bytes:
            raise ServiceError(
                f"Incomplete download of {item.name}: got {len(data)} "
                f"bytes, expected {item.size_bytes}"
            )
        logger.info(
            "Downloaded %s (%d bytes, version %s)",
            item.name,
            len(data),
            item.version,
        )
        return data

    # ---------------------------------------------------------------
    # Resolution and mapping
    # ---------------------------------------------------------------

    def _resolve_drive_id(self) -> str:
        """Look up (once) the drive ID for the configured library.

        Returns:
            Graph drive ID.

        Raises:
            ConfigError: Site or library not found.
            ServiceError: Graph failed.
        """
        if self._drive_id is not None:
            return self._drive_id

        if self._site_id is None:
            site = quote(self.config.site_path, safe="/")
            url = f"{self._graph_url}/sites/{self.config.hostname}:{site}"
            try:
                body = self._get_json(url)
            except NotFound:
                raise ConfigError(
                    f"SharePoint site not found: {self.config.hostname}"
                    f"{self.config.site_path} (check hostname/site_path "
                    "and that the app has access)"
                ) from None
            self._site_id = self._field(body, "id", "site")

        drives = self._get_all(
            f"{self._graph_url}/sites/{self._site_id}/drives"
        )
        wanted = self.config.library.casefold()
        for drive in drives:
            if str(drive.get("name", "")).casefold() == wanted:
                self._drive_id = self._field(drive, "id", "drive")
                logger.debug(
                    "Resolved library %s to drive %s",
                    self.config.library,
                    self._drive_id,
                )
                return self._drive_id

        names = sorted(str(d.get("name", "")) for d in drives)
        raise ConfigError(
            f"SharePoint library '{self.config.library}' not found on "
            f"{self.config.hostname}{self.config.site_path}. "
            f"Available: {', '.join(names) or 'none'}"
        )

    def _wanted(self, entry: Mapping[str, Any]) -> bool:
        """Whether a driveItem is a file with an allowed extension.

        Args:
            entry: Graph driveItem.

        Returns:
            True if it should become a SourceItem.
        """
        if "file" not in entry:
            return False
        name = str(entry.get("name", "")).lower()
        return name.endswith(self.config.extensions)

    def _to_source_item(self, entry: Mapping[str, Any]) -> SourceItem:
        """Map a Graph driveItem onto a SourceItem.

        Args:
            entry: Graph driveItem for a file.

        Returns:
            SourceItem.

        Raises:
            ServiceError: A required field is missing.
        """
        size = entry.get("size")
        if not isinstance(size, int):
            raise ServiceError(
                f"Graph item {entry.get('id')!r} has no valid 'size'"
            )
        return SourceItem(
            name=self._field(entry, "name", "item"),
            identity=self._field(entry, "id", "item"),
            version=self._field(entry, "eTag", "item"),
            size_bytes=size,
        )

    def _check_version(
        self, item: SourceItem, item_url: str, when: str
    ) -> None:
        """Raise if the item's current eTag differs from discovery.

        Args:
            item: Discovered SourceItem.
            item_url: Graph URL of the item's metadata.
            when: "before download" or "during download", for the log.

        Raises:
            VersionMismatch: eTag changed.
            NotFound: Item no longer exists.
            ServiceError: Graph failed.
        """
        current = self._field(self._get_json(item_url), "eTag", "item")
        if current != item.version:
            raise VersionMismatch(
                f"{item.name} changed {when}: discovered {item.version}, "
                f"now {current}"
            )

    @staticmethod
    def _field(body: Mapping[str, Any], key: str, what: str) -> str:
        """Read a required non-empty string field from a Graph body.

        Args:
            body: Parsed Graph JSON object.
            key: Field name.
            what: What the body describes, for the error message.

        Returns:
            Field value.

        Raises:
            ServiceError: Field missing or not a non-empty string.
        """
        value = body.get(key)
        if not isinstance(value, str) or not value:
            raise ServiceError(f"Graph {what} response has no valid '{key}'")
        return value

    # ---------------------------------------------------------------
    # HTTP
    # ---------------------------------------------------------------

    def _get_all(self, url: str) -> list[dict[str, Any]]:
        """GET a Graph collection, following every @odata.nextLink.

        Args:
            url: First page URL.

        Returns:
            All entries from all pages, in page order.

        Raises:
            NotFound: First page returned 404.
            ServiceError: Graph failed, returned a non-collection, or
                a nextLink repeated (pagination loop).
        """
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        next_url: str | None = url
        while next_url:
            if next_url in seen:
                raise ServiceError(
                    f"Graph pagination loop: {self._path(next_url)} "
                    "was returned twice"
                )
            seen.add(next_url)
            body = self._get_json(next_url)
            page = body.get("value")
            if not isinstance(page, list):
                raise ServiceError(
                    f"Graph response for {self._path(next_url)} has no "
                    "'value' list"
                )
            entries.extend(e for e in page if isinstance(e, dict))
            link = body.get("@odata.nextLink")
            next_url = link if isinstance(link, str) else None
        return entries

    def _get_json(self, url: str) -> dict[str, Any]:
        """GET a URL and parse a JSON object body.

        Args:
            url: Graph URL.

        Returns:
            Parsed JSON object.

        Raises:
            NotFound: 404.
            ServiceError: Request failed or body isn't a JSON object.
        """
        response = self._request(url)
        try:
            body = response.json()
        except ValueError:
            raise ServiceError(
                f"Graph response for {self._path(url)} is not JSON"
            ) from None
        if not isinstance(body, dict):
            raise ServiceError(
                f"Graph response for {self._path(url)} is not a JSON object"
            )
        return body

    def _request(self, url: str) -> HttpResponse:
        """GET with bearer token, retrying throttling/transient errors.

        Args:
            url: Graph URL.

        Returns:
            Successful (status < 400) response.

        Raises:
            NotFound: 404.
            ServiceError: Token unavailable, non-retryable error, or
                retries exhausted.
        """
        path = self._path(url)
        for attempt in range(1, self._max_attempts + 1):
            headers = {"Authorization": f"Bearer {self._token()}"}
            logger.debug("GET %s (attempt %d)", path, attempt)
            try:
                response = self._session.get(
                    url, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as e:
                reason = type(e).__name__
                if attempt == self._max_attempts:
                    raise ServiceError(
                        f"Graph GET {path} failed after {attempt} "
                        f"attempt(s): {reason}"
                    ) from e
                wait = self._backoff(attempt)
            else:
                status = response.status_code
                if status < 400:
                    return response
                if status == 404:
                    raise NotFound(f"Graph GET {path} returned 404")
                if (
                    status not in RETRYABLE_STATUSES
                    or attempt == self._max_attempts
                ):
                    raise ServiceError(self._describe_error(path, response))
                reason = str(status)
                wait = self._retry_after(response) or self._backoff(attempt)

            logger.warning(
                "Graph GET %s failed (%s); retry %d of %d in %.0fs",
                path,
                reason,
                attempt,
                self._max_attempts - 1,
                wait,
            )
            self._sleep(wait)

        raise AssertionError("unreachable")  # pragma: no cover

    def _token(self) -> str:
        """Get an access token from the injected provider.

        Returns:
            Non-empty token string.

        Raises:
            ServiceError: Provider failed or returned nothing.
        """
        try:
            token = self._token_provider()
        except Exception as e:
            raise ServiceError(
                "Could not obtain an access token for Microsoft Graph "
                f"({type(e).__name__})"
            ) from e
        if not isinstance(token, str) or not token:
            raise ServiceError("Token provider returned an empty access token")
        return token

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential wait: 1s, 2s, 4s... capped.

        Args:
            attempt: Attempt number that just failed (1-based).

        Returns:
            Seconds to wait.
        """
        return float(min(2 ** (attempt - 1), MAX_WAIT_SECONDS))

    @staticmethod
    def _retry_after(response: HttpResponse) -> float | None:
        """Read a Retry-After header given in seconds.

        Args:
            response: Throttled response.

        Returns:
            Capped seconds to wait, or None if absent/unparseable.
        """
        value = response.headers.get("Retry-After")
        try:
            seconds = float(value) if value is not None else None
        except ValueError:
            return None
        if seconds is None or seconds < 0:
            return None
        return min(seconds, MAX_WAIT_SECONDS)

    @staticmethod
    def _describe_error(path: str, response: HttpResponse) -> str:
        """Build an error message from a Graph error response.

        Args:
            path: Request path (no host, no token).
            response: Failed response.

        Returns:
            Message with status and Graph error code/message.
        """
        code, message = "unknown", ""
        try:
            error = response.json().get("error", {})
            code = str(error.get("code", code))
            message = str(error.get("message", ""))
        except (ValueError, AttributeError):
            pass
        return (
            f"Graph GET {path} returned {response.status_code} "
            f"({code}: {message})"
        )

    def _path(self, url: str) -> str:
        """Strip the Graph base URL for shorter logs and errors.

        Args:
            url: Full URL.

        Returns:
            Path relative to the Graph base URL.
        """
        return url.removeprefix(self._graph_url)
