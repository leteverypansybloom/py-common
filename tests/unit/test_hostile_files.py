"""Damaged, malformed and oversized files end as quarantines.

A file that can never load is the file's fault, so it is quarantined
rather than retried as a service failure, and nothing it holds is
loaded as if it were real data.
"""

import json
import tracemalloc
import zipfile
from datetime import datetime
from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from _memory_store import MemoryStore
from google.api_core.exceptions import BadRequest
from openpyxl import Workbook
from openpyxl.chart import BarChart

from py_common.adapters._bq_jobs import wait_for_load
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.contract import (
    Column,
    Contract,
    Worksheet,
    read_csv,
    read_json,
    read_jsonl,
)
from py_common.errors import ContractError, ServiceError, ValidationError
from py_common.graph import GraphJsonLinesContract
from py_common.model import Outcome, SourceItem
from py_common.pipeline import Pipeline

SHEET_PART = "xl/worksheets/sheet1.xml"
TYPES_PART = "[Content_Types].xml"
NO_WORKBOOK_TYPES = (
    b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
    b'package/2006/content-types"></Types>'
)
HEADINGS = (
    b'<row r="1"><c r="A1" t="inlineStr"><is><t>a</t></is></c>'
    b'<c r="B1" t="inlineStr"><is><t>b</t></is></c></row>'
)
COLUMNS = [
    Column("a", "string", nullable=True),
    Column("b", "any", nullable=True),
]
MEMORY_LIMIT = 32 * 1024 * 1024


def workbook(*rows):
    """Build a one-sheet workbook.

    Args:
        *rows: Rows to append to the sheet "S".

    Returns:
        The .xlsx bytes.
    """
    book = Workbook()
    sheet = book.active
    sheet.title = "S"
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def replaced(data, part, content):
    """Copy a zip with one part's content replaced.

    Args:
        data: The zip's bytes.
        part: Name of the part to replace.
        content: Its new bytes.

    Returns:
        The new zip's bytes.
    """
    source = zipfile.ZipFile(BytesIO(data))
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(
                info.filename,
                (
                    content
                    if info.filename == part
                    else source.read(info.filename)
                ),
            )
    return out.getvalue()


def with_rows(*rows):
    """Build a workbook whose sheet XML is exactly these rows.

    Args:
        *rows: Raw <row> elements.

    Returns:
        The .xlsx bytes.
    """
    xml = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/'
        b'spreadsheetml/2006/main"><sheetData>'
        + b"".join(rows)
        + b"</sheetData></worksheet>"
    )
    return replaced(workbook(["a", "b"]), SHEET_PART, xml)


def data_row(number, text):
    """Write a <row> element with a text cell.

    Args:
        number: The row number.
        text: The A cell's text.

    Returns:
        The element's bytes.
    """
    return (
        b'<row r="%d"><c r="A%d" t="inlineStr"><is><t>%s</t></is></c></row>'
        % (number, number, text)
    )


def patched_headers(data, signature, flag_offset, method_offset, flag, method):
    """Change a flag or the method in each zip header of one kind.

    Args:
        data: The zip's bytes.
        signature: The header's signature.
        flag_offset: Where its general purpose flag sits.
        method_offset: Where its compression method sits.
        flag: Bits to set in the flag.
        method: The method to write, or None to leave it.

    Returns:
        The changed bytes.
    """
    changed = bytearray(data)
    at = 0
    while (at := changed.find(signature, at)) != -1:
        changed[at + flag_offset] |= flag
        if method is not None:
            changed[at + method_offset] = method
        at += len(signature)
    return bytes(changed)


def damaged_zip(flag=0, method=None):
    """Build a workbook with altered zip headers.

    Args:
        flag: Bits to set in each header's flag.
        method: Compression method to write in each header.

    Returns:
        The .xlsx bytes.
    """
    data = patched_headers(
        workbook(["a", "b"], ["x", 1]), b"PK\x03\x04", 6, 8, flag, method
    )
    return patched_headers(data, b"PK\x01\x02", 8, 10, flag, method)


