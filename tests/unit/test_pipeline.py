"""Unit tests for Pipeline outcome handling and storage keys.

Uses mocked Source/Warehouse/Contract and either a Mock or a small
in-memory ObjectStore (InMemoryStore, which enforces the real put()
conflict rule), not the local adapters (LocalObjectStore/LocalSource
have a separate, already-tracked Windows path issue unrelated to
what's being tested here).
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import Mock

import pandas as pd
import pytest
from openpyxl import Workbook

from py_common.contract import Column, Contract, Worksheet
from py_common.errors import (
    ContractError,
    IndeterminateCommitError,
    ObjectStoreConflict,
    ServiceError,
    ValidationError,
    VersionMismatch,
)
from py_common.model import Outcome, Result, SourceItem, digest
from py_common.pipeline import Pipeline


@pytest.fixture
def item() -> SourceItem:
    """A single discovered source file."""
    return SourceItem(
        name="events.xlsx", identity="id1", version="v1", size_bytes=10
    )


@pytest.fixture
def pipeline(item):
    """Pipeline wired to mocks; contract.validate() controlled per test."""
    source = Mock()
    source.download.return_value = b"excel bytes"
    store = Mock()
    store.get.return_value = None  # not a duplicate
    warehouse = Mock()
    contract = Mock()
    return Pipeline(
        source=source, store=store, warehouse=warehouse, contract=contract
    )


class TestPipelineProcessQuarantineOutcomes:
    """Fix for issue #7: ContractError must quarantine, not FAIL.

    ContractError is a subclass of ValidationError (see errors.py),
    so Pipeline.process()'s `except ValidationError` already catches
    it. These are regression tests locking that behaviour in - no
    test previously exercised Pipeline.process() at all.
    """

    def test_contract_error_is_quarantined(self, pipeline, item):
        """A missing worksheet/column (ContractError) quarantines."""
        pipeline.contract.validate.side_effect = ContractError(
            "Missing worksheet 'Events'"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.QUARANTINED
        assert "Missing worksheet" in result.errors[0]
        pipeline.warehouse.load.assert_not_called()

    def test_validation_error_is_quarantined(self, pipeline, item):
        """A bad data row (ValidationError) quarantines, unchanged."""
        pipeline.contract.validate.side_effect = ValidationError(
            "Row 2: 'attendees' is required"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.QUARANTINED
        pipeline.warehouse.load.assert_not_called()

    def test_contract_error_stores_raw_and_quarantine_copies(
        self, pipeline, item
    ):
        """ContractError still stores raw + quarantine copies for
        triage, same as any other quarantine outcome."""
        pipeline.contract.validate.side_effect = ContractError(
            "Missing column 'attendees'"
        )

        pipeline.process(item)

        put_keys = [c.args[0] for c in pipeline.store.put.call_args_list]
        assert any(k.startswith("raw/") for k in put_keys)
        assert any(k.startswith("quarantine/") for k in put_keys)


class TestPipelineProcessSkipOutcomes:
    """process() skips a file without touching validate/warehouse.

    Both cases short-circuit before contract validation, so the
    lightweight `pipeline`/`item` fixtures (with unparseable "excel
    bytes" source data) are enough here.
    """

    def test_version_mismatch_is_skipped(self, pipeline, item):
        """Source raising VersionMismatch on download skips the file.

        Args:
            pipeline: Pipeline fixture wired to mocks.
            item: SourceItem fixture.

        Returns:
            None

        Raises:
            AssertionError: If outcome is not SKIPPED.
        """
        pipeline.source.download.side_effect = VersionMismatch(
            "changed during download"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.SKIPPED
        assert result.checksum == ""
        pipeline.contract.validate.assert_not_called()

    def test_duplicate_checksum_is_skipped(self, pipeline, item):
        """A checksum already recorded in the store skips the file.

        Args:
            pipeline: Pipeline fixture wired to mocks.
            item: SourceItem fixture.

        Returns:
            None

        Raises:
            AssertionError: If outcome is not SKIPPED.
        """
        pipeline.store.get.return_value = b"processed"

        result = pipeline.process(item)

        assert result.outcome == Outcome.SKIPPED
        assert result.checksum != ""
        pipeline.contract.validate.assert_not_called()


class TestPipelineProcessWarehouseOutcomes:
    """Exercises process() past validation: Parquet conversion and
    warehouse load.

    Unlike `pipeline`/`item` above, these fixtures use a real
    Contract and a real minimal workbook so `_to_parquet`'s
    worksheet lookup (`contract.worksheets[0].name`) resolves
    against actual data instead of a Mock.
    """

    @pytest.fixture
    def workbook_bytes(self) -> bytes:
        """A minimal one-row workbook, sheet named "Events"."""
        wb = Workbook()
        ws = wb.active
        ws.title = "Events"
        ws.append(["name", "count"])
        ws.append(["a", 1])
        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    @pytest.fixture
    def contract(self) -> Contract:
        """Real Contract matching workbook_bytes' single worksheet."""
        return Contract(
            worksheets=[
                Worksheet(
                    name="Events",
                    columns=[
                        Column(name="name", data_type="string"),
                        Column(name="count", data_type="integer"),
                    ],
                )
            ]
        )

    @pytest.fixture
    def item(self) -> SourceItem:
        """A single discovered source file."""
        return SourceItem(
            name="events.xlsx", identity="id1", version="v1", size_bytes=10
        )

    @pytest.fixture
    def pipeline(self, contract, workbook_bytes) -> Pipeline:
        """Pipeline wired to mocks, with a real Contract + workbook."""
        source = Mock()
        source.download.return_value = workbook_bytes
        store = Mock()
        store.get.return_value = None  # not a duplicate
        warehouse = Mock()
        return Pipeline(
            source=source,
            store=store,
            warehouse=warehouse,
            contract=contract,
        )

    def test_valid_file_is_loaded(self, pipeline, item):
        """A validated file converts, stores and loads successfully.

        Args:
            pipeline: Pipeline fixture with real Contract + workbook.
            item: SourceItem fixture.

        Returns:
            None

        Raises:
            AssertionError: If outcome is not LOADED.
        """
        result = pipeline.process(item)

        assert result.outcome == Outcome.LOADED
        assert result.rows_processed == 1
        assert result.errors == []
        assert result.warehouse_key
        pipeline.warehouse.load.assert_called_once()

    def test_warehouse_service_error_quarantines(self, pipeline, item):
        """A ServiceError from the warehouse quarantines the file.

        Args:
            pipeline: Pipeline fixture with real Contract + workbook.
            item: SourceItem fixture.

        Returns:
            None

        Raises:
            AssertionError: If outcome is not QUARANTINED.
        """
        pipeline.warehouse.load.side_effect = ServiceError(
            "warehouse unavailable"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.QUARANTINED
        assert any("Warehouse error" in e for e in result.errors)

    def test_warehouse_indeterminate_commit_fails(self, pipeline, item):
        """An indeterminate commit is reported as FAILED, not LOADED.

        Args:
            pipeline: Pipeline fixture with real Contract + workbook.
            item: SourceItem fixture.

        Returns:
            None

        Raises:
            AssertionError: If outcome is not FAILED.
        """
        pipeline.warehouse.load.side_effect = IndeterminateCommitError(
            "commit timed out"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.FAILED
        assert "Warehouse commit indeterminate" in result.errors
        assert any("commit timed out" in e for e in result.errors)


class TestPipelineAuditKey:
    """_audit() must produce Windows-safe object store keys.

    datetime.isoformat() timestamps contain colons, which are
    invalid in Windows filenames; _audit() must strip them before
    using the timestamp as part of an object store key.
    """

    def test_audit_key_has_no_colons(self, pipeline, item):
        """The audit key passed to store.put() contains no colon.

        Args:
            pipeline: Pipeline fixture wired to mocks.
            item: SourceItem fixture.

        Returns:
            None

        Raises:
            AssertionError: If the audit key contains a colon.
        """
        result = Result(
            item=item,
            outcome=Outcome.LOADED,
            checksum="abc123",
            rows_processed=1,
            errors=[],
            processing_time_seconds=0.1,
        )

        pipeline._audit(result)

        put_keys = [c.args[0] for c in pipeline.store.put.call_args_list]
        audit_key = next(k for k in put_keys if k.startswith("audit/records/"))
        assert ":" not in audit_key


class InMemoryStore:
    """Dict-backed ObjectStore with the real put() conflict rule."""

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        """Return stored bytes or None.

        Args:
            key: Object key.

        Returns:
            Stored bytes, or None if absent.
        """
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        """Store bytes; identical rewrite is fine, different raises.

        Args:
            key: Object key.
            data: Bytes to store.

        Raises:
            ObjectStoreConflict: Key holds different bytes.
        """
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreConflict(key)
        self.objects[key] = data

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """No-op lock.

        Args:
            key: Lock key.

        Yields:
            None.
        """
        yield


def _workbook(rows: list[tuple[str, int]]) -> bytes:
    """Build an "Events" workbook with name/count rows.

    Args:
        rows: Data rows.

    Returns:
        .xlsx bytes.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["name", "count"])
    for row in rows:
        ws.append(list(row))
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class TestPipelineStorageKeysAreVersioned:
    """A file edited in place (same identity, new bytes) must load.

    Stored copies are keyed by identity *and* content checksum, so a
    new version of a file lands beside the old one instead of
    colliding with it (ObjectStoreConflict -> FAILED). Old versions
    are kept, which is what an auditable raw layer needs.
    """

    @pytest.fixture
    def contract(self) -> Contract:
        """Contract for the "Events" name/count sheet."""
        return Contract(
            worksheets=[
                Worksheet(
                    name="Events",
                    columns=[
                        Column(name="name", data_type="string"),
                        Column(name="count", data_type="integer"),
                    ],
                )
            ]
        )

    @pytest.fixture
    def store(self) -> InMemoryStore:
        """Empty in-memory store."""
        return InMemoryStore()

    def _pipeline(
        self, data: bytes, store: InMemoryStore, contract: Contract
    ) -> Pipeline:
        """Pipeline whose source returns data for any item.

        Args:
            data: Bytes the source returns.
            store: Object store.
            contract: Contract.

        Returns:
            Pipeline.
        """
        source = Mock()
        source.download.return_value = data
        warehouse = Mock()
        warehouse.identity = "project.dataset.events"
        return Pipeline(
            source=source, store=store, warehouse=warehouse, contract=contract
        )

    def test_edited_file_with_same_identity_loads(
        self, store: InMemoryStore, contract: Contract
    ) -> None:
        """Second version of the same item loads, not FAILED.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        v1 = SourceItem("events.xlsx", "ID1", "v1", 10)
        v2 = SourceItem("events.xlsx", "ID1", "v2", 10)
        first = self._pipeline(_workbook([("a", 1)]), store, contract)
        second = self._pipeline(_workbook([("a", 2)]), store, contract)

        assert first.process(v1).outcome == Outcome.LOADED
        result = second.process(v2)

        assert result.outcome == Outcome.LOADED, result.errors

    def test_both_raw_versions_are_kept(
        self, store: InMemoryStore, contract: Contract
    ) -> None:
        """Each version's original bytes stay recoverable.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        old, new = _workbook([("a", 1)]), _workbook([("a", 2)])
        item = SourceItem("events.xlsx", "ID1", "v1", 10)
        self._pipeline(old, store, contract).process(item)
        self._pipeline(new, store, contract).process(item)

        assert store.objects[f"raw/ID1/{digest(old)}/events.xlsx"] == old
        assert store.objects[f"raw/ID1/{digest(new)}/events.xlsx"] == new

    def test_processed_key_includes_checksum(
        self, store: InMemoryStore, contract: Contract
    ) -> None:
        """Parquet is stored per content version too.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        data = _workbook([("a", 1)])
        item = SourceItem("events.xlsx", "ID1", "v1", 10)
        self._pipeline(data, store, contract).process(item)

        assert (
            f"processed/project.dataset.events/ID1/{digest(data)}/"
            "events.xlsx.parquet" in store.objects
        )

    def test_quarantine_keys_include_checksum(
        self, store: InMemoryStore, contract: Contract
    ) -> None:
        """Invalid versions are also kept side by side.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        bad = b"not a workbook"
        item = SourceItem("events.xlsx", "ID1", "v1", 10)
        result = self._pipeline(bad, store, contract).process(item)

        assert result.outcome == Outcome.QUARANTINED
        key = f"ID1/{digest(bad)}/events.xlsx"
        assert store.objects[f"raw/{key}"] == bad
        assert store.objects[f"quarantine/{key}"] == bad

    def test_corrected_file_loads_after_quarantine(
        self, store: InMemoryStore, contract: Contract
    ) -> None:
        """Fixing a quarantined file in place lets it load.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        item = SourceItem("events.xlsx", "ID1", "v1", 10)
        self._pipeline(b"broken", store, contract).process(item)
        fixed = self._pipeline(_workbook([("a", 1)]), store, contract)

        assert fixed.process(item).outcome == Outcome.LOADED


class TestPipelineRetryAfterWarehouseFailure:
    """A file whose warehouse load failed must load on the next run.

    Processed Parquet is keyed by content checksum and written before
    the warehouse load. If it carried a conversion-time timestamp, a
    retry would produce different bytes for the same key and fail
    with ObjectStoreConflict on every later run. So the Parquet must
    be a pure function of the source bytes; the warehouse stamps
    _loaded_at at commit time instead.
    """

    @pytest.fixture
    def contract(self) -> Contract:
        """Contract for the "Events" name/count sheet."""
        return Contract(
            worksheets=[
                Worksheet(
                    name="Events",
                    columns=[
                        Column(name="name", data_type="string"),
                        Column(name="count", data_type="integer"),
                    ],
                )
            ]
        )

    def test_parquet_is_identical_for_identical_input(
        self, contract: Contract
    ) -> None:
        """Converting the same workbook twice gives the same bytes.

        Args:
            contract: Contract fixture.
        """
        pipeline = Pipeline(Mock(), Mock(), Mock(), contract)
        data = _workbook([("a", 1), ("b", 2)])

        assert pipeline._to_parquet(data) == pipeline._to_parquet(data)

    def test_parquet_holds_only_source_columns(
        self, contract: Contract
    ) -> None:
        """No pipeline-added columns such as _loaded_at.

        Args:
            contract: Contract fixture.
        """
        pipeline = Pipeline(Mock(), Mock(), Mock(), contract)
        parquet = pipeline._to_parquet(_workbook([("a", 1)]))

        assert list(pd.read_parquet(BytesIO(parquet)).columns) == [
            "name",
            "count",
        ]

    @pytest.mark.parametrize(
        ("error", "first_outcome"),
        [
            (ServiceError("BigQuery unavailable"), Outcome.QUARANTINED),
            (RuntimeError("transaction rolled back"), Outcome.FAILED),
        ],
    )
    def test_next_run_loads_after_warehouse_failure(
        self,
        contract: Contract,
        error: Exception,
        first_outcome: Outcome,
    ) -> None:
        """Run 1 fails at the warehouse; run 2 of the same file loads.

        Args:
            contract: Contract fixture.
            error: What the warehouse raises on the first load.
            first_outcome: Expected outcome of the first run.
        """
        item = SourceItem("events.xlsx", "ID1", "v1", 10)
        source = Mock()
        source.items.return_value = [item]
        source.download.return_value = _workbook([("a", 1)])
        warehouse = Mock()
        warehouse.identity = "project.dataset.events"
        warehouse.load.side_effect = [error, None]
        pipeline = Pipeline(source, InMemoryStore(), warehouse, contract)

        first = pipeline.run()
        second = pipeline.run()

        assert first[0].outcome == first_outcome
        assert second[0].outcome == Outcome.LOADED, second[0].errors


class TestPipelineToParquetHeaderRow:
    """_to_parquet() reads headings from the contract's header_row."""

    def test_title_line_above_headings_is_not_a_column(self) -> None:
        """Columns come from row 2 when header_row=2."""
        contract = Contract(
            worksheets=[
                Worksheet(
                    name="Events",
                    header_row=2,
                    columns=[
                        Column(name="name", data_type="string"),
                        Column(name="count", data_type="integer"),
                    ],
                )
            ]
        )
        wb = Workbook()
        ws = wb.active
        ws.title = "Events"
        ws.append(["Last updated: 02/03/2026"])
        ws.append(["name", "count"])
        ws.append(["a", 1])
        buffer = BytesIO()
        wb.save(buffer)
        pipeline = Pipeline(Mock(), Mock(), Mock(), contract)

        frame = pd.read_parquet(
            BytesIO(pipeline._to_parquet(buffer.getvalue()))
        )

        assert list(frame.columns) == ["name", "count"]
        assert frame.to_dict("records") == [{"name": "a", "count": 1}]


class TestPipelineToParquetHeadingNames:
    """_to_parquet() keeps every column under its read_sheet() name."""

    def test_repeated_headings_are_all_kept(self) -> None:
        """Both "Other" columns reach the Parquet file."""
        contract = Contract(
            worksheets=[
                Worksheet(
                    name="Events",
                    columns=[
                        Column(name="Other", data_type="string"),
                        Column(name="Other_2", data_type="string"),
                    ],
                )
            ]
        )
        wb = Workbook()
        ws = wb.active
        ws.title = "Events"
        ws.append(["Other", "Other"])
        ws.append(["a", "b"])
        buffer = BytesIO()
        wb.save(buffer)
        pipeline = Pipeline(Mock(), Mock(), Mock(), contract)

        frame = pd.read_parquet(
            BytesIO(pipeline._to_parquet(buffer.getvalue()))
        )

        assert frame.to_dict("records") == [{"Other": "a", "Other_2": "b"}]


class TestPipelineToParquetColumnSelection:
    """_to_parquet() writes only the columns the contract selects."""

    def test_excluded_column_is_not_written(self) -> None:
        """An excluded personal-data column never reaches Parquet."""
        contract = Contract(
            worksheets=[
                Worksheet(
                    name="Events",
                    columns=[Column(name="name", data_type="string")],
                    excluded=["email"],
                )
            ]
        )
        wb = Workbook()
        ws = wb.active
        ws.title = "Events"
        ws.append(["name", "email"])
        ws.append(["a", "a@example.org"])
        buffer = BytesIO()
        wb.save(buffer)
        pipeline = Pipeline(Mock(), Mock(), Mock(), contract)

        frame = pd.read_parquet(
            BytesIO(pipeline._to_parquet(buffer.getvalue()))
        )

        assert list(frame.columns) == ["name"]


class TestPipelineProcessWarnings:
    """Warnings from a "warn" contract are kept, logged and audited."""

    @staticmethod
    def _warn(data: bytes, name: str, warnings: list[str]) -> int:
        """Stand-in for Contract.validate() that records a warning.

        Args:
            data: Workbook bytes (unused).
            name: Source file name.
            warnings: List to append to.

        Returns:
            Row count.
        """
        warnings.append(f"{name} [Events]: Unexpected columns: extra")
        return 1

    @pytest.fixture
    def warn_pipeline(self, pipeline: Pipeline) -> Pipeline:
        """Pipeline whose contract passes the file with one warning."""
        pipeline.contract.validate.side_effect = self._warn
        pipeline._to_parquet = Mock(return_value=b"parquet")
        return pipeline

    def test_file_with_warnings_loads(
        self, warn_pipeline: Pipeline, item: SourceItem
    ) -> None:
        """A warning doesn't stop the load.

        Args:
            warn_pipeline: Pipeline fixture.
            item: SourceItem fixture.
        """
        result = warn_pipeline.process(item)

        assert result.outcome == Outcome.LOADED, result.errors
        assert result.warnings == [
            "events.xlsx [Events]: Unexpected columns: extra"
        ]
        warn_pipeline.warehouse.load.assert_called_once()

    def test_warnings_are_logged_at_warning_level(
        self,
        warn_pipeline: Pipeline,
        item: SourceItem,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """WARNING log lines are what alerts are built on.

        Args:
            warn_pipeline: Pipeline fixture.
            item: SourceItem fixture.
            caplog: pytest log capture.
        """
        with caplog.at_level("WARNING", logger="py_common.pipeline"):
            warn_pipeline.process(item)

        assert any(
            r.levelname == "WARNING" and "Unexpected columns" in r.message
            for r in caplog.records
        )

    def test_warnings_are_in_the_audit_record(
        self, warn_pipeline: Pipeline, item: SourceItem
    ) -> None:
        """The audit record says what was tolerated.

        Args:
            warn_pipeline: Pipeline fixture.
            item: SourceItem fixture.
        """
        result = warn_pipeline.process(item)
        warn_pipeline._audit(result)

        records = [
            json.loads(c.args[1])
            for c in warn_pipeline.store.put.call_args_list
            if c.args[0].startswith("audit/records/")
        ]
        assert records[0]["warnings"] == result.warnings

    def test_clean_file_has_no_warnings(
        self, pipeline: Pipeline, item: SourceItem
    ) -> None:
        """Results default to an empty warnings list.

        Args:
            pipeline: Pipeline fixture.
            item: SourceItem fixture.
        """
        pipeline.contract.validate.return_value = 1
        pipeline._to_parquet = Mock(return_value=b"parquet")

        assert pipeline.process(item).warnings == []


class TestPipelineKeysPerDestination:
    """One workbook can feed several pipelines, one per table."""

    @staticmethod
    def _sheet_pipeline(
        sheet: str, table: str, data: bytes, store: InMemoryStore
    ) -> Pipeline:
        """Pipeline loading one sheet of `data` into `table`.

        Args:
            sheet: Worksheet the contract reads.
            table: Warehouse identity.
            data: Workbook bytes the source returns.
            store: Shared object store.

        Returns:
            Pipeline.
        """
        source = Mock()
        source.download.return_value = data
        warehouse = Mock()
        warehouse.identity = table
        contract = Contract(
            worksheets=[
                Worksheet(
                    name=sheet,
                    columns=[Column(name="name", data_type="string")],
                )
            ]
        )
        return Pipeline(source, store, warehouse, contract)

    @staticmethod
    def _two_sheet_workbook() -> bytes:
        """Workbook with differently-filled "A" and "B" sheets.

        Returns:
            .xlsx bytes.
        """
        wb = Workbook()
        first = wb.active
        first.title = "A"
        first.append(["name"])
        first.append(["from A"])
        second = wb.create_sheet("B")
        second.append(["name"])
        second.append(["from B"])
        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    def test_second_pipeline_on_same_file_is_not_skipped(self) -> None:
        """Loading sheet A doesn't mark the file done for sheet B."""
        store = InMemoryStore()
        data = self._two_sheet_workbook()
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)
        first = self._sheet_pipeline("A", "p.d.a", data, store)
        second = self._sheet_pipeline("B", "p.d.b", data, store)

        first_result = first.process(item)
        first._audit(first_result)
        second_result = second.process(item)

        assert first_result.outcome == Outcome.LOADED
        assert second_result.outcome == Outcome.LOADED, second_result.errors
        second.warehouse.load.assert_called_once()

    def test_same_pipeline_still_skips_a_repeat(self) -> None:
        """A rerun into the same table is still a duplicate."""
        store = InMemoryStore()
        data = self._two_sheet_workbook()
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)
        pipeline = self._sheet_pipeline("A", "p.d.a", data, store)

        pipeline._audit(pipeline.process(item))
        repeat = pipeline.process(item)

        assert repeat.outcome == Outcome.SKIPPED

    def test_each_destination_keeps_its_own_processed_copy(self) -> None:
        """Different sheets' Parquet doesn't collide in the store."""
        store = InMemoryStore()
        data = self._two_sheet_workbook()
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)

        self._sheet_pipeline("A", "p.d.a", data, store).process(item)
        self._sheet_pipeline("B", "p.d.b", data, store).process(item)

        suffix = f"ID1/{digest(data)}/hww.xlsx.parquet"
        assert f"processed/p.d.a/{suffix}" in store.objects
        assert f"processed/p.d.b/{suffix}" in store.objects

    def test_audit_record_names_the_destination(self) -> None:
        """The audit record says which table the attempt was for."""
        store = InMemoryStore()
        data = self._two_sheet_workbook()
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)
        pipeline = self._sheet_pipeline("A", "p.d.a", data, store)

        pipeline._audit(pipeline.process(item))

        record = next(
            json.loads(value)
            for key, value in store.objects.items()
            if key.startswith("audit/records/")
        )
        assert record["warehouse"] == "p.d.a"


