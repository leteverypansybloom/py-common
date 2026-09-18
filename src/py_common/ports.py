"""Adapter protocols.

Cloud services implement these; core pipeline code imports no SDKs.
Protocols allow testing with local implementations and mocks.
"""

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Protocol

from py_common.model import SourceItem


class Source(Protocol):
    """Discover and download files from a source.

    Examples: SharePoint, local folder, S3 bucket, Azure Blob.
    """

    def items(self) -> Iterable[SourceItem]:
        """List current files at source.

        Returns:
            Iterator of SourceItem describing each file.
        """
        ...

    def download(self, item: SourceItem) -> bytes:
        """Fetch exact bytes of discovered version.

        The caller has already seen this version at discovery time.
        If the version changed on the source between discovery and
        download, raise VersionMismatch. This prevents processing
        a partially-written file.

        Args:
            item: SourceItem from a prior items() call.

        Returns:
            Complete file bytes.

        Raises:
            VersionMismatch: Source version changed since discovery.
        """
        ...


class ObjectStore(Protocol):
    """Immutable storage for raw bytes and derived data.

    Examples: Google Cloud Storage, S3, Azure Blob, local filesystem.
    """

    def get(self, key: str) -> bytes | None:
        """Read object by key.

        Args:
            key: Object path or identifier.

        Returns:
            Bytes if object exists, None if not found.
        """
        ...

    def put(self, key: str, data: bytes) -> None:
        """Write object; accept if identical copy already exists.

        Idempotent: calling put twice with same key and data
        must succeed on both calls.

        Args:
            key: Object path or identifier.
            data: Bytes to store.
        """
        ...

    def lock(self, key: str) -> AbstractContextManager[None]:
        """Acquire exclusive write lock on key.

        Used to prevent concurrent writers when staging to
        the warehouse. Lock must not auto-expire or steal
        from long-running processes.

        Args:
            key: Identifier for the lock (e.g., warehouse ID).

        Returns:
            Context manager that holds lock on __enter__,
            releases on __exit__.

        Raises:
            RuntimeError: Another process holds the lock.
        """
        ...


class Warehouse(Protocol):
    """Stage, validate and transactionally append data.

    Examples: BigQuery, Snowflake, PostgreSQL.
    """

    @property
    def identity(self) -> str:
        """Unique identifier for this warehouse.

        Used to scope the exclusive write lock.
        """
        ...

    def load(
        self,
        key: str,
        checksum: str,
        data: bytes,
        rows: int,
    ) -> None:
        """Atomically commit data or fail; never partial writes.

        Steps:
        1. Load data into staging table (truncate before each run)
        2. Verify row count matches expectation
        3. Merge or append into final table on contract key columns
        4. Record audit entry with checksum and row count
        5. Commit transaction or roll back everything

        Args:
            key: Unique identifier for this load (filename or ID).
            checksum: SHA-256 of data for duplicate detection.
            data: Parquet bytes to load.
            rows: Expected row count; verify before committing.

        Raises:
            ValueError: Row count mismatch or schema error.
            RuntimeError: Cannot acquire write lock.
        """
        ...


class SecretStore(Protocol):
    """Retrieve secrets from a secure store.

    Examples: Google Secret Manager, Azure Key Vault, HashiCorp Vault.
    """

    def get_secret(self, secret_name: str) -> str:
        """Fetch secret value by name.

        Args:
            secret_name: Secret identifier (version handled by store).

        Returns:
            Secret string (e.g., OAuth token or API key).

        Raises:
            KeyError: Secret not found.
        """
        ...
