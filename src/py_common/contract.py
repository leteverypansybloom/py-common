"""Workbook contract validation.

A contract specifies required worksheets, columns, data types,
and business rules. Validation is deterministic and repeatable.
"""

from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Literal

from openpyxl import load_workbook

from py_common.errors import ContractError, ValidationError


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

        Args:
            excel_bytes: Raw Excel file bytes.
            source_name: Filename for error messages.

        Returns:
            Number of rows in first worksheet if valid.

        Raises:
            ContractError: Worksheet structure doesn't match contract.
            ValidationError: Data violates contract rules.
        """
        errors: list[str] = []

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

        # Check worksheets exist
        for ws_def in self.worksheets:
            if ws_def.name not in wb.sheetnames:
                errors.append(
                    f"Missing worksheet '{ws_def.name}'. "
                    f"Found: {', '.join(wb.sheetnames)}"
                )

        if errors:
            raise ContractError(f"{source_name}: {'; '.join(errors)}")

        # Validate first worksheet
        ws = wb.active
        if not ws:
            raise ContractError(f"{source_name}: No active worksheet")

        ws_def = self.worksheets[0]

        # Check columns
        headers = [cell.value for cell in ws[1]]
        for col_def in ws_def.columns:
            if col_def.name not in headers:
                errors.append(f"Missing column '{col_def.name}'")

        # Check for unexpected columns
        expected = {col.name for col in ws_def.columns}
        actual = {h for h in headers if h}
        unexpected = actual - expected
        if unexpected:
            errors.append(
                f"Unexpected columns: {', '.join(sorted(unexpected))}"
            )

        if errors:
            raise ContractError(f"{source_name}: {'; '.join(errors)}")

        # Validate data rows
        row_errors: list[str] = []
        seen_values: dict[str, set[Any]] = {
            col.name: set() for col in ws_def.columns if col.unique
        }

        for row_idx, row in enumerate(
            ws.iter_rows(min_row=2, values_only=True), start=2
        ):
            for col_def in ws_def.columns:
                col_idx = [
                    i
                    for i, c in enumerate(ws_def.columns)
                    if c.name == col_def.name
                ][0]
                value = row[col_idx] if col_idx < len(row) else None

                # Check nullable
                if value is None and not col_def.nullable:
                    row_errors.append(
                        f"Row {row_idx}: '{col_def.name}' is required"
                    )
                    continue

                # Check type
                if value is not None:
                    if col_def.data_type == "integer":
                        if not isinstance(value, int):
                            row_errors.append(
                                f"Row {row_idx}: '{col_def.name}' "
                                f"is {type(value).__name__}, "
                                f"expected integer"
                            )
                    elif col_def.data_type == "float":
                        if not isinstance(value, (int, float)):
                            row_errors.append(
                                f"Row {row_idx}: '{col_def.name}' "
                                f"is {type(value).__name__}, "
                                f"expected number"
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
                f"{source_name}: {'; '.join(row_errors[:5])}"
                + (
                    f"... and {len(row_errors) - 5} more"
                    if len(row_errors) > 5
                    else ""
                )
            )

        # Return row count (excluding header)
        return ws.max_row - 1 if ws.max_row else 0
