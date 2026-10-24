"""Delivery behaviour decided for 0.2.0 (see CHANGELOG).

- Every new upload of a file loads, even if its bytes repeat an
  earlier upload (a restore); the same upload never loads twice.
- An empty worksheet is rejected unless the contract allows it.
- "Already loaded" is tracked per destination table, so adding a
  table to a pipeline only loads the new table.
"""

import io
import json
from typing import Any
from unittest.mock import patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from openpyxl import Workbook

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ContractError
from py_common.model import SourceItem

ITEM = SourceItem("f.xlsx", "landing/f.xlsx", "7", 10)


def _book(rows: list[list[Any]]) -> bytes:
    """Build an Events workbook.

    Args:
        rows: The rows, headings first.

    Returns:
        The .xlsx bytes.
    """
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Events"
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class TestEmptySheets:
    """A sheet with headings but no rows is rejected by default."""

    @pytest.mark.parametrize("mode", ["quarantine", "warn"])
    def test_empty_sheet_is_rejected_in_either_mode(self, mode: str) -> None:
        """A sheet with no rows is rejected, strict or "warn".

        Args:
            mode: The sheet's on_mismatch setting.
        """
        contract = Contract(
            worksheets=[
                Worksheet(
                    "Events",
                    [Column("name", "string")],
                    on_mismatch=mode,  # type: ignore[arg-type]
                )
            ]
        )

        with pytest.raises(ContractError, match="no data rows"):
            contract.read(_book([["name"]]), "f.xlsx", [])

    def test_allow_empty_accepts_it(self) -> None:
        """allow_empty=True accepts a sheet with no rows."""
        contract = Contract(
            worksheets=[
                Worksheet(
                    "Events", [Column("name", "string")], allow_empty=True
                )
            ]
        )

        sheets = contract.read(_book([["name"]]), "f.xlsx")

        assert sheets["Events"].rows == []


def _ndjson(*sheets: str) -> bytes:
    """Build raw NDJSON with one row per sheet name given.

    Args:
        *sheets: The sheet of each row, in order.

    Returns:
        The NDJSON bytes.
    """
    return b"".join(
        json.dumps(
            {
                "source_file": "f.xlsx",
                "source_generation": "7",
                "sheet_name": sheet,
                "row_number": n,
                "checksum": "c",
                "payload": {},
            }
        ).encode()
        + b"\n"
        for n, sheet in enumerate(sheets, start=2)
    )


def _raw(client: FakeBigQueryClient, tables: dict[str, str]) -> Any:
    """Build a raw JSON warehouse on the fake client.

    Args:
        client: The fake client.
        tables: Sheet name to table name.

    Returns:
        The warehouse.
    """
    with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
        cls.return_value = client
        return RawJsonWarehouse("p", "d", tables)


class TestPerTableTracking:
    """Adding a table to a pipeline only loads the new table."""

    def test_adding_a_table_loads_only_that_table(self) -> None:
        """A table added later loads; the existing one isn't loaded again."""
        client = FakeBigQueryClient()
        before = _raw(client, {"S": "t1"})
        before.load("k", "c" * 64, _ndjson("S"), 1, ITEM)

        after = _raw(client, {"S": "t1", "T": "t2"})
        assert after.load("k", "c" * 64, _ndjson("S", "T"), 2, ITEM)

        assert client.rows["p.d.t1"] == 1  # not loaded twice
        assert client.rows["p.d.t2"] == 1
        assert [r["destination"] for r in client.audit] == [
            "p.d.t1",
            "p.d.t2",
        ]

    def test_every_table_already_loaded_returns_false(self) -> None:
        """load() returns False when every table already has the upload."""
        client = FakeBigQueryClient()
        warehouse = _raw(client, {"S": "t1", "T": "t2"})
        warehouse.load("k", "c" * 64, _ndjson("S", "T"), 2, ITEM)

        assert not warehouse.load("k", "c" * 64, _ndjson("S", "T"), 2, ITEM)
        assert client.rows["p.d.t1"] == 1
        assert client.rows["p.d.t2"] == 1

    def test_one_audit_row_per_table_with_its_row_count(self) -> None:
        """Each table gets its own audit row and row count."""
        client = FakeBigQueryClient()
        _raw(client, {"S": "t1", "T": "t2"}).load(
            "k", "c" * 64, _ndjson("S", "S", "T"), 3, ITEM
        )

        assert [(r["destination"], r["row_count"]) for r in client.audit] == [
            ("p.d.t1", 2),
            ("p.d.t2", 1),
        ]
