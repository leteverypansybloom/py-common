"""H2: converted copies never clash (REVIEW_.md).

The store is write-once: writing different bytes to an existing key
raises ObjectStoreConflict. The typed route used to key its Parquet
copy by the file's checksum alone, so the same file converted again
with different results (a contract fix, a pyarrow upgrade) hit that
conflict, and failed on every retry. The usual way in: fix the
contract, then upload the same file again so it loads with the fix.

Processed copies are now keyed by the upload (version) and by a
digest of the converted bytes themselves, so whatever changes the
output gets a new key, and identical output reuses its key.
"""

import io
import json
from dataclasses import replace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from _memory_store import MemoryStore
from openpyxl import Workbook

import py_common
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ServiceError
from py_common.model import Outcome, SourceItem, digest
from py_common.pipeline import Pipeline

#: Excludes "extra"; the fixed contract below loads it too.
OLD = Contract(
    worksheets=[
        Worksheet("Events", [Column("name", "string")], excluded=["extra"])
    ]
)
FIXED = Contract(
    worksheets=[
        Worksheet(
            "Events", [Column("name", "string"), Column("extra", "string")]
        )
    ]
)


def _book() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Events"
    ws.append(["name", "extra"])
    ws.append(["x", "y"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


DATA = _book()


class StaticSource:
    """One file, whose version the test sets."""

    def __init__(self, version: str) -> None:
        self.version = version

    def items(self) -> list[SourceItem]:
        return [SourceItem("events.xlsx", "was/events.xlsx", self.version, 10)]

    def download(self, item: SourceItem) -> bytes:
        return DATA


def _pipeline(
    store: MemoryStore,
    contract: Contract,
    version: str = "1",
    output_format: Any = "parquet",
) -> Pipeline:
    warehouse = Mock(identity="p.d.t", spec=["identity", "load"])
    warehouse.load.return_value = True
    if output_format == "raw_json":
        warehouse.tables = {"Events": "events_raw"}
    return Pipeline(
        StaticSource(version),
        store,
        warehouse,
        contract,
        output_format=output_format,
    )


def _processed(store: MemoryStore) -> list[str]:
    return sorted(k for k in store.objects if k.startswith("processed/"))


class TestTypedRoute:
    def test_contract_fix_then_same_file_again_loads(self) -> None:
        store = MemoryStore()
        first = _pipeline(store, OLD, version="1").run()

        again = _pipeline(store, FIXED, version="2").run()

        assert first[0].outcome == Outcome.LOADED
        assert again[0].outcome == Outcome.LOADED, again[0].errors
        assert len(_processed(store)) == 2

    def test_retry_after_a_contract_change_loads(self) -> None:
        # The copy is stored before the load; the load fails; the
        # contract changes before the retry of the same upload.
        store = MemoryStore()
        failing = _pipeline(store, OLD)
        failing.warehouse.load.side_effect = ServiceError("down")
        assert failing.run()[0].outcome == Outcome.FAILED

        retry = _pipeline(store, FIXED).run()

        assert retry[0].outcome == Outcome.LOADED, retry[0].errors

    def test_a_converter_change_gets_its_own_copy(self) -> None:
        # E.g. a pyarrow upgrade: the Parquet footer names the
        # library version that wrote it, so the bytes differ.
        store = MemoryStore()
        failing = _pipeline(store, OLD)
        failing.warehouse.load.side_effect = ServiceError("down")
        failing.run()

        with patch("py_common.pipeline.to_parquet", return_value=b"PAR1x"):
            retry = _pipeline(store, OLD).run()

        assert retry[0].outcome == Outcome.LOADED, retry[0].errors
        assert len(_processed(store)) == 2

    def test_a_plain_retry_reuses_its_copy(self) -> None:
        store = MemoryStore()
        failing = _pipeline(store, OLD)
        failing.warehouse.load.side_effect = ServiceError("down")
        failing.run()

        retry = _pipeline(store, OLD).run()

        assert retry[0].outcome == Outcome.LOADED
        assert len(_processed(store)) == 1

    def test_key_names_upload_and_content(self) -> None:
        store = MemoryStore()
        pipeline = _pipeline(store, OLD, version="1601")

        result = pipeline.run()[0]

        parquet = pipeline.warehouse.load.call_args.kwargs["data"]
        key = (
            f"processed/p.d.t/was/events.xlsx/{digest(DATA)}/1601/"
            f"{digest(parquet)[:16]}/events.xlsx.parquet"
        )
        assert _processed(store) == [key]
        assert store.objects[key] == parquet
        assert result.processed_key == key


class TestRawRoute:
    def test_retry_after_a_contract_change_loads(self) -> None:
        store = MemoryStore()
        failing = _pipeline(store, OLD, output_format="raw_json")
        failing.warehouse.load.side_effect = ServiceError("down")
        assert failing.run()[0].outcome == Outcome.FAILED

        retry = _pipeline(store, FIXED, output_format="raw_json").run()

        assert retry[0].outcome == Outcome.LOADED, retry[0].errors

    def test_key_names_upload_and_content(self) -> None:
        store = MemoryStore()
        pipeline = _pipeline(store, OLD, output_format="raw_json")

        pipeline.run()

        ndjson = pipeline.warehouse.load.call_args.kwargs["data"]
        assert _processed(store) == [
            f"processed/p.d.t/was/events.xlsx/{digest(DATA)}/1/"
            f"{digest(ndjson)[:16]}/events.xlsx.ndjson"
        ]


class TestAuditRecord:
    @staticmethod
    def _record(store: MemoryStore) -> dict[str, Any]:
        (key,) = [k for k in store.objects if k.startswith("audit/records/")]
        return dict(json.loads(store.objects[key]))

    def test_names_the_copy_contract_and_library_version(self) -> None:
        store = MemoryStore()
        result = _pipeline(store, OLD).run()[0]

        record = self._record(store)

        assert record["processed_key"] == result.processed_key
        assert record["contract_fingerprint"] == OLD.fingerprint()
        assert record["py_common_version"] == py_common.__version__

    def test_quarantined_file_has_no_processed_copy(self) -> None:
        store = MemoryStore()
        named = replace(OLD, file_name="other.xlsx")

        result = _pipeline(store, named).run()[0]

        assert result.outcome == Outcome.QUARANTINED
        assert self._record(store)["processed_key"] == ""


class TestContractFingerprint:
    def test_equal_contracts_have_the_same_fingerprint(self) -> None:
        assert OLD.fingerprint() == replace(OLD).fingerprint()

    @pytest.mark.parametrize(
        "change",
        [
            {"key_columns": ["name"]},
            {"file_name": "events.xlsx"},
            {"max_file_bytes": 1024},
        ],
    )
    def test_any_change_changes_it(self, change: dict[str, Any]) -> None:
        assert replace(OLD, **change).fingerprint() != OLD.fingerprint()

    def test_a_worksheet_change_changes_it(self) -> None:
        assert FIXED.fingerprint() != OLD.fingerprint()

    def test_it_is_short_hex(self) -> None:
        assert len(OLD.fingerprint()) == 16
        int(OLD.fingerprint(), 16)
