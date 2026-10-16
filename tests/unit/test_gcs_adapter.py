"""Unit tests for the GCSObjectStore and GCSSource adapters.

Tests define the ObjectStore protocol contract without requiring live
GCS services. All external dependencies are mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test name describes what is being verified
- Transparent: No hardcoded secrets; mocks are explicit
"""

import base64
import hashlib
import json

import pytest
from _fake_gcs import FakeBucket
from unittest.mock import Mock, MagicMock, patch

from google.api_core.exceptions import (
    Forbidden,
    NotFound,
    PreconditionFailed,
)

from py_common.adapters.gcs import GCSObjectStore, GCSSource
from py_common.errors import NotFound as ItemNotFound
from py_common.errors import (
    ObjectStoreConflict,
    ServiceError,
    VersionMismatch,
)
from py_common.model import SourceItem


def _stored(data: bytes) -> Mock:
    """Metadata of an existing object holding `data`, as get_blob gives.

    Args:
        data: The object's bytes.

    Returns:
        Mock blob with a base64 md5_hash, like GCS's.
    """
    blob = Mock()
    blob.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()
    return blob


class TestGCSObjectStoreInit:
    """Test GCSObjectStore initialization."""

    def test_init_strips_gs_prefix(self):
        """Bucket name normalized (gs:// prefix removed).

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If bucket name not normalized
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore(
                project_id="test-project", bucket_name="gs://my-bucket"
            )
            assert store.bucket_name == "my-bucket"

    def test_init_accepts_bucket_without_prefix(self):
        """Bucket name works without gs:// prefix.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If bucket name not accepted
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore(
                project_id="test-project", bucket_name="my-bucket"
            )
            assert store.bucket_name == "my-bucket"

    def test_init_sets_project_id(self):
        """Project ID stored correctly.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If project ID not stored
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore(
                project_id="my-project", bucket_name="my-bucket"
            )
            assert store.project_id == "my-project"


class TestGCSObjectStoreGet:
    """Test GCSObjectStore.get() method.

    Contract: get(key) returns bytes if object exists, None if not.
    Never raises on missing object; any other failure raises
    ServiceError (see test_object_store_contract.py).
    """

    @pytest.fixture
    def mock_bucket(self):
        """Fixture: Mock GCS bucket."""
        return MagicMock()

    @pytest.fixture
    def store(self, mock_bucket):
        """Fixture: GCSObjectStore with mocked bucket."""
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore(
                project_id="test-project", bucket_name="test-bucket"
            )
            store.bucket = mock_bucket
            return store

    def test_get_existing_object_returns_bytes(self, store, mock_bucket):
        """get() returns bytes when object exists.

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If get() does not return bytes
        """
        blob = Mock()
        blob.download_as_bytes.return_value = b"test data"
        mock_bucket.blob.return_value = blob

        result = store.get("test-key")

        assert result == b"test data"
        mock_bucket.blob.assert_called_with("test-key")

    def test_get_missing_object_returns_none(self, store, mock_bucket):
        """get() returns None when object does not exist.

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If get() does not return None
        """
        blob = Mock()
        blob.download_as_bytes.side_effect = NotFound("Blob not found")
        mock_bucket.blob.return_value = blob

        result = store.get("missing-key")

        assert result is None

    def test_get_logs_debug_on_missing(self, store, mock_bucket):
        """get() logs debug message when object missing (auditable).

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If debug log not captured
        """
        blob = Mock()
        blob.download_as_bytes.side_effect = NotFound("Not found")
        mock_bucket.blob.return_value = blob

        with patch("py_common.adapters.gcs.logger") as mock_logger:
            store.get("missing-key")
            # Verify debug was logged
            assert mock_logger.debug.called


class TestGCSObjectStorePut:
    """Test GCSObjectStore.put() method.

    Critical contract (idempotent and safe):
    - put(k, d) twice with same key and data must succeed both times
    - put(k, d1); put(k, d2) with d1 != d2 must raise
      ObjectStoreConflict
    """

    @pytest.fixture
    def mock_bucket(self):
        """Fixture: Mock GCS bucket."""
        return MagicMock()

    @pytest.fixture
    def store(self, mock_bucket):
        """Fixture: GCSObjectStore with mocked bucket."""
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore(
                project_id="test-project", bucket_name="test-bucket"
            )
            store.bucket = mock_bucket
            return store

    def test_put_new_object_succeeds(self, store, mock_bucket):
        """put() creates new object when key does not exist.

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If object not uploaded
        """
        blob = Mock()
        mock_bucket.blob.return_value = blob

        store.put("new-key", b"new data")

        blob.upload_from_string.assert_called_once_with(
            b"new data", if_generation_match=0
        )

    def test_put_uses_generation_precondition(self, store, mock_bucket):
        """put() uses if_generation_match=0 for atomic create-if-absent.

        Fix for issue #4: the old put() called blob.exists() then
        blob.upload_from_string() as two separate calls, so two
        processes could both see "not exists" and both overwrite the
        key. if_generation_match=0 makes GCS itself reject the write
        if any generation already exists, closing that race.

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If the precondition is not applied
        """
        blob = Mock()
        mock_bucket.blob.return_value = blob

        store.put("new-key", b"data")

        _, kwargs = blob.upload_from_string.call_args
        assert kwargs["if_generation_match"] == 0
        blob.exists.assert_not_called()

    def test_put_idempotent_same_data(self, store, mock_bucket):
        """put() is idempotent when data matches (no re-upload).

        Calling put twice with same key and data must both succeed
        without re-uploading.

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If idempotency not enforced
        """
        blob = Mock()
        blob.upload_from_string.side_effect = PreconditionFailed(
            "generation exists"
        )
        mock_bucket.blob.return_value = blob
        mock_bucket.get_blob.return_value = _stored(b"data")

        # Call put twice with same key and data
        store.put("existing-key", b"data")
        store.put("existing-key", b"data")

        # Both calls attempted the atomic write and fell back to a
        # content comparison; neither should raise.
        assert blob.upload_from_string.call_count == 2

    def test_put_raises_on_conflict(self, store, mock_bucket):
        """put() raises ObjectStoreConflict when data differs.

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If ObjectStoreConflict not raised
        """
        blob = Mock()
        blob.upload_from_string.side_effect = PreconditionFailed(
            "generation exists"
        )
        mock_bucket.blob.return_value = blob
        mock_bucket.get_blob.return_value = _stored(b"old data")

        with pytest.raises(ObjectStoreConflict, match="different content"):
            store.put("existing-key", b"new data")

    def test_put_logs_debug_on_write(self, store, mock_bucket):
        """put() logs debug message when writing (auditable).

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If debug log not captured
        """
        blob = Mock()
        mock_bucket.blob.return_value = blob

        with patch("py_common.adapters.gcs.logger") as mock_logger:
            store.put("new-key", b"test data")
            # Verify debug was logged
            assert mock_logger.debug.called


class TestGCSObjectStoreLock:
    """Test GCSObjectStore.lock() context manager.

    Contract (ports.ObjectStore.lock): exclusive write lock on key.
    Raises RuntimeError if another process holds it. Never expires
    and never steals a lock from a long-running process.

    Implemented with a lock object created via GCS's atomic
    create-if-absent (if_generation_match=0), so two executions can
    never both hold it, whatever started them.
    """

    @pytest.fixture
    def bucket(self):
        """Fixture: in-memory fake bucket."""
        return FakeBucket()

    @pytest.fixture
    def store(self, bucket):
        """Fixture: GCSObjectStore backed by the fake bucket."""
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore(
                project_id="test-project", bucket_name="test-bucket"
            )
        store.bucket = bucket
        return store

    def test_lock_creates_lock_object_while_held(self, store, bucket):
        """Lock object exists inside the with block, gone after.

        Args:
            store: GCSObjectStore fixture
            bucket: Fake bucket fixture

        Returns:
            None

        Raises:
            AssertionError: If lock object lifecycle is wrong
        """
        with store.lock("locks/abc"):
            assert "locks/abc" in bucket.objects
        assert "locks/abc" not in bucket.objects

    def test_lock_records_holder_for_audit(self, store, bucket):
        """Lock object says who holds it and since when (auditable).

        Args:
            store: GCSObjectStore fixture
            bucket: Fake bucket fixture

        Returns:
            None

        Raises:
            AssertionError: If holder details are missing
        """
        with store.lock("locks/abc"):
            holder = json.loads(bucket.objects["locks/abc"][0])
        assert {"run_id", "host", "pid", "acquired_at"} <= holder.keys()
        assert holder["acquired_at"].endswith("+00:00")

    def test_second_lock_on_same_key_raises_runtime_error(self, store):
        """A second acquirer is refused while the first holds the lock.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If overlap is not blocked
        """
        with store.lock("locks/abc"):
            with pytest.raises(RuntimeError, match="locks/abc"):
                with store.lock("locks/abc"):
                    pytest.fail("second lock must not be entered")

    def test_refusal_names_current_holder(self, store):
        """Error message includes holder details to help an operator.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If holder is not reported
        """
        with store.lock("locks/abc"):
            with pytest.raises(RuntimeError, match="run_id"):
                with store.lock("locks/abc"):
                    pass

    def test_refused_acquirer_does_not_release_holders_lock(
        self, store, bucket
    ):
        """A refused second run must leave the first run's lock intact.

        Args:
            store: GCSObjectStore fixture
            bucket: Fake bucket fixture

        Returns:
            None

        Raises:
            AssertionError: If the holder's lock was removed
        """
        with store.lock("locks/abc"):
            with pytest.raises(RuntimeError):
                with store.lock("locks/abc"):
                    pass
            assert "locks/abc" in bucket.objects

    def test_different_keys_do_not_block_each_other(self, store):
        """Locks are per key.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If unrelated keys contend
        """
        with store.lock("locks/a"):
            with store.lock("locks/b"):
                pass

    def test_lock_released_when_body_raises(self, store, bucket):
        """Lock is released even if the pipeline fails inside it.

        Args:
            store: GCSObjectStore fixture
            bucket: Fake bucket fixture

        Returns:
            None

        Raises:
            AssertionError: If lock leaks on error
        """
        with pytest.raises(ValueError):
            with store.lock("locks/abc"):
                raise ValueError("boom")
        assert "locks/abc" not in bucket.objects

    def test_lock_can_be_reacquired_after_release(self, store):
        """Sequential runs work: release then acquire again.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If lock cannot be reacquired
        """
        with store.lock("locks/abc"):
            pass
        with store.lock("locks/abc"):
            pass

    def test_acquire_uses_atomic_create_if_absent(self, store):
        """Acquire relies on if_generation_match=0, not exists().

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If the atomic precondition is not used
        """
        blob = MagicMock()
        blob.generation = 7
        store.bucket = MagicMock()
        store.bucket.blob.return_value = blob
        with store.lock("locks/abc"):
            pass
        assert blob.upload_from_string.call_args.kwargs == {
            "if_generation_match": 0
        }

    def test_release_only_deletes_own_generation(self, store):
        """Release is conditional on our generation (never deletes
        a lock someone else now holds).

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If delete is not generation-guarded
        """
        blob = MagicMock()
        blob.generation = 7
        store.bucket = MagicMock()
        store.bucket.blob.return_value = blob
        with store.lock("locks/abc"):
            pass
        blob.delete.assert_called_once_with(if_generation_match=7)

    def test_release_when_lock_already_gone_logs_warning(self, store):
        """A missing lock at release is logged, not raised.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If release raises or does not warn
        """
        blob = MagicMock()
        blob.generation = 7
        blob.delete.side_effect = NotFound("gone")
        store.bucket = MagicMock()
        store.bucket.blob.return_value = blob
        with patch("py_common.adapters.gcs.logger") as mock_logger:
            with store.lock("locks/abc"):
                pass
        mock_logger.warning.assert_called_once()

    def test_release_when_lock_was_replaced_logs_error(self, store):
        """A changed lock at release is logged loudly, not deleted.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If release raises or does not log error
        """
        blob = MagicMock()
        blob.generation = 7
        blob.delete.side_effect = PreconditionFailed("changed")
        store.bucket = MagicMock()
        store.bucket.blob.return_value = blob
        with patch("py_common.adapters.gcs.logger") as mock_logger:
            with store.lock("locks/abc"):
                pass
        mock_logger.error.assert_called_once()

    def test_unexpected_gcs_error_on_acquire_is_service_error(self, store):
        """Non-conflict GCS errors are ServiceError, not 'lock held'.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If the error is swallowed or reworded
        """
        blob = MagicMock()
        blob.upload_from_string.side_effect = Forbidden("no access")
        store.bucket = MagicMock()
        store.bucket.blob.return_value = blob
        with pytest.raises(ServiceError, match="no access"):
            with store.lock("locks/abc"):
                pass

    def test_lock_logs_acquire_and_release(self, store):
        """Acquire and release are both logged (auditable).

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If log messages are missing
        """
        with patch("py_common.adapters.gcs.logger") as mock_logger:
            with store.lock("locks/abc"):
                pass
        messages = [c.args[0] for c in mock_logger.info.call_args_list]
        assert any("Acquired" in m for m in messages)
        assert any("Released" in m for m in messages)
        mock_logger.warning.assert_not_called()


def _listed_blob(name: str, generation: int = 1, size: int = 10) -> Mock:
    """Mock storage.Blob as list_blobs() returns it.

    Args:
        name: Full object name.
        generation: Object generation.
        size: Size in bytes.

    Returns:
        Mock with name, generation and size.
    """
    blob = Mock()
    blob.name = name
    blob.generation = generation
    blob.size = size
    return blob


class TestGCSSourceInit:
    """Test GCSSource initialization."""

    def test_init_normalises_bucket_and_prefix(self):
        """gs:// is stripped and the prefix ends in one slash.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If bucket or prefix not normalised
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            source = GCSSource(
                project_id="test-project",
                bucket_name="gs://landing",
                prefix="/hww/monthly/",
            )
            assert source.bucket_name == "landing"
            assert source.prefix == "hww/monthly/"

    def test_init_empty_prefix_means_bucket_root(self):
        """No prefix lists the top of the bucket.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If prefix is not empty
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            source = GCSSource(project_id="p", bucket_name="landing")
            assert source.prefix == ""

    def test_init_defaults_to_xlsx_only(self):
        """.xls is not picked up: openpyxl can't read it.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If default extensions are not .xlsx only
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            source = GCSSource(project_id="p", bucket_name="landing")
            assert source.extensions == (".xlsx",)

    def test_init_rejects_extension_without_dot(self):
        """Extensions must look like ".xlsx".

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If ValueError isn't raised
        """
        with patch("py_common.adapters.gcs.storage.Client"):
            with pytest.raises(ValueError, match="extensions"):
                GCSSource(
                    project_id="p", bucket_name="b", extensions=("xlsx",)
                )