def chart_sheet_workbook():
    """Build a workbook whose sheet "S" is a chart sheet.

    Returns:
        The .xlsx bytes.
    """
    book = Workbook()
    book.active.title = "Data"
    book.create_chartsheet("S").add_chart(BarChart())
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def contract(columns=None, file_format="xlsx", **settings):
    """Build a one-table contract for the sheet "S".

    Args:
        columns: Listed columns, or None for all but the excluded.
        file_format: The contract's file format.
        **settings: Other Worksheet settings.

    Returns:
        The contract.
    """
    if columns is None:
        settings["column_selection"] = "all_except"
    return Contract(
        [Worksheet("S", columns or [], **settings)], file_format=file_format
    )


def peak_memory(read):
    """Measure the memory one call takes at its peak.

    Args:
        read: The call, which may raise ValidationError.

    Returns:
        Peak bytes allocated.
    """
    tracemalloc.start()
    try:
        try:
            read()
        except ValidationError:
            pass
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def pipeline_for(data, name, the_contract, size=None):
    """Build the real pipeline and adapter on fake services.

    Args:
        data: Bytes the source serves.
        name: The file's name.
        the_contract: The contract to read with.
        size: The size the source lists, if not the data's own.

    Returns:
        The pipeline, its source and the fake BigQuery client.
    """
    item = SourceItem(
        name, f"in/{name}", "1", len(data) if size is None else size
    )
    source = Mock()
    source.items.return_value = [item]
    source.download.return_value = data
    client = FakeBigQueryClient()
    with patch(
        "py_common.adapters.bigquery_raw.bigquery.Client", return_value=client
    ):
        warehouse = RawJsonWarehouse("p", "d", {"S": "rows"})
    return (
        Pipeline(source, MemoryStore(), warehouse, the_contract),
        source,
        client,
    )


