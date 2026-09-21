"""Workbook contract validation.

A contract specifies required worksheets, columns, data types,
and business rules. Validation is deterministic and repeatable.
"""

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Any, Literal

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet as XlWorksheet

from py_common.errors import ContractError, ValidationError

# Maps a Column.data_type to the Python type(s) a cell value must be
# an instance of, and the word used in error messages. bool is
# excluded everywhere because Python's bool is a subclass of int and
# would otherwise silently pass "integer"/"float" columns.
_TYPE_CHECKS: dict[str, tuple[type | tuple[type, ...], str]] = {
    "integer": (int, "integer"),
    "float": ((int, float), "number"),
    "string": (str, "text"),
    # datetime.datetime is a subclass of datetime.date, so this
    # covers both date-only and date+time cell values.
    "date": (date, "date"),
}


@dataclass
class Column:
    """Column definition within a worksheet.

    Attributes:
        name: Column header (must match worksheet exactly).
        data_type: Expected type: 'string', 'integer', 'float', 'date'.
        nullable: Whether column may contain null/empty values.
        unique: Whether all values must be distinct (no duplicates).
    """

    name: str
    data_type: Literal["string", "integer", "float", "date"]
    nullable: bool = False
    unique: bool = False


@dataclass
class Worksheet:
    """Worksheet definition within a workbook.

    Attributes:
        name: Worksheet name (must match Excel sheet name).
        columns: List of required columns.
    """

    name: str
    columns: list[Column] = field(default_factory=list)


@dataclass
class Contract:
    """Workbook schema and validation rules.

    Attributes:
        key_columns: Column names used for deduplication
                     on warehouse merge. Empty means append.
        worksheets: List of required worksheets.
    """

    key_columns: list[str] = field(default_factory=list)
    worksheets: list[Worksheet] = field(default_factory=list)

    def validate(
        self,
        excel_bytes: bytes,
        source_name: str,
    ) -> int:
        """Validate workbook against this contract.

        Every worksheet in `self.worksheets` is validated against its
        matching sheet, looked up by name — never the workbook's
        active sheet, which has no relation to what the contract
        expects and can silently differ from it.

        Args:
            excel_bytes: Raw Excel file bytes.
            source_name: Filename for error messages.

        Returns:
            Row count of the first configured worksheet if valid.

        Raises:
            ContractError: Contract is misconfigured, or worksheet
                structure doesn't match it.
            ValidationError: Data violates contract rules.
        """
        if not self.worksheets:
            raise ContractError(
                f"{source_name}: Contract has no worksheets configured"
            )

        try:
            wb = load_workbook(
                # file-like object:
                filename=BytesIO(excel_bytes),
                data_only=True,
                read_only=False,
            )
        except Exception as e:
            raise ContractError(
                f"{source_name}: Cannot parse Excel: {e}"
            ) from e

        # Check every configured worksheet exists before validating
        # any of them, so one missing sheet reports cleanly instead
        # of a KeyError on the next lookup.
        missing = [
            ws_def.name
            for ws_def in self.worksheets
            if ws_def.name not in wb.sheetnames
        ]
        if missing:
            raise ContractError(
                f"{source_name}: Missing worksheet(s) "
                f"{', '.join(missing)}. Found: {', '.join(wb.sheetnames)}"
            )

        primary_rows = 0
        for index, ws_def in enumerate(self.worksheets):
            ws = wb[ws_def.name]
            rows = self._validate_worksheet(ws, ws_def, source_name)
            if index == 0:
                primary_rows = rows

        return primary_rows

    def _validate_worksheet(
        self,
        ws: XlWorksheet,
        ws_def: Worksheet,
        source_name: str,
    ) -> int:
        """Validate one worksheet against its Worksheet definition.

        Args:
            ws: The openpyxl worksheet to validate.
            ws_def: Contract definition for this worksheet.
            source_name: Filename for error messages.

        Returns:
            Row count (excluding header) if valid.

        Raises:
            ContractError: Column structure doesn't match ws_def.
            ValidationError: Data violates ws_def's column rules.
        """
        errors: list[str] = []
        header = f"{source_name} [{ws_def.name}]"

        headers = [cell.value for cell in ws[1]]
        for col_def in ws_def.columns:
            if col_def.name not in headers:
                errors.append(f"Missing column '{col_def.name}'")

        expected = {col.name for col in ws_def.columns}
        actual = {h for h in headers if h}
        unexpected = actual - expected
        if unexpected:
            errors.append(
                f"Unexpected columns: {', '.join(sorted(unexpected))}"
            )

        if errors:
            raise ContractError(f"{header}: {'; '.join(errors)}")

        # Column position in the sheet may not match ws_def.columns'
        # order, so look up each column's index by its header name
        # rather than assuming the two lists are aligned.
        header_index = {h: i for i, h in enumerate(headers) if h is not None}

        row_errors: list[str] = []
        seen_values: dict[str, set[Any]] = {
            col.name: set() for col in ws_def.columns if col.unique
        }

        for row_idx, row in enumerate(
            ws.iter_rows(min_row=2, values_only=True), start=2
        ):
            for col_def in ws_def.columns:
                col_idx = header_index.get(col_def.name)
                value = (
                    row[col_idx]
                    if col_idx is not None and col_idx < len(row)
                    else None
                )

                # Check nullable
                if value is None and not col_def.nullable:
                    row_errors.append(
                        f"Row {row_idx}: '{col_def.name}' is required"
                    )
                    continue

                if value is not None:
                    expected_type, label = _TYPE_CHECKS[col_def.data_type]
                    valid = isinstance(
                        value, expected_type
                    ) and not isinstance(value, bool)
                    if not valid:
                        row_errors.append(
                            f"Row {row_idx}: '{col_def.name}' "
                            f"is {type(value).__name__}, "
                            f"expected {label}"
                        )

                    # Check uniqueness
                    if col_def.unique:
                        if value in seen_values[col_def.name]:
                            row_errors.append(
                                f"Row {row_idx}: Duplicate value "
                                f"in unique column '{col_def.name}'"
                            )
                        seen_values[col_def.name].add(value)

        if row_errors:
            raise ValidationError(
                f"{header}: {'; '.join(row_errors[:5])}"
                + (
                    f"... and {len(row_errors) - 5} more"
                    if len(row_errors) > 5
                    else ""
                )
            )

        # Return row count (excluding header)
        return ws.max_row - 1 if ws.max_row else 0
