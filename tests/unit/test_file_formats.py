"""CSV, JSON and JSON Lines files (Contract.file_format).

Covers the readers (read_csv, read_json, read_jsonl), the contract's
checks on each format, the settings each format allows, LocalSource's
extensions, Pipeline's extension check and a load end to end.
"""

import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from _memory_store import MemoryStore

from py_common.adapters.local import LocalSource
from py_common.contract import (
    Column,
    Contract,
    Worksheet,
    read_csv,
    read_json,
    read_jsonl,
)
from py_common.errors import ConfigError, ContractError, ValidationError
from py_common.model import Outcome
from py_common.pipeline import Pipeline
from py_common.raw import json_value


def _contract(file_format: Any, *columns: Column, **settings: Any) -> Contract:
    """Build a one-table contract for an "items" table.

    Args:
        file_format: The contract's file_format.
        *columns: The listed columns.
        **settings: Extra Worksheet settings, such as on_mismatch.

    Returns:
        The contract.
    """
    return Contract(
        worksheets=[Worksheet("items", columns=list(columns), **settings)],
        file_format=file_format,
    )


def _jsonl(*records: Any) -> bytes:
    """Write records as JSON Lines.

    Args:
        *records: One value per line.

    Returns:
        The file's bytes.
    """
    return "".join(f"{json.dumps(r)}\n" for r in records).encode()


class TestReadCsv:
    """read_csv() reads text fields under named headings."""

    def test_fields_are_text_and_empty_fields_none(self) -> None:
        """Values stay text as written; an empty field is None."""
        names, rows = read_csv(b"id,name\r\n1,Ann\r\n2,\r\n")

        assert names == ["id", "name"]
        assert rows == [(2, ("1", "Ann")), (3, ("2", None))]

    def test_byte_order_mark_is_dropped(self) -> None:
        """Excel's "CSV UTF-8" BOM isn't part of the first heading."""
        names, _ = read_csv("﻿id,name\n1,Siân\n".encode())

        assert names == ["id", "name"]

    def test_rows_count_records_not_lines(self) -> None:
        """A quoted line break doesn't shift later row numbers."""
        data = b'id,note\n1,"two\nlines"\n2,x\n'

        _, rows = read_csv(data)

        assert rows == [(2, ("1", "two\nlines")), (3, ("2", "x"))]

    def test_headings_are_named_as_in_a_sheet(self) -> None:
        """Blank headings over data get col_X; repeats get _2."""
        names, rows = read_csv(b"Other,,Other\na,b,c\n")

        assert names == ["Other", "col_B", "Other_2"]
        assert rows == [(2, ("a", "b", "c"))]

    def test_header_row_skips_lines_above_the_headings(self) -> None:
        """header_row=2 reads headings from the second record."""
        names, rows = read_csv(b"Exported 1 Nov\nid\n7\n", header_row=2)

        assert names == ["id"]
        assert rows == [(3, ("7",))]

    def test_blank_rows_are_skipped(self) -> None:
        """A blank line or a record of empty fields isn't a row."""
        _, rows = read_csv(b"id\n\n,\n5\n")

        assert rows == [(4, ("5",))]

    def test_non_utf8_is_refused(self) -> None:
        """A Windows-1252 export is refused, naming the bad byte."""
        with pytest.raises(ValueError, match="not UTF-8 text"):
            read_csv("id,name\n1,café\n".encode("cp1252"))

    def test_broken_quoting_is_refused(self) -> None:
        """Text after a closing quote isn't valid CSV."""
        with pytest.raises(ValueError, match="invalid CSV at line 2"):
            read_csv(b'id\n"1"x\n')