class TestGCSSourceItems:
    """Test GCSSource.items() method.

    Contract: lists matching files directly in the landing folder,
    sorted, with the object generation as the version.
    """

    @pytest.fixture
    def source(self):
        """Fixture: GCSSource on "landing/hww/" with a mocked client."""
        with patch("py_common.adapters.gcs.storage.Client"):
            return GCSSource(
                project_id="test-project",
                bucket_name="landing",
                prefix="hww",
            )

    def test_items_describe_each_workbook(self, source):
        """Name, identity, version and size come from the object.

        Args:
            source: GCSSource fixture

        Returns:
            None

        Raises:
            AssertionError: If SourceItem fields are wrong
        """
        source.client.list_blobs.return_value = [
            _listed_blob("hww/was.xlsx", generation=1712, size=2048)
        ]

        items = source.items()

        assert items == [
            SourceItem(
                name="was.xlsx",
                identity="hww/was.xlsx",
                version="1712",
                size_bytes=2048,
            )
        ]

    def test_items_list_only_the_landing_folder(self, source):
        """The listing uses the prefix and stops at sub-folders.

        Args:
            source: GCSSource fixture

        Returns:
            None

        Raises:
            AssertionError: If list_blobs isn't scoped to the folder
        """
        source.client.list_blobs.return_value = []

        source.items()

        source.client.list_blobs.assert_called_once_with(
            "landing", prefix="hww/", delimiter="/"
        )

    def test_items_skip_other_file_types(self, source):
        """Only .xlsx (any case) is listed; .xls and .csv are not.

        Args:
            source: GCSSource fixture

        Returns:
            None

        Raises:
            AssertionError: If a non-.xlsx object is listed
        """
        source.client.list_blobs.return_value = [
            _listed_blob("hww/notes.csv"),
            _listed_blob("hww/legacy.xls"),
            _listed_blob("hww/PEERS.XLSX"),
        ]

        assert [i.name for i in source.items()] == ["PEERS.XLSX"]

    def test_items_are_sorted_by_object_name(self, source):
        """Order is stable whatever order GCS returns.

        Args:
            source: GCSSource fixture

        Returns:
            None

        Raises:
            AssertionError: If items are not sorted
        """
        source.client.list_blobs.return_value = [
            _listed_blob("hww/was.xlsx"),
            _listed_blob("hww/peers.xlsx"),
        ]

        assert [i.identity for i in source.items()] == [
            "hww/peers.xlsx",
            "hww/was.xlsx",
        ]

    def test_items_wraps_listing_failure_in_service_error(self, source):
        """A permission error surfaces as ServiceError.

        Args:
            source: GCSSource fixture

        Returns:
            None

        Raises:
            AssertionError: If ServiceError isn't raised
        """
        source.client.list_blobs.side_effect = Forbidden("no access")

        with pytest.raises(ServiceError, match="Cannot list"):
            source.items()


