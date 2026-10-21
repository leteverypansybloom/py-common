"""Unit tests for the raw JSON conversion (py_common.raw).

No cloud services or mocks: the conversion is pure, so tests build
SheetRows directly and read the NDJSON back with json.loads.
"""

import json
from datetime import date, datetime, time, timedelta

import pytest

from py_common.contract import SheetRows
from py_common.model import SourceItem
from py_common.raw import ROW_FIELDS, json_value, to_ndjson


@pytest.fixture
def item() -> SourceItem:
    """A monthly upload of the WAS export at generation 1712."""
    return SourceItem(
        name="was.xlsx",
        identity="landing/hww/was.xlsx",
        version="1712",
        size_bytes=10,
    )


def _lines(data: bytes) -> list[dict]:
    """Parse NDJSON bytes into one dict per line.

    Args:
        data: NDJSON bytes.

    Returns:
        Parsed records, in order.
    """
    return [json.loads(line) for line in data.decode().splitlines()]


class TestJsonValue:
    """json_value() keeps Excel values as close as JSON allows."""

    @pytest.mark.parametrize(
        "value", [None, "Org 1", 12, 0.25, True, False, ""]
    )
    def test_json_native_values_are_unchanged(self, value):
        """Text, numbers, booleans and None pass straight through.

        Args:
            value: A value JSON can already hold.
        """
        assert json_value(value) == value

    def test_datetime_is_iso_without_time_zone(self):
        """Excel stores no time zone, so none is invented."""
        assert (
            json_value(datetime(2025, 12, 16, 11, 46)) == "2025-12-16T11:46:00"
        )

    def test_date_is_iso_date(self):
        """A date-only value has no time part."""
        assert json_value(date(2025, 11, 17)) == "2025-11-17"

    def test_time_is_iso_time(self):
        """A time-only value has no date part."""
        assert json_value(time(9, 30)) == "09:30:00"

    def test_duration_is_iso_seconds(self):
        """Durations become ISO 8601 seconds."""
        assert json_value(timedelta(hours=1, minutes=30)) == "PT5400S"

    def test_fractional_and_negative_durations(self):
        """Part seconds and negative durations stay exact."""
        assert json_value(timedelta(seconds=1.5)) == "PT1.5S"
        assert json_value(timedelta(seconds=-60)) == "-PT60S"

    def test_non_finite_float_becomes_null(self):
        """NaN isn't valid JSON, so it is written as null."""
        assert json_value(float("nan")) is None

    def test_other_values_become_text(self):
        """Anything JSON can't hold is written as text."""
        assert json_value(b"abc") == "b'abc'"


class TestToNdjson:
    """to_ndjson() writes one JSON line per row of every sheet."""

    def test_each_row_has_source_columns_and_payload(self, item):
        """Lines carry where the row came from, plus its values.

        Args:
            item: SourceItem fixture.
        """
        sheets = {
            "query": SheetRows(
                columns=["Participant ID", "Date of Enquiry"],
                rows=[(2, (1, datetime(2025, 11, 17)))],
            )
        }

        data, rows = to_ndjson(sheets, item, "abc123")

        assert rows == 1
        assert _lines(data) == [
            {
                "source_file": "was.xlsx",
                "source_generation": "1712",
                "sheet_name": "query",
                "row_number": 2,
                "checksum": "abc123",
                "payload": {
                    "Participant ID": 1,
                    "Date of Enquiry": "2025-11-17T00:00:00",
                },
            }
        ]

    def test_fields_follow_row_fields_order(self, item):
        """Keys are in ROW_FIELDS order, so output is predictable.

        Args:
            item: SourceItem fixture.
        """
        sheets = {"query": SheetRows(columns=["a"], rows=[(2, (1,))])}

        data, _ = to_ndjson(sheets, item, "abc123")

        assert tuple(_lines(data)[0]) == ROW_FIELDS

    def test_every_sheet_is_written_in_order(self, item):
        """Rows from all sheets go into one file, sheet by sheet.

        Args:
            item: SourceItem fixture.
        """
        sheets = {
            "Account signups": SheetRows(
                columns=["Org"], rows=[(3, ("Org 1",)), (4, ("Org 2",))]
            ),
            "Survey info": SheetRows(columns=["Org"], rows=[(3, ("Org 3",))]),
        }

        data, rows = to_ndjson(sheets, item, "abc123")

        assert rows == 3
        assert [(r["sheet_name"], r["row_number"]) for r in _lines(data)] == [
            ("Account signups", 3),
            ("Account signups", 4),
            ("Survey info", 3),
        ]

    def test_empty_cells_are_null_not_missing(self, item):
        """Every selected column is present, so the schema is visible.

        Args:
            item: SourceItem fixture.
        """
        sheets = {
            "query": SheetRows(columns=["a", "b"], rows=[(2, (1, None))])
        }

        data, _ = to_ndjson(sheets, item, "abc123")

        assert _lines(data)[0]["payload"] == {"a": 1, "b": None}

    def test_payload_keeps_column_order(self, item):
        """Payload keys are in SheetRows' column order.

        Args:
            item: SourceItem fixture.
        """
        sheets = {
            "query": SheetRows(columns=["z", "a", "m"], rows=[(2, (1, 2, 3))])
        }

        data, _ = to_ndjson(sheets, item, "abc123")

        assert list(_lines(data)[0]["payload"]) == ["z", "a", "m"]

    def test_non_ascii_text_is_kept_readable(self, item):
        """Welsh text is written as UTF-8, not as escaped code points.

        Args:
            item: SourceItem fixture.
        """
        sheets = {"query": SheetRows(columns=["Enw"], rows=[(2, ("Iâl",))])}

        data, _ = to_ndjson(sheets, item, "abc123")

        assert "Iâl" in data.decode("utf-8")

    def test_output_is_identical_for_identical_input(self, item):
        """No timestamps: a retry writes the same bytes.

        Args:
            item: SourceItem fixture.
        """
        sheets = {"query": SheetRows(columns=["a"], rows=[(2, (1,))])}

        assert to_ndjson(sheets, item, "c") == to_ndjson(sheets, item, "c")

    def test_no_rows_gives_empty_output(self, item):
        """A sheet with only headings writes nothing.

        Args:
            item: SourceItem fixture.
        """
        sheets = {"query": SheetRows(columns=["a"], rows=[])}

        assert to_ndjson(sheets, item, "abc123") == (b"", 0)
