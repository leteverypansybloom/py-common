"""Contract-typed Parquet conversion (review H5).

The Parquet schema comes from Column.data_type, never from pandas or
pyarrow inference, so a "warn" sheet with mixed values still
converts, and an all-empty column keeps its declared type month to
month.
"""

from datetime import date, datetime
from io import BytesIO
from typing import Any
from unittest.mock import Mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from openpyxl import Workbook

from py_common.contract import Column, Contract, SheetRows, Worksheet
from py_common.model import Outcome, SourceItem
from py_common.parquet import ARROW_TYPES, to_parquet
from py_common.pipeline import Pipeline
import io
from unittest.mock import MagicMock
from py_common.errors import ValidationError


def _read(data: bytes) -> pa.Table:
    return pq.read_table(BytesIO(data))


def _sheet(**kwargs: Any) -> Worksheet:
    return Worksheet(
        "Events",
        columns=[
            Column("name", "string"),
            Column("count", "integer", nullable=True),
            Column("score", "float", nullable=True),
            Column("held_on", "date", nullable=True),
        ],
        **kwargs,
    )


def _rows(*rows: tuple[Any, ...]) -> SheetRows:
    return SheetRows(
        columns=["name", "count", "score", "held_on"],
        rows=[(n, row) for n, row in enumerate(rows, start=2)],
    )


class TestSchema:
    """Types come from the contract."""

    def test_schema_matches_declared_types(self) -> None:
        data = to_parquet(
            _rows(("a", 1, 2.5, datetime(2026, 1, 2, 3, 4))), _sheet()
        )

        schema = _read(data).schema
        assert schema.field("name").type == pa.string()
        assert schema.field("count").type == pa.int64()
        assert schema.field("score").type == pa.float64()
        assert schema.field("held_on").type == pa.timestamp("us")
        assert set(ARROW_TYPES) == {"string", "integer", "float", "date"}

    def test_all_empty_column_keeps_its_declared_type(self) -> None:
        """Not inferred as null type, so BigQuery's type is stable."""
        data = to_parquet(_rows(("a", None, None, None)), _sheet())

        table = _read(data)
        assert table.schema.field("count").type == pa.int64()
        assert table.column("count").to_pylist() == [None]

    def test_untyped_all_except_column_is_string(self) -> None:
        sheet = Worksheet(
            "Events",
            columns=[Column("count", "integer")],
            column_selection="all_except",
        )
        rows = SheetRows(["count", "notes"], [(2, (1, 42)), (3, (2, "x"))])

        table = _read(to_parquet(rows, sheet))

        assert table.schema.field("notes").type == pa.string()
        assert table.column("notes").to_pylist() == ["42", "x"]

    def test_date_only_value_becomes_midnight(self) -> None:
        table = _read(
            to_parquet(_rows(("a", 1, 1.0, date(2026, 3, 1))), _sheet())
        )

        assert table.column("held_on").to_pylist() == [datetime(2026, 3, 1)]


class TestCoercion:
    """In "warn" mode, values that don't fit become NULL with a warning.

    In "quarantine" mode they raise (TestInt64Range, below).
    """

    def test_int_and_na_in_one_column_does_not_raise(self) -> None:
        """Regression: pandas inference raised ArrowInvalid here."""
        warnings: list[str] = []

        data = to_parquet(
            _rows(("a", 1, None, None), ("b", "NA", None, None)),
            _sheet(on_mismatch="warn"),
            source_name="f.xlsx",
            warnings=warnings,
        )

        assert _read(data).column("count").to_pylist() == [1, None]
        assert warnings == [
            "f.xlsx [Events]: 'count' row 3 is not integer; loaded as NULL"
        ]

    def test_lossless_conversions_are_kept(self) -> None:
        """Whole floats to integers, ints to floats, numbers as text."""
        warnings: list[str] = []
        rows = SheetRows(
            ["name", "count", "score", "held_on"],
            [(2, (123, 4.0, 3, None))],
        )

        table = _read(
            to_parquet(rows, _sheet(on_mismatch="warn"), warnings=warnings)
        )

        assert table.to_pylist() == [
            {"name": "123", "count": 4, "score": 3.0, "held_on": None}
        ]
        assert warnings == []

    @pytest.mark.parametrize(
        ("column", "value"),
        [("count", 2.5), ("count", True), ("score", "high"), ("held_on", 7)],
    )
    def test_unconvertible_values_become_null(
        self, column: str, value: Any
    ) -> None:
        row = {"name": "a", "count": None, "score": None, "held_on": None}
        row[column] = value
        warnings: list[str] = []

        table = _read(
            to_parquet(
                _rows(tuple(row.values())),
                _sheet(on_mismatch="warn"),
                warnings=warnings,
            )
        )

        assert table.column(column).to_pylist() == [None]
        assert len(warnings) == 1 and f"'{column}'" in warnings[0]

    def test_many_bad_values_are_summarised(self) -> None:
        warnings: list[str] = []
        rows = _rows(*[("a", "NA", None, None)] * 7)

        to_parquet(rows, _sheet(on_mismatch="warn"), warnings=warnings)

        assert len(warnings) == 1
        assert (
            "7 values" in warnings[0] and "rows 2, 3, 4, 5, 6" in warnings[0]
        )


