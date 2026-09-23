"""Local file system adapters for testing without cloud services.

LocalSource: List and download files from a folder
LocalObjectStore: Read/write files to a folder

There is no local Warehouse: tests use a Mock, and
docs/GETTING_STARTED.md shows a small in-memory fake.
"""

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator,  Iterable

from py_common.errors import NotFound, ObjectStoreConflict, VersionMismatch
from py_common.model import SourceItem

logger = logging.getLogger("py_common.adapters.local")


class LocalSource:
    """Discover and download files from a folder.

    Use for testing or when source is a shared network folder.
    """

    def __init__(self, folder: Path | str):
        """Initialize with folder path.

        Args:
            folder: Path to folder containing Excel files.
        """
        self.folder = Path(folder)

    def items(self) -> Iterable[SourceItem]:
        """List all .xlsx and .xls files in folder.

        Yields:
            SourceItem for each Excel file.
        """
        if not self.folder.exists():
            logger.warning("Source folder does not exist: %s", self.folder)
            return

        for path in sorted(self.folder.glob("*.xlsx")) + sorted(
            self.folder.glob("*.xls")
        ):
            stat = path.stat()
            yield SourceItem(
                name=path.name,
                # Relative to self.folder, not the full filesystem
                # path: pipeline.py embeds identity straight into
                # object store keys, and an absolute Windows path
                # (with its drive-letter colon) is neither a valid
                # filename component nor a portable identifier.
                identity=path.relative_to(self.folder).as_posix(),
                version=str(int(stat.st_mtime)),
                size_bytes=stat.st_size,
            )

    def download(self, item: SourceItem) -> bytes:
        """Read file bytes.

        Args:
            item: SourceItem from discovery.

        Returns:
            File bytes.

        Raises:
            VersionMismatch: File was modified since discovery.
            NotFound: File no longer exists.
        """
        path = self.folder / item.identity

        if not path.exists():
            raise NotFound(f"File not found: {path}")

        stat = path.stat()
        current_version = str(int(stat.st_mtime))

        if current_version != item.version:
            raise VersionMismatch(
                f"File modified since discovery: {path} "
                f"(was {item.version}, now {current_version})"
            )

        with open(path, "rb") as f:
            return f.read()


class LocalObjectStore:
    """Read and write files in a folder.

    Use for testing audit logs and processed files without cloud storage.
    """

    def __init__(self, folder: Path | str):
        """Initialize with folder path.

        Args:
            folder: Folder to use as object store root.
        """
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, key: str) -> Path:
        """Resolve a key to a path guaranteed to stay under folder.

        Args:
            key: Object path or identifier (e.g., "raw/events.xlsx").

        Returns:
            Path under self.folder.

        Raises:
            ValueError: Key is an absolute path or uses ".." to
                escape self.folder (e.g. directory traversal).
        """
        path = self.folder / key
        try:
            path.resolve().relative_to(self.folder.resolve())
        except ValueError:
            raise ValueError(f"Key escapes object store root: {key}") from None
        return path

    def get(self, key: str) -> bytes | None:
        """Read file by key path.

        Args:
            key: Object path (e.g., "audit/checksum123").

        Returns:
            File bytes if exists, None otherwise.

        Raises:
            ValueError: Key escapes the object store root.
        """
        path = self._resolve_path(key)
        if not path.exists():
            return None
        return path.read_bytes()

    def put(self, key: str, data: bytes) -> None:
        """Write file; idempotent if identical content exists.

        Matches GCSObjectStore semantics: if the key already exists
        with identical bytes, the write succeeds without touching
        the file (idempotent). If it exists with different bytes,
        raises rather than silently overwriting.

        Args:
            key: Object path (e.g., "processed/item123.parquet").
            data: Bytes to store.

        Raises:
            ObjectStoreConflict: Key exists with different content.
            ValueError: Key escapes the object store root.
        """
        path = self._resolve_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = path.read_bytes()
            if existing == data:
                logger.debug("Object already exists (identical): %s", key)
                return
            raise ObjectStoreConflict(
                f"Key {key} exists with different content"
            )

        path.write_bytes(data)
        logger.debug("Wrote object: %s (%d bytes)", key, len(data))

    @contextmanager
    def lock(self, key: str) -> Generator[None, None, None]:
        """Acquire exclusive lock (no-op for local files).

        In production, this would use a distributed lock service.
        For testing, we just yield since there's no concurrency.

        Args:
            key: Lock identifier.

        Yields:
            Lock context (held for duration of with block).
        """
        # Local testing: no actual lock needed
        yield
