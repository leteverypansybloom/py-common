"""Google Cloud Storage adapter for immutable file storage.

Implements ObjectStore protocol. All writes are append-only: if a key
already exists with identical content, put() succeeds (idempotent). If
content differs, put() fails with ValueError (prevents corruption).

RAP Compliance:
- Reproducible: Same key and data always produces same result
- Auditable: All operations logged at debug level
- Transparent: No hardcoded credentials; uses Application Default
"""

import logging
from contextlib import contextmanager
from typing import Optional, Generator

from google.cloud import storage  # type: ignore

logger = logging.getLogger("py_common.adapters.gcs")


class GCSObjectStore:
    """Immutable object storage using Google Cloud Storage.

    Implements ObjectStore protocol from py_common.ports. Provides
    idempotent put/get operations suitable for distributed, retryable
    data pipelines.

    Key invariant: put(k, d); put(k, d) succeeds both times.
    If data differs: put(k, d1); put(k, d2) raises ValueError.

    Attributes:
        project_id (str): GCP project ID.
        bucket_name (str): GCS bucket name (without gs:// prefix).
        client (storage.Client): GCS client.
        bucket (storage.Bucket): Reference to GCS bucket.
    """

    def __init__(self, project_id: str, bucket_name: str) -> None:
        """Initialize GCS adapter.

        Args:
            project_id (str): GCP project ID.
            bucket_name (str): GCS bucket name. If prefixed with
                gs://, prefix is removed for normalization.

        Returns:
            None

        Raises:
            None (credential errors raised on first operation)
        """
        # Normalize bucket name (remove gs:// prefix if present).
        # This allows users to specify either "bucket" or "gs://bucket".
        self.bucket_name = bucket_name.replace("gs://", "")
        self.project_id = project_id
        self.client = storage.Client(project=project_id)
        self.bucket = self.client.bucket(self.bucket_name)

    def get(self, key: str) -> Optional[bytes]:
        """Read object by key.

        Returns bytes if object exists, None if not found or on error.
        Never raises on missing object (graceful degradation).

        Args:
            key (str): Object path, e.g., "raw/events.xlsx".

        Returns:
            Optional[bytes]: Bytes if object exists, None otherwise.

        Raises:
            None (missing objects return None, not exception)
        """
        blob = self.bucket.blob(key)
        try:
            data: bytes = blob.download_as_bytes()
            logger.debug(f"Read object {key} ({len(data)} bytes)")
            return data
        except Exception as e:
            # Object missing, not readable, or transient error.
            # Log at debug level (not warning) since missing is expected.
            logger.debug(f"Object {key} not found or read failed: {e}")
            return None

    def put(self, key: str, data: bytes) -> None:
        """Write object; idempotent if identical data exists.

        If object exists with identical bytes, succeeds without
        re-upload (idempotent). If object exists with different
        bytes, raises ValueError (prevents silent corruption).

        This is critical for retryable pipelines: a network failure
        cannot be distinguished from success + network loss, so we
        must be safe to retry.

        Args:
            key (str): Object path, e.g., "raw/events.xlsx".
            data (bytes): Bytes to store.

        Returns:
            None

        Raises:
            ValueError: Object exists with different content.
        """
        blob = self.bucket.blob(key)

        # Check if object already exists.
        if blob.exists():
            existing = blob.download_as_bytes()
            if existing == data:
                # Idempotent success: data matches.
                logger.debug(f"Object {key} already exists (idempotent)")
                return
            else:
                # Conflict: object exists with different content.
                raise ValueError(
                    f"Object {key} exists with different content. "
                    f"Existing: {len(existing)} bytes, "
                    f"New: {len(data)} bytes"
                )

        # Write new object.
        logger.debug(f"Writing object {key} ({len(data)} bytes)")
        blob.upload_from_string(data)

    @contextmanager
    def lock(self, key: str) -> Generator[None, None, None]:
        """Acquire exclusive write lock.

        Stub implementation (no-op). Real locking strategy TBD:
        - Option A: GCS object generations + optimistic concurrency
        - Option B: Firestore for centralized locks
        - Option C: Accept sequential-only processing (no parallelism)

        For now, this is a context manager that does nothing but
        allows the protocol to be satisfied and logging to happen.

        Args:
            key (str): Lock identifier (e.g., warehouse table name).

        Returns:
            Generator (context manager).

        Raises:
            None (stub; future: RuntimeError if lock unavailable)
        """
        logger.debug(f"Acquiring lock for {key} (stub)")
        try:
            yield
        finally:
            logger.debug(f"Releasing lock for {key} (stub)")
