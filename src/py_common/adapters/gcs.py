"""Google Cloud Storage adapters: immutable storage and a source.

GCSObjectStore implements the ObjectStore protocol. All writes are
append-only: if a key already exists with identical content, put()
succeeds (idempotent). If content differs, put() fails with
ObjectStoreConflict (prevents corruption).

GCSSource implements the Source protocol: it lists workbooks in a
landing folder of a bucket and downloads the exact object generation
it listed.

RAP Compliance:
- Reproducible: Same key and data always produces same result
- Auditable: All operations logged at debug level
- Transparent: No hardcoded credentials; uses Application Default

lock() gives real mutual exclusion via atomic create-if-absent.
"""

import base64
import hashlib
import json
import logging
import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from google.api_core.exceptions import (
    GoogleAPICallError,
    NotFound,
    PreconditionFailed,
)
import google_crc32c
import google.cloud.storage as storage

from py_common.errors import NotFound as ItemNotFound
from py_common.errors import (
    ObjectStoreConflict,
    ServiceError,
    VersionMismatch,
)
from py_common.model import SourceItem

logger = logging.getLogger("py_common.adapters.gcs")


class GCSObjectStore:
    """Immutable object storage using Google Cloud Storage.

    Implements ObjectStore protocol from py_common.ports. Provides
    idempotent put/get operations suitable for distributed, retryable
    data pipelines.

    Key invariant: put(k, d); put(k, d) succeeds both times.
    If data differs: put(k, d1); put(k, d2) raises
    ObjectStoreConflict.

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

        Fail-closed: only a missing object reads as None. Any other
        failure (permission, outage, timeout) raises, because the
        pipeline's "already loaded?" check would otherwise take an
        unreadable marker as "never loaded" and load the file again.

        Args:
            key (str): Object path, e.g.,
                "raw/<identity>/<checksum>/events.xlsx".

        Returns:
            Optional[bytes]: Bytes if the object exists, None if not.

        Raises:
            ServiceError: GCS failed for a reason other than the
                object being missing.
        """
        blob = self.bucket.blob(key)
        try:
            data: bytes = blob.download_as_bytes()
        except NotFound:
            logger.debug("Object %s not found", key)
            return None
        except GoogleAPICallError as e:
            raise ServiceError(
                f"Cannot read gs://{self.bucket_name}/{key}: {e}"
            ) from e
        logger.debug("Read object %s (%d bytes)", key, len(data))
        return data

    def put(self, key: str, data: bytes) -> None:
        """Write object; idempotent if identical data exists.

        If object exists with identical bytes, succeeds without
        re-upload (idempotent). If object exists with different
        bytes, raises ObjectStoreConflict (prevents silent
        corruption).

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
            ObjectStoreConflict: Object exists with different content.
            ServiceError: GCS failed (permission, outage).
        """
        blob = self.bucket.blob(key)
        where = f"gs://{self.bucket_name}/{key}"

        try:
            # if_generation_match=0 tells GCS "only write if no
            # generation of this object exists yet" — an atomic
            # create-if-absent. This closes the previous race where
            # two processes both called exists() before either
            # uploaded, and both then thought they held a fresh key.
            # See: google.cloud.storage.blob.Blob#if_generation_match
            blob.upload_from_string(data, if_generation_match=0)
            logger.debug("Wrote object %s (%d bytes)", key, len(data))
            return
        except PreconditionFailed:
            pass
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot write {where}: {e}") from e

        # The object already exists (written by us on a retry, or by
        # a concurrent writer). Compare hashes from its metadata to
        # decide between idempotent success and a real conflict,
        # rather than downloading what may be a large workbook.
        try:
            existing = self.bucket.get_blob(key)
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot read {where}: {e}") from e
        if existing is None:
            raise ServiceError(
                f"{where} existed on write but is gone now; "
                "something is deleting store objects"
            )
        if _same_content(existing, data):
            logger.debug("Object %s already exists (idempotent)", key)
            return
        raise ObjectStoreConflict(
            f"Object {where} exists with different content"
        )

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
            ServiceError: GCS failed for a reason other than the lock
                being held.
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
        except GoogleAPICallError as e:
            raise ServiceError(
                f"Cannot create lock gs://{self.bucket_name}/{key}: {e}"
            ) from e

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


