"""Small correctness fixes from the review: L1, L3, L4, L5, M1, N3."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore, GCSSource
from py_common.adapters.local import LocalSource
from py_common.contract import SheetRows
from py_common.model import Outcome, SourceItem
from py_common.raw import json_value, to_ndjson

ITEM = SourceItem("f.xlsx", "f.xlsx", "1", 1)


class TestLocalSourceVersion:
    """L1: an edit in the same second as discovery is still seen."""

    def test_version_is_mtime_ns_and_size(self, tmp_path: Path) -> None:
        path = tmp_path / "a.xlsx"
        path.write_bytes(b"one")
        stat = path.stat()

        [item] = LocalSource(tmp_path).items()

        assert item.version == f"{stat.st_mtime_ns}-{stat.st_size}"

    def test_same_second_edit_is_a_version_mismatch(
        self, tmp_path: Path
    ) -> None:
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
    """L3: datetimes are naive UTC; Decimal is a number."""

    def test_aware_datetime_becomes_naive_utc(self) -> None:
        aware = datetime(
            2026, 6, 1, 12, 0, tzinfo=timezone(timedelta(hours=1))
        )

        assert json_value(aware) == "2026-06-01T11:00:00"

    def test_naive_datetime_is_unchanged(self) -> None:
        assert json_value(datetime(2026, 6, 1, 12, 0)) == "2026-06-01T12:00:00"

    def test_decimal_is_a_json_number(self) -> None:
        sheets = {
            "S": SheetRows(["a", "b"], [(2, (Decimal("1.25"), Decimal("7")))])
        }

        data, _ = to_ndjson(sheets, ITEM, "c")

        payload = json.loads(data)["payload"]
        assert payload == {"a": 1.25, "b": 7}
        assert isinstance(payload["b"], int)


class TestTypedRowCount:
    """L4: the staged row count comes from the load job, not COUNT(*)."""

    def test_no_count_query_and_mismatch_still_detected(self) -> None:
        client = MagicMock()
        with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
            cls.return_value = client
            warehouse = BigQueryWarehouse("p", "d", "t")
        client.load_table_from_file.return_value = Mock(
            job_id="load-1", output_rows=3
        )

        try:
            warehouse.load("k", "c" * 64, b"pq", 2, ITEM)
        except ValueError as e:
            assert "expected 2, got 3" in str(e)
        else:
            raise AssertionError("row count mismatch not raised")
        assert not any(
            "COUNT(*) as cnt" in c.args[0] for c in client.query.call_args_list
        )


class TestBucketPrefix:
    """L5: only a leading gs:// is removed."""

    def test_store_and_source_strip_only_the_prefix(self) -> None:
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "gs://my-gs://bucket")
            source = GCSSource("p", "gs://my-gs://bucket")

        assert store.bucket_name == "my-gs://bucket"
        assert source.bucket_name == "my-gs://bucket"


class TestRawTableClustering:
    """M1: raw tables cluster on what the latest-upload query filters."""

    def test_new_raw_table_is_clustered(self) -> None:
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
    """N3: StrEnum, so it formats as its value."""
    assert issubclass(Outcome, StrEnum)
    assert f"{Outcome.LOADED}" == "loaded"