class TestReadJson:
    """read_json() reads an array of objects."""

    def test_columns_are_every_key_in_first_seen_order(self) -> None:
        """A key one object leaves out reads as None."""
        data = json.dumps([{"a": 1, "b": "x"}, {"c": True, "a": 2}])

        names, rows = read_json(data.encode())

        assert names == ["a", "b", "c"]
        assert rows == [(1, (1, "x", None)), (2, (2, None, True))]

    def test_nested_values_are_kept(self) -> None:
        """Person and multiple-choice fields keep their shape."""
        record = {"Owner": {"Email": "a@example.org"}, "Tags": ["x", "y"]}

        _, rows = read_json(json.dumps([record]).encode())

        assert rows == [(1, ({"Email": "a@example.org"}, ["x", "y"]))]

    def test_blank_objects_are_skipped(self) -> None:
        """An object of nulls and blanks is a blank row."""
        data = json.dumps([{"a": None, "b": " "}, {"a": 3}])

        _, rows = read_json(data.encode())

        assert rows == [(2, (3, None))]

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ('{"a": 1}', "expected an array of objects, got an object"),
            ("[1]", "item 1 is integer, expected an object"),
            ('[{"a": 1, "a": 2}]', "repeats a key"),
            ('[{"a": NaN}]', "NaN is not a JSON number"),
            ('[{"a": 1e400}]', "too large for a float"),
            ("[{", "Expecting property name"),
        ],
    )
    def test_invalid_files_are_refused(self, text: str, message: str) -> None:
        """Anything but a clean array of objects is refused.

        Args:
            text: The file's text.
            message: Part of the expected message.
        """
        with pytest.raises(ValueError, match=message):
            read_json(text.encode())

    def test_deep_nesting_is_refused(self) -> None:
        """Nesting too deep to parse is a ValueError, not a crash."""
        with pytest.raises(ValueError, match="nested too deeply"):
            read_json(b"[" * 100_000 + b"]" * 100_000)


class TestReadJsonl:
    """read_jsonl() reads one object per line."""

    def test_rows_are_line_numbers(self) -> None:
        """Blank lines are skipped but still counted."""
        data = b'{"id": 1}\r\n\n{"id": 2}\n'

        names, rows = read_jsonl(data)

        assert names == ["id"]
        assert rows == [(1, (1,)), (3, (2,))]

    def test_line_separator_inside_text_does_not_split(self) -> None:
        """U+2028 may appear unescaped in a JSON string."""
        data = '{"note": "a b"}\n'.encode()

        _, rows = read_jsonl(data)

        assert rows == [(1, ("a b",))]

    def test_a_bad_line_is_named(self) -> None:
        """The message gives the line number, not its content."""
        with pytest.raises(ValueError, match="^line 2: Expecting"):
            read_jsonl(b'{"id": 1}\n{"id": \n')

    def test_a_line_that_is_not_an_object_is_refused(self) -> None:
        """Each line must be an object."""
        with pytest.raises(ValueError, match="line 1 is an array"):
            read_jsonl(b"[1, 2]\n")


