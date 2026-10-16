"""Behaviour chosen for H01, H02 and H04 (see CHANGELOG 0.2.0).

- Every new upload of a file loads, even if its bytes repeat an
  earlier upload (a restore); the same upload never loads twice.
- Typed tables can mark rows that a file no longer contains as
  deleted, instead of leaving them looking current.
- An empty worksheet is rejected unless the contract allows it.
- "Already loaded" is tracked per destination table, so adding a
  table to a raw pipeline only loads the new table.
"""

import io
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from openpyxl import Workbook

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ContractError
from py_common.model import SourceItem

ITEM = SourceItem("f.xlsx", "landing/f.xlsx", "7", 10)


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


# ------------------------------------------------------------------ H02


class TestEmptySheets:
    """A sheet with headings but no rows is rejected by default."""

    @pytest.mark.parametrize("mode", ["quarantine", "warn"])
    def test_empty_sheet_is_rejected_in_either_mode(self, mode: str) -> None:
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
        contract = Contract(
            worksheets=[
                Worksheet(
                    "Events", [Column("name", "string")], allow_empty=True
                )
            ]
        )

        sheets = contract.read(_book([["name"]]), "f.xlsx")

        assert sheets["Events"].rows == []


# ------------------------------------------------------------------ H04


def _ndjson(*sheets: str) -> bytes:
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
    with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
        cls.return_value = client
        return RawJsonWarehouse("p", "d", tables)


class TestPerTableTracking:
    """Adding a table to a raw pipeline only loads the new table."""

    def test_adding_a_table_loads_only_that_table(self) -> None:
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
        client = FakeBigQueryClient()
        warehouse = _raw(client, {"S": "t1", "T": "t2"})
        warehouse.load("k", "c" * 64, _ndjson("S", "T"), 2, ITEM)

        assert not warehouse.load("k", "c" * 64, _ndjson("S", "T"), 2, ITEM)
        assert client.rows["p.d.t1"] == 1
        assert client.rows["p.d.t2"] == 1

    def test_one_audit_row_per_table_with_its_row_count(self) -> None:
        client = FakeBigQueryClient()
        _raw(client, {"S": "t1", "T": "t2"}).load(
            "k", "c" * 64, _ndjson("S", "S", "T"), 3, ITEM
        )

        assert [(r["destination"], r["row_count"]) for r in client.audit] == [
            ("p.d.t1", 2),
            ("p.d.t2", 1),
        ]


# ------------------------------------------------------------------ H01


def _typed(mark_deleted: bool, final: Any = None) -> tuple[Any, MagicMock]:
    """Typed warehouse keyed on id, over a mocked client."""
    client = MagicMock()
    with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = BigQueryWarehouse(
            "p", "d", "t", key_columns=["id"], mark_deleted_rows=mark_deleted
        )
    staging = SimpleNamespace(
        schema=[
            SimpleNamespace(name="id", field_type="INTEGER"),
            SimpleNamespace(name="n", field_type="INTEGER"),
        ]
    )

    def get_table(table_id: str) -> Any:
        if table_id == "p.d.t":
            if final is None:
                from google.api_core.exceptions import NotFound

                raise NotFound(table_id)
            return final
        return staging

    client.get_table.side_effect = get_table
    client.load_table_from_file.return_value = Mock(job_id="l", output_rows=1)

    def query(sql: str, **_: Any) -> Mock:
        job = Mock(job_id="q")
        job.result.return_value = (
            [("LOADED",)] if "BEGIN TRANSACTION" in sql else []
        )
        return job

    client.query.side_effect = query
    return warehouse, client


def _commit(client: MagicMock) -> tuple[str, dict[str, Any]]:
    [call] = [
        c
        for c in client.query.call_args_list
        if "BEGIN TRANSACTION" in c.args[0]
    ]
    params = {
        p.name: p.value for p in call.kwargs["job_config"].query_parameters
    }
    return call.args[0], params


class TestMarkDeletedRows:
    """mark_deleted_rows: rows a file no longer has get _deleted_at."""

    def test_rows_missing_from_this_file_are_marked(self) -> None:
        warehouse, client = _typed(True)
        warehouse.load("k", "c" * 64, b"pq", 1, ITEM)

        sql, params = _commit(client)
        assert (
            "WHEN NOT MATCHED BY SOURCE AND T.`_source_file` = @source_file "
            "AND T.`_deleted_at` IS NULL "
            "THEN UPDATE SET T.`_deleted_at` = @timestamp"
        ) in sql
        assert params["source_file"] == "landing/f.xlsx"

    def test_a_row_that_comes_back_is_undeleted(self) -> None:
        warehouse, client = _typed(True)
        warehouse.load("k", "c" * 64, b"pq", 1, ITEM)

        sql, _ = _commit(client)
        matched = sql[
            sql.index("WHEN MATCHED") : sql.index("WHEN NOT MATCHED")
        ]
        assert "T.`_deleted_at` = NULL" in matched
        assert "T.`_source_file` = @source_file" in matched

    def test_new_rows_record_their_source_file(self) -> None:
        warehouse, client = _typed(True)
        warehouse.load("k", "c" * 64, b"pq", 1, ITEM)

        sql, _ = _commit(client)
        insert = sql[sql.index("WHEN NOT MATCHED THEN") :]
        assert "`_deleted_at`, `_source_file`" in insert
        assert "NULL, @source_file" in insert

    def test_final_table_is_created_with_the_columns(self) -> None:
        warehouse, client = _typed(True)
        warehouse.load("k", "c" * 64, b"pq", 1, ITEM)

        [table] = [
            c.args[0]
            for c in client.create_table.call_args_list
            if c.args[0].table_id == "t"
        ]
        assert [(f.name, f.field_type) for f in table.schema][-3:] == [
            ("_loaded_at", "TIMESTAMP"),
            ("_deleted_at", "TIMESTAMP"),
            ("_source_file", "STRING"),
        ]

    def test_existing_table_gains_the_columns(self) -> None:
        final = SimpleNamespace(
            schema=[
                SimpleNamespace(name="id", field_type="INTEGER"),
                SimpleNamespace(name="n", field_type="INTEGER"),
                SimpleNamespace(name="_loaded_at", field_type="TIMESTAMP"),
            ]
        )
        warehouse, client = _typed(True, final)
        warehouse.load("k", "c" * 64, b"pq", 1, ITEM)

        alters = [
            c.args[0]
            for c in client.query.call_args_list
            if "ALTER TABLE" in c.args[0]
        ]
        assert alters == [
            "ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS `_deleted_at` "
            "TIMESTAMP",
            "ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS `_source_file` "
            "STRING",
        ]

    def test_off_by_default(self) -> None:
        warehouse, client = _typed(False)
        warehouse.load("k", "c" * 64, b"pq", 1, ITEM)

        sql, params = _commit(client)
        assert "NOT MATCHED BY SOURCE" not in sql
        assert "source_file" not in params

    def test_needs_key_columns(self) -> None:
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            with pytest.raises(ValueError, match="key_columns"):
                BigQueryWarehouse("p", "d", "t", mark_deleted_rows=True)

    @pytest.mark.parametrize("name", ["_deleted_at", "_source_file"])
    def test_metadata_names_are_reserved(self, name: str) -> None:
        warehouse, client = _typed(True)
        client.get_table.side_effect = None
        client.get_table.return_value = SimpleNamespace(
            schema=[
                SimpleNamespace(name="id", field_type="INTEGER"),
                SimpleNamespace(name=name, field_type="STRING"),
            ]
        )

        with pytest.raises(ContractError, match=name):
            warehouse.load("k", "c" * 64, b"pq", 1, ITEM)
