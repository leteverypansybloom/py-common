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

from google.api_core.exceptions import PreconditionFailed  # type: ignore
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

        try:
            # if_generation_match=0 tells GCS "only write if no
            # generation of this object exists yet" — an atomic
            # create-if-absent. This closes the previous race where
            # two processes both called exists() before either
            # uploaded, and both then thought they held a fresh key.
            # See: google.cloud.storage.blob.Blob#if_generation_match
            blob.upload_from_string(data, if_generation_match=0)
            logger.debug(f"Writing object {key} ({len(data)} bytes)")
        except PreconditionFailed:
            # Object already exists (created by us on a retry, or by
            # a concurrent writer). Compare content to decide between
            # idempotent success and a real conflict.
            existing = blob.download_as_bytes()
            if existing == data:
                logger.debug(f"Object {key} already exists (idempotent)")
                return
            raise ValueError(
                f"Object {key} exists with different content. "
                f"Existing: {len(existing)} bytes, "
                f"New: {len(data)} bytes"
            ) from None

    @contextmanager
    def lock(self, key: str) -> Generator[None, None, None]:
        """Acquire exclusive write lock.

        Deliberate no-op stub: PHW runs one ingestion job at a time,
        triggered by a single Cloud Scheduler job, so there is no
        concurrent writer to lock out. This is a documented decision,
        not an oversight — see README "Concurrency" section.

        If PHW ever runs concurrent pipelines against the same
        warehouse table (parallel triggers, manual re-runs overlapping
        the schedule), this stub is UNSAFE: two processes can both
        stage and merge into the same {table}_staging table at once.
        Replace it with real distributed locking (Firestore document
        locks are the recommended option on GCP) before enabling any
        form of concurrent execution.

        Args:
            key (str): Lock identifier (e.g., warehouse table name).

        Returns:
            Generator (context manager).

        Raises:
            None (stub; future: RuntimeError if lock unavailable)
        """
        logger.warning(
            f"GCS lock stub for {key}: no concurrent protection. "
            "Safe only for sequential (one-job-at-a-time) execution."
        )
        try:
            yield
        finally:
            logger.debug(f"Releasing lock for {key} (stub)")
