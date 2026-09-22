"""Tests for workbook contract validation."""

from datetime import datetime
from io import BytesIO
from typing import Any

import pytest
from openpyxl import Workbook

from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ContractError, ValidationError
from py_common.fixtures import make_fixtures


def _build_workbook(
    sheets: dict[str, tuple[list[str], list[list[Any]]]],
) -> bytes:
    """Build minimal .xlsx bytes with one or more named worksheets.

    Args:
        sheets: Maps worksheet name to (headers, data rows). The
            first entry becomes the workbook's active sheet, so
            tests can put a *different* sheet first than the
            contract expects, to prove active-sheet order is
            irrelevant to validation.

    Returns:
        Raw .xlsx file bytes.
    """
    wb = Workbook()
    wb.remove(wb.active)
    for name, (headers, rows) in sheets.items():
        ws = wb.create_sheet(title=name)
        for col_idx, header in enumerate(headers, start=1):
            ws.cell(row=1, column=col_idx, value=header)
        for row_idx, row in enumerate(rows, start=2):
            for col_idx, value in enumerate(row, start=1):
                ws.cell(row=row_idx, column=col_idx, value=value)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_valid_fixture(temp_dir, events_contract):
    """Valid fixture passes all validations."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (temp_dir / "fixtures" / "events_valid.xlsx").read_bytes()

    rows = events_contract.validate(excel_bytes, "events_valid.xlsx")

    assert rows == 2


def test_missing_worksheet(temp_dir, events_contract):
    """Missing worksheet raises ContractError."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (
        temp_dir / "fixtures" / "events_missing_sheet.xlsx"
    ).read_bytes()

    with pytest.raises(ContractError, match="Missing worksheet"):
        events_contract.validate(
            excel_bytes,
            "events_missing_sheet.xlsx",
        )


def test_missing_column(temp_dir, events_contract):
    """Missing required column raises ContractError."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (
        temp_dir / "fixtures" / "events_missing_column.xlsx"
    ).read_bytes()

    with pytest.raises(ContractError, match="Missing column"):
        events_contract.validate(
            excel_bytes,
            "events_missing_column.xlsx",
        )


def test_extra_column(temp_dir, events_contract):
    """Extra unexpected column raises ContractError."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (
        temp_dir / "fixtures" / "events_extra_column.xlsx"
    ).read_bytes()

    with pytest.raises(ContractError, match="Unexpected columns"):
        events_contract.validate(
            excel_bytes,
            "events_extra_column.xlsx",
        )


def test_extra_columns_with_mixed_type_headers():
    """Multiple unexpected columns of different cell types don't crash.

    Regression test: 'unexpected' column names come straight from
    Excel header cells, which can hold different types (text,
    numbers, dates, ...). The old code passed those raw values
    straight to sorted(), which raises TypeError at runtime when
    asked to compare, e.g., a str header against an int header.
    Column names are stringified before comparing/sorting to avoid
    that crash and report a stable, readable error instead.
    """
    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[Column(name="event_id", data_type="string")],
            ),
        ]
    )
    excel_bytes = _build_workbook(
        {
            "Events": (
                ["event_id", "notes", 42],
                [["EVT001", "text header", "numeric header"]],
            ),
        }
    )

    with pytest.raises(ContractError, match="Unexpected columns"):
        contract.validate(excel_bytes, "mixed_headers.xlsx")


def test_invalid_data_type(temp_dir, events_contract):
    """Invalid data type raises ValidationError."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (
        temp_dir / "fixtures" / "events_invalid_type.xlsx"
    ).read_bytes()

    with pytest.raises(ValidationError, match="expected integer"):
        events_contract.validate(
            excel_bytes,
            "events_invalid_type.xlsx",
        )


def test_missing_required_value(temp_dir, events_contract):
    """Null in required column raises ValidationError."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (
        temp_dir / "fixtures" / "events_missing_value.xlsx"
    ).read_bytes()

    with pytest.raises(ValidationError, match="is required"):
        events_contract.validate(
            excel_bytes,
            "events_missing_value.xlsx",
        )


def test_duplicate_in_unique_column(temp_dir, events_contract):
    """Duplicate in unique column raises ValidationError."""
    make_fixtures(temp_dir / "fixtures")
    excel_bytes = (
        temp_dir / "fixtures" / "events_duplicate.xlsx"
    ).read_bytes()

    with pytest.raises(ValidationError, match="Duplicate value"):
        events_contract.validate(
            excel_bytes,
            "events_duplicate.xlsx",
        )


