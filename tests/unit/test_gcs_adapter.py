"""Unit tests for GCSObjectStore adapter.

Tests define the ObjectStore protocol contract without requiring live
GCS services. All external dependencies are mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test name describes what is being verified
- Transparent: No hardcoded secrets; mocks are explicit
"""

import json

import pytest
from unittest.mock import Mock, MagicMock, patch

from google.api_core.exceptions import (
    Forbidden,
    NotFound,
    PreconditionFailed,
)

from py_common.adapters.gcs import GCSObjectStore


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
    Never raises on missing object.
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
        blob.download_as_bytes.side_effect = Exception("Blob not found")
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
        blob.download_as_bytes.side_effect = Exception("Not found")
        mock_bucket.blob.return_value = blob

        with patch("py_common.adapters.gcs.logger") as mock_logger:
            store.get("missing-key")
            # Verify debug was logged
            assert mock_logger.debug.called


class TestGCSObjectStorePut:
    """Test GCSObjectStore.put() method.

    Critical contract (idempotent and safe):
    - put(k, d) twice with same key and data must succeed both times
    - put(k, d1); put(k, d2) with d1 != d2 must raise ValueError
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
        blob.download_as_bytes.return_value = b"data"
        mock_bucket.blob.return_value = blob

        # Call put twice with same key and data
        store.put("existing-key", b"data")
        store.put("existing-key", b"data")

        # Both calls attempted the atomic write and fell back to a
        # content comparison; neither should raise.
        assert blob.upload_from_string.call_count == 2

    def test_put_raises_on_conflict(self, store, mock_bucket):
        """put() raises ValueError when data differs (prevent corruption).

        Args:
            store: GCSObjectStore fixture
            mock_bucket: Mocked GCS bucket

        Returns:
            None

        Raises:
            AssertionError: If ValueError not raised on conflict
        """
        blob = Mock()
        blob.upload_from_string.side_effect = PreconditionFailed(
            "generation exists"
        )
        blob.download_as_bytes.return_value = b"old data"
        mock_bucket.blob.return_value = blob

        with pytest.raises(ValueError, match="different content"):
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


class _FakeBlob:
    """In-memory stand-in for a GCS blob with generation checks."""

    def __init__(self, objects, key):
        self._objects = objects
        self._key = key
        self.generation = None

    def upload_from_string(self, data, if_generation_match=None):
        exists = self._key in self._objects
        if if_generation_match == 0 and exists:
            raise PreconditionFailed("exists")
        gen = self._objects.get(self._key, (None, 0))[1] + 1
        raw = data.encode() if isinstance(data, str) else data
        self._objects[self._key] = (raw, gen)
        self.generation = gen

    def download_as_bytes(self):
        if self._key not in self._objects:
            raise NotFound("missing")
        return self._objects[self._key][0]

    def delete(self, if_generation_match=None):
        if self._key not in self._objects:
            raise NotFound("missing")
        current = self._objects[self._key][1]
        if if_generation_match not in (None, current):
            raise PreconditionFailed("generation changed")
        del self._objects[self._key]


class _FakeBucket:
    """In-memory stand-in for a GCS bucket."""

    def __init__(self):
        self.objects = {}

    def blob(self, key):
        return _FakeBlob(self.objects, key)


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
        return _FakeBucket()

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

    def test_unexpected_gcs_error_on_acquire_propagates(self, store):
        """Non-conflict GCS errors are not disguised as 'lock held'.

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
        with pytest.raises(Forbidden):
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
