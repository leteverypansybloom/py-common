"""Edge cases across the adapters and value conversion."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from unittest.mock import MagicMock, patch

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore, GCSSource
from py_common.adapters.local import LocalSource
from py_common.contract import SheetRows
from py_common.model import Outcome, SourceItem
from py_common.raw import json_value, to_ndjson

ITEM = SourceItem("f.xlsx", "f.xlsx", "1", 1)


class TestLocalSourceVersion:
    """An edit in the same second as discovery is still seen."""

    def test_version_is_mtime_ns_and_size(self, tmp_path: Path) -> None:
        """The version is the nanosecond mtime and the size.

        Args:
            tmp_path: Folder for the file.
        """
        path = tmp_path / "a.xlsx"
        path.write_bytes(b"one")
        stat = path.stat()

        [item] = LocalSource(tmp_path).items()

        assert item.version == f"{stat.st_mtime_ns}-{stat.st_size}"

    def test_same_second_edit_is_a_version_mismatch(
        self, tmp_path: Path
    ) -> None:
        """An edit within the same second still changes the version.

        Args:
            tmp_path: Folder for the file.
        """
        from py_common.errors import VersionMismatch

        path = tmp_path / "a.xlsx"
        path.write_bytes(b"one")
        os.utime(
            path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000)
        )
        [item] = LocalSource(tmp_path).items()
        path.write_bytes(b"two!")
        os.utime(
            path, ns=(1_000_000_000_000_000_001, 1_000_000_000_000_000_001)
        )

        try:
            LocalSource(tmp_path).download(item)
        except VersionMismatch:
            return
        raise AssertionError("same-second edit was not detected")


class TestRawJsonValues:
    """Datetimes are naive UTC; Decimal is a number."""

    def test_aware_datetime_becomes_naive_utc(self) -> None:
        """An aware datetime is converted to UTC and loses its zone."""
        aware = datetime(
            2026, 6, 1, 12, 0, tzinfo=timezone(timedelta(hours=1))
        )

        assert json_value(aware) == "2026-06-01T11:00:00"

    def test_naive_datetime_is_unchanged(self) -> None:
        """A naive datetime is written as it is."""
        assert json_value(datetime(2026, 6, 1, 12, 0)) == "2026-06-01T12:00:00"

    def test_decimal_is_a_json_number(self) -> None:
        """A Decimal is written as a JSON number, not a string."""
        sheets = {
            "S": SheetRows(["a", "b"], [(2, (Decimal("1.25"), Decimal("7")))])
        }

        data, _ = to_ndjson(sheets, ITEM, "c")

        payload = json.loads(data)["payload"]
        assert payload == {"a": 1.25, "b": 7}
        assert isinstance(payload["b"], int)


class TestBucketPrefix:
    """Only a leading gs:// is removed."""

    def test_store_and_source_strip_only_the_prefix(self) -> None:
        """Only a leading "gs://" is removed from the name."""
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "gs://my-gs://bucket")
            source = GCSSource("p", "gs://my-gs://bucket")

        assert store.bucket_name == "my-gs://bucket"
        assert source.bucket_name == "my-gs://bucket"


class TestRawTableClustering:
    """Raw tables cluster on what the latest-upload query filters."""

    def test_new_raw_table_is_clustered(self) -> None:
        """A new raw table is clustered by file and upload."""
        from google.api_core.exceptions import NotFound

        client = MagicMock()
        with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
            cls.return_value = client
            warehouse = RawJsonWarehouse("p", "d", {"S": "raw_s"})
        client.get_table.side_effect = NotFound("missing")

        warehouse._ensure_raw_table("raw_s")

        table = client.create_table.call_args.args[0]
        assert table.clustering_fields == ["source_file", "source_generation"]
        assert table.time_partitioning.field == "ingested_at"


def test_outcome_is_a_str_enum() -> None:
    """Outcome is a StrEnum, so it formats as its value."""
    assert issubclass(Outcome, StrEnum)
    assert f"{Outcome.LOADED}" == "loaded"