class TestContractRead:
    """Contract.read refuses what it can't read whole and correctly."""

    @pytest.mark.parametrize(
        "data",
        [
            replaced(workbook(["a"]), TYPES_PART, NO_WORKBOOK_TYPES),
            damaged_zip(method=9),
            damaged_zip(flag=1),
            chart_sheet_workbook(),
        ],
        ids=["no-workbook-part", "zip-method", "encrypted", "chart-sheet"],
    )
    def test_unreadable_workbook_is_a_contract_error(self, data):
        """A package that can't be read as a workbook is the file's fault.

        Args:
            data: The workbook's bytes.
        """
        with pytest.raises(ContractError):
            contract(COLUMNS).read(data, "f.xlsx")

    @pytest.mark.parametrize(
        "rows",
        [
            [HEADINGS, data_row(5, b"five"), data_row(3, b"three")],
            [HEADINGS, data_row(2, b"first"), data_row(2, b"second")],
        ],
        ids=["out-of-order", "repeated"],
    )
    @pytest.mark.parametrize("mode", ["quarantine", "warn"])
    def test_rows_that_would_be_lost_are_refused(self, rows, mode):
        """A sheet openpyxl can't read whole is refused in either mode.

        Args:
            rows: The sheet's rows.
            mode: The worksheet's on_mismatch.
        """
        with pytest.raises(ContractError, match="out of order or repeated"):
            contract(COLUMNS, on_mismatch=mode).read(
                with_rows(*rows), "f.xlsx"
            )

    def test_rows_without_numbers_are_in_order(self):
        """A row with no number follows the one before it."""
        data = with_rows(
            b'<row><c t="inlineStr"><is><t>a</t></is></c>'
            b'<c t="inlineStr"><is><t>b</t></is></c></row>',
            b'<row><c t="inlineStr"><is><t>x</t></is></c></row>',
        )
        sheets = contract(COLUMNS).read(data, "f.xlsx")
        assert sheets["S"].rows == [(2, ("x", None))]

    def test_cached_excel_error_is_refused(self):
        """A failed formula's stored result doesn't load as text."""
        data = with_rows(
            HEADINGS,
            b'<row r="2"><c r="A2" t="e"><f>VLOOKUP(1,X,2)</f>'
            b"<v>#REF!</v></c></row>",
        )
        with pytest.raises(ValidationError, match=r"Row 2: 'a'") as info:
            contract(COLUMNS).read(data, "f.xlsx")
        assert "#REF!" not in str(info.value)

    @pytest.mark.parametrize(
        "text", ["#REF!", "#N/A", " #DIV/0! ", "#value!", "#SPILL!"]
    )
    def test_error_text_in_a_cell_is_refused(self, text):
        """The same text typed or exported is refused too.

        Args:
            text: The cell's text.
        """
        with pytest.raises(ValidationError, match="Excel error value"):
            contract(COLUMNS).read(workbook(["a", "b"], [text, 1]), "f.xlsx")

    def test_error_in_a_column_not_loaded_is_ignored(self):
        """An excluded column's error can't reach the load."""
        data = workbook(["a", "b", "c"], ["x", 1, "#REF!"])
        sheets = contract(COLUMNS, excluded=["c"]).read(data, "f.xlsx")
        assert sheets["S"].rows == [(2, ("x", 1))]

    def test_excel_error_is_a_warning_in_warn_mode(self):
        """A "warn" worksheet records the problem and loads the file."""
        warnings = []
        data = workbook(["a", "b"], ["#REF!", 1])
        contract(COLUMNS, on_mismatch="warn").read(
            data, "f.xlsx", warnings=warnings
        )
        assert any("Excel error value" in w for w in warnings)

    def test_excel_error_text_in_csv_is_refused(self):
        """A CSV file saved from Excel holds the same text."""
        with pytest.raises(ValidationError, match="Excel error value"):
            contract(file_format="csv").read(b"a\n#REF!\n", "f.csv")

    def test_error_like_json_text_is_data(self):
        """JSON has no Excel errors, so the text is left alone."""
        sheets = contract(file_format="jsonl").read(
            b'{"a":"#REF!"}\n', "f.jsonl"
        )
        assert sheets["S"].rows == [(1, ("#REF!",))]

    def test_csv_shift_can_not_carry_an_excluded_value(self):
        """An unquoted comma doesn't push a name into a loaded column."""
        with pytest.raises(ContractError, match="unquoted comma"):
            contract(file_format="csv", excluded=["name"]).read(
                b"name,org\nSmith,John,Acme\n", "f.csv"
            )

    def test_wide_sheet_is_refused_as_it_is_read(self):
        """One far-right cell per row can't fill memory."""
        rows = b"".join(
            b'<row r="%d"><c r="XFD%d"><v>1</v></c></row>' % (n, n)
            for n in range(2, 802)
        )
        data = with_rows(HEADINGS, rows)
        with pytest.raises(ContractError, match="max_columns"):
            contract(COLUMNS).read(data, "f.xlsx")
        peak = peak_memory(lambda: contract(COLUMNS).read(data, "f.xlsx"))
        assert peak < MEMORY_LIMIT

    def test_sheet_with_too_many_cells_is_refused(self):
        """A workbook over max_cells is refused."""
        the_contract = Contract(
            [Worksheet("S", COLUMNS)],
            max_cells=3,
        )
        with pytest.raises(ContractError, match="max_cells"):
            the_contract.read(
                workbook(["a", "b"], ["x", 1], ["y", 2]), "f.xlsx"
            )

    @pytest.mark.parametrize("limit", [{"max_columns": 0}, {"max_cells": -1}])
    def test_limits_must_be_positive(self, limit):
        """A limit below one is a mistake in the contract.

        Args:
            limit: The setting.
        """
        with pytest.raises(ValueError, match="must be positive"):
            Contract([Worksheet("S", COLUMNS)], **limit)


