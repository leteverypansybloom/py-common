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
from datetime import datetime, timedelta, timezone
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

    def __init__(
        self,
        project_id: str,
        bucket_name: str,
        lease_seconds: int = 3600,
    ) -> None:
        """Initialize GCS adapter.

        Args:
            project_id (str): GCP project ID.
            bucket_name (str): GCS bucket name. If prefixed with
                gs://, prefix is removed for normalization.
            lease_seconds (int): How long a run's lock lasts before
                another run may take it over. Must exceed the Cloud
                Run job or service timeout, so a lease outlasts any
                normal run. Not a guarantee: a service container can
                keep running after its request times out, and the
                lease isn't renewed. Defaults to one hour.

        Returns:
            None

        Raises:
            ValueError: lease_seconds is not positive.
        """
        if lease_seconds <= 0:
            raise ValueError(
                f"lease_seconds must be positive, got {lease_seconds}"
            )
        self.lease_seconds = lease_seconds
        # Normalize bucket name (remove gs:// prefix if present).
        # This allows users to specify either "bucket" or "gs://bucket".
        self.bucket_name = bucket_name.removeprefix("gs://")
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
        """Acquire an exclusive write lease, or fail immediately.

        The lock is a small GCS object created with
        ``if_generation_match=0`` (atomic create-if-absent), so of any
        number of executions that try at once - a second scheduler
        trigger, a manual re-run, a retry - exactly one succeeds.
        Protection does not depend on how runs are started.

        The lock is a lease: it records when it expires
        (lease_seconds after it was taken). A lock that has not
        expired is never taken, so a live run is never robbed of it,
        provided lease_seconds exceeds the job or service timeout.
        A run killed without releasing its lock (Cloud Run timeout,
        OOM, a deploy) no longer blocks every later run: once the
        lease has expired the next run takes it over, logged at
        WARNING with the old holder. The takeover is an overwrite
        conditioned on the expired lock's generation, so when two
        runs find the same expired lock only one gets it.

        The lock object records the holder (run ID, host, process ID,
        UTC start and expiry) so a refusal can say who holds it. On
        release the object is deleted only if it is still the one
        this run wrote.

        Args:
            key (str): Lock identifier (e.g., "locks/<warehouse hash>").

        Yields:
            None: Control while the lock is held.

        Raises:
            RuntimeError: Another run holds an unexpired lock, or took
                over the expired one first.
            ServiceError: GCS failed for a reason other than the lock
                being held.
        """
        blob = self.bucket.blob(key)
        now = _utcnow()
        holder = {
            "run_id": uuid.uuid4().hex,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": now.isoformat(),
            "expires_at": (
                now + timedelta(seconds=self.lease_seconds)
            ).isoformat(),
        }
        where = f"gs://{self.bucket_name}/{key}"
        try:
            blob.upload_from_string(json.dumps(holder), if_generation_match=0)
        except PreconditionFailed:
            self._take_over_if_expired(blob, key, holder, now)
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot create lock {where}: {e}") from e

        generation = blob.generation
        logger.info("Acquired lock %s (run_id=%s)", key, holder["run_id"])
        try:
            yield
        finally:
            self._release_lock(blob, key, generation)

    def _take_over_if_expired(
        self, blob: Any, key: str, holder: dict[str, Any], now: datetime
    ) -> None:
        """Take over an existing lock if, and only if, it has expired.

        Args:
            blob (Any): Our lock blob (written on success).
            key (str): Lock identifier.
            holder (dict[str, Any]): Our holder record.
            now (datetime): Current UTC time.

        Raises:
            RuntimeError: The lock is live, unreadable, or another run
                took it over first.
            ServiceError: GCS failed.
        """
        where = f"gs://{self.bucket_name}/{key}"
        try:
            current = self.bucket.get_blob(key)
            raw = None if current is None else current.download_as_bytes()
        except NotFound:
            current, raw = None, None
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot read lock {where}: {e}") from e

        if current is None or raw is None:
            # Released between our write and our read: try once more
            # to create it, as a fresh acquire.
            try:
                blob.upload_from_string(
                    json.dumps(holder), if_generation_match=0
                )
                return
            except PreconditionFailed:
                raise RuntimeError(
                    f"Lock {key} is held by another run. If no run is "
                    f"active, delete {where} and retry."
                ) from None

        expires = self._expiry(raw)
        if expires is None or expires > now:
            when = expires.isoformat() if expires else "an unknown time"
            raise RuntimeError(
                f"Lock {key} is held by another run: "
                f"{raw.decode(errors='replace')}. It expires at {when}; "
                f"if no run is active, delete {where} and retry."
            )
        try:
            blob.upload_from_string(
                json.dumps(holder), if_generation_match=current.generation
            )
        except PreconditionFailed:
            raise RuntimeError(
                f"Lock {key} had expired, but another run took it over "
                "first"
            ) from None
        except GoogleAPICallError as e:
            raise ServiceError(f"Cannot take over lock {where}: {e}") from e
        logger.warning(
            "Took over expired lock %s (expired %s) from %s",
            key,
            expires.isoformat(),
            raw.decode(errors="replace"),
        )

    def _expiry(self, raw: bytes) -> datetime | None:
        """When a stored lock expires, or None if that can't be told.

        Locks written before leases (0.1.x) have no expires_at; they
        expire lease_seconds after acquired_at.

        Args:
            raw (bytes): The lock object's content.

        Returns:
            datetime | None: Expiry (UTC), or None if the lock is
                unreadable, so it is never taken over.
        """
        try:
            info = json.loads(raw)
            if "expires_at" in info:
                return datetime.fromisoformat(info["expires_at"])
            acquired = datetime.fromisoformat(info["acquired_at"])
            return acquired + timedelta(seconds=self.lease_seconds)
        except (ValueError, KeyError, TypeError):
            return None

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
            logger.info("Released lock %s", key)
        except NotFound:
            logger.warning("Lock %s was already gone at release", key)
        except PreconditionFailed:
            logger.error(
                "Lock %s was replaced by another run; not deleting it. "
                "Check for overlapping runs.",
                key,
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
        self.bucket_name = bucket_name.removeprefix("gs://")
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
            "Found %d file(s) in gs://%s/%s",
            len(items),
            self.bucket_name,
            self.prefix,
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

        logger.debug("Downloaded %s (%d bytes)", where, len(data))
        return data


def _utcnow() -> datetime:
    """The current UTC time (a seam for tests).

    Returns:
        datetime: Timezone-aware now.
    """
    return datetime.now(timezone.utc)


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
