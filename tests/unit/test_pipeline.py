"""Unit tests for Pipeline outcome handling and storage keys.

Uses mocked Source/Warehouse/Contract and either a Mock or a small
in-memory ObjectStore (InMemoryStore, which enforces the real put()
conflict rule), not the local adapters (LocalObjectStore/LocalSource
have a separate, already-tracked Windows path issue unrelated to
what's being tested here).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import Mock

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
        return Pipeline(
            source=source, store=store, warehouse=Mock(), contract=contract
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
            f"processed/ID1/{digest(data)}/events.xlsx.parquet"
            in store.objects
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