class GCSSource:
    """Discover and download workbooks in a GCS landing folder.

    Implements the Source protocol from py_common.ports. Files are
    read from one folder (prefix) of a bucket; sub-folders are not
    read, matching SharePointSource.

    Each item's identity is its full object name, and its version is
    the object generation, which GCS changes on every overwrite. With
    object versioning on, the same file name uploaded each month gets
    a new generation each time, and earlier generations are kept.

    Keep the pipeline's own object store out of the landing folder
    (a separate bucket, or a prefix outside it), so the pipeline
    never lists its own raw/ or processed/ copies as new files.

    Attributes:
        project_id (str): GCP project ID.
        bucket_name (str): GCS bucket name (without gs:// prefix).
        prefix (str): Landing folder, ending in "/" (empty means the
            bucket root).
        extensions (tuple[str, ...]): File extensions to pick up,
            lower case, with dot.
        client (storage.Client): GCS client.
        bucket (storage.Bucket): Reference to GCS bucket.
    """

    def __init__(
        self,
        project_id: str,
        bucket_name: str,
        prefix: str = "",
        extensions: tuple[str, ...] = (".xlsx",),
    ) -> None:
        """Initialize GCS source.

        Args:
            project_id (str): GCP project ID.
            bucket_name (str): GCS bucket name, with or without gs://.
            prefix (str): Landing folder, e.g. "landing/hww". Leading
                and trailing slashes are optional.
            extensions (tuple[str, ...]): File extensions to pick up.
                Defaults to .xlsx only: openpyxl can't read .xls.

        Returns:
            None

        Raises:
            ValueError: An extension doesn't start with a dot.
        """
        if not extensions or not all(
            e.startswith(".") and len(e) > 1 for e in extensions
        ):
            raise ValueError(
                f"GCSSource extensions must look like '.xlsx', "
                f"got {extensions!r}"
            )
        self.project_id = project_id
        self.bucket_name = bucket_name.replace("gs://", "")
        folder = prefix.strip("/")
        self.prefix = f"{folder}/" if folder else ""
        self.extensions = tuple(e.lower() for e in extensions)
        self.client = storage.Client(project=project_id)
        self.bucket = self.client.bucket(self.bucket_name)

    def items(self) -> list[SourceItem]:
        """List workbooks directly inside the landing folder.

        Returns:
            list[SourceItem]: One per matching object, sorted by
                object name.

        Raises:
            ServiceError: GCS listing failed (permission, outage).
        """
        try:
            blobs = list(
                self.client.list_blobs(
                    self.bucket_name, prefix=self.prefix, delimiter="/"
                )
            )
        except GoogleAPICallError as e:
            raise ServiceError(
                f"Cannot list gs://{self.bucket_name}/{self.prefix}: {e}"
            ) from e

        items = [
            SourceItem(
                name=blob.name.rsplit("/", 1)[-1],
                identity=blob.name,
                version=str(blob.generation),
                size_bytes=int(blob.size or 0),
            )
            for blob in blobs
            if blob.name.lower().endswith(self.extensions)
        ]
        items.sort(key=lambda item: item.identity)
        logger.info(
            f"Found {len(items)} file(s) in "
            f"gs://{self.bucket_name}/{self.prefix}"
        )
        return items

    def download(self, item: SourceItem) -> bytes:
        """Download the exact generation that items() listed.

        Args:
            item (SourceItem): Item from a prior items() call.

        Returns:
            bytes: The object's bytes at the listed generation.

        Raises:
            NotFound: The object no longer exists.
            VersionMismatch: The object was overwritten since it was
                listed (a newer upload is in progress or done).
            ServiceError: GCS failed for another reason.
        """
        where = f"gs://{self.bucket_name}/{item.identity}"
        try:
            current = self.bucket.get_blob(item.identity)
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot read {where}: {e}") from e
        if current is None:
            raise ItemNotFound(f"Object no longer exists: {where}")
        if str(current.generation) != item.version:
            raise VersionMismatch(
                f"Object changed since discovery: {where} "
                f"(was generation {item.version}, "
                f"now {current.generation})"
            )

        # The precondition closes the gap between the check above and
        # the download: an upload in between fails here instead of
        # returning bytes from a different generation.
        try:
            data: bytes = self.bucket.blob(item.identity).download_as_bytes(
                if_generation_match=int(item.version)
            )
        except PreconditionFailed:
            raise VersionMismatch(
                f"Object changed during download: {where}"
            ) from None
        except NotFound:
            raise ItemNotFound(f"Object no longer exists: {where}") from None
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot download {where}: {e}") from e

        logger.debug(f"Downloaded {where} ({len(data)} bytes)")
        return data


def _same_content(blob: Any, data: bytes) -> bool:
    """Whether a stored object holds exactly `data`, by its hashes.

    GCS records an MD5 for objects uploaded in one piece (all of
    ours) and a CRC32C for every object, both base64-encoded. MD5 is
    preferred; CRC32C covers composite objects, which have no MD5.

    Args:
        blob (Any): Blob with metadata loaded (from get_blob()).
        data (bytes): Bytes we tried to write.

    Returns:
        bool: True if the hashes match.
    """
    if blob.md5_hash:
        md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
        return bool(blob.md5_hash == md5)
    # google_crc32c is untyped; value() is its CRC32C as an int.
    crc32c: Any = google_crc32c
    raw = int(crc32c.value(data)).to_bytes(4, "big")
    return bool(blob.crc32c == base64.b64encode(raw).decode())
