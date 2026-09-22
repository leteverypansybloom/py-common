"""Unit tests for LocalObjectStore adapter.

Tests define the ObjectStore protocol contract using a real temp
folder (no mocking needed for local filesystem operations).

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test name describes what is being verified
- Transparent: No hardcoded secrets; uses pytest's tmp_path fixture
"""

import pytest

from py_common.adapters.local import LocalObjectStore
from py_common.errors import ObjectStoreConflict


class TestLocalObjectStorePut:
    """Test LocalObjectStore.put() method.

    Critical contract (idempotent and safe), matching GCSObjectStore:
    - put(k, d) twice with same key and data must succeed both times
    - put(k, d1); put(k, d2) with d1 != d2 must raise
      ObjectStoreConflict
    """

    @pytest.fixture
    def store(self, tmp_path):
        """Fixture: LocalObjectStore rooted at a temp folder."""
        return LocalObjectStore(tmp_path / "store")

    def test_put_new_key_writes_normally(self, store):
        """put() writes bytes when key does not exist yet.

        Args:
            store: LocalObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If written bytes do not match input
        """
        store.put("new-key", b"new data")

        assert store.get("new-key") == b"new data"

    def test_put_idempotent_same_data(self, store):
        """put() is idempotent when data matches (no error).

        Calling put twice with same key and data must both succeed.

        Args:
            store: LocalObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If idempotency not enforced
        """
        store.put("existing-key", b"data")
        store.put("existing-key", b"data")

        assert store.get("existing-key") == b"data"

    def test_put_raises_on_conflict(self, store):
        """put() raises ObjectStoreConflict when data differs.

        Args:
            store: LocalObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If ObjectStoreConflict not raised
        """
        store.put("existing-key", b"old data")

        with pytest.raises(ObjectStoreConflict, match="different content"):
            store.put("existing-key", b"new data")

    def test_put_conflict_does_not_overwrite_existing_content(self, store):
        """put() leaves original content untouched after a conflict.

        Args:
            store: LocalObjectStore fixture

        Returns:
            None

        Raises:
            AssertionError: If original content was overwritten
        """
        store.put("existing-key", b"old data")

        with pytest.raises(ObjectStoreConflict):
            store.put("existing-key", b"new data")

        assert store.get("existing-key") == b"old data"
