"""A dict-backed ObjectStore for unit tests."""

from collections.abc import Iterator
from contextlib import contextmanager

from py_common.errors import ObjectStoreConflict


class MemoryStore:
    """Dict-backed ObjectStore with the real put() conflict rule.

    Used instead of LocalObjectStore, whose deep keys can pass
    Windows' 260-character path limit under a long temp directory.

    Attributes:
        objects: Key -> bytes written.
    """

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        """Return an object's bytes.

        Args:
            key: The object's key.

        Returns:
            The bytes, or None if there is no such object.
        """
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        """Write an object once, as the real stores do.

        Args:
            key: The object's key.
            data: The bytes to store.

        Raises:
            ObjectStoreConflict: The key holds different bytes.
        """
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreConflict(key)
        self.objects[key] = data

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """Hold no lock: tests using this store run one at a time.

        Args:
            key: The lock's key, ignored.

        Yields:
            Nothing.
        """
        yield
