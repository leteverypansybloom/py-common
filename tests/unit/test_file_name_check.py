"""H1: a contract can require the agreed file name (REVIEW_.md).

A feed is identified by its file name: the raw route's "latest
upload" and the typed route's deleted-row marking both work per
name. So an upload under another name (a copy saved as "WAS export
(1).xlsx", say) would become a second "latest" upload, and duplicate
the current rows downstream. Contract(file_name=...) quarantines it
instead, before it is parsed.
"""

import io
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from _memory_store import MemoryStore
from openpyxl import Workbook

from py_common.adapters.local import LocalSource
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ContractError
from py_common.model import Outcome
from py_common.pipeline import Pipeline

SHEETS = [Worksheet("Events", [Column("name", "string")])]
AGREED = "WAS export.xlsx"


def _book(rows: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Events"
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


GOOD = _book([["name"], ["x"]])


class TestContractFileName:
    def test_the_agreed_name_is_accepted(self) -> None:
        contract = Contract(worksheets=SHEETS, file_name=AGREED)

        assert contract.validate(GOOD, AGREED) == 1

    def test_another_name_is_rejected_naming_the_agreed_one(self) -> None:
        contract = Contract(worksheets=SHEETS, file_name=AGREED)

        with pytest.raises(ContractError, match="expected 'WAS export.xlsx'"):
            contract.validate(GOOD, "WAS export (1).xlsx")

    def test_the_match_is_exact_including_capitals(self) -> None:
        contract = Contract(worksheets=SHEETS, file_name=AGREED)

        with pytest.raises(ContractError, match="wrong file name"):
            contract.read(GOOD, "WAS Export.xlsx")

    def test_the_name_is_checked_before_the_file_is_parsed(self) -> None:
        contract = Contract(worksheets=SHEETS, file_name=AGREED)

        # Not a workbook at all: the name is what gets reported.
        with pytest.raises(ContractError, match="wrong file name"):
            contract.validate(b"not a workbook", "other.xlsx")

    def test_without_file_name_any_name_is_accepted(self) -> None:
        contract = Contract(worksheets=SHEETS)

        assert contract.validate(GOOD, "anything at all.xlsx") == 1

    def test_extract_does_not_check_the_name(self) -> None:
        # extract() reads without validating, as for every other rule.
        contract = Contract(worksheets=SHEETS, file_name=AGREED)

        sheets = contract.extract(GOOD, "other.xlsx")

        assert len(sheets["Events"].rows) == 1

    @pytest.mark.parametrize(
        "bad", ["", "   ", "was/WAS export.xlsx", "was\\WAS export.xlsx"]
    )
    def test_file_name_must_be_a_bare_file_name(self, bad: str) -> None:
        with pytest.raises(ValueError, match="file_name"):
            Contract(worksheets=SHEETS, file_name=bad)


class TestPipelineQuarantinesOtherNames:
    @pytest.fixture
    def folder(self, tmp_path: Path) -> Path:
        (tmp_path / AGREED).write_bytes(GOOD)
        (tmp_path / "WAS export (1).xlsx").write_bytes(GOOD)
        return tmp_path

    @staticmethod
    def _pipeline(folder: Path, store: MemoryStore, warehouse: Mock) -> Any:
        contract = Contract(worksheets=SHEETS, file_name=AGREED)
        return Pipeline(LocalSource(folder), store, warehouse, contract)

    @staticmethod
    def _warehouse() -> Mock:
        warehouse = Mock(identity="p.d.t")
        warehouse.load.return_value = True
        return warehouse

    def test_a_misnamed_upload_is_quarantined_and_not_loaded(
        self, folder: Path
    ) -> None:
        store = MemoryStore()
        warehouse = self._warehouse()

        results = {
            r.item.name: r
            for r in self._pipeline(folder, store, warehouse).run()
        }

        assert results[AGREED].outcome == Outcome.LOADED
        stray = results["WAS export (1).xlsx"]
        assert stray.outcome == Outcome.QUARANTINED
        assert "expected 'WAS export.xlsx'" in stray.errors[0]
        assert any(
            key.startswith("quarantine/WAS export (1).xlsx/")
            for key in store.objects
        )
        assert warehouse.load.call_count == 1

    def test_it_is_flagged_again_on_every_run_until_deleted(
        self, folder: Path
    ) -> None:
        store = MemoryStore()
        pipeline = self._pipeline(folder, store, self._warehouse())
        pipeline.run()

        again = {r.item.name: r.outcome for r in pipeline.run()}

        assert again == {
            AGREED: Outcome.SKIPPED,
            "WAS export (1).xlsx": Outcome.QUARANTINED,
        }

        (folder / "WAS export (1).xlsx").unlink()
        assert [r.outcome for r in pipeline.run()] == [Outcome.SKIPPED]