def test_malformed_excel(temp_dir, events_contract):
    """Malformed Excel file raises ContractError."""
    bad_excel = b"not a zip file"

    with pytest.raises(ContractError, match="Cannot parse Excel"):
        events_contract.validate(bad_excel, "bad.xlsx")


def test_empty_worksheets_raises_contract_error():
    """A Contract with no configured worksheets is a config error.

    Regression test: the old code did `self.worksheets[0]` and would
    raise an opaque IndexError instead of a clear ContractError.
    """
    contract = Contract(worksheets=[])
    excel_bytes = _build_workbook({"Sheet1": (["a"], [[1]])})

    with pytest.raises(ContractError, match="no worksheets configured"):
        contract.validate(excel_bytes, "empty.xlsx")


def test_multiple_worksheets_validated_against_matching_definitions():
    """Every configured worksheet is checked against its own schema.

    Fix for issue #5: the old code only ever validated
    self.worksheets[0]/wb.active, so a second worksheet's columns
    were never checked at all.
    """
    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[Column(name="event_id", data_type="string")],
            ),
            Worksheet(
                name="Employers",
                columns=[Column(name="employer_id", data_type="string")],
            ),
        ]
    )
    excel_bytes = _build_workbook(
        {
            "Events": (["event_id"], [["EVT001"]]),
            # Wrong column name in the second sheet - must be caught.
            "Employers": (["wrong_column"], [["EMP001"]]),
        }
    )

    with pytest.raises(ContractError, match="Missing column 'employer_id'"):
        contract.validate(excel_bytes, "multi.xlsx")


def test_active_sheet_order_has_no_effect():
    """Validation targets worksheets by name, never wb.active.

    Fix for issue #5: puts a non-contract sheet first (making it
    openpyxl's active sheet by default) and the contract's primary
    sheet second, then confirms the *contract's* first worksheet
    ("Events") is what gets validated and counted - not whichever
    sheet happens to be active in the workbook.
    """
    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[Column(name="event_id", data_type="string")],
            ),
        ]
    )
    excel_bytes = _build_workbook(
        {
            # "Summary" is first, so openpyxl makes it wb.active.
            "Summary": (["note"], [["irrelevant"]]),
            "Events": (
                ["event_id"],
                [["EVT001"], ["EVT002"], ["EVT003"]],
            ),
        }
    )

    rows = contract.validate(excel_bytes, "order.xlsx")

    assert rows == 3


def test_string_type_rejects_non_string_value():
    """A non-string value in a 'string' column is a ValidationError.

    Fix for issue #6: Contract.validate() previously only checked
    integer/float columns, so 'string' silently accepted anything.
    """
    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[Column(name="event_id", data_type="string")],
            ),
        ]
    )
    excel_bytes = _build_workbook({"Events": (["event_id"], [[12345]])})

    with pytest.raises(ValidationError, match="expected text"):
        contract.validate(excel_bytes, "bad_string.xlsx")


def test_date_type_validates_value():
    """Date columns are validated: wrong type rejected, real date ok.

    Fix for issue #6: 'date' was never checked before.
    """
    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[Column(name="held_on", data_type="date")],
            ),
        ]
    )

    bad_bytes = _build_workbook({"Events": (["held_on"], [["not-a-date"]])})
    with pytest.raises(ValidationError, match="expected date"):
        contract.validate(bad_bytes, "bad_date.xlsx")

    good_bytes = _build_workbook(
        {"Events": (["held_on"], [[datetime(2026, 1, 1)]])}
    )
    rows = contract.validate(good_bytes, "good_date.xlsx")
    assert rows == 1


def test_bool_rejected_by_integer_column():
    """A Python bool does not satisfy an 'integer' column.

    Fix for issue #6: bool is a subclass of int in Python, so
    isinstance(True, int) is True. Without an explicit exclusion,
    True/False would silently pass an integer field.
    """
    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[Column(name="attendees", data_type="integer")],
            ),
        ]
    )
    excel_bytes = _build_workbook({"Events": (["attendees"], [[True]])})

    with pytest.raises(ValidationError, match="expected integer"):
        contract.validate(excel_bytes, "bool.xlsx")
