"""Structured logs for Cloud Logging.

Every line a run writes carries the run ID and warehouse, every line
about one file carries that file's identity, version and checksum,
BigQuery job IDs are logged at INFO, and each file ends with one
summary line.
"""

import ast
import io
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from openpyxl import Workbook

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.contract import Column, Contract, Worksheet
from py_common.logging import JsonFormatter, configure_json_logging
from py_common.model import Outcome, SourceItem
from py_common.pipeline import Pipeline


def _workbook() -> bytes:
    """Build a one-row Events workbook.

    Returns:
        The .xlsx bytes.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["name"])
    ws.append(["a"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _run() -> None:
    """One raw-route run of one file against the fake BigQuery."""
    with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
        cls.return_value = FakeBigQueryClient()
        warehouse = RawJsonWarehouse("p", "d", {"Events": "raw_events"})
    source = Mock()
    source.items.return_value = [
        SourceItem("e.xlsx", "landing/e.xlsx", "17", 1)
    ]
    source.download.return_value = _workbook()
    store = MagicMock()
    store.get.return_value = None
    contract = Contract(
        worksheets=[Worksheet("Events", columns=[Column("name", "string")])]
    )
    Pipeline(source, store, warehouse, contract, "raw_json").run()


def _capture() -> list[dict[str, Any]]:
    """JSON lines logged during one run, parsed."""
    stream = io.StringIO()
    root = logging.getLogger()
    before = list(root.handlers), root.level
    configure_json_logging(stream=stream)
    try:
        _run()
    finally:
        root.handlers[:], root.level = before
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_every_line_has_severity_message_run_id_and_warehouse() -> None:
    """Every line has a severity, a message, the run ID and warehouse."""
    logged = _capture()

    assert logged
    assert len({line.get("run_id") for line in logged} - {None}) == 1
    for line in logged:
        assert line["severity"] in {"DEBUG", "INFO", "WARNING", "ERROR"}
        assert isinstance(line["message"], str)
        assert line["run_id"]
        assert line["warehouse"] == "p.d.raw_events"


def test_file_lines_carry_identity_version_and_checksum() -> None:
    """Lines about the file carry its identity, version and checksum."""
    logged = _capture()

    about_file = [line for line in logged if "item_identity" in line]
    assert about_file
    for line in about_file:
        assert line["item_identity"] == "landing/e.xlsx"
        assert line["version"] == "17"
    assert any(len(line.get("checksum", "")) == 64 for line in about_file)


def test_bigquery_job_ids_are_logged_at_info() -> None:
    """BigQuery job IDs are logged at INFO, against the file."""
    logged = _capture()

    jobs = [line for line in logged if "bq_job_id" in line]
    assert jobs
    assert all(line["severity"] == "INFO" for line in jobs)
    assert all(line["item_identity"] == "landing/e.xlsx" for line in jobs)


def test_one_summary_line_per_file() -> None:
    """Each file ends with one summary line giving outcome and rows."""
    logged = _capture()

    summaries = [line for line in logged if "outcome" in line]
    assert len(summaries) == 1
    [summary] = summaries
    assert summary["outcome"] == "loaded"
    assert summary["rows"] == 1
    assert summary["item_identity"] == "landing/e.xlsx"
    assert summary["seconds"] >= 0


def test_exceptions_are_included() -> None:
    """A logged exception's traceback is in the JSON line."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("x").makeRecord(
            "x", logging.ERROR, "f", 1, "failed", None, sys.exc_info()
        )

    line = json.loads(JsonFormatter().format(record))

    assert line["severity"] == "ERROR"
    assert line["message"] == "failed"
    assert "ValueError: boom" in line["exception"]


def test_configure_twice_installs_one_handler() -> None:
    """Configuring twice still writes each line once."""
    root = logging.getLogger()
    before = list(root.handlers), root.level
    try:
        configure_json_logging(stream=io.StringIO())
        configure_json_logging(stream=io.StringIO())
        ours = [
            h for h in root.handlers if isinstance(h.formatter, JsonFormatter)
        ]
        assert len(ours) == 1
    finally:
        root.handlers[:], root.level = before


class TestJsonLoggingReplacesHandlers:
    """Each line is written once, as JSON, unless asked otherwise."""

    @pytest.fixture(autouse=True)
    def restore_root(self) -> Any:
        """Put the root logger's handlers and level back after the test.

        Yields:
            Nothing.
        """
        root = logging.getLogger()
        saved = list(root.handlers), root.level
        yield
        root.handlers[:], root.level = saved

    @pytest.fixture
    def plain(self) -> logging.Handler:
        """A plain-text handler, as logging.basicConfig() would add.

        Added directly: under pytest the root logger already has
        pytest's capture handlers, so basicConfig() would do nothing.
        """
        handler = logging.StreamHandler(io.StringIO())
        logging.getLogger().addHandler(handler)
        return handler

    def test_existing_handlers_are_replaced(
        self, plain: logging.Handler
    ) -> None:
        """Handlers already on the root logger are replaced.

        Args:
            plain: A handler installed before the test.
        """
        configure_json_logging(stream=io.StringIO())

        handlers = logging.getLogger().handlers
        assert plain not in handlers
        assert [type(h.formatter) for h in handlers] == [JsonFormatter]

    def test_replace_false_keeps_them(self, plain: logging.Handler) -> None:
        """replace=False keeps existing handlers and adds one of ours.

        Args:
            plain: A handler installed before the test.
        """
        configure_json_logging(stream=io.StringIO(), replace=False)
        configure_json_logging(stream=io.StringIO(), replace=False)

        handlers = logging.getLogger().handlers
        assert plain in handlers
        assert [
            h for h in handlers if isinstance(h.formatter, JsonFormatter)
        ] == [handlers[-1]]


ITEM = SourceItem("f.xlsx", "f.xlsx", "1", 1)


class TestFailureSeverity:
    """Alerts match structured outcomes at the right severity."""

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
        """Each outcome's summary line has the severity alerts look for.

        Args:
            outcome: The file's outcome.
            severity: The severity expected.
        """
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


def test_smoke_script_enables_json_logging() -> None:
    """The alert test recipe needs JSON log lines."""
    tree = ast.parse(Path("scripts/demo.py").read_text(encoding="utf-8"))
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "configure_json_logging" in calls
