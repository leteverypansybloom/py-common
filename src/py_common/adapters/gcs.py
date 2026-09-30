"""Google Cloud Storage adapter for immutable file storage.

Implements ObjectStore protocol. All writes are append-only: if a key
already exists with identical content, put() succeeds (idempotent). If
content differs, put() fails with ValueError (prevents corruption).

RAP Compliance:
- Reproducible: Same key and data always produces same result
- Auditable: All operations logged at debug level
- Transparent: No hardcoded credentials; uses Application Default

lock() gives real mutual exclusion via atomic create-if-absent.
"""

import json
import logging
import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from google.api_core.exceptions import NotFound, PreconditionFailed
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
            key (str): Object path, e.g.,
                "raw/<identity>/<checksum>/events.xlsx".

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
            key (str): Object path, e.g.,
                "raw/<identity>/<checksum>/events.xlsx".
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
        """Acquire an exclusive write lock, or fail immediately.

        The lock is a small GCS object created with
        ``if_generation_match=0`` (atomic create-if-absent), so of any
        number of executions that try at once - a second scheduler
        trigger, a manual re-run, a retry - exactly one succeeds.
        Protection does not depend on how runs are started.

        The lock never expires and is never stolen, as the
        ObjectStore protocol requires. If a run dies without
        releasing it (for example the container is killed), later
        runs are refused until an operator has checked no run is
        active and deleted the lock object named in the error.

        The lock object records the holder (run ID, host, process ID,
        UTC start time) so a refusal can say who holds it. On release
        the object is deleted only if it is still the one this run
        created.

        Args:
            key (str): Lock identifier (e.g., "locks/<warehouse hash>").

        Yields:
            None: Control while the lock is held.

        Raises:
            RuntimeError: Another process holds the lock.
            google.api_core.exceptions.GoogleAPICallError: GCS failed
                for a reason other than the lock being held.
        """
        blob = self.bucket.blob(key)
        holder = {
            "run_id": uuid.uuid4().hex,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            blob.upload_from_string(json.dumps(holder), if_generation_match=0)
        except PreconditionFailed:
            raise RuntimeError(
                f"Lock {key} is held by another run: "
                f"{self._describe_holder(key)}. If no run is active, "
                f"delete gs://{self.bucket_name}/{key} and retry."
            ) from None

        generation = blob.generation
        logger.info(f"Acquired lock {key} (run_id={holder['run_id']})")
        try:
            yield
        finally:
            self._release_lock(blob, key, generation)

    def _describe_holder(self, key: str) -> str:
        """Read the current lock holder for an error message.

        Args:
            key (str): Lock identifier.

        Returns:
            str: Holder details as stored, or a note that they could
                not be read.
        """
        try:
            raw: bytes = self.bucket.blob(key).download_as_bytes()
            return raw.decode()
        except Exception as e:
            return f"holder unreadable ({e})"

    def _release_lock(self, blob: Any, key: str, generation: int) -> None:
        """Delete the lock object if it is still the one we created.

        Never raises: a failure to release must not mask the
        pipeline's own result or exception.

        Args:
            blob (Any): Lock blob created by this run.
            key (str): Lock identifier, for logging.
            generation (int): Generation of the lock we created.

        Returns:
            None
        """
        try:
            blob.delete(if_generation_match=generation)
            logger.info(f"Released lock {key}")
        except NotFound:
            logger.warning(f"Lock {key} was already gone at release")
        except PreconditionFailed:
            logger.error(
                f"Lock {key} was replaced by another run; "
                "not deleting it. Check for overlapping runs."
            )