def test_pipeline_loads_warn_sheet_with_mixed_column() -> None:
    """End to end: the file loads, the bad value is NULL and warned."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["name", "count"])
    ws.append(["a", 1])
    ws.append(["b", "NA"])
    buffer = BytesIO()
    wb.save(buffer)
    contract = Contract(
        worksheets=[
            Worksheet(
                "Events",
                columns=[Column("name", "string"), Column("count", "integer")],
                on_mismatch="warn",
            )
        ]
    )
    source = Mock()
    source.download.return_value = buffer.getvalue()
    store = Mock()
    store.get.return_value = None
    warehouse = Mock()
    warehouse.identity = "p.d.events"
    warehouse.load.return_value = True
    pipeline = Pipeline(source, store, warehouse, contract)

    result = pipeline.process(SourceItem("e.xlsx", "e.xlsx", "1", 1))

    assert result.outcome == Outcome.LOADED, result.errors
    loaded = _read(warehouse.load.call_args.kwargs["data"])
    assert loaded.column("count").to_pylist() == [1, None]
    assert any("loaded as NULL" in w for w in result.warnings)


ITEM = SourceItem("f.xlsx", "f.xlsx", "1", 1)


def _book(rows: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _with_cell_value(data: bytes, old: bytes, new: bytes) -> bytes:
    """Rewrite one stored cell value inside the sheet XML.

    Excel stores numbers as doubles, so openpyxl can't write an
    integer above 2**53 exactly; the XML is edited instead, as a file
    from another tool could hold it.
    """
    import zipfile

    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            body = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                body = body.replace(old, new)
            dst.writestr(info, body)
    return out.getvalue()


def _contract(mode: str = "quarantine") -> Contract:
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


class TestInt64Range:
    """H07: an integer BigQuery can't hold is never loaded as NULL."""

    def test_quarantine_mode_rejects_out_of_range_integer(self) -> None:
        data = _with_cell_value(
            _book([["name", "n"], ["a", 1]]),
            b"<v>1</v>",
            f"<v>{2**63}</v>".encode(),
        )

        with pytest.raises(ValidationError, match="INT64"):
            _contract().read(data, "f.xlsx")

    def test_warn_mode_warns(self) -> None:
        warnings: list[str] = []
        data = _with_cell_value(
            _book([["name", "n"], ["a", 1]]),
            b"<v>1</v>",
            f"<v>{-(2**63) - 1}</v>".encode(),
        )
        _contract("warn").read(data, "f.xlsx", warnings)
        assert any("INT64" in w for w in warnings)

    def test_conversion_in_quarantine_mode_raises_not_nulls(self) -> None:
        sheet = SheetRows(["name", "n"], [(2, ("a", "NA"))])

        with pytest.raises(ValidationError, match="'n' row 2"):
            to_parquet(sheet, _contract().worksheets[0], "f.xlsx", [])

    def test_pipeline_quarantines_conversion_failure(self) -> None:
        contract = _contract()
        pipeline, objects = _pipeline(
            _book([["name", "n"], ["a", 1]]), contract
        )
        pipeline._sheets_to_parquet = Mock(  # type: ignore[method-assign]
            side_effect=ValidationError("bad value")
        )

        result = pipeline.process(ITEM)

        assert result.outcome == Outcome.QUARANTINED
        assert any(k.startswith("quarantine/") for k in objects)