class TestReadCsv:
    """read_csv refuses rows that would shift and tables over a limit."""

    def test_row_longer_than_the_headings_is_refused(self):
        """More fields than headings means the values have moved."""
        with pytest.raises(ValueError, match="row 2 has 3 fields"):
            read_csv(b"a,b\n1,2,3\n")

    def test_lines_above_the_headings_are_not_compared(self):
        """A title line wider than the headings is skipped as usual."""
        names, rows = read_csv(b"title,x,y\na,b\n1,2\n", header_row=2)
        assert (names, rows) == (["a", "b"], [(3, ("1", "2"))])

    def test_short_row_still_reads(self):
        """Fewer fields than headings reads as empty cells."""
        assert read_csv(b"a,b\n1\n")[1] == [(2, ("1", None))]

    def test_too_many_cells_are_refused(self):
        """The file is refused before all of it is held."""
        with pytest.raises(ValueError, match="max_cells"):
            read_csv(b"a,b\n1,2\n3,4\n", max_cells=5)

    def test_too_many_columns_are_refused(self):
        """A row wider than max_columns is refused."""
        with pytest.raises(ValueError, match="max_columns"):
            read_csv(b"a,b,c\n", max_columns=2)


class TestReadJson:
    """read_json refuses a table over a limit."""

    def test_too_many_cells_are_refused(self):
        """Records times distinct keys is the table's real size."""
        with pytest.raises(ValueError, match="max_cells"):
            read_json(b'[{"a":1},{"b":1},{"c":1}]', max_cells=8)


class TestReadJsonl:
    """read_jsonl refuses a table over a limit before building it."""

    def test_too_many_keys_are_refused(self):
        """More distinct keys than max_columns is refused."""
        with pytest.raises(ValueError, match="max_columns"):
            read_jsonl(b'{"a":1}\n{"b":1}\n{"c":1}\n', max_columns=2)

    def test_sparse_records_are_refused_by_their_real_size(self):
        """A key per line would make records times keys cells."""
        with pytest.raises(ValueError, match="max_cells"):
            read_jsonl(b'{"a":1}\n{"b":1}\n{"c":1}\n', max_cells=8)

    def test_too_many_lines_are_refused_while_parsing(self):
        """A line's cells are counted before the next is parsed."""
        with pytest.raises(ValueError, match="max_cells"):
            read_jsonl(b'{"a":1}\n' * 5, max_cells=3)


class TestGraphJsonLinesContractRead:
    """GraphJsonLinesContract.read on what Microsoft exports can hold."""

    @staticmethod
    def graph():
        """Build a contract with one date column.

        Returns:
            The contract.
        """
        return GraphJsonLinesContract(
            worksheets=[Worksheet("S", [Column("d", "date", nullable=True)])],
            file_format="jsonl",
            field_map={"d": "D"},
            site_time_zone="Europe/London",
        )

    @pytest.mark.parametrize(
        "value",
        [
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59Z",
            "1899-12-30T00:00:00Z",
            "2300-06-01T00:00:00Z",
        ],
    )
    def test_date_outside_the_accepted_years_is_refused(self, value):
        """A placeholder or mistyped year is the file's fault.

        Args:
            value: The date as Graph sends it.
        """
        data = json.dumps({"id": "1", "fields": {"D": value}}).encode()
        with pytest.raises(ValidationError, match="expected a date"):
            self.graph().read(data, "f.jsonl")

    def test_date_in_the_accepted_years_still_reads(self):
        """An ordinary date converts to the site's time zone."""
        data = b'{"id":"1","fields":{"D":"2026-05-31T23:00:00Z"}}\n'
        sheets = self.graph().read(data, "f.jsonl")
        assert sheets["S"].rows == [(1, ("2026-06-01T00:00:00",))]

    def test_sparse_export_is_refused_with_little_memory(self):
        """A new top-level key on each line can't fill memory."""
        data = "\n".join(
            json.dumps({"id": "1", f"x{n}": 1, "fields": {"D": None}})
            for n in range(3000)
        ).encode()
        graph = self.graph()
        with pytest.raises(ValidationError):
            graph.read(data, "f.jsonl")
        assert peak_memory(lambda: graph.read(data, "f.jsonl")) < MEMORY_LIMIT