class TestPipelineOutputFormat:
    """output_format chooses Parquet or raw JSON rows for the load."""

    @staticmethod
    def _two_sheet_contract() -> Contract:
        """Contract for "Account signups" and "Survey info" sheets.

        Returns:
            Contract.
        """
        return Contract(
            worksheets=[
                Worksheet(
                    name="Account signups",
                    columns=[Column(name="Org", data_type="string")],
                ),
                Worksheet(
                    name="Survey info",
                    columns=[Column(name="Org", data_type="string")],
                ),
            ]
        )

    @staticmethod
    def _two_sheet_workbook() -> bytes:
        """Workbook with two sign-ups and one survey response.

        Returns:
            .xlsx bytes.
        """
        wb = Workbook()
        signups = wb.active
        signups.title = "Account signups"
        signups.append(["Org"])
        signups.append(["Org 1"])
        signups.append(["Org 2"])
        survey = wb.create_sheet("Survey info")
        survey.append(["Org"])
        survey.append(["Org 3"])
        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    def _raw_pipeline(self, store: InMemoryStore) -> Pipeline:
        """Pipeline in raw_json mode over the two-sheet workbook.

        Args:
            store: Object store.

        Returns:
            Pipeline.
        """
        source = Mock()
        source.download.return_value = self._two_sheet_workbook()
        warehouse = Mock()
        warehouse.identity = "p.raw.signups+survey"
        return Pipeline(
            source,
            store,
            warehouse,
            self._two_sheet_contract(),
            output_format="raw_json",
        )

    def test_default_is_parquet(self) -> None:
        """Existing pipelines keep loading Parquet."""
        pipeline = Pipeline(Mock(), Mock(), Mock(), Mock())

        assert pipeline.output_format == "parquet"

    def test_unknown_format_is_rejected(self) -> None:
        """A typo fails when the pipeline is built."""
        with pytest.raises(ValueError, match="output_format"):
            Pipeline(Mock(), Mock(), Mock(), Mock(), output_format="csv")

    def test_raw_json_loads_rows_from_every_sheet(self) -> None:
        """The warehouse gets NDJSON for all sheets and their total."""
        pipeline = self._raw_pipeline(InMemoryStore())
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)

        result = pipeline.process(item)

        assert result.outcome == Outcome.LOADED, result.errors
        assert result.rows_processed == 3
        call = pipeline.warehouse.load.call_args
        assert call.kwargs["rows"] == 3
        sheets = [
            json.loads(line)["sheet_name"]
            for line in call.kwargs["data"].decode().splitlines()
        ]
        assert sheets == ["Account signups", "Account signups", "Survey info"]

    def test_raw_json_processed_copy_is_ndjson(self) -> None:
        """The stored copy is the NDJSON the warehouse was given."""
        store = InMemoryStore()
        pipeline = self._raw_pipeline(store)
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)
        data = pipeline.source.download.return_value

        pipeline.process(item)

        key = (
            f"processed/p.raw.signups+survey/ID1/{digest(data)}/"
            "hww.xlsx.ndjson"
        )
        assert (
            store.objects[key]
            == pipeline.warehouse.load.call_args.kwargs["data"]
        )

    def test_raw_json_retry_after_failure_loads(self) -> None:
        """Identical NDJSON on retry: no processed/ conflict."""
        store = InMemoryStore()
        pipeline = self._raw_pipeline(store)
        pipeline.warehouse.load.side_effect = [RuntimeError("boom"), None]
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)

        first = pipeline.process(item)
        second = pipeline.process(item)

        assert first.outcome == Outcome.FAILED
        assert second.outcome == Outcome.LOADED, second.errors
