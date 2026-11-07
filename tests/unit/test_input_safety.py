"""Input validation and deterministic raw payloads."""

import json
import logging
import zipfile
from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from _memory_store import MemoryStore
from openpyxl import Workbook

from py_common.contract import Column, Contract, Worksheet
from py_common.model import Outcome, SourceItem
from py_common.pipeline import Pipeline
from py_common.raw import to_ndjson


def process(data, fmt="json", columns=None):
    """Process synthetic bytes with an in-memory store.

    Args:
        data: File bytes.
        fmt: Input format.
        columns: Explicit column definitions or all columns.

    Returns:
        Result and pipeline.
    """
    item = SourceItem("probe." + fmt, "probe", "1", len(data))
    source = Mock()
    source.download.return_value = data
    source.items.return_value = [item]
    warehouse = Mock(identity="test.raw")
    warehouse.load.return_value = True
    sheet = Worksheet(
        "Sheet",
        columns=columns or [],
        column_selection="listed" if columns else "all_except",
    )
    pipeline = Pipeline(
        source, MemoryStore(), warehouse, Contract([sheet], file_format=fmt)
    )
    return pipeline.process(item), pipeline


class TestPipelineProcess:
    """Malformed inputs quarantine and preserve diagnostic privacy."""

    def test_missing_headings_do_not_expose_cells(self, caplog):
        """A data row mistaken for headings stays out of diagnostics.

        Args:
            caplog: Captured pipeline logs.
        """
        marker = "synthetic.person@example.invalid"
        data = f"Synthetic Person,{marker}\nOther Person,other\n".encode()
        result, pipeline = process(
            data, "csv", [Column("id", "string"), Column("org", "string")]
        )
        pipeline._audit(result)
        audit = b"".join(
            value
            for key, value in pipeline.store.objects.items()
            if key.startswith("audit/records/")
        ).decode()
        assert result.outcome == Outcome.QUARANTINED
        assert marker not in str(result.errors) + caplog.text + audit
        assert (
            "Synthetic Person" not in str(result.errors) + caplog.text + audit
        )

    @pytest.mark.parametrize("fmt", ["json", "jsonl"])
    def test_surrogate_quarantines(self, fmt):
        """An unpaired surrogate is an invalid UTF-8 payload.

        Args:
            fmt: JSON container format.
        """
        data = b'{"value":"\\ud800"}'
        if fmt == "json":
            data = b"[" + data + b"]"
        result, _ = process(data, fmt)
        assert result.outcome == Outcome.QUARANTINED, result.errors

    def test_csv_huge_integer_quarantines(self):
        """An out-of-range integer is a file validation failure."""
        result, _ = process(
            b"id\n" + b"9" * 5000 + b"\n", "csv", [Column("id", "integer")]
        )
        assert result.outcome == Outcome.QUARANTINED, result.errors

    def test_workbook_memory_error_is_failed(self):
        """A worker memory failure is retryable, not a bad-file verdict."""
        workbook = Workbook()
        workbook.active.append(["value"])
        workbook.active.append([1])
        stream = BytesIO()
        workbook.save(stream)
        with patch(
            "py_common.contract.load_workbook",
            side_effect=MemoryError("synthetic memory pressure"),
        ):
            result, _ = process(stream.getvalue(), "xlsx")
        assert result.outcome == Outcome.FAILED, result.errors

    def test_numeric_overflow_cannot_load_as_null(self):
        """An unrepresentable required number is invalid input."""
        workbook = Workbook()
        workbook.active.append(["value"])
        workbook.active.append([123])
        stream = BytesIO()
        workbook.save(stream)
        modified = BytesIO()
        with zipfile.ZipFile(stream) as src:
            with zipfile.ZipFile(modified, "w") as dst:
                for name in src.namelist():
                    value = src.read(name)
                    if name == "xl/worksheets/sheet1.xml":
                        value = value.replace(b"<v>123</v>", b"<v>1e400</v>")
                    dst.writestr(name, value)
        result, pipeline = process(
            modified.getvalue(), "xlsx", [Column("value", "float")]
        )
        if result.processed_key:
            print(pipeline.store.objects[result.processed_key])
        assert result.outcome == Outcome.QUARANTINED, result

    def test_convertible_json_depth_quarantines(self):
        """Nesting accepted by the parser cannot crash conversion."""
        data = b'[{"value":' + b"[" * 600 + b"0" + b"]" * 600 + b"}]"
        result, _ = process(data)
        assert result.outcome == Outcome.QUARANTINED, result.errors

    def test_invalid_xlsx_numeric_value_is_private(self, caplog):
        """Parser diagnostics omit cell contents.

        Args:
            caplog: Captured log records.
        """
        workbook = Workbook()
        workbook.active.append(["value"])
        workbook.active.append([123])
        original = BytesIO()
        workbook.save(original)
        altered = BytesIO()
        marker = b"synthetic.person@example.invalid"
        with zipfile.ZipFile(original) as src:
            with zipfile.ZipFile(altered, "w") as dst:
                for name in src.namelist():
                    value = src.read(name)
                    if name == "xl/worksheets/sheet1.xml":
                        value = value.replace(
                            b"<v>123</v>", b"<v>" + marker + b"</v>"
                        )
                    dst.writestr(name, value)
        with caplog.at_level(logging.WARNING):
            result, _ = process(altered.getvalue(), "xlsx")
        assert marker.decode() not in str(result.errors) + caplog.text

    @pytest.mark.parametrize(
        "fmt,data",
        [
            ("xlsx", b"not a zip"),
            ("csv", b'value\n"unterminated'),
            ("csv", b"value\n\xff"),
            ("csv", b"value\n" + b"x" * 140000),
            ("json", b'[{"x":1,"x":2}]'),
            ("json", b'[{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}]"),
            ("json", b""),
            ("jsonl", b""),
        ],
        # Short names: a test's name goes into an environment variable,
        # which Windows limits to 32,767 characters.
        ids=[
            "xlsx-not-a-zip",
            "csv-unterminated-quote",
            "csv-not-utf8",
            "csv-field-too-long",
            "json-repeated-key",
            "json-too-deep",
            "json-empty",
            "jsonl-empty",
        ],
    )
    def test_malformed_quarantines(self, fmt, data):
        """Unsupported or malformed files are acknowledged as invalid.

        Args:
            fmt: File format.
            data: Synthetic file bytes.
        """
        result, _ = process(data, fmt)
        assert result.outcome == Outcome.QUARANTINED, result.errors


class TestToNdjson:
    """Equivalent JSON objects have equivalent processed bytes."""

    def test_key_order_is_stable(self):
        """Top-level and nested object key order has no meaning."""
        contract = Contract(
            [Worksheet("Sheet", column_selection="all_except")],
            file_format="json",
        )
        item = SourceItem("probe.json", "probe", "1", 1)
        first = b'[{"a":1,"b":{"x":2,"y":3}}]'
        second = b'[{"b":{"y":3,"x":2},"a":1}]'
        left = to_ndjson(contract.read(first, item.name), item, "same")[0]
        right = to_ndjson(contract.read(second, item.name), item, "same")[0]
        assert left == right, (left, right)


class TestContractRead:
    """Awkward but bounded format cases have explicit outcomes."""

    @pytest.mark.parametrize(
        "data",
        [
            "value\nWelsh £".encode("cp1252"),
            "value\nWelsh".encode("utf-16"),
            b"a;b\n1;2\n",
            b"a\tb\n1\t2\n",
            b"a,b\n1,2,3\n",
        ],
    )
    def test_csv_layout_is_rejected(self, data):
        """Unsupported encodings and layouts fail the declared columns.

        Args:
            data: Synthetic CSV bytes.
        """
        result, _ = process(
            data, "csv", [Column("a", "string"), Column("b", "string")]
        )
        assert result.outcome == Outcome.QUARANTINED, result.errors

    def test_xlsx_repeated_blank_and_merged_headings_keep_values(self):
        """Merged and repeated headings receive distinct payload keys."""
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Other", "Other", None, "Title", None])
        sheet.append(["a", "b", "c", "d", "e"])
        sheet.merge_cells("D1:E1")
        stream = BytesIO()
        workbook.save(stream)
        result, pipeline = process(stream.getvalue(), "xlsx")
        assert result.outcome == Outcome.LOADED, result.errors
        record = json.loads(pipeline.store.objects[result.processed_key])
        assert record["payload"] == {
            "Other": "a",
            "Other_2": "b",
            "col_C": "c",
            "Title": "d",
            "col_E": "e",
        }

    def test_uncached_xlsx_formula_quarantines(self):
        """Py-common refuses a formula with no saved value."""
        workbook = Workbook()
        workbook.active.append(["id", "value"])
        workbook.active.append([1, "=1+1"])
        stream = BytesIO()
        workbook.save(stream)
        result, _ = process(stream.getvalue(), "xlsx")
        assert result.outcome == Outcome.QUARANTINED, result.errors

    def test_json_large_integer_typed_quarantines(self):
        """An explicit integer column refuses values outside INT64."""
        result, _ = process(
            b'[{"id":18446744073709551616}]', "json", [Column("id", "integer")]
        )
        assert result.outcome == Outcome.QUARANTINED, result.errors

    def test_jsonl_unusual_separators_within_strings_load(self):
        """Unicode line separators remain part of one string."""
        data = '{"value":"a\u2028b\u2029c"}\r\n'.encode()
        result, pipeline = process(data, "jsonl")
        assert result.outcome == Outcome.LOADED, result.errors
        record = json.loads(pipeline.store.objects[result.processed_key])
        assert record["payload"]["value"] == "a\u2028b\u2029c"

    def test_unusual_json_keys_reach_raw_payload(self):
        """Payload keys remain JSON keys instead of SQL identifiers."""
        data = b'[{"a.b":1,"a b":2,"$x":3,"":4,"quote\\"":5}]'
        result, pipeline = process(data)
        assert result.outcome == Outcome.LOADED, result.errors
        record = json.loads(pipeline.store.objects[result.processed_key])
        assert record["payload"] == json.loads(data)[0]

    def test_same_bytes_repeat_identically(self):
        """Fresh runs produce the same processed bytes without a clock."""
        data = b'[{"a":1,"b":{"x":2,"y":3}}]'
        result, pipeline = process(data)
        other, second = process(data)
        assert pipeline.store.objects[result.processed_key] == (
            second.store.objects[other.processed_key]
        )
