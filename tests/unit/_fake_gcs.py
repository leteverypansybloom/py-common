"""An in-memory stand-in for the parts of a GCS bucket the store uses.

Objects carry generations and an MD5 like real GCS, writes honour
`if_generation_match`, and any operation can be made to fail. A hook
can run just before the next conditional write, to stage a race with
another process.
"""

import base64
import hashlib
from collections.abc import Callable
from typing import Any

from google.api_core.exceptions import NotFound, PreconditionFailed


class FakeBlob:
    """Fake blob: content, generation and an md5_hash like GCS's."""

    def __init__(self, bucket: "FakeBucket", key: str) -> None:
        """Create a blob handle; the object need not exist.

        Args:
            bucket: The bucket it belongs to.
            key: The object's name.
        """
        self._bucket = bucket
        self.name = key
        self.generation: int | None = None
        self.md5_hash: str | None = None
        self.crc32c: str | None = None

    def upload_from_string(
        self, data: bytes | str, if_generation_match: int | None = None
    ) -> None:
        """Write the object, honouring if_generation_match.

        Args:
            data: The content.
            if_generation_match: 0 to create only, a generation to
                replace only that generation, or None for any.

        Raises:
            PreconditionFailed: The generation doesn't match.
        """
        self._bucket.check("upload")
        if if_generation_match not in (None, 0):
            hook, self._bucket.before_takeover = (
                self._bucket.before_takeover,
                None,
            )
            if hook is not None:
                hook()
        raw = data.encode() if isinstance(data, str) else data
        current = self._bucket.objects.get(self.name)
        if if_generation_match == 0 and current is not None:
            raise PreconditionFailed("exists")
        if if_generation_match not in (None, 0) and (
            current is None or current[1] != if_generation_match
        ):
            raise PreconditionFailed("generation changed")
        self._bucket.last_generation += 1
        generation = self._bucket.last_generation
        self._bucket.objects[self.name] = (raw, generation)
        self.generation = generation

    def download_as_bytes(self, **_: Any) -> bytes:
        """Return the object's content.

        Args:
            **_: Download options, ignored.

        Returns:
            The bytes.

        Raises:
            NotFound: The object doesn't exist.
        """
        self._bucket.check("download")
        if self.name not in self._bucket.objects:
            raise NotFound("missing")
        return self._bucket.objects[self.name][0]

    def delete(self, if_generation_match: int | None = None) -> None:
        """Delete the object, honouring if_generation_match.

        Args:
            if_generation_match: Only delete this generation, or None for
                any.

        Raises:
            NotFound: The object doesn't exist.
            PreconditionFailed: The generation doesn't match.
        """
        if self.name not in self._bucket.objects:
            raise NotFound("missing")
        current = self._bucket.objects[self.name][1]
        if if_generation_match not in (None, current):
            raise PreconditionFailed("generation changed")
        del self._bucket.objects[self.name]


class FakeBucket:
    """Fake bucket whose calls can be made to raise.

    Attributes:
        objects: key -> (bytes, generation).
        failures: Operation name -> exception to raise.
        downloads: Number of download calls so far.
        before_takeover: Called once, just before the next write
            conditioned on a non-zero generation.
    """

    def __init__(self) -> None:
        """Start empty, with no failures staged."""
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.failures: dict[str, Exception] = {}
        self.downloads = 0
        self.last_generation = 0
        self.before_takeover: Callable[[], None] | None = None

    def check(self, operation: str) -> None:
        """Count a download, or raise the failure staged for an operation.

        Args:
            operation: "upload", "download", "metadata" and so on.
        """
        if operation == "download":
            self.downloads += 1
        if operation in self.failures:
            raise self.failures[operation]

    def blob(self, key: str) -> FakeBlob:
        """Return a handle to an object, which need not exist.

        Args:
            key: The object's name.

        Returns:
            The blob.
        """
        return FakeBlob(self, key)

    def get_blob(self, key: str) -> FakeBlob | None:
        """Return an object's metadata, as GCS's get_blob does.

        Args:
            key: The object's name.

        Returns:
            The blob with its generation and hashes, or None if missing.
        """
        self.check("metadata")
        if key not in self.objects:
            return None
        data, generation = self.objects[key]
        blob = FakeBlob(self, key)
        blob.generation = generation
        blob.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()
        return blob
