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
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreConflict(key)
        self.objects[key] = data

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        yield
