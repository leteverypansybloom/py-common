"""Unit tests for Pipeline outcome handling and storage keys.

Uses mocked Source/Warehouse/Contract and either a Mock or a small
in-memory ObjectStore (the shared MemoryStore, which enforces the real put()
conflict rule), not the local adapters (LocalObjectStore/LocalSource
have a separate, already-tracked Windows path issue unrelated to
what's being tested here).
"""

import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from _fake_gcs import FakeBucket
from _memory_store import MemoryStore
from google.api_core.exceptions import Forbidden
from openpyxl import Workbook

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore
from py_common.adapters.local import LocalSource
from py_common.contract import Column, Contract, SheetRows, Worksheet
from py_common.errors import (
    ConfigError,
    ContractError,
    IndeterminateCommitError,
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
    """Pipeline wired to mocks; contract.read() controlled per test."""
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
    """ContractError must quarantine, not FAIL.

    ContractError is a subclass of ValidationError (see errors.py),
    so Pipeline.process()'s `except ValidationError` catches it.
    These tests lock that behaviour in.
    """

    def test_contract_error_is_quarantined(self, pipeline, item):
        """A missing worksheet/column (ContractError) quarantines."""
        pipeline.contract.read.side_effect = ContractError(
            "Missing worksheet 'Events'"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.QUARANTINED
        assert "Missing worksheet" in result.errors[0]
        pipeline.warehouse.load.assert_not_called()

    def test_validation_error_is_quarantined(self, pipeline, item):
        """A bad data row (ValidationError) quarantines, unchanged."""
        pipeline.contract.read.side_effect = ValidationError(
            "Row 2: 'attendees' is required"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.QUARANTINED
        pipeline.warehouse.load.assert_not_called()

    def test_contract_error_stores_raw_and_quarantine_copies(
        self, pipeline, item
    ):
        """ContractError stores raw and quarantine copies for triage.

        The same as any other quarantine outcome.
        """
        pipeline.contract.read.side_effect = ContractError(
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
        """
        pipeline.source.download.side_effect = VersionMismatch(
            "changed during download"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.SKIPPED
        assert result.checksum == ""
        pipeline.contract.read.assert_not_called()

    def test_already_loaded_upload_is_skipped(self, pipeline, item):
        """An upload whose marker exists is skipped without a download.

        The marker is keyed by identity and version, so nothing needs
        downloading (or hashing) to find it: the result has no
        checksum.

        Args:
            pipeline: Pipeline fixture wired to mocks.
            item: SourceItem fixture.
        """
        pipeline.store.get.return_value = b"processed"

        result = pipeline.process(item)

        assert result.outcome == Outcome.SKIPPED
        assert result.checksum == ""
        pipeline.source.download.assert_not_called()
        pipeline.contract.read.assert_not_called()


class TestPipelineProcessWarehouseOutcomes:
    """Exercise process() past validation: conversion and load.

    Unlike `pipeline`/`item` above, these fixtures use a real
    Contract and a real minimal workbook, so conversion reads
    actual worksheet data instead of a Mock.
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
        """
        result = pipeline.process(item)

        assert result.outcome == Outcome.LOADED
        assert result.rows_processed == 1
        assert result.errors == []
        assert result.warehouse_key
        pipeline.warehouse.load.assert_called_once()

    def test_warehouse_service_error_fails_for_retry(self, pipeline, item):
        """A warehouse ServiceError is FAILED (retryable), not quarantined.

        The file is fine; the service isn't. QUARANTINED would ack
        the upload event and fire the bad-file alert.

        Args:
            pipeline: Pipeline fixture with real Contract + workbook.
            item: SourceItem fixture.
        """
        pipeline.warehouse.load.side_effect = ServiceError(
            "warehouse unavailable"
        )

        result = pipeline.process(item)

        assert result.outcome == Outcome.FAILED
        assert any("Warehouse error" in e for e in result.errors)
        written = [c.args[0] for c in pipeline.store.put.call_args_list]
        assert not any(k.startswith("quarantine/") for k in written)

    def test_warehouse_validation_error_quarantines(
        self, contract, workbook_bytes, item
    ):
        """A file the warehouse rejects as invalid is quarantined.

        Args:
            contract: Real Contract fixture.
            workbook_bytes: Workbook fixture.
            item: SourceItem fixture.
        """
        source = Mock()
        source.download.return_value = workbook_bytes
        store = MemoryStore()
        warehouse = Mock()
        warehouse.identity = "p.d.events"
        warehouse.load.side_effect = ValidationError("duplicate keys: 7")
        pipeline = Pipeline(source, store, warehouse, contract)

        result = pipeline.process(item)

        assert result.outcome == Outcome.QUARANTINED
        assert result.errors == ["duplicate keys: 7"]
        assert any(k.startswith("quarantine/") for k in store.objects)

    def test_warehouse_indeterminate_commit_fails(self, pipeline, item):
        """An indeterminate commit is reported as FAILED, not LOADED.

        Args:
            pipeline: Pipeline fixture with real Contract + workbook.
            item: SourceItem fixture.
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


def _loaded(contract: Contract, data: bytes) -> bytes:
    """Run a workbook through a pipeline and return what it loaded.

    Args:
        contract: The contract to read it with.
        data: The workbook's bytes.

    Returns:
        The NDJSON the warehouse was given.
    """
    source = Mock()
    source.download.return_value = data
    warehouse = _warehouse()
    result = Pipeline(source, MemoryStore(), warehouse, contract).process(
        SourceItem("events.xlsx", "ID1", "v1", 10)
    )
    assert result.outcome == Outcome.LOADED, result.errors
    loaded: bytes = warehouse.load.call_args.kwargs["data"]
    return loaded


def _lines(data: bytes) -> list[dict[str, Any]]:
    """Parse NDJSON into one dict per row.

    Args:
        data: The NDJSON bytes.

    Returns:
        The rows, in order.
    """
    return [json.loads(line) for line in data.decode().splitlines()]


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
    def store(self) -> MemoryStore:
        """Empty in-memory store."""
        return MemoryStore()

    def _pipeline(
        self, data: bytes, store: MemoryStore, contract: Contract
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
        self, store: MemoryStore, contract: Contract
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
        self, store: MemoryStore, contract: Contract
    ) -> None:
        """Each version's original bytes stay recoverable.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        old, new = _workbook([("a", 1)]), _workbook([("a", 2)])
        # An edited file is a new version at the source.
        v1 = SourceItem("events.xlsx", "ID1", "v1", 10)
        v2 = SourceItem("events.xlsx", "ID1", "v2", 10)
        self._pipeline(old, store, contract).process(v1)
        self._pipeline(new, store, contract).process(v2)

        assert store.objects[f"raw/ID1/{digest(old)}/events.xlsx"] == old
        assert store.objects[f"raw/ID1/{digest(new)}/events.xlsx"] == new

    def test_processed_key_includes_checksum(
        self, store: MemoryStore, contract: Contract
    ) -> None:
        """NDJSON is stored per content version and upload too.

        Args:
            store: In-memory store.
            contract: Contract fixture.
        """
        data = _workbook([("a", 1)])
        item = SourceItem("events.xlsx", "ID1", "v1", 10)
        result = self._pipeline(data, store, contract).process(item)

        prefix = f"processed/project.dataset.events/ID1/{digest(data)}/v1/"
        assert result.processed_key.startswith(prefix)
        assert result.processed_key.endswith("/events.xlsx.ndjson")
        assert result.processed_key in store.objects

    def test_quarantine_keys_include_checksum(
        self, store: MemoryStore, contract: Contract
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
        self, store: MemoryStore, contract: Contract
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

    Processed NDJSON is keyed by content checksum and written before
    the warehouse load. If it carried a conversion-time timestamp, a
    retry would produce different bytes for the same key and fail
    with ObjectStoreConflict on every later run. So the NDJSON must
    be a pure function of the source bytes; the warehouse sets
    ingested_at at commit time instead.
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

    def test_ndjson_is_identical_for_identical_input(
        self, contract: Contract
    ) -> None:
        """Converting the same workbook twice gives the same bytes.

        Args:
            contract: Contract fixture.
        """
        data = _workbook([("a", 1), ("b", 2)])

        assert _loaded(contract, data) == _loaded(contract, data)

    def test_ndjson_holds_only_source_columns(
        self, contract: Contract
    ) -> None:
        """No load time is written: the warehouse sets ingested_at.

        Args:
            contract: Contract fixture.
        """
        lines = _lines(_loaded(contract, _workbook([("a", 1)])))

        assert "ingested_at" not in lines[0]
        assert list(lines[0]["payload"]) == ["name", "count"]

    @pytest.mark.parametrize(
        ("error", "first_outcome"),
        [
            (ServiceError("BigQuery unavailable"), Outcome.FAILED),
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
        warehouse.load.side_effect = [error, True]
        pipeline = Pipeline(source, MemoryStore(), warehouse, contract)

        first = pipeline.run()
        second = pipeline.run()

        assert first[0].outcome == first_outcome
        assert second[0].outcome == Outcome.LOADED, second[0].errors


class TestPipelineHeaderRow:
    """Conversion reads headings from the contract's header_row."""

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
        lines = _lines(_loaded(contract, buffer.getvalue()))

        assert [line["payload"] for line in lines] == [
            {"name": "a", "count": 1}
        ]
        assert lines[0]["row_number"] == 3


class TestPipelineHeadingNames:
    """Conversion keeps every column, repeated headings numbered."""

    def test_repeated_headings_are_all_kept(self) -> None:
        """Both "Other" columns reach the payload."""
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
        lines = _lines(_loaded(contract, buffer.getvalue()))

        assert lines[0]["payload"] == {"Other": "a", "Other_2": "b"}


class TestPipelineColumnSelection:
    """Conversion writes only the columns the contract selects."""

    def test_excluded_column_is_not_written(self) -> None:
        """An excluded personal-data column never reaches the payload."""
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
        data = _loaded(contract, buffer.getvalue())

        assert _lines(data)[0]["payload"] == {"name": "a"}
        assert b"a@example.org" not in data


class TestPipelineProcessWarnings:
    """Warnings from a "warn" contract are kept, logged and audited."""

    @staticmethod
    def _warn(
        data: bytes, name: str, warnings: list[str]
    ) -> dict[str, SheetRows]:
        """Stand-in for Contract.read() that records a warning.

        Args:
            data: Workbook bytes (unused).
            name: Source file name.
            warnings: List to append to.

        Returns:
            One row of the "Events" sheet.
        """
        warnings.append(f"{name} [Events]: Unexpected columns: extra")
        return {"Events": SheetRows(["a"], [(2, (1,))])}

    @pytest.fixture
    def warn_pipeline(self, pipeline: Pipeline) -> Pipeline:
        """Pipeline whose contract passes the file with one warning."""
        pipeline.contract.worksheets = [Worksheet("Events")]
        pipeline.contract.read.side_effect = self._warn
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
        pipeline.contract.worksheets = [Worksheet("Events")]
        pipeline.contract.read.return_value = {
            "Events": SheetRows(["a"], [(2, (1,))])
        }

        assert pipeline.process(item).warnings == []


class TestPipelineKeysPerDestination:
    """One workbook can feed several pipelines, one per table."""

    @staticmethod
    def _sheet_pipeline(
        sheet: str, table: str, data: bytes, store: MemoryStore
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
        store = MemoryStore()
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
        store = MemoryStore()
        data = self._two_sheet_workbook()
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)
        pipeline = self._sheet_pipeline("A", "p.d.a", data, store)

        pipeline._audit(pipeline.process(item))
        repeat = pipeline.process(item)

        assert repeat.outcome == Outcome.SKIPPED

    def test_each_destination_keeps_its_own_processed_copy(self) -> None:
        """Different sheets' NDJSON doesn't collide in the store."""
        store = MemoryStore()
        data = self._two_sheet_workbook()
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)

        a = self._sheet_pipeline("A", "p.d.a", data, store).process(item)
        b = self._sheet_pipeline("B", "p.d.b", data, store).process(item)

        upload = f"ID1/{digest(data)}/v1/"
        assert a.processed_key.startswith(f"processed/p.d.a/{upload}")
        assert b.processed_key.startswith(f"processed/p.d.b/{upload}")
        assert {a.processed_key, b.processed_key} <= set(store.objects)

    def test_audit_record_names_the_destination(self) -> None:
        """The audit record says which table the attempt was for."""
        store = MemoryStore()
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


class TestPipelineRawRows:
    """The warehouse is given every sheet's rows as NDJSON."""

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

    def _raw_pipeline(self, store: MemoryStore) -> Pipeline:
        """Pipeline over the two-sheet workbook.

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
        )

    def test_raw_json_loads_rows_from_every_sheet(self) -> None:
        """The warehouse gets NDJSON for all sheets and their total."""
        pipeline = self._raw_pipeline(MemoryStore())
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
        store = MemoryStore()
        pipeline = self._raw_pipeline(store)
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)
        data = pipeline.source.download.return_value

        result = pipeline.process(item)

        # Keyed by upload and by the NDJSON's own digest.
        ndjson = pipeline.warehouse.load.call_args.kwargs["data"]
        key = (
            f"processed/p.raw.signups+survey/ID1/{digest(data)}/v1/"
            f"{digest(ndjson)[:16]}/hww.xlsx.ndjson"
        )
        assert result.processed_key == key
        assert store.objects[key] == ndjson

    def test_raw_json_retry_after_failure_loads(self) -> None:
        """Identical NDJSON on retry: no processed/ conflict."""
        store = MemoryStore()
        pipeline = self._raw_pipeline(store)
        pipeline.warehouse.load.side_effect = [RuntimeError("boom"), True]
        item = SourceItem("hww.xlsx", "ID1", "v1", 10)

        first = pipeline.process(item)
        second = pipeline.process(item)

        assert first.outcome == Outcome.FAILED
        assert second.outcome == Outcome.LOADED, second.errors


CONTRACT = Contract(
    worksheets=[Worksheet("Events", [Column("name", "string")])]
)


def _book(rows: list[list[Any]], title: str = "Events") -> bytes:
    """Build a one-sheet workbook.

    Args:
        rows: The rows, headings first.
        title: The sheet's name.

    Returns:
        The .xlsx bytes.
    """
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _warehouse() -> Mock:
    """Build a warehouse that accepts every load.

    Returns:
        The mock warehouse.
    """
    warehouse = Mock(identity="p.d.t")
    warehouse.load.return_value = True
    return warehouse


def _raw(tables: dict[str, str]) -> RawJsonWarehouse:
    """Build a raw JSON warehouse without a real client.

    Args:
        tables: Sheet name to table name.

    Returns:
        The warehouse.
    """
    with patch("py_common.adapters.bigquery_raw.bigquery.Client"):
        return RawJsonWarehouse("p", "d", tables)


TWO_SHEETS = Contract(
    worksheets=[
        Worksheet("Events", [Column("id", "integer")]),
        Worksheet("Other", [Column("x", "integer")]),
    ]
)


class TestLockReleaseNeverRaises:
    """A failed lock release is logged; it never hides the run's result."""

    @pytest.fixture
    def store(self) -> tuple[GCSObjectStore, FakeBucket]:
        """Return a GCS store whose bucket refuses to delete locks.

        Returns:
            The store and its fake bucket.
        """
        bucket = FakeBucket()
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "store")
        store.bucket = bucket
        real_blob = bucket.blob

        def blob(key: str) -> Any:
            """Return the bucket's blob, unable to delete if it's a lock.

            Args:
                key: The object's name.

            Returns:
                The blob.
            """
            found = real_blob(key)
            if key.startswith("locks/"):

                def refuse(if_generation_match: Any = None) -> None:
                    """Fail the delete as a missing permission would.

                    Args:
                        if_generation_match: Ignored.

                    Raises:
                        Forbidden: Always.
                    """
                    raise Forbidden("no storage.objects.delete on locks/")

                found.delete = refuse  # type: ignore[method-assign]
            return found

        bucket.blob = blob  # type: ignore[method-assign]
        return store, bucket

    def test_release_failure_is_logged_not_raised(
        self,
        store: tuple[GCSObjectStore, FakeBucket],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A lock that can't be deleted is logged and left to expire.

        Args:
            store: The store and its bucket.
            caplog: Captures the error log.
        """
        gcs, bucket = store

        with caplog.at_level(logging.ERROR, logger="py_common.adapters.gcs"):
            with gcs.lock("locks/k"):
                pass

        assert "locks/k" in bucket.objects  # left to expire
        assert any(
            "Could not release lock locks/k" in r.getMessage()
            for r in caplog.records
        )

    def test_run_still_returns_its_results(
        self, store: tuple[GCSObjectStore, FakeBucket], tmp_path: Path
    ) -> None:
        """The run's results come back despite the failed release.

        Args:
            store: The store and its bucket.
            tmp_path: The landing folder.
        """
        gcs, _ = store
        (tmp_path / "a.xlsx").write_bytes(_book([["name"], ["x"]]))

        results = Pipeline(
            LocalSource(tmp_path), gcs, _warehouse(), CONTRACT
        ).run()

        assert [r.outcome for r in results] == [Outcome.LOADED]


class TestMarkerBeforeDownload:
    """An upload already loaded here isn't downloaded again."""

    @pytest.fixture
    def setup(self, tmp_path: Path) -> tuple[Pipeline, list[str]]:
        """Build a pipeline that records each download.

        Args:
            tmp_path: Folder for the landing folder.

        Returns:
            The pipeline and the list of names downloaded.
        """
        folder = tmp_path / "in"
        folder.mkdir()
        (folder / "a.xlsx").write_bytes(_book([["name"], ["x"]]))
        source = LocalSource(folder)
        downloads: list[str] = []
        real = source.download

        def download(item: Any) -> bytes:
            """Record the download, then do it.

            Args:
                item: The file to download.

            Returns:
                Its bytes.
            """
            downloads.append(item.name)
            return real(item)

        source.download = download  # type: ignore[method-assign]
        pipeline = Pipeline(source, MemoryStore(), _warehouse(), CONTRACT)
        return pipeline, downloads

    def test_second_run_skips_without_downloading(
        self, setup: tuple[Pipeline, list[str]]
    ) -> None:
        """The second run skips the file without downloading it.

        Args:
            setup: The pipeline and its download list.
        """
        pipeline, downloads = setup

        first = pipeline.run()
        second = pipeline.run()

        assert [r.outcome for r in first] == [Outcome.LOADED]
        assert [r.outcome for r in second] == [Outcome.SKIPPED]
        assert downloads == ["a.xlsx"]

    def test_unreachable_but_loaded_file_is_not_failed(
        self, setup: tuple[Pipeline, list[str]]
    ) -> None:
        """A loaded file that can't be downloaded now isn't an alert."""
        pipeline, _ = setup
        pipeline.run()

        def refuse(item: Any) -> bytes:
            """Fail the download as an unavailable source would.

            Args:
                item: The file asked for.

            Raises:
                ServiceError: Always.
            """
            raise ServiceError("503 from the source")

        pipeline.source.download = refuse  # type: ignore[method-assign]

        assert [r.outcome for r in pipeline.run()] == [Outcome.SKIPPED]


class TestPipelineConfigChecks:
    """Mismatched sheet mappings are refused when built."""

    def test_raw_sheet_without_a_table_is_refused(self) -> None:
        """Every sheet in the contract needs a raw table."""
        with pytest.raises(ConfigError, match="Other"):
            Pipeline(Mock(), Mock(), _raw({"Events": "t1"}), TWO_SHEETS)

    def test_raw_table_without_a_sheet_is_refused(self) -> None:
        """Every raw table needs a sheet in the contract."""
        with pytest.raises(ConfigError, match="Extra"):
            Pipeline(
                Mock(),
                Mock(),
                _raw({"Events": "t1", "Other": "t2", "Extra": "t3"}),
                TWO_SHEETS,
            )

    def test_matching_mapping_is_accepted(self) -> None:
        """A one-to-one sheet mapping builds a pipeline."""
        assert Pipeline(
            Mock(),
            Mock(),
            _raw({"Events": "t1", "Other": "t2"}),
            TWO_SHEETS,
        )


class TestOnlyParserErrorsQuarantine:
    """A sheet the parser rejects is the file's fault; nothing else is."""

    def test_memory_error_is_not_blamed_on_the_file(self) -> None:
        """Running out of memory is raised, not quarantined."""
        with patch("py_common.contract.read_sheet", side_effect=MemoryError):
            with pytest.raises(MemoryError):
                CONTRACT.read(_book([["name"], ["x"]]), "f.xlsx")

    def test_parser_error_quarantines(self) -> None:
        """A parser error is the file's fault, so it's a ContractError."""
        with patch(
            "py_common.contract.read_sheet", side_effect=ValueError("bad")
        ):
            with pytest.raises(ContractError, match="Cannot read worksheet"):
                CONTRACT.read(_book([["name"], ["x"]]), "f.xlsx")


ITEM = SourceItem("f.xlsx", "f.xlsx", "1", 1)


def _with_cell_value(data: bytes, old: bytes, new: bytes) -> bytes:
    """Rewrite one stored cell value inside the sheet XML.

    Excel stores numbers as doubles, so openpyxl can't write an
    integer above 2**53 exactly; the XML is edited instead, as a file
    from another tool could hold it.
    """
    import zipfile

    out = BytesIO()
    with (
        zipfile.ZipFile(BytesIO(data)) as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            body = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                body = body.replace(old, new)
            dst.writestr(info, body)
    return out.getvalue()


def _contract(mode: str = "quarantine") -> Contract:
    """Build a contract for an Events sheet of name and n.

    Args:
        mode: The sheet's on_mismatch setting.

    Returns:
        The contract.
    """
    return Contract(
        worksheets=[
            Worksheet(
                "Events",
                columns=[Column("name", "string"), Column("n", "integer")],
                on_mismatch=mode,  # type: ignore[arg-type]
            )
        ]
    )


def _pipeline(data: bytes, contract: Contract) -> tuple[Pipeline, dict]:
    """Build a pipeline over one upload, with an in-memory store.

    Args:
        data: The upload's bytes.
        contract: The contract to check it against.

    Returns:
        The pipeline, and the dict the store writes into.
    """
    objects: dict[str, bytes] = {}
    store = MagicMock()
    store.get.side_effect = objects.get
    store.put.side_effect = objects.__setitem__
    source = Mock()
    source.items.return_value = [ITEM]
    source.download.return_value = data
    warehouse = Mock(identity="p.d.t")
    warehouse.load.return_value = True
    return Pipeline(source, store, warehouse, contract), objects


class TestMalformedWorkbook:
    """A workbook that fails mid-read is quarantined, raw kept."""

    def test_lazy_parse_error_is_quarantined_with_raw_copy(self) -> None:
        """A cell openpyxl can't read mid-sheet quarantines the file."""
        bad = _with_cell_value(
            _book([["name", "n"], ["a", 1]]),
            b"<v>1</v>",
            b"<v>not-a-number</v>",
        )
        pipeline, objects = _pipeline(bad, _contract())

        result = pipeline.process(ITEM)

        assert result.outcome == Outcome.QUARANTINED, result.errors
        assert any(k.startswith("raw/") for k in objects)
        assert "Cannot read" in result.errors[0]
