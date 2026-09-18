"""Tests for workbook contract validation."""

import pytest

from py_common.errors import ContractError, ValidationError
from py_common.fixtures import make_fixtures


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
