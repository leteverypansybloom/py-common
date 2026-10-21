"""Unit tests for LocalObjectStore and LocalSource adapters.

Tests define the ObjectStore/Source protocol contracts using a real
temp folder (no mocking needed for local filesystem operations).
"""

import pytest

from py_common.adapters.local import LocalObjectStore, LocalSource
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
        """
        store.put("new-key", b"new data")

        assert store.get("new-key") == b"new data"

    def test_put_idempotent_same_data(self, store):
        """put() is idempotent when data matches (no error).

        Calling put twice with same key and data must both succeed.

        Args:
            store: LocalObjectStore fixture
        """
        store.put("existing-key", b"data")
        store.put("existing-key", b"data")

        assert store.get("existing-key") == b"data"

    def test_put_raises_on_conflict(self, store):
        """put() raises ObjectStoreConflict when data differs.

        Args:
            store: LocalObjectStore fixture
        """
        store.put("existing-key", b"old data")

        with pytest.raises(ObjectStoreConflict, match="different content"):
            store.put("existing-key", b"new data")

    def test_put_conflict_does_not_overwrite_existing_content(self, store):
        """put() leaves original content untouched after a conflict.

        Args:
            store: LocalObjectStore fixture
        """
        store.put("existing-key", b"old data")

        with pytest.raises(ObjectStoreConflict):
            store.put("existing-key", b"new data")

        assert store.get("existing-key") == b"old data"


class TestLocalObjectStoreKeyResolution:
    """put()/get() must resolve keys under folder, never outside it.

    Joining a pathlib path with an absolute right-hand side discards
    the left side, so an absolute key such as C:/Users/.../file.xlsx
    would escape the store's root folder rather than raise.
    """

    @pytest.fixture
    def store(self, tmp_path):
        """Fixture: LocalObjectStore rooted at a temp folder."""
        return LocalObjectStore(tmp_path / "store")

    def test_relative_key_resolves_under_folder(self, store, tmp_path):
        """A normal relative key is written under the store folder.

        Args:
            store: LocalObjectStore fixture
            tmp_path: pytest temp directory fixture
        """
        store.put("raw/events.xlsx", b"data")

        assert (tmp_path / "store" / "raw" / "events.xlsx").exists()

    def test_absolute_path_key_is_rejected(self, store, tmp_path):
        """A key that is itself an absolute path is rejected.

        Args:
            store: LocalObjectStore fixture
            tmp_path: pytest temp directory fixture
        """
        escape_target = tmp_path / "outside.txt"

        with pytest.raises(ValueError, match="escapes"):
            store.put(str(escape_target), b"data")

        assert not escape_target.exists()

    def test_directory_traversal_key_is_rejected(self, store, tmp_path):
        """A key using ".." to climb out of folder is rejected.

        Args:
            store: LocalObjectStore fixture
            tmp_path: pytest temp directory fixture
        """
        with pytest.raises(ValueError, match="escapes"):
            store.put("../outside.txt", b"data")

        assert not (tmp_path / "outside.txt").exists()

    def test_get_rejects_traversal_key(self, store):
        """get() applies the same key-resolution guard as put().

        Args:
            store: LocalObjectStore fixture
        """
        with pytest.raises(ValueError, match="escapes"):
            store.get("../../outside.txt")


class TestLocalSourceIdentity:
    """LocalSource.items() must use portable, relative identities.

    An absolute path's drive-letter colon would break Windows file
    names once pipeline.py puts the identity in an object store key.
    """

    def test_identity_is_relative_to_folder(self, tmp_path):
        """Identity is the filename, not an absolute path.

        Args:
            tmp_path: pytest temp directory fixture
        """
        (tmp_path / "events.xlsx").write_bytes(b"data")
        source = LocalSource(tmp_path)

        items = list(source.items())

        assert len(items) == 1
        assert items[0].identity == "events.xlsx"
        assert ":" not in items[0].identity

    def test_identity_is_relative_for_nested_folder(self, tmp_path):
        """Identity stays relative when the source folder is nested.

        Args:
            tmp_path: pytest temp directory fixture
        """
        nested = tmp_path / "batch1"
        nested.mkdir()
        (nested / "events.xlsx").write_bytes(b"data")
        source = LocalSource(nested)

        items = list(source.items())

        assert items[0].identity == "events.xlsx"

    def test_download_round_trips_with_relative_identity(self, tmp_path):
        """download() resolves the relative identity back to a file.

        Args:
            tmp_path: pytest temp directory fixture
        """
        (tmp_path / "events.xlsx").write_bytes(b"workbook bytes")
        source = LocalSource(tmp_path)
        item = next(iter(source.items()))

        assert source.download(item) == b"workbook bytes"


class TestLocalSourceItems:
    """LocalSource.items() lists only workbooks openpyxl can read."""

    def test_old_xls_files_are_not_listed(self, tmp_path):
        """.xls files are skipped; openpyxl can't open them.

        Args:
            tmp_path: pytest temp directory fixture
        """
        (tmp_path / "events.xlsx").write_bytes(b"data")
        (tmp_path / "legacy.xls").write_bytes(b"data")
        source = LocalSource(tmp_path)

        names = [item.name for item in source.items()]

        assert names == ["events.xlsx"]
