"""ObjectStore protocol conformance, run against every implementation.

The pipeline relies on the same behaviour from every store: an
identical put is a no-op, a conflicting put raises
ObjectStoreConflict, a missing key reads as None, and a read that
fails for any other reason raises ServiceError (never None, which
the dedup check would take as "not loaded yet").

LocalObjectStore runs against a temporary folder; GCSObjectStore
runs against an in-memory fake of the parts of the GCS client it
uses, which can be told to fail.
"""

import base64
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from google.api_core.exceptions import (
    Forbidden,
    NotFound,
    PreconditionFailed,
    ServiceUnavailable,
)

from py_common.adapters.gcs import GCSObjectStore
from py_common.adapters.local import LocalObjectStore
from py_common.errors import ObjectStoreConflict, ServiceError
from py_common.ports import ObjectStore


class _Blob:
    """Fake blob: content, generation and an md5_hash like GCS's."""

    def __init__(self, bucket: "_Bucket", key: str) -> None:
        self._bucket = bucket
        self.name = key
        self.generation: int | None = None
        self.md5_hash: str | None = None
        self.crc32c: str | None = None

    def upload_from_string(
        self, data: bytes | str, if_generation_match: int | None = None
    ) -> None:
        self._bucket.check("upload")
        raw = data.encode() if isinstance(data, str) else data
        current = self._bucket.objects.get(self.name)
        if if_generation_match == 0 and current is not None:
            raise PreconditionFailed("exists")
        if if_generation_match not in (None, 0) and (
            current is None or current[1] != if_generation_match
        ):
            raise PreconditionFailed("generation changed")
        generation = (current[1] if current else 0) + 1
        self._bucket.objects[self.name] = (raw, generation)
        self.generation = generation

    def download_as_bytes(self, **_: Any) -> bytes:
        self._bucket.check("download")
        if self.name not in self._bucket.objects:
            raise NotFound("missing")
        return self._bucket.objects[self.name][0]


class _Bucket:
    """Fake bucket whose calls can be made to raise."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.failures: dict[str, Exception] = {}
        self.downloads = 0

    def check(self, operation: str) -> None:
        if operation == "download":
            self.downloads += 1
        if operation in self.failures:
            raise self.failures[operation]

    def blob(self, key: str) -> _Blob:
        return _Blob(self, key)

    def get_blob(self, key: str) -> _Blob | None:
        self.check("metadata")
        if key not in self.objects:
            return None
        data, generation = self.objects[key]
        blob = _Blob(self, key)
        blob.generation = generation
        blob.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()
        return blob


@pytest.fixture
def gcs_store() -> Iterator[tuple[GCSObjectStore, _Bucket]]:
    """GCSObjectStore backed by the fake bucket."""
    with patch("py_common.adapters.gcs.storage.Client"):
        store = GCSObjectStore("test-project", "test-bucket")
    bucket = _Bucket()
    store.bucket = bucket
    yield store, bucket


def _break(store: Any, key: str) -> None:
    """Make reading `key` from `store` fail with a real error.

    GCS: the download returns 503. Local: the key is a directory, so
    reading it raises OSError, as a permission or disk fault would.

    Args:
        store: Store under test.
        key: Key whose read should fail.
    """
    if isinstance(store, GCSObjectStore):
        store.bucket.failures["download"] = ServiceUnavailable("down")
    else:
        (store.folder / key).mkdir(parents=True)


@pytest.fixture(params=["local", "gcs"])
def store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    gcs_store: tuple[GCSObjectStore, _Bucket],
) -> ObjectStore:
    """Each ObjectStore implementation in turn."""
    if request.param == "local":
        return LocalObjectStore(tmp_path)
    return gcs_store[0]


class TestObjectStoreContract:
    """Behaviour every ObjectStore must share."""

    def test_identical_put_is_idempotent(self, store: ObjectStore) -> None:
        """Putting the same bytes twice succeeds and keeps them."""
        store.put("raw/a", b"data")
        store.put("raw/a", b"data")

        assert store.get("raw/a") == b"data"

    def test_conflicting_put_raises_object_store_conflict(
        self, store: ObjectStore
    ) -> None:
        """Different bytes under an existing key are refused."""
        store.put("raw/a", b"data")

        with pytest.raises(ObjectStoreConflict, match="raw/a"):
            store.put("raw/a", b"other")
        assert store.get("raw/a") == b"data"

    def test_missing_get_returns_none(self, store: ObjectStore) -> None:
        """A key that was never written reads as None."""
        assert store.get("audit/never-written") is None

    def test_erroring_get_raises_service_error(
        self, store: ObjectStore
    ) -> None:
        """A failed read is an error, never "not found"."""
        _break(store, "audit/marker")

        with pytest.raises(ServiceError):
            store.get("audit/marker")


class TestGCSObjectStoreFailClosed:
    """GCS-specific mapping of API errors (review C1, M10, M6)."""

    @pytest.mark.parametrize(
        "error", [ServiceUnavailable("503"), Forbidden("403")]
    )
    def test_get_raises_service_error_naming_the_object(
        self, gcs_store: tuple[GCSObjectStore, _Bucket], error: Exception
    ) -> None:
        """5xx and 403 on the marker read must not look like "missing"."""
        store, bucket = gcs_store
        bucket.failures["download"] = error

        with pytest.raises(ServiceError, match="gs://test-bucket/k"):
            store.get("k")

    def test_put_wraps_api_errors_in_service_error(
        self, gcs_store: tuple[GCSObjectStore, _Bucket]
    ) -> None:
        """A GCS failure on write is a ServiceError."""
        store, bucket = gcs_store
        bucket.failures["upload"] = Forbidden("no access")

        with pytest.raises(ServiceError, match="no access"):
            store.put("k", b"data")

    def test_put_compares_by_hash_without_downloading(
        self, gcs_store: tuple[GCSObjectStore, _Bucket]
    ) -> None:
        """An existing object is compared by md5_hash, not re-read."""
        store, bucket = gcs_store
        store.put("k", b"data")
        bucket.downloads = 0

        store.put("k", b"data")
        with pytest.raises(ObjectStoreConflict):
            store.put("k", b"other")

        assert bucket.downloads == 0