class TestContractSettings:
    """The settings each file_format allows."""

    def test_unknown_format_is_refused(self) -> None:
        """Only the four formats are accepted."""
        with pytest.raises(ValueError, match="file_format must be one of"):
            Contract(file_format="xml")  # type: ignore[arg-type]

    def test_file_name_must_match_the_format(self) -> None:
        """A JSON Lines contract can't expect a .xlsx file."""
        with pytest.raises(ValueError, match=r"\.jsonl or \.ndjson"):
            Contract(file_name="Items.xlsx", file_format="jsonl")

    def test_file_name_extension_ignores_case(self) -> None:
        """Items.CSV is a CSV file name."""
        Contract(file_name="Items.CSV", file_format="csv")

    def test_a_table_file_has_one_worksheet(self) -> None:
        """A CSV file can't hold two tables."""
        with pytest.raises(ValueError, match="one Worksheet; got 2"):
            Contract(
                worksheets=[Worksheet("a"), Worksheet("b")],
                file_format="csv",
            )

    def test_json_has_no_heading_row(self) -> None:
        """header_row means nothing in a JSON file."""
        with pytest.raises(ValueError, match="header_row must be 1"):
            _contract("json", header_row=2)

    def test_csv_takes_a_heading_row(self) -> None:
        """A CSV file can have lines above its headings."""
        _contract("csv", header_row=3)

    def test_workbook_fingerprint_is_unchanged(self) -> None:
        """An xlsx contract's fingerprint leaves settings at default out.

        So adding a setting doesn't change the fingerprint in the audit
        records of contracts that don't use it.
        """
        contract = _contract("xlsx", Column("id", "integer"))
        settings = asdict(contract)
        for name in ("file_format", "max_columns", "max_cells"):
            del settings[name]
        before = sha256(
            json.dumps(
                settings, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()[:16]

        assert contract.fingerprint() == before

    def test_format_changes_the_fingerprint(self) -> None:
        """The same columns as CSV are a different contract."""
        columns = [Column("id", "integer")]

        assert (
            _contract("csv", *columns).fingerprint()
            != _contract("xlsx", *columns).fingerprint()
        )


class TestCsvContract:
    """Contract.read() on a CSV file."""

    def test_valid_file_loads_text_as_written(self) -> None:
        """Typed columns check the text but keep it unchanged."""
        contract = _contract(
            "csv",
            Column("id", "integer", unique=True),
            Column("score", "float"),
            Column("seen", "date"),
        )
        data = (
            b"id,score,seen\n"
            b"007,-1.5e2,2026-10-01T09:30:00Z\n"
            b"8,.5,2026-02-28\n"
        )

        sheets = contract.read(data, "items.csv")

        assert sheets["items"].rows == [
            (2, ("007", "-1.5e2", "2026-10-01T09:30:00Z")),
            (3, ("8", ".5", "2026-02-28")),
        ]

    @pytest.mark.parametrize(
        ("column", "text", "message"),
        [
            (Column("v", "integer"), "1.5", "doesn't read as a whole number"),
            (Column("v", "integer"), "١٢", "doesn't read as a whole number"),
            (Column("v", "integer"), "9" * 20, "is outside the INT64 range"),
            (Column("v", "float"), "inf", "doesn't read as a number"),
            (Column("v", "date"), "01/10/2026", "doesn't read as a date"),
            (Column("v", "date"), "2026-02-30", "doesn't read as a date"),
        ],
    )
    def test_text_that_does_not_fit_is_quarantined(
        self, column: Column, text: str, message: str
    ) -> None:
        """A field that doesn't read as its type fails the file.

        Args:
            column: The column definition.
            text: The field's text.
            message: Part of the expected message.
        """
        contract = _contract("csv", column)

        with pytest.raises(ValidationError, match=f"Row 2: 'v' {message}"):
            contract.read(f"v\n{text}\n".encode(), "items.csv")

    def test_required_and_unique_still_apply(self) -> None:
        """An empty required field and a repeat are both reported."""
        contract = _contract(
            "csv", Column("id", "string", unique=True), Column("n", "string")
        )

        with pytest.raises(ValidationError) as caught:
            contract.read(b'id,n\na,1\n" ",2\na,3\n', "items.csv")

        assert "Row 3: 'id' is required" in str(caught.value)
        assert "Row 4: Duplicate value" in str(caught.value)

    def test_warn_mode_loads_and_reports(self) -> None:
        """on_mismatch="warn" loads the file and records the problem."""
        contract = _contract(
            "csv", Column("id", "integer"), on_mismatch="warn"
        )
        warnings: list[str] = []

        sheets = contract.read(b"id,extra\nx,1\n", "items.csv", warnings)

        assert sheets["items"].rows == [(2, ("x",))]
        assert "Unexpected columns: 1" in warnings[0]
        assert "doesn't read as a whole number" in warnings[1]

    def test_empty_file_is_refused(self) -> None:
        """Headings with no rows fail unless allow_empty is set."""
        contract = _contract("csv", Column("id", "string"))

        with pytest.raises(ContractError, match="no data rows"):
            contract.read(b"id\n", "items.csv")

    def test_unreadable_file_is_a_contract_error(self) -> None:
        """A file that isn't UTF-8 is quarantined, naming the file."""
        contract = _contract("csv", Column("id", "string"))

        with pytest.raises(ContractError, match="items.csv: Cannot read CSV"):
            contract.read(b"id\n\xff\n", "items.csv")

    def test_size_limit_applies(self) -> None:
        """max_file_bytes refuses a big file before it is read."""
        contract = Contract(
            worksheets=[Worksheet("items")],
            file_format="csv",
            max_file_bytes=4,
        )

        with pytest.raises(ContractError, match="max_file_bytes"):
            contract.read(b"id\n12\n", "items.csv")


class TestJsonContract:
    """Contract.read() on JSON and JSON Lines files."""

    def test_json_types_are_checked(self) -> None:
        """Numbers, text and ISO dates fit their columns."""
        contract = _contract(
            "jsonl",
            Column("id", "integer"),
            Column("score", "float"),
            Column("title", "string"),
            Column("modified", "date"),
        )
        data = _jsonl(
            {
                "id": 1,
                "score": 2,
                "title": "A",
                "modified": "2026-10-01T09:30:00Z",
            }
        )

        sheets = contract.read(data, "items.jsonl")

        assert sheets["items"].rows == [
            (1, (1, 2, "A", "2026-10-01T09:30:00Z"))
        ]

    @pytest.mark.parametrize(
        ("column", "value", "message"),
        [
            (Column("v", "integer"), True, "is true/false, expected integer"),
            (Column("v", "integer"), "1", "is text, expected integer"),
            (Column("v", "string"), {"a": 1}, "is an object, expected text"),
            (Column("v", "date"), 20261001, "is integer, expected a date"),
            (Column("v", "date"), "1 Oct 2026", "is text, expected a date"),
        ],
    )
    def test_wrong_types_are_quarantined(
        self, column: Column, value: Any, message: str
    ) -> None:
        """A value of the wrong JSON type fails the file.

        Args:
            column: The column definition.
            value: The value in the file.
            message: Part of the expected message.
        """
        contract = _contract("json", column)
        data = json.dumps([{"v": value}]).encode()

        with pytest.raises(ValidationError, match=f"Row 1: 'v' {message}"):
            contract.read(data, "items.json")

    def test_unique_works_on_nested_values(self) -> None:
        """Equal objects are duplicates, whatever their key order."""
        contract = _contract(
            "jsonl", Column("v", "string", unique=True), on_mismatch="warn"
        )
        warnings: list[str] = []

        contract.read(
            _jsonl({"v": {"a": 1, "b": 2}}, {"v": {"b": 2, "a": 1}}),
            "items.jsonl",
            warnings,
        )

        assert "Row 2: Duplicate value in unique column 'v'" in warnings[0]

    def test_all_except_picks_up_new_keys(self) -> None:
        """New list columns load without a contract change."""
        contract = _contract(
            "jsonl", column_selection="all_except", excluded=["Email"]
        )

        sheets = contract.read(
            _jsonl({"id": 1, "Email": "a@example.org", "New": "x"}),
            "items.jsonl",
        )

        assert sheets["items"].columns == ["id", "New"]

    def test_empty_file_is_refused_unless_allowed(self) -> None:
        """An empty array is "no records"; allow_empty lets it load."""
        with pytest.raises(ContractError, match="no records"):
            _contract("json").read(b"[]", "items.json")

        sheets = _contract("json", allow_empty=True).read(b"[]", "items.json")
        assert sheets["items"].rows == []

    def test_wrong_file_name_is_refused_first(self) -> None:
        """file_name is checked before the file is read."""
        contract = Contract(
            worksheets=[Worksheet("items")],
            file_name="Items.jsonl",
            file_format="jsonl",
        )

        with pytest.raises(ContractError, match="wrong file name"):
            contract.read(b"not json", "Copy of Items.jsonl")


class TestJsonValue:
    """json_value() keeps nested JSON values."""

    def test_objects_and_arrays_keep_their_shape(self) -> None:
        """Values inside are converted the same way."""
        value = {"a": [1, float("nan"), {"b": None}]}

        assert json_value(value) == {"a": [1, None, {"b": None}]}


class TestLocalSourceExtensions:
    """LocalSource lists the extensions it is given."""

    def test_lists_only_matching_files_ignoring_case(
        self, tmp_path: Path
    ) -> None:
        """Other files and sub-folders are left alone.

        Args:
            tmp_path: pytest's temporary folder.
        """
        for name in ("a.jsonl", "B.JSONL", "c.xlsx", "d.json"):
            (tmp_path / name).write_bytes(b"{}\n")
        (tmp_path / "e.jsonl").mkdir()

        source = LocalSource(tmp_path, extensions=(".jsonl",))

        names = sorted(item.name for item in source.items())
        assert names == ["B.JSONL", "a.jsonl"]

    def test_default_is_workbooks(self, tmp_path: Path) -> None:
        """Without extensions, only .xlsx files are listed.

        Args:
            tmp_path: pytest's temporary folder.
        """
        (tmp_path / "a.xlsx").write_bytes(b"")
        (tmp_path / "b.csv").write_bytes(b"")

        assert [i.name for i in LocalSource(tmp_path).items()] == ["a.xlsx"]

    def test_extension_needs_a_dot(self, tmp_path: Path) -> None:
        """An extension without its dot is refused.

        Args:
            tmp_path: pytest's temporary folder.
        """
        with pytest.raises(ValueError, match="must look like"):
            LocalSource(tmp_path, extensions=("csv",))


def _warehouse() -> Mock:
    """Build a warehouse that accepts every load.

    Returns:
        The mock warehouse.
    """
    warehouse = Mock(identity="p.d.items")
    warehouse.load.return_value = True
    return warehouse


class TestPipeline:
    """The pipeline with CSV and JSON contracts."""

    def test_source_extensions_must_match_the_format(
        self, tmp_path: Path
    ) -> None:
        """A .csv source for a workbook contract fails at start-up.

        Args:
            tmp_path: pytest's temporary folder.
        """
        with pytest.raises(ConfigError, match=r"\['\.csv'\]"):
            Pipeline(
                LocalSource(tmp_path, extensions=(".csv",)),
                MemoryStore(),
                _warehouse(),
                _contract("xlsx", Column("id", "string")),
            )

    def test_jsonl_file_loads_as_raw_rows(self, tmp_path: Path) -> None:
        """A list export lands as one raw row per item, nested intact.

        Args:
            tmp_path: pytest's temporary folder.
        """
        (tmp_path / "Items.jsonl").write_bytes(
            _jsonl(
                {"ID": 1, "Title": "A", "Owner": {"Email": "a@example.org"}},
                {"ID": 2, "Title": "B", "Owner": None},
            )
        )
        warehouse = _warehouse()
        store = MemoryStore()
        contract = Contract(
            worksheets=[
                Worksheet(
                    "items",
                    columns=[Column("ID", "integer", unique=True)],
                    column_selection="all_except",
                )
            ],
            file_name="Items.jsonl",
            file_format="jsonl",
        )

        [result] = Pipeline(
            LocalSource(tmp_path, extensions=(".jsonl",)),
            store,
            warehouse,
            contract,
        ).run()

        assert result.outcome == Outcome.LOADED, result.errors
        assert result.rows_processed == 2
        ndjson = warehouse.load.call_args.kwargs["data"]
        first = json.loads(ndjson.splitlines()[0])
        assert first["sheet_name"] == "items"
        assert first["row_number"] == 1
        assert first["payload"] == {
            "ID": 1,
            "Title": "A",
            "Owner": {"Email": "a@example.org"},
        }

    def test_bad_csv_is_quarantined(self, tmp_path: Path) -> None:
        """A CSV that fails its contract is kept in quarantine/.

        Args:
            tmp_path: pytest's temporary folder.
        """
        (tmp_path / "items.csv").write_bytes(b"id\nx\n")
        store = MemoryStore()

        [result] = Pipeline(
            LocalSource(tmp_path, extensions=(".csv",)),
            store,
            _warehouse(),
            _contract("csv", Column("id", "integer")),
        ).run()

        assert result.outcome == Outcome.QUARANTINED
        assert any(
            key.startswith("quarantine/") and key.endswith("/items.csv")
            for key in store.objects
        )
