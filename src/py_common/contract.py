"""Workbook contract validation.

A contract specifies required worksheets, columns, data types,
and business rules. Validation is deterministic and repeatable.
"""

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Any, Literal

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
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
        header_row: Row holding the column headings (1 = first row).
            Data starts on the row after it. Sheets laid out for
            people often put a title or "Last updated" line above
            the headings.
    """

    name: str
    columns: list[Column] = field(default_factory=list)
    header_row: int = 1

    def __post_init__(self) -> None:
        """Check settings that would otherwise fail confusingly later.

        Raises:
            ValueError: header_row is less than 1.
        """
        if self.header_row < 1:
            raise ValueError(
                f"Worksheet '{self.name}': header_row must be 1 or "
                f"more, got {self.header_row}"
            )


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
            Number of non-empty data rows (below the heading row) if
            valid.

        Raises:
            ContractError: Column structure doesn't match ws_def.
            ValidationError: Data violates ws_def's column rules.
        """
        errors: list[str] = []
        header = f"{source_name} [{ws_def.name}]"

        headers, rows = read_sheet(ws, ws_def.header_row)
        for col_def in ws_def.columns:
            if col_def.name not in headers:
                errors.append(f"Missing column '{col_def.name}'")

        expected = {col.name for col in ws_def.columns}
        # read_sheet() has already turned every heading into text
        # (headings can be numbers or dates too), so they compare and
        # sort cleanly here.
        unexpected = set(headers) - expected
        if unexpected:
            errors.append(
                f"Unexpected columns: {', '.join(sorted(unexpected))}"
            )

        if errors:
            raise ContractError(f"{header}: {'; '.join(errors)}")

        # Column position in the sheet may not match ws_def.columns'
        # order, so look up each column's index by its header name
        # rather than assuming the two lists are aligned.
        header_index = {h: i for i, h in enumerate(headers)}

        row_errors: list[str] = []
        seen_values: dict[str, set[Any]] = {
            col.name: set() for col in ws_def.columns if col.unique
        }

        for row_idx, row in rows:
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

        return len(rows)


def read_sheet(
    ws: XlWorksheet, header_row: int
) -> tuple[list[str], list[tuple[int, tuple[Any, ...]]]]:
    """Read a worksheet's column names and the data rows below them.

    Headings are turned into names that are always present and
    unique, so no column is lost on the way to the warehouse:

    - a heading cell is used as text, exactly as written (no
      trimming, so contracts must match it exactly);
    - a blank heading over a column that holds data is named after
      its column letter, e.g. "col_B";
    - a blank heading over an empty column is ignored, as it is only
      formatting;
    - a repeated heading keeps its name the first time and gets
      "_2", "_3", ... after that, left to right, skipping any name
      the sheet already uses.

    Rows with no values at all are skipped.

    Args:
        ws: The openpyxl worksheet to read.
        header_row: Row holding the headings (1 = first row).

    Returns:
        (names, rows): the column names, and each data row as
        (Excel row number, values lined up with names). Row numbers
        are the ones shown in Excel, so errors can be traced back to
        the sheet.
    """
    grid = list(ws.iter_rows(min_row=header_row, values_only=True))
    if not grid:
        return [], []
    headings = list(grid[0])
    data = [
        (header_row + offset, values)
        for offset, values in enumerate(grid[1:], start=1)
        if not all(_is_blank(value) for value in values)
    ]

    # Reserve every heading actually written in the sheet, so a
    # renamed repeat ("Other" -> "Other_2") never takes a name that
    # a later heading uses itself.
    written = {str(h) for h in headings if not _is_blank(h)}
    names: list[str] = []
    keep: list[int] = []
    for index, heading in enumerate(headings):
        if _is_blank(heading):
            has_data = any(
                index < len(values) and not _is_blank(values[index])
                for _, values in data
            )
            if not has_data:
                continue
            base = f"col_{get_column_letter(index + 1)}"
        else:
            base = str(heading)
        name = base
        suffix = 2
        while name in names or (name != base and name in written):
            name = f"{base}_{suffix}"
            suffix += 1
        names.append(name)
        keep.append(index)

    rows = [
        (
            row_number,
            tuple(values[i] if i < len(values) else None for i in keep),
        )
        for row_number, values in data
    ]
    return names, rows


def _is_blank(value: Any) -> bool:
    """Whether a cell holds nothing (empty, or only whitespace).

    Args:
        value: Cell value from openpyxl.

    Returns:
        True for None or a whitespace-only string.
    """
    return value is None or (isinstance(value, str) and not value.strip())
