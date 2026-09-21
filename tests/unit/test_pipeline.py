"""Unit tests for Pipeline.process() outcome handling.

Uses mocked Source/ObjectStore/Warehouse/Contract throughout so these
tests exercise only process()'s control flow, not the local adapters
(LocalObjectStore/LocalSource have a separate, already-tracked
Windows path issue unrelated to what's being tested here).
"""

from unittest.mock import Mock

import pytest

from py_common.errors import ContractError, ValidationError
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