class TestTypeProblem:
    """A date column accepts the same years whatever the file format."""

    @pytest.mark.parametrize(
        ("file_format", "data"),
        [
            ("csv", b"a\n1500-01-01\n"),
            ("json", b'[{"a":"2300-01-01"}]'),
            ("jsonl", b'{"a":"2300-01-01T00:00:00"}\n'),
        ],
    )
    def test_text_date_outside_the_years_is_refused(self, file_format, data):
        """A date as text outside the accepted years is refused.

        Args:
            file_format: The file format.
            data: The file's bytes.
        """
        the_contract = contract(
            [Column("a", "date", nullable=True)], file_format
        )
        with pytest.raises(ValidationError):
            the_contract.read(data, f"f.{file_format}")

    def test_excel_date_outside_the_years_is_refused(self):
        """A date cell outside the accepted years is refused."""
        the_contract = contract([Column("a", "date", nullable=True)])
        data = workbook(["a"], [datetime(2300, 1, 1)])
        with pytest.raises(ValidationError, match="outside the years"):
            the_contract.read(data, "f.xlsx")


class TestPipelineRun:
    """A whole run quarantines these files instead of failing."""

    @pytest.mark.parametrize(
        "data",
        [
            replaced(workbook(["a"]), TYPES_PART, NO_WORKBOOK_TYPES),
            chart_sheet_workbook(),
        ],
        ids=["no-workbook-part", "chart-sheet"],
    )
    def test_unreadable_workbook_is_quarantined(self, data):
        """A file that can never load isn't retried as a failure.

        Args:
            data: The workbook's bytes.
        """
        pipeline, _, _ = pipeline_for(data, "f.xlsx", contract(COLUMNS))
        assert pipeline.run()[0].outcome == Outcome.QUARANTINED

    def test_oversized_file_is_not_downloaded(self):
        """The listed size stops the file before it is in memory."""
        the_contract = Contract([Worksheet("S", COLUMNS)], max_file_bytes=1024)
        pipeline, source, client = pipeline_for(
            b"", "f.xlsx", the_contract, size=1024**3
        )
        result = pipeline.run()[0]
        source.download.assert_not_called()
        assert result.outcome == Outcome.QUARANTINED
        assert "max_file_bytes" in result.errors[0]
        assert client.audit == []

    def test_file_at_the_limit_is_downloaded(self):
        """A file exactly at the limit isn't over it."""
        data = workbook(["a", "b"], ["x", 1])
        the_contract = Contract(
            [Worksheet("S", COLUMNS)], max_file_bytes=len(data)
        )
        pipeline, _, _ = pipeline_for(data, "f.xlsx", the_contract)
        assert pipeline.run()[0].outcome == Outcome.LOADED

    def test_rows_bigquery_refuses_are_quarantined(self):
        """Data BigQuery refuses is kept and not retried."""
        pipeline, _, client = pipeline_for(
            b'{"a":"x"}\n', "f.jsonl", contract(file_format="jsonl")
        )
        job = Mock(job_id="load-1")
        job.result.side_effect = BadRequest(
            "Error while reading data", errors=[{"reason": "invalid"}]
        )
        with patch.object(client, "load_table_from_file", return_value=job):
            result = pipeline.run()[0]
        assert result.outcome == Outcome.QUARANTINED
        assert result.rows_processed == 0
        assert any(k.startswith("quarantine/") for k in pipeline.store.objects)


class TestWaitForLoad:
    """wait_for_load tells refused rows from other failures."""

    def test_refused_rows_are_a_validation_error(self):
        """A 400 with the reason "invalid" comes from the data."""
        job = Mock(job_id="load-1")
        job.result.side_effect = BadRequest(
            "Could not parse 'someone@example.invalid'",
            errors=[{"reason": "invalid"}],
        )
        with pytest.raises(ValidationError, match="load-1") as info:
            wait_for_load(job, "k")
        assert "someone" not in str(info.value)
        assert info.value.__cause__ is None

    @pytest.mark.parametrize(
        "error",
        [
            BadRequest("no reason given"),
            BadRequest("other", errors=[{"reason": "badRequest"}]),
        ],
        ids=["no-reason", "other-reason"],
    )
    def test_other_failures_stay_service_errors(self, error):
        """Anything else may pass with time, so it is retried.

        Args:
            error: What waiting raises.
        """
        job = Mock(job_id="load-1")
        job.result.side_effect = error
        with pytest.raises(ServiceError):
            wait_for_load(job, "k")
