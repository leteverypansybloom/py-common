"""A file is never loaded into BigQuery twice (review C2, H2, M4).

Runs the real Pipeline against the real BigQuery adapters on a
stateful fake client (_fake_bigquery.py), so the warehouse's own
audit check is what decides, not a Mock's return value.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import Any
from unittest.mock import Mock, patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from openpyxl import Workbook

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ObjectStoreConflict, ServiceError
from py_common.model import Outcome, SourceItem
from py_common.pipeline import Pipeline

TYPED_TABLE = "p.d.events"
RAW_TABLE = "p.d.raw_events"


class Store:
    """Dict-backed ObjectStore whose writes can be made to fail.

    Attributes:
        objects: Stored bytes by key.
        fail_on: Key prefix whose next put() raises `failure`.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_on: str | None = None
        self.failure: Exception = ServiceError("store write failed")

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        if self.fail_on is not None and key.startswith(self.fail_on):
            self.fail_on = None
            raise self.failure
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreConflict(key)
        self.objects[key] = data

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        yield


def _workbook(*names: str) -> bytes:
    """An "Events" workbook with one row per name."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["name", "count"])
    for count, name in enumerate(names, start=1):
        ws.append([name, count])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


CONTRACT = Contract(
    key_columns=["name"],
    worksheets=[
        Worksheet(
            name="Events",
            columns=[
                Column("name", "string"),
                Column("count", "integer"),
            ],
        )
    ],
)


class Harness:
    """A pipeline over one source file whose bytes and version change.

    Attributes:
        client: The fake BigQuery client (the warehouse's state).
        store: The object store.
        pipeline: The pipeline under test.
        table: Final or raw table ID the rows land in.
    """

    def __init__(self, route: str) -> None:
        self.client = FakeBigQueryClient()
        self.store = Store()
        self.source = Mock()
        self.route = route
        if route == "typed":
            with patch("py_common.adapters.bigquery.bigquery.Client") as c:
                c.return_value = self.client
                warehouse: Any = BigQueryWarehouse(
                    "p", "d", "events", key_columns=["name"]
                )
            self.table = TYPED_TABLE
            self.client.tables[TYPED_TABLE] = ["name", "count", "_loaded_at"]
            fmt = "parquet"
        else:
            with patch("py_common.adapters.bigquery_raw.bigquery.Client") as c:
                c.return_value = self.client
                warehouse = RawJsonWarehouse(
                    "p", "d", {"Events": "raw_events"}
                )
            self.table = RAW_TABLE
            fmt = "raw_json"
        self.pipeline = Pipeline(
            self.source,
            self.store,
            warehouse,
            CONTRACT,
            output_format=fmt,  # type: ignore[arg-type]
        )

    def upload(self, data: bytes, version: str) -> Outcome:
        """Make `data` the file's content at `version`; run once."""
        item = SourceItem("events.xlsx", "landing/events.xlsx", version, 1)
        self.source.items.return_value = [item]
        self.source.download.return_value = data
        [result] = self.pipeline.run()
        return result.outcome

    @property
    def rows(self) -> int:
        return self.client.rows.get(self.table, 0)


@pytest.fixture(params=["typed", "raw"])
def harness(request: pytest.FixtureRequest) -> Harness:
    return Harness(request.param)


A = _workbook("a", "b")
B = _workbook("a", "b", "c")


class TestCrashAfterCommit:
    """Rows committed, marker missing: the retry must not reload."""

    def test_marker_write_fails_then_retry_is_a_no_op(
        self, harness: Harness
    ) -> None:
        """Commit succeeds, the marker put fails: FAILED, then SKIPPED."""
        harness.store.fail_on = "audit/p.d."

        first = harness.upload(A, "1")
        rows_after_first = harness.rows
        second = harness.upload(A, "1")

        assert first == Outcome.FAILED
        assert rows_after_first == 2
        assert second == Outcome.SKIPPED
        assert harness.rows == 2
        assert len(harness.client.audit) == 1

    def test_lost_commit_wait_then_retry_is_a_no_op(
        self, harness: Harness
    ) -> None:
        """Connection drops after COMMIT: indeterminate, then no-op."""
        harness.client.lose_next_commit_wait = ConnectionError("reset")

        first = harness.upload(A, "1")
        second = harness.upload(A, "1")

        assert first == Outcome.FAILED
        assert second == Outcome.SKIPPED
        assert harness.rows == 2
        assert len(harness.client.audit) == 1

    def test_lost_store_markers_are_rebuilt_from_warehouse(
        self, harness: Harness
    ) -> None:
        """Even with the whole store cache gone, BigQuery decides."""
        harness.upload(A, "1")
        harness.store.objects = {
            k: v
            for k, v in harness.store.objects.items()
            if not k.startswith("audit/p.d.")
        }

        assert harness.upload(A, "1") == Outcome.SKIPPED
        assert harness.rows == 2
        assert any(k.startswith("audit/p.d.") for k in harness.store.objects)


class TestAuditRecordFailure:
    """A failed audit JSON write fails that file only (M4)."""

    def test_audit_write_failure_marks_file_failed_and_run_continues(
        self, harness: Harness
    ) -> None:
        """The run returns a result per file instead of aborting."""
        second = SourceItem("other.xlsx", "landing/other.xlsx", "1", 1)
        first = SourceItem("events.xlsx", "landing/events.xlsx", "1", 1)
        harness.source.items.return_value = [first, second]
        harness.source.download.side_effect = [A, B]
        harness.store.fail_on = "audit/records/"

        results = harness.pipeline.run()

        assert [r.outcome for r in results] == [
            Outcome.FAILED,
            Outcome.LOADED,
        ]
        assert "Audit write failed" in results[0].errors[-1]

    def test_marker_is_written_before_audit_record(
        self, harness: Harness
    ) -> None:
        """The marker (the cache) lands even if the audit put fails."""
        harness.store.fail_on = "audit/records/"

        harness.upload(A, "1")

        assert any(k.startswith("audit/p.d.") for k in harness.store.objects)


class TestDedupKey:
    """Typed: content decides. Raw: identity + version decides (H2)."""

    @pytest.fixture
    def raw(self) -> Harness:
        return Harness("raw")

    @pytest.fixture
    def typed(self) -> Harness:
        return Harness("typed")

    def test_raw_revert_to_earlier_content_is_loaded(
        self, raw: Harness
    ) -> None:
        """gen1 = A, gen2 = B, gen3 = A: gen3 is the latest, so it loads."""
        outcomes = [raw.upload(A, "1"), raw.upload(B, "2"), raw.upload(A, "3")]

        assert outcomes == [Outcome.LOADED] * 3
        assert raw.rows == 2 + 3 + 2
        assert [r["source_version"] for r in raw.client.audit] == [
            "1",
            "2",
            "3",
        ]

    def test_raw_same_generation_twice_is_skipped(self, raw: Harness) -> None:
        """The same identity + version is one load, whatever happens."""
        raw.upload(A, "1")

        assert raw.upload(A, "1") == Outcome.SKIPPED
        assert raw.rows == 2

    def test_typed_restore_to_earlier_content_is_loaded(
        self, typed: Harness
    ) -> None:
        """gen1 = A, gen2 = B, gen3 = A: the restore loads, so A's
        values win again (both routes key on the upload, not bytes)."""
        outcomes = [
            typed.upload(A, "1"),
            typed.upload(B, "2"),
            typed.upload(A, "3"),
        ]

        assert outcomes == [Outcome.LOADED] * 3
        assert [r["source_version"] for r in typed.client.audit] == [
            "1",
            "2",
            "3",
        ]

    def test_typed_same_upload_twice_is_skipped(self, typed: Harness) -> None:
        typed.upload(A, "1")

        assert typed.upload(A, "1") == Outcome.SKIPPED
        assert len(typed.client.audit) == 1

    def test_audit_row_names_destination_and_version(
        self, harness: Harness
    ) -> None:
        """The audit row says where the file went and which version."""
        harness.upload(A, "7")

        [row] = harness.client.audit
        # One table per warehouse here, so the table is the identity.
        assert row["destination"] == harness.table
        assert row["source_version"] == "7"
        assert row["outcome"] == "LOADED"


class TestCommitScriptSql:
    """The SQL parts that carry the idempotency semantics."""

    def _script(self, harness: Harness) -> str:
        harness.upload(A, "1")
        return harness.client.scripts[-1]

    def test_check_runs_inside_the_transaction(self, harness: Harness) -> None:
        sql = self._script(harness)
        begin = sql.index("BEGIN TRANSACTION")
        check = sql.index("SELECT COUNT(*) > 0")
        write = sql.index("IF NOT already_loaded_0 THEN")
        commit = sql.index("COMMIT TRANSACTION")
        assert begin < check < write < commit

    def test_check_matches_only_loaded_rows_for_this_destination(
        self, harness: Harness
    ) -> None:
        sql = self._script(harness)
        check = sql[sql.index("SELECT COUNT(*) > 0") :].split(");")[0]
        # Both routes: this table + this upload (file and version),
        # never the bytes, so a restore of earlier content loads.
        assert "destination = @destination_0" in check
        assert "filename = @filename" in check
        assert "source_version = @source_version" in check
        assert "outcome = 'LOADED'" in check
        assert "checksum" not in check

    def test_status_is_selected_after_the_block(
        self, harness: Harness
    ) -> None:
        sql = self._script(harness)
        assert sql.rstrip().endswith(
            "SELECT IF(any_loaded, 'LOADED', 'ALREADY_LOADED') AS status;"
        )
