"""Fixes from the second review of 0.2.0 (H06, H07, M04, M05, M07, M12)."""

import base64
import io
import json
import logging
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from openpyxl import Workbook

from py_common.contract import Column, Contract, Worksheet, read_sheet
from py_common.errors import ServiceError, ValidationError
from py_common.logging import configure_json_logging
from py_common.model import Outcome, SourceItem
from py_common.parquet import to_parquet
from py_common.contract import SheetRows
from py_common.pipeline import Pipeline
from py_common.runner import handle_upload, parse_upload

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


class TestSynthesisedNames:
    """M04: a literal "col_A" heading keeps its name."""

    def test_blank_heading_never_takes_a_written_name(self) -> None:
        wb = Workbook()
        ws = wb.active
        ws.append([None, "col_A"])
        ws.append(["x", "y"])

        names, rows = read_sheet(ws, 1)

        assert names == ["col_A_2", "col_A"]
        assert rows == [(2, ("x", "y"))]


class TestMalformedWorkbook:
    """M05: a workbook that fails mid-read is quarantined, raw kept."""

    def test_lazy_parse_error_is_quarantined_with_raw_copy(self) -> None:
        bad = _with_cell_value(
            _book([["name", "n"], ["a", 1]]),
            b"<v>1</v>",
            b"<v>not-a-number</v>",
        )
        pipeline, objects = _pipeline(bad, _contract())

        result = pipeline.process(ITEM)

        assert result.outcome == Outcome.QUARANTINED, result.errors
        assert any(k.startswith("raw/") for k in objects)
        assert "Cannot read" in result.errors[0]


class TestUploadShape:
    """M07: a notification without a string name is ignored, not a crash."""

    @pytest.mark.parametrize("name", [None, 7, ["x"]])
    def test_bad_name_is_rejected(self, name: Any) -> None:
        payload = {"bucket": "b", "name": name}
        data = {
            "message": {
                "data": base64.b64encode(
                    json.dumps(payload).encode()
                ).decode(),
                "attributes": {"eventType": "OBJECT_FINALIZE"},
            }
        }

        with pytest.raises(ValueError, match="name"):
            parse_upload(data)
        assert handle_upload(data, {"": Mock()}) == []


class TestGraphNextLink:
    """M12: the bearer token never goes to a host other than Graph."""

    def test_foreign_next_link_is_refused(self) -> None:
        from py_common.adapters.sharepoint import SharePointSource

        source = SharePointSource.__new__(SharePointSource)
        source._graph_url = "https://graph.microsoft.com/v1.0"
        pages = [
            {"value": [{"a": 1}], "@odata.nextLink": "https://evil.example/x"}
        ]
        source._get_json = Mock(side_effect=pages)  # type: ignore
        source._path = lambda url: url  # type: ignore

        with pytest.raises(ServiceError, match="outside"):
            source._get_all("https://graph.microsoft.com/v1.0/a")
        assert source._get_json.call_count == 1


class TestFailureSeverity:
    """H06: alerts match structured outcomes at the right severity."""

    @pytest.mark.parametrize(
        ("outcome", "severity"),
        [
            (Outcome.FAILED, "ERROR"),
            (Outcome.QUARANTINED, "WARNING"),
            (Outcome.LOADED, "INFO"),
        ],
    )
    def test_summary_line_severity(
        self, outcome: Outcome, severity: str
    ) -> None:
        from py_common.model import Result

        stream = io.StringIO()
        root = logging.getLogger()
        before = list(root.handlers), root.level
        configure_json_logging(stream=stream)
        try:
            Pipeline._log_summary(Result(ITEM, outcome, "c", 0, [], 0.1))
        finally:
            root.handlers[:], root.level = before

        [line] = [json.loads(x) for x in stream.getvalue().splitlines()]
        assert line["severity"] == severity
        assert line["outcome"] == outcome.value
