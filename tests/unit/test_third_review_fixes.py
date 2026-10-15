"""Fixes from the third review of 0.2.0 (N01–N03, M13, M21)."""

import ast
import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from openpyxl import Workbook

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.config import load_config
from py_common.contract import (
    Column,
    Contract,
    SheetRows,
    Worksheet,
    formulas_without_values,
)
from py_common.errors import ConfigError, ContractError, ValidationError
from py_common.model import Outcome, SourceItem
from py_common.parquet import to_parquet
from py_common.pipeline import Pipeline

ITEM = SourceItem("f.xlsx", "f.xlsx", "1", 1)
SECRET = "jo.bloggs@example.org"


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


class TestReservedColumn:
    """N02: a source `_loaded_at` column quarantines, not retries."""

    def _warehouse(self) -> BigQueryWarehouse:
        client = MagicMock()
        with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
            cls.return_value = client
            warehouse = BigQueryWarehouse("p", "d", "t")
        from types import SimpleNamespace

        client.get_table.return_value = SimpleNamespace(
            schema=[
                SimpleNamespace(name="_loaded_at", field_type="STRING"),
            ]
        )
        client.load_table_from_file.return_value = Mock(
            job_id="l", output_rows=1
        )
        return warehouse

    def test_raises_contract_error(self) -> None:
        with pytest.raises(ContractError, match="_loaded_at"):
            self._warehouse().load("k", "c" * 64, b"pq", 1, ITEM)

    def test_pipeline_quarantines_the_file(self) -> None:
        objects: dict[str, bytes] = {}
        store = MagicMock()
        store.get.side_effect = objects.get
        store.put.side_effect = objects.__setitem__
        source = Mock()
        source.download.return_value = _book([["_loaded_at"], ["x"]])
        contract = Contract(
            worksheets=[Worksheet("Events", [Column("_loaded_at", "string")])]
        )
        pipeline = Pipeline(source, store, self._warehouse(), contract)

        result = pipeline.process(ITEM)

        assert result.outcome == Outcome.QUARANTINED
        assert any(k.startswith("quarantine/") for k in objects)


def _with_sheet_target(data: bytes, target: str) -> bytes:
    """Rewrite the workbook's relationship target for sheet1."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            body = src.read(info)
            if info.filename == "xl/_rels/workbook.xml.rels":
                body = body.replace(
                    b'Target="/xl/worksheets/sheet1.xml"',
                    f'Target="{target}"'.encode(),
                )
            dst.writestr(info, body)
    return out.getvalue()


class TestFormulaPaths:
    """N03: every spelling of the sheet's part path is scanned."""

    @pytest.mark.parametrize(
        "target",
        [
            "/xl/worksheets/sheet1.xml",
            "worksheets/sheet1.xml",
            "./worksheets/sheet1.xml",
            "worksheets/../worksheets/sheet1.xml",
        ],
    )
    def test_uncached_formula_is_found(self, target: str) -> None:
        data = _with_sheet_target(_book([["a", "b"], ["x", "=1+1"]]), target)

        assert formulas_without_values(data, ["Events"]) == {
            "Events": [(2, "B2")]
        }

    def test_unresolvable_sheet_part_is_an_error(self) -> None:
        data = _with_sheet_target(
            _book([["a"], ["x"]]), "../../outside/sheet1.xml"
        )

        with pytest.raises(ContractError, match="Events"):
            formulas_without_values(data, ["Events"])


class TestNoValuesInMessages:
    """M13: errors and warnings name rows and columns, not values."""

    def test_type_error_has_no_value(self) -> None:
        contract = Contract(
            worksheets=[Worksheet("Events", [Column("n", "integer")])]
        )
        with pytest.raises(ValidationError) as info:
            contract.read(_book([["n"], [SECRET]]), "f.xlsx")
        assert SECRET not in str(info.value)

    def test_duplicate_key_message_has_no_value(self) -> None:
        contract = Contract(
            key_columns=["id"],
            worksheets=[Worksheet("Events", [Column("id", "string")])],
        )
        with pytest.raises(ValidationError, match="Row 3") as info:
            contract.read(_book([["id"], [SECRET], [SECRET]]), "f.xlsx")
        assert SECRET not in str(info.value)

    def test_conversion_warning_has_no_value(self) -> None:
        warnings: list[str] = []
        sheet = Worksheet(
            "Events", [Column("n", "integer")], on_mismatch="warn"
        )
        to_parquet(SheetRows(["n"], [(2, (SECRET,))]), sheet, "f", warnings)
        assert warnings and SECRET not in warnings[0]


class TestConfigFile:
    """M21: UTF-8, and the top level must be a mapping."""

    def test_list_root_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(path)

    def test_utf8_is_read_on_any_platform(self, tmp_path: Path) -> None:
        path = tmp_path / "c.yaml"
        path.write_bytes("name: Iechyd Cyhoeddus – Cymru\n".encode("utf-8"))
        assert load_config(path) == {"name": "Iechyd Cyhoeddus – Cymru"}


def test_smoke_script_enables_json_logging() -> None:
    """N01: the alert test recipe needs JSON log lines."""
    tree = ast.parse(Path("scripts/demo.py").read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "configure_json_logging" in calls