class TestGCSSourceDownload:
    """Test GCSSource.download() method.

    Contract: returns the bytes of exactly the listed generation, or
    raises VersionMismatch / NotFound instead.
    """

    @pytest.fixture
    def mock_bucket(self):
        """Fixture: Mock GCS bucket."""
        return MagicMock()

    @pytest.fixture
    def source(self, mock_bucket):
        """Fixture: GCSSource with mocked bucket."""
        with patch("py_common.adapters.gcs.storage.Client"):
            source = GCSSource(project_id="p", bucket_name="landing")
            source.bucket = mock_bucket
            return source

    @pytest.fixture
    def item(self):
        """Fixture: item listed at generation 5."""
        return SourceItem(
            name="was.xlsx",
            identity="hww/was.xlsx",
            version="5",
            size_bytes=10,
        )

    def test_download_returns_listed_generation(
        self, source, mock_bucket, item
    ):
        """Bytes are fetched with a generation precondition.

        Args:
            source: GCSSource fixture
            mock_bucket: Mocked GCS bucket
            item: SourceItem fixture

        Returns:
            None

        Raises:
            AssertionError: If bytes or precondition are wrong
        """
        mock_bucket.get_blob.return_value = _listed_blob(
            "hww/was.xlsx", generation=5
        )
        blob = mock_bucket.blob.return_value
        blob.download_as_bytes.return_value = b"workbook"

        assert source.download(item) == b"workbook"
        mock_bucket.blob.assert_called_once_with("hww/was.xlsx")
        blob.download_as_bytes.assert_called_once_with(if_generation_match=5)

    def test_download_raises_when_object_overwritten(
        self, source, mock_bucket, item
    ):
        """A newer generation means the listed one is stale.

        Args:
            source: GCSSource fixture
            mock_bucket: Mocked GCS bucket
            item: SourceItem fixture

        Returns:
            None

        Raises:
            AssertionError: If VersionMismatch isn't raised
        """
        mock_bucket.get_blob.return_value = _listed_blob(
            "hww/was.xlsx", generation=6
        )

        with pytest.raises(VersionMismatch, match="generation 5"):
            source.download(item)
        mock_bucket.blob.return_value.download_as_bytes.assert_not_called()

    def test_download_raises_when_overwritten_mid_download(
        self, source, mock_bucket, item
    ):
        """The generation precondition failing is a VersionMismatch.

        Args:
            source: GCSSource fixture
            mock_bucket: Mocked GCS bucket
            item: SourceItem fixture

        Returns:
            None

        Raises:
            AssertionError: If VersionMismatch isn't raised
        """
        mock_bucket.get_blob.return_value = _listed_blob(
            "hww/was.xlsx", generation=5
        )
        mock_bucket.blob.return_value.download_as_bytes.side_effect = (
            PreconditionFailed("generation changed")
        )

        with pytest.raises(VersionMismatch, match="during download"):
            source.download(item)

    def test_download_raises_not_found_when_deleted(
        self, source, mock_bucket, item
    ):
        """A deleted object raises py_common's NotFound.

        Args:
            source: GCSSource fixture
            mock_bucket: Mocked GCS bucket
            item: SourceItem fixture

        Returns:
            None

        Raises:
            AssertionError: If NotFound isn't raised
        """
        mock_bucket.get_blob.return_value = None

        with pytest.raises(ItemNotFound, match="no longer exists"):
            source.download(item)

    def test_download_wraps_other_errors_in_service_error(
        self, source, mock_bucket, item
    ):
        """A permission error surfaces as ServiceError.

        Args:
            source: GCSSource fixture
            mock_bucket: Mocked GCS bucket
            item: SourceItem fixture

        Returns:
            None

        Raises:
            AssertionError: If ServiceError isn't raised
        """
        mock_bucket.get_blob.side_effect = Forbidden("no access")

        with pytest.raises(ServiceError, match="Cannot read"):
            source.download(item)
