"""Unit tests for GCSObjectStore adapter.

Tests define the ObjectStore protocol contract without requiring live
GCS services. All external dependencies are mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test name describes what is being verified
- Transparent: No hardcoded secrets; mocks are explicit
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from google.api_core.exceptions import PreconditionFailed

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


class TestGCSObjectStoreLock:
    """Test GCSObjectStore.lock() context manager.

    Contract: lock(key) acquires and releases exclusive access.
    Deliberate no-op stub for PHW's sequential (one-job-at-a-time)
    deployment - see README "Concurrency" section. It must still warn
    loudly every time it's used, since silently offering no real
    protection is exactly what makes a stub dangerous to forget about.
    """

    @pytest.fixture
    def store(self):
        """Fixture: GCSObjectStore with mocked client."""
        with patch("py_common.adapters.gcs.storage.Client"):
            return GCSObjectStore(
                project_id="test-project", bucket_name="test-bucket"
            )

    def test_lock_context_manager(self, store):
        """lock() returns usable context manager.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If lock() context manager fails
        """
        with store.lock("test-lock"):
            pass  # Should not raise

    def test_lock_logs_debug(self, store):
        """lock() logs a release debug message (auditable).

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If debug log not captured
        """
        with patch("py_common.adapters.gcs.logger") as mock_logger:
            with store.lock("test-lock"):
                pass
            assert mock_logger.debug.called

    def test_lock_warns_no_concurrency_protection(self, store):
        """lock() warns every acquisition that it offers no real lock.

        Fix for issue #3: a silent no-op stub is easy to forget about
        once concurrency assumptions change. A visible warning at
        every acquisition keeps that risk in the logs.

        Args:
            store: GCSObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If warning not logged
        """
        with patch("py_common.adapters.gcs.logger") as mock_logger:
            with store.lock("test-lock"):
                pass
            mock_logger.warning.assert_called_once()
            assert "test-lock" in mock_logger.warning.call_args[0][0]
