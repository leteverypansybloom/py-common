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
        the warehouse. Never taken from a live holder. A lock may
        be a lease that a later run takes over once it has expired
        (GCSObjectStore), provided the lease outlasts any run.

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
    """Transactionally load one file's rows plus an audit record.

    Examples: BigQuery, Snowflake, PostgreSQL.

    Pipeline decides what happens to a file from the type of error
    load() raises, so implementations must use these types:

    - ServiceError: the warehouse failed and nothing was committed
      (permission, quota, outage, a job reported as failed). The file
      is FAILED and retried.
    - IndeterminateCommitError: it is unknown whether the commit
      happened (e.g. the connection dropped while waiting). The file
      is FAILED and retried.
    - ValidationError (including ContractError): the file's data
      can't be loaded as it is. The file is QUARANTINED and not
      retried.

    Any other exception is treated as FAILED too, but is a bug in
    the adapter: map it to one of the types above.
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
        item: SourceItem,
    ) -> bool:
        """Atomically commit the data and its audit record, or neither.

        Must be idempotent: if the warehouse's own audit record shows
        this file already loaded here, write nothing and return
        False. The check belongs inside the commit transaction, so a
        retry after a commit whose confirmation was lost is safe
        whatever the object store's marker says.

        Args:
            key: Unique identifier for this load. Pipeline derives it
                from the source identity and checksum.
            checksum: SHA-256 of the source file.
            data: NDJSON rows to load (py_common.raw.to_ndjson()).
            rows: Expected row count; verified before committing.
            item: Source file the data came from (for its version).

        Returns:
            True if the data was written, False if it had already
            been loaded.

        Raises:
            ServiceError: Nothing was committed; safe to retry.
            IndeterminateCommitError: Commit state unknown.
            ValidationError: The data itself can't be loaded.
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
