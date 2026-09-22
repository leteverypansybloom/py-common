"""Unit tests for Pipeline.process() outcome handling.

Uses mocked Source/ObjectStore/Warehouse/Contract throughout so these
tests exercise only process()'s control flow, not the local adapters
(LocalObjectStore/LocalSource have a separate, already-tracked
Windows path issue unrelated to what's being tested here).
"""

from io import BytesIO
from unittest.mock import Mock

import pytest
from openpyxl import Workbook

from py_common.contract import Column, Contract, Worksheet
from py_common.errors import (
    ContractError,
    IndeterminateCommitError,
    ServiceError,
    ValidationError,
    VersionMismatch,
)
from py_common.model import Outcome, SourceItem
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
