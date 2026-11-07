"""Workbook contract validation.

A contract specifies required worksheets, columns, data types,
and business rules. Validation is deterministic and repeatable.
The file can be an Excel workbook, or a CSV, JSON or JSON Lines file
holding one table (Contract.file_format).
"""

import csv
import json
import math
import posixpath
import re
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO, StringIO
from typing import IO, Any, Literal
from xml.etree import ElementTree

from openpyxl import Workbook, load_workbook
from openpyxl.chartsheet import Chartsheet
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet as XlWorksheet

from py_common.errors import ContractError, ValidationError

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

#: Default size limits for an uploaded workbook (see Contract). The
#: monthly HWW workbooks are well under 1 MB; these only stop a
#: pathological or malicious file before it is parsed.
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024

#: Default limits on the table a file unpacks to (see Contract). A file
#: can be tiny and still ask for a huge table, such as one cell in the
#: last column of every row, so its size alone doesn't bound memory.
DEFAULT_MAX_COLUMNS = 2_000
DEFAULT_MAX_CELLS = 5_000_000

#: The years a "date" column accepts. Outside them a date is a typing
#: slip or a placeholder such as .NET's 0001-01-01 or 9999-12-31.
_MIN_DATE_YEAR, _MAX_DATE_YEAR = 1900, 2100

#: What Excel shows for a formula that failed. Read for its values, such
#: a cell is this text, which would load as if it were real data. A CSV
#: file saved from Excel holds the same text.
_EXCEL_ERRORS = frozenset(
    {
        "#BLOCKED!",
        "#CALC!",
        "#DIV/0!",
        "#FIELD!",
        "#GETTING_DATA",
        "#N/A",
        "#NAME?",
        "#NULL!",
        "#NUM!",
        "#REF!",
        "#SPILL!",
        "#UNKNOWN!",
        "#VALUE!",
    }
)

#: What openpyxl and zipfile raise for a workbook they can't read.
#: Anything else (MemoryError, a bug) is not the file's fault, so it
#: isn't turned into a quarantine. defusedxml's refusals (entity
#: expansion, external entities) are ValueErrors. The bytes are already
#: in memory, so an OSError (no workbook part) comes from the file, and
#: so does a RuntimeError (an encrypted part) or NotImplementedError (a
#: compression method zipfile can't read), which is a RuntimeError.
_PARSE_ERRORS: tuple[type[BaseException], ...] = (
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    EOFError,
    OSError,
    RuntimeError,
    zipfile.BadZipFile,
    zlib.error,
    ElementTree.ParseError,
)

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

#: The file types a Contract reads, and the extensions (lower case)
#: each is uploaded with. Pipeline checks a source picks up only
#: extensions its contract's file_format reads.
FILE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "xlsx": (".xlsx", ".xlsm"),
    "csv": (".csv",),
    "json": (".json",),
    "jsonl": (".jsonl", ".ndjson"),
}

#: The error for a whole number BigQuery's INT64 can't hold (it would
#: otherwise load as NULL). Such a value isn't checked for uniqueness.
_OUT_OF_RANGE = "is outside the INT64 range"

# Text a CSV field must match to fit an "integer", "float" or "date"
# column. ASCII digits only: \d would also accept other scripts'
# digits, which BigQuery won't cast.
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_NUMBER_TEXT = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?")
_ISO_DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}([T ].+)?")

# What a CSV field must read as, for error messages.
_TEXT_LABELS = {
    "integer": "a whole number",
    "float": "a number",
    "date": "a date (YYYY-MM-DD)",
}


class _TableTooLarge(ValueError):
    """A file unpacks to more columns or cells than its contract allows."""


@dataclass
class Column:
    """Column definition within a worksheet.

    Attributes:
        name: Column header (must match worksheet exactly).
        data_type: Expected type: 'string', 'integer', 'float', 'date',
            or 'any' for a mixed or nested value.
        nullable: Whether column may contain null/empty values.
        unique: Whether all values must be distinct (no duplicates).
    """

    name: str
    data_type: Literal["string", "integer", "float", "date", "any"]
    nullable: bool = False
    unique: bool = False


@dataclass
class Worksheet:
    """Worksheet definition within a workbook.

    A CSV or JSON file holds one table, which its contract's one
    Worksheet describes.

    Attributes:
        name: Worksheet name (must match Excel sheet name). For a
            CSV or JSON file, any name for its table, e.g. "items":
            it is the raw rows' sheet_name and the key of the
            warehouse's table mapping.
        columns: List of required columns. In a JSON file a column
            is a key of the records' objects.
        header_row: Row holding the column headings (1 = first row).
            Data starts on the row after it. Sheets laid out for
            people often put a title or "Last updated" line above
            the headings. Excel and CSV only: a JSON file has no
            heading row, so it must be 1.
        column_selection: Which columns are loaded. "listed" (the
            default) loads only `columns`, and any other column in
            the sheet is a mismatch unless it is in `excluded`.
            "all_except" loads every column except `excluded`, so
            new columns are picked up without a contract change.
        excluded: Columns known to be in the sheet that are never
            loaded, e.g. names or email addresses the analysis
            doesn't need. Names are as read_sheet() gives them.
        on_mismatch: What a mismatch does. "quarantine" (the default)
            rejects the file, as before. "warn" records the problem
            as a warning and loads the file anyway: missing columns
            load as empty, and row-level problems (wrong type,
            missing required value, duplicate) are warnings too, left
            for the cleaning layer to deal with row by row.
        allow_empty: Whether a sheet with headings but no data rows
            is valid. False (the default) rejects the file whatever
            on_mismatch says: an empty upload usually means an export
            went wrong, and loading it would leave the previous
            upload's rows looking current. Set True for a sheet where
            "nothing this time" is genuinely possible.
    """

    name: str
    columns: list[Column] = field(default_factory=list)
    header_row: int = 1
    column_selection: Literal["listed", "all_except"] = "listed"
    excluded: list[str] = field(default_factory=list)
    on_mismatch: Literal["quarantine", "warn"] = "quarantine"
    allow_empty: bool = False

    def __post_init__(self) -> None:
        """Check settings that would otherwise fail confusingly later.

        Raises:
            ValueError: header_row is less than 1, column_selection
                or on_mismatch is unknown, or a column is both listed
                and excluded.
        """
        if self.on_mismatch not in ("quarantine", "warn"):
            raise ValueError(
                f"Worksheet '{self.name}': on_mismatch must be "
                f"'quarantine' or 'warn', got {self.on_mismatch!r}"
            )
        if self.header_row < 1:
            raise ValueError(
                f"Worksheet '{self.name}': header_row must be 1 or "
                f"more, got {self.header_row}"
            )
        if self.column_selection not in ("listed", "all_except"):
            raise ValueError(
                f"Worksheet '{self.name}': column_selection must be "
                f"'listed' or 'all_except', got "
                f"{self.column_selection!r}"
            )
        both = sorted({c.name for c in self.columns} & set(self.excluded))
        if both:
            raise ValueError(
                f"Worksheet '{self.name}': columns both listed and "
                f"excluded: {', '.join(both)}"
            )


@dataclass
class SheetRows:
    """The columns and rows to load from one worksheet.

    Attributes:
        columns: Names of the columns to load. In "listed" mode this
            is the contract's column order; in "all_except" mode it
            is the sheet's order.
        rows: (Excel row number, values lined up with `columns`) for
            each non-empty data row. A listed column missing from
            the sheet has None in every row.
    """

    columns: list[str]
    rows: list[tuple[int, tuple[Any, ...]]]


@dataclass
class Contract:
    """Workbook schema and validation rules.

    Attributes:
        worksheets: List of required worksheets.
        max_file_bytes: Largest workbook accepted, as uploaded. A
            larger one is rejected (quarantined) before it is parsed.
        max_uncompressed_bytes: Largest total size of the workbook's
            parts once unzipped, read from the zip directory before
            anything is extracted. Guards against "zip bombs": a
            small file that would expand to fill memory. Lower both
            to fit the job's memory if needed.
        file_name: The name every upload of this workbook must have,
            e.g. "WAS export.xlsx". A file under any other name is
            rejected (quarantined) before it is parsed. A source is
            tracked by its file name: the raw tables' "latest upload"
            works per name, so a copy under another name would become
            a second "latest" upload. None (the default) accepts any name.
            The match is exact, capitals included.
        file_format: The kind of file: "xlsx" (the default, an Excel
            workbook), "csv", "json" (an array of objects) or "jsonl"
            (JSON Lines: one object per line). A CSV or JSON file
            holds one table, so its contract has one Worksheet.
            read_csv(), read_json() and read_jsonl() say how each is
            read. max_uncompressed_bytes applies to workbooks only.
        max_columns: Most columns one table may have, including empty
            ones a row reaches past. A wider one is rejected
            (quarantined) as it is read, before it can fill memory.
        max_cells: Most cells one table may have, counted as a row's
            width added up over its rows. A larger one is rejected
            (quarantined) as it is read. Lower both to fit the job's
            memory if needed.
    """

    worksheets: list[Worksheet] = field(default_factory=list)
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES
    file_name: str | None = None
    file_format: Literal["xlsx", "csv", "json", "jsonl"] = "xlsx"
    max_columns: int = DEFAULT_MAX_COLUMNS
    max_cells: int = DEFAULT_MAX_CELLS

    def __post_init__(self) -> None:
        """Check settings that would otherwise fail confusingly later.

        Raises:
            ValueError: A size limit isn't positive, file_name is
                blank, a path or has the wrong extension for
                file_format, file_format is unknown, or a CSV or JSON
                contract has more than one Worksheet or a JSON one
                sets header_row.
        """
        if self.file_format not in FILE_EXTENSIONS:
            raise ValueError(
                "Contract file_format must be one of "
                f"{', '.join(FILE_EXTENSIONS)}; got {self.file_format!r}"
            )
        if self.file_name is not None and (
            not self.file_name.strip()
            or "/" in self.file_name
            or "\\" in self.file_name
        ):
            raise ValueError(
                "Contract file_name must be a file's name, without a "
                f"folder, e.g. 'WAS export.xlsx'; got {self.file_name!r}"
            )
        extensions = FILE_EXTENSIONS[self.file_format]
        if self.file_name is not None and not self.file_name.lower().endswith(
            extensions
        ):
            raise ValueError(
                f"Contract file_name {self.file_name!r} must end in "
                f"{' or '.join(extensions)} for file_format "
                f"{self.file_format!r}"
            )
        for name in (
            "max_file_bytes",
            "max_uncompressed_bytes",
            "max_columns",
            "max_cells",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(
                    f"Contract {name} must be positive, got "
                    f"{getattr(self, name)}"
                )
        if self.file_format == "xlsx":
            return
        if len(self.worksheets) > 1:
            raise ValueError(
                f"A {self.file_format} file holds one table, so its "
                f"Contract has one Worksheet; got {len(self.worksheets)}"
            )
        if self.file_format != "csv" and any(
            ws.header_row != 1 for ws in self.worksheets
        ):
            raise ValueError(
                f"A {self.file_format} file has no heading row, so its "
                "Worksheet's header_row must be 1"
            )

    def fingerprint(self) -> str:
        """A short ID for this contract's settings.

        Recorded in every audit record, so the audit trail shows
        which contract each upload was loaded with. Equal contracts
        have equal fingerprints, and changing any setting (a column,
        a worksheet option, a limit, file_name) changes it.

        Returns:
            The first 16 hex digits of the SHA-256 of the contract's
            settings, written as canonical JSON.
        """
        settings = asdict(self)
        # Settings at these defaults are left out, so adding a setting
        # doesn't change the fingerprint of every existing contract.
        for name, default in (
            ("file_format", "xlsx"),
            ("max_columns", DEFAULT_MAX_COLUMNS),
            ("max_cells", DEFAULT_MAX_CELLS),
        ):
            if settings[name] == default:
                del settings[name]
        text = json.dumps(settings, sort_keys=True, separators=(",", ":"))
        return sha256(text.encode()).hexdigest()[:16]

    def read(
        self,
        excel_bytes: bytes,
        source_name: str,
        warnings: list[str] | None = None,
    ) -> dict[str, SheetRows]:
        """Validate the file and read its rows, in one parse.

        The workbook is opened once, read-only (openpyxl streams the
        sheet XML rather than building cell objects for formatting),
        and each worksheet's rows are both checked against the
        contract and selected for loading. The selected rows are held
        in memory, which is why Contract has size limits.
        Every worksheet in `self.worksheets` is looked up by name,
        never the workbook's active sheet, which has no relation to
        what the contract expects. A CSV or JSON file is read whole,
        and its one table checked and selected the same way.

        Args:
            excel_bytes: Raw file bytes, of the contract's
                file_format.
            source_name: The file's name: checked against file_name
                if the contract sets one, and used in error messages.
            warnings: List to append problems to for worksheets whose
                on_mismatch is "warn" (mutated). If None, those
                problems are still tolerated but not reported.

        Returns:
            SheetRows for each configured worksheet, keyed by name,
            in contract order.

        Raises:
            ContractError: Contract is misconfigured, the file has
                the wrong name, or worksheet structure doesn't match
                the contract.
            ValidationError: Data violates contract rules.
        """
        return self._read(excel_bytes, source_name, warnings, check=True)

    def validate(
        self,
        excel_bytes: bytes,
        source_name: str,
        warnings: list[str] | None = None,
    ) -> int:
        """Validate a file against this contract.

        A thin wrapper over read(), kept for callers that only want
        the check.

        Args:
            excel_bytes: Raw file bytes, as read().
            source_name: As read().
            warnings: As read().

        Returns:
            Row count of the first configured worksheet if valid.

        Raises:
            ContractError: As read().
            ValidationError: Data violates contract rules.
        """
        sheets = self.read(excel_bytes, source_name, warnings)
        return len(sheets[self.worksheets[0].name].rows)

    def extract(
        self,
        excel_bytes: bytes,
        source_name: str = "workbook",
    ) -> dict[str, SheetRows]:
        """Read the columns and rows to load from every worksheet.

        Applies each worksheet's header_row, heading names and column
        selection. Does not validate: use read() to do both at once.

        Args:
            excel_bytes: Raw file bytes, as read().
            source_name: Filename for error messages.

        Returns:
            SheetRows for each configured worksheet, keyed by name,
            in contract order.

        Raises:
            ContractError: Contract is misconfigured, the workbook
                can't be parsed, or a worksheet is missing.
        """
        return self._read(excel_bytes, source_name, None, check=False)

    def _read(
        self,
        excel_bytes: bytes,
        source_name: str,
        warnings: list[str] | None,
        check: bool,
    ) -> dict[str, SheetRows]:
        """Body of read() and extract().

        Args:
            excel_bytes: Raw file bytes, as read().
            source_name: Filename for error messages.
            warnings: As read().
            check: Validate as well as extract.

        Returns:
            SheetRows per configured worksheet.
        """
        if check:
            self._check_file_name(source_name)
        if warnings is None:
            warnings = []
        if self.file_format != "xlsx":
            return self._read_table_file(
                excel_bytes, source_name, warnings, check
            )
        wb = self._open(excel_bytes, source_name)
        try:
            scans = (
                _scan_sheets(
                    excel_bytes, [ws_def.name for ws_def in self.worksheets]
                )
                if check
                else {}
            )
            sheets: dict[str, SheetRows] = {}
            for ws_def in self.worksheets:
                scan = scans.get(ws_def.name, _SheetScan())
                if scan.misplaced_rows:
                    # openpyxl drops such a row without a word, so the
                    # file can't be read whole, whatever on_mismatch says.
                    raise ContractError(
                        f"{source_name} [{ws_def.name}]: row "
                        f"{scan.misplaced_rows[0]} is out of order or "
                        "repeated, so rows would be lost; open and save "
                        "the file in Excel"
                    )
                ws = wb[ws_def.name]
                # Read-only mode trusts the sheet's stored dimensions,
                # which some tools write wrongly; read what is there.
                ws.reset_dimensions()
                try:
                    names, rows = read_sheet(
                        ws,
                        ws_def.header_row,
                        max_columns=self.max_columns,
                        max_cells=self.max_cells,
                    )
                except _TableTooLarge as e:
                    raise ContractError(
                        f"{source_name} [{ws_def.name}]: {e}"
                    ) from None
                except _PARSE_ERRORS as e:
                    # Read-only mode parses cells lazily, so a corrupt
                    # value surfaces here, not when the file opens.
                    # It is a problem with the file: quarantine it.
                    # The parser's message can quote the cell's text
                    # (personal data, possibly), so only its type is
                    # given, and the original isn't chained into
                    # tracebacks.
                    raise ContractError(
                        f"{source_name} [{ws_def.name}]: Cannot read "
                        f"worksheet ({type(e).__name__}): a cell's "
                        "stored value is invalid, or the file was "
                        "saved by an unsupported tool"
                    ) from None
                if check:
                    self._check_formulas(
                        scan.uncached,
                        ws_def,
                        source_name,
                        warnings,
                    )
                sheets[ws_def.name] = self._checked(
                    names, rows, ws_def, source_name, warnings, check
                )
            return sheets
        finally:
            wb.close()

    def _read_table_file(
        self,
        data: bytes,
        source_name: str,
        warnings: list[str],
        check: bool,
    ) -> dict[str, SheetRows]:
        """Read a CSV or JSON file's one table (file_format isn't xlsx).

        Args:
            data: Raw file bytes.
            source_name: Filename for error messages.
            warnings: As read() (mutated).
            check: Validate as well as extract.

        Returns:
            SheetRows for the contract's one worksheet.

        Raises:
            ContractError: Contract has no worksheets, the file is too
                big or can't be read as its file_format, or (checking)
                its columns don't match the contract.
            ValidationError: (checking) Data violates contract rules.
        """
        if not self.worksheets:
            raise ContractError(
                f"{source_name}: Contract has no worksheets configured"
            )
        self._check_size(data, source_name)
        ws_def = self.worksheets[0]
        try:
            limits = {
                "max_columns": self.max_columns,
                "max_cells": self.max_cells,
            }
            if self.file_format == "csv":
                names, rows = read_csv(data, ws_def.header_row, **limits)
            elif self.file_format == "json":
                names, rows = read_json(data, **limits)
            else:
                names, rows = read_jsonl(data, **limits)
        except ValueError as e:
            # The readers' messages give positions, never values.
            raise ContractError(
                f"{source_name}: Cannot read {self.file_format.upper()}: "
                f"{e}"
            ) from None
        return {
            ws_def.name: self._checked(
                names, rows, ws_def, source_name, warnings, check
            )
        }

    def _checked(
        self,
        names: list[str],
        rows: list[tuple[int, tuple[Any, ...]]],
        ws_def: Worksheet,
        source_name: str,
        warnings: list[str],
        check: bool,
    ) -> SheetRows:
        """Validate one table's rows if asked, then select its columns.

        Args:
            names: Column names, from the file's reader.
            rows: Data rows, from the file's reader.
            ws_def: The table's contract.
            source_name: Filename for error messages.
            warnings: As read() (mutated).
            check: Validate as well as select.

        Returns:
            The rows to load.

        Raises:
            ContractError: (checking) The columns don't match ws_def,
                or there are no rows and ws_def.allow_empty is False.
            ValidationError: (checking) Data violates ws_def's rules.
        """
        selected = _select(names, rows, ws_def)
        if check:
            for row_number, values in selected.rows:
                for value in values:
                    try:
                        _check_value(value)
                    except ValueError as error:
                        raise ValidationError(
                            f"{source_name} [{ws_def.name}]: "
                            f"Row {row_number}: {error}"
                        ) from None
            self._check_error_values(selected, ws_def, source_name, warnings)
            self._validate_worksheet(
                names, rows, ws_def, source_name, warnings
            )
            # After the column checks, so a wrong header_row or
            # missing columns are reported as such first.
            if not rows and not ws_def.allow_empty:
                found = (
                    "no data rows below the headings"
                    if self.file_format in ("xlsx", "csv")
                    else "no records"
                )
                raise ContractError(
                    f"{source_name} [{ws_def.name}]: {found}. If that is "
                    "valid for this source, set allow_empty=True on its "
                    "Worksheet."
                )
        return selected

    def _check_error_values(
        self,
        selected: SheetRows,
        ws_def: Worksheet,
        source_name: str,
        warnings: list[str],
    ) -> None:
        """Report loaded cells that hold an Excel error such as #REF!.

        Only loaded columns are read: an error in a column that is never
        loaded does no harm. JSON has no Excel errors, so it isn't read.

        Args:
            selected: The rows to load.
            ws_def: The worksheet's contract.
            source_name: Filename for messages.
            warnings: Where the problem goes in "warn" mode (mutated).

        Raises:
            ValidationError: A loaded cell holds one, and on_mismatch is
                "quarantine".
        """
        if self.file_format not in ("xlsx", "csv"):
            return
        found = [
            f"Row {row_number}: '{column}'"
            for row_number, values in selected.rows
            for column, value in zip(selected.columns, values, strict=True)
            if isinstance(value, str)
            and value.strip().upper() in _EXCEL_ERRORS
        ]
        if not found:
            return
        shown = "; ".join(found[:5]) + ("; ..." if len(found) > 5 else "")
        message = (
            f"{source_name} [{ws_def.name}]: {len(found)} cell(s) hold an "
            f"Excel error value ({shown}); correct the formula or value and "
            "upload the file again"
        )
        if ws_def.on_mismatch == "quarantine":
            raise ValidationError(message)
        warnings.append(message)

    @staticmethod
    def _check_formulas(
        cells: list[tuple[int, str]],
        ws_def: Worksheet,
        source_name: str,
        warnings: list[str],
    ) -> None:
        """Report formula cells that have no saved result.

        Files written by tools other than Excel (or generated by
        code) often store a formula without its result. Read for its
        values, such a cell is empty, so it would load as NULL or
        fail a "required" check with a misleading message.

        Args:
            cells: (row, cell reference) of each such formula cell.
            ws_def: The worksheet's contract.
            source_name: Filename for messages.
            warnings: Where the problem goes in "warn" mode (mutated).

        Raises:
            ValidationError: There are such cells at or below the
                heading row, and on_mismatch is "quarantine".
        """
        found = [ref for row, ref in cells if row >= ws_def.header_row]
        if not found:
            return
        shown = ", ".join(found[:5]) + (", ..." if len(found) > 5 else "")
        message = (
            f"{source_name} [{ws_def.name}]: {len(found)} formula "
            f"cell(s) have no saved value and read as empty ({shown}); "
            "open and save the file in Excel to store the results"
        )
        if ws_def.on_mismatch == "quarantine":
            raise ValidationError(message)
        warnings.append(message)

    def _check_file_name(self, source_name: str) -> None:
        """Refuse a file that doesn't have the agreed name.

        Checked first, before the file is opened: the name alone
        decides it, and a stray file needn't be parsed to be refused.

        Args:
            source_name: The file's name at the source.

        Raises:
            ContractError: file_name is set and source_name differs.
        """
        if self.file_name is None or source_name == self.file_name:
            return
        raise ContractError(
            f"{source_name}: wrong file name, expected '{self.file_name}' "
            "(Contract.file_name). Upload the file under that name and "
            "delete this one; until then it is quarantined on every run."
        )

    def _open(self, excel_bytes: bytes, source_name: str) -> Workbook:
        """Parse the workbook and check every worksheet is present.

        Args:
            excel_bytes: Raw Excel file bytes.
            source_name: Filename for error messages.

        Returns:
            The openpyxl workbook.

        Raises:
            ContractError: Contract has no worksheets, the workbook
                can't be parsed, or a configured worksheet is missing.
        """
        if not self.worksheets:
            raise ContractError(
                f"{source_name}: Contract has no worksheets configured"
            )
        self._check_size(excel_bytes, source_name)

        try:
            wb = load_workbook(
                # file-like object:
                filename=BytesIO(excel_bytes),
                data_only=True,
                # Streams the sheets instead of building every cell
                # object; nothing here needs merged-cell or style info.
                read_only=True,
            )
        except _PARSE_ERRORS as e:
            raise ContractError(
                f"{source_name}: Cannot parse Excel " f"({type(e).__name__})"
            ) from None

        # Check every configured worksheet exists before reading any
        # of them, so one missing sheet reports cleanly instead of a
        # KeyError on the next lookup.
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
        charts = [
            ws_def.name
            for ws_def in self.worksheets
            if isinstance(wb[ws_def.name], Chartsheet)
        ]
        if charts:
            raise ContractError(
                f"{source_name}: {', '.join(charts)} is a chart sheet, not "
                "a worksheet with rows"
            )
        return wb

    def check_file_size(self, size_bytes: int, source_name: str) -> None:
        """Refuse a file over max_file_bytes.

        A source lists a file's size, so Pipeline calls this before it
        downloads the file into memory.

        Args:
            size_bytes: The file's size.
            source_name: Filename for error messages.

        Raises:
            ContractError: The file is over the limit.
        """
        if size_bytes > self.max_file_bytes:
            raise ContractError(
                f"{source_name}: file is {size_bytes:,} bytes, over "
                f"the contract's max_file_bytes ({self.max_file_bytes:,})"
            )

    def _check_size(self, excel_bytes: bytes, source_name: str) -> None:
        """Refuse a file too big to parse safely.

        Args:
            excel_bytes: Raw file bytes.
            source_name: Filename for error messages.

        Raises:
            ContractError: The file, or a workbook's unzipped parts,
                exceed the contract's limits.
        """
        self.check_file_size(len(excel_bytes), source_name)
        if self.file_format != "xlsx":
            return  # only a workbook is a zip
        try:
            with zipfile.ZipFile(BytesIO(excel_bytes)) as archive:
                unpacked = sum(info.file_size for info in archive.infolist())
        except _PARSE_ERRORS:
            return  # not a readable zip: load_workbook reports it
        if unpacked > self.max_uncompressed_bytes:
            raise ContractError(
                f"{source_name}: unzips to {unpacked:,} bytes, over the "
                "contract's max_uncompressed_bytes "
                f"({self.max_uncompressed_bytes:,})"
            )

    def _validate_worksheet(
        self,
        headers: list[str],
        rows: list[tuple[int, tuple[Any, ...]]],
        ws_def: Worksheet,
        source_name: str,
        warnings: list[str],
    ) -> int:
        """Validate one worksheet against its Worksheet definition.

        Args:
            headers: Column names, from read_sheet().
            rows: Data rows, from read_sheet().
            ws_def: Contract definition for this worksheet.
            source_name: Filename for error messages.
            warnings: Where problems go when ws_def.on_mismatch is
                "warn" (mutated).

        Returns:
            Number of non-empty data rows (below the heading row) if
            valid.

        Raises:
            ContractError: Column structure doesn't match ws_def, and
                ws_def.on_mismatch is "quarantine".
            ValidationError: Data violates ws_def's column rules, and
                ws_def.on_mismatch is "quarantine".
        """
        errors: list[str] = []
        header = f"{source_name} [{ws_def.name}]"
        # A JSON record that leaves a key out holds None for it, so a
        # key no record uses is a column of None values, checked row by
        # row like any other, and a file with no records has no columns.
        keyed = self.file_format in ("json", "jsonl")

        for col_def in ws_def.columns:
            if col_def.name not in headers and not keyed:
                errors.append(f"Missing column '{col_def.name}'")

        if ws_def.column_selection == "listed":
            # read_sheet() has already turned every heading into text
            # (headings can be numbers or dates too), so they compare
            # and sort cleanly here.
            expected = {col.name for col in ws_def.columns}
            unexpected = set(headers) - expected - set(ws_def.excluded)
            if unexpected:
                errors.append(
                    f"Unexpected columns: {len(unexpected)} unrecognised "
                    "heading(s); check the heading row"
                )

        if errors:
            message = f"{header}: {'; '.join(errors)}"
            if ws_def.on_mismatch == "quarantine":
                raise ContractError(message)
            warnings.append(message)

        # Column position in the sheet may not match ws_def.columns'
        # order, so look up each column's index by its header name
        # rather than assuming the two lists are aligned.
        header_index = {h: i for i, h in enumerate(headers)}

        row_errors: list[str] = []
        seen_values: dict[str, set[Any]] = {
            col.name: set() for col in ws_def.columns if col.unique
        }

        # A missing column has already been reported once above (only
        # possible in "warn" mode); don't report it again on every row.
        present = [
            c for c in ws_def.columns if c.name in header_index or keyed
        ]

        for row_idx, row in rows:
            for col_def in present:
                col_idx = header_index.get(col_def.name, len(row))
                value = row[col_idx] if col_idx < len(row) else None

                # Whitespace-only text counts as empty, as it does for
                # skipping blank rows and naming blank headings.
                if _is_blank(value):
                    if not col_def.nullable:
                        row_errors.append(
                            f"Row {row_idx}: '{col_def.name}' is required"
                        )
                    continue

                problem = _type_problem(
                    value, col_def.data_type, self.file_format
                )
                if problem is not None:
                    row_errors.append(
                        f"Row {row_idx}: '{col_def.name}' {problem}"
                    )
                    if problem == _OUT_OF_RANGE:
                        continue

                if col_def.unique:
                    key = _hashable(value)
                    if key in seen_values[col_def.name]:
                        row_errors.append(
                            f"Row {row_idx}: Duplicate value "
                            f"in unique column '{col_def.name}'"
                        )
                    seen_values[col_def.name].add(key)

        if row_errors:
            message = f"{header}: {'; '.join(row_errors[:5])}" + (
                f"... and {len(row_errors) - 5} more"
                if len(row_errors) > 5
                else ""
            )
            if ws_def.on_mismatch == "quarantine":
                raise ValidationError(message)
            warnings.append(message)

        return len(rows)


def _bounded(
    rows: Iterable[Sequence[Any]], max_columns: int, max_cells: int
) -> Iterator[Sequence[Any]]:
    """Pass rows through, stopping at the first one over a limit.

    The rows are checked as they are read, so a table too large to hold
    is refused before it is held.

    Args:
        rows: The rows, one at a time.
        max_columns: Most values one row may have.
        max_cells: Most values all the rows may have.

    Yields:
        Each row.

    Raises:
        _TableTooLarge: A row is too wide, or there are too many cells.
    """
    cells = 0
    for number, row in enumerate(rows, start=1):
        if len(row) > max_columns:
            raise _TableTooLarge(
                f"row {number} has {len(row):,} columns, over the "
                f"contract's max_columns ({max_columns:,})"
            )
        # An empty row still takes up room.
        cells += max(len(row), 1)
        if cells > max_cells:
            raise _TableTooLarge(
                f"more than {max_cells:,} cells, over the contract's "
                "max_cells"
            )
        yield row


def read_sheet(
    ws: XlWorksheet,
    header_row: int,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_cells: int = DEFAULT_MAX_CELLS,
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
        max_columns: Most columns allowed.
        max_cells: Most cells allowed.

    Returns:
        (names, rows): the column names, and each data row as
        (Excel row number, values lined up with names). Row numbers
        are the ones shown in Excel, so errors can be traced back to
        the sheet.

    Raises:
        ValueError: The sheet is over a limit, or holds a value that
            can't be read.
    """
    grid = [
        tuple(row)
        for row in _bounded(
            ws.iter_rows(min_row=header_row, values_only=True),
            max_columns,
            max_cells,
        )
    ]
    return _name_columns(grid, header_row)


def _name_columns(
    grid: list[tuple[Any, ...]], header_row: int
) -> tuple[list[str], list[tuple[int, tuple[Any, ...]]]]:
    """Name a grid's columns and pick its data rows, as read_sheet().

    Args:
        grid: The heading row's values, then each row below it.
        header_row: The heading row's number, to number the rows.

    Returns:
        As read_sheet().
    """
    if not grid:
        return [], []
    # Read-only worksheets give each row only up to its last stored
    # cell, so a heading row can be shorter than the data under it.
    width = max(len(values) for values in grid)
    headings = list(grid[0]) + [None] * (width - len(grid[0]))
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
        # A synthesised name (col_B) must not take a heading the
        # sheet writes itself, any more than a renamed repeat may.
        synthesised = _is_blank(heading)
        while name in names or (
            (name != base or synthesised) and name in written
        ):
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


def read_csv(
    data: bytes,
    header_row: int = 1,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> tuple[list[str], list[tuple[int, tuple[Any, ...]]]]:
    """Read a CSV file's column names and the data rows below them.

    The file must be UTF-8 (with or without the byte order mark
    Excel's "CSV UTF-8" writes) and comma-separated, with double
    quotes around any field that holds a comma, a quote or a line
    break. Headings are named as read_sheet() names them, and rows
    with no values are skipped. An empty field reads as None, like an
    empty cell; every other field is text exactly as written, for
    Dataform to type.

    A row with more fields than the heading row is refused: a value
    holding an unquoted comma pushes every value after it into the next
    column, which could carry an excluded column's text into a loaded
    one.

    Two files saved the wrong way are refused too, as they would
    otherwise load as one column of garbled text: text holding NUL
    characters, as UTF-16 does when read as UTF-8, and a heading row
    that is one heading holding a semicolon or a tab, as a file
    separated by those is.

    Args:
        data: The file's bytes.
        header_row: Row holding the headings (1 = first row).
        max_columns: Most columns allowed.
        max_cells: Most cells allowed.

    Returns:
        As read_sheet(). Row numbers count records, as a spreadsheet
        shows them, so a quoted field's line breaks don't shift them.

    Raises:
        ValueError: The file isn't UTF-8 text or isn't valid CSV, it
            holds a NUL character, its headings are separated by
            semicolons or tabs, a row has more fields than the heading
            row, or the file is over a limit.
    """
    text = _decode(data)
    if "\x00" in text:
        raise ValueError(
            "the file holds NUL characters, as a UTF-16 file does; save "
            "it as CSV UTF-8"
        )
    reader = csv.reader(StringIO(text, newline=""), strict=True)
    try:
        records = list(_bounded(reader, max_columns, max_cells))
    except csv.Error as e:
        raise ValueError(
            f"invalid CSV at line {reader.line_num} ({e})"
        ) from None
    if len(records) >= header_row:
        headings = records[header_row - 1]
        if len(headings) == 1 and any(c in headings[0] for c in ";\t"):
            raise ValueError(
                f"row {header_row} is one heading holding a semicolon or "
                "a tab; save the file comma-separated"
            )
        width = len(headings)
        for number, record in enumerate(
            records[header_row:], start=header_row + 1
        ):
            if len(record) > width and any(f.strip() for f in record):
                raise ValueError(
                    f"row {number} has {len(record)} fields, but the heading "
                    f"row has {width}; check for an unquoted comma"
                )
    grid = [
        tuple(value if value != "" else None for value in record)
        for record in records[header_row - 1 :]
    ]
    return _name_columns(grid, header_row)


def read_json(
    data: bytes,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> tuple[list[str], list[tuple[int, tuple[Any, ...]]]]:
    """Read a JSON file: an array of objects, one per row.

    The columns are every key the objects use, exactly as written, in
    the order they first appear; a key an object leaves out reads as
    None. Values keep their JSON types: text, numbers, true and
    false, null, and nested objects and arrays (a SharePoint person
    or multiple-choice field, say), which load into the payload
    unchanged. An object whose values are all null or blank is
    skipped, as a blank row is.

    Args:
        data: The file's bytes, UTF-8.
        max_columns: Most distinct keys allowed.
        max_cells: Most cells allowed, as records times distinct keys.

    Returns:
        As read_sheet(). Row numbers are positions in the array, the
        first object being row 1.

    Raises:
        ValueError: The file isn't UTF-8 JSON or isn't an array of
            objects, an object repeats a key, a number is NaN, Infinity
            or too large for a float, or the file is over a limit.
    """
    document = _parse_json(_decode(data))
    if not isinstance(document, list):
        raise ValueError(
            f"expected an array of objects, got {_json_type(document)}"
        )
    return _records(
        list(enumerate(document, start=1)), "item", max_columns, max_cells
    )


def read_jsonl(
    data: bytes,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> tuple[list[str], list[tuple[int, tuple[Any, ...]]]]:
    """Read a JSON Lines file: one JSON object per line.

    Read as read_json() reads an array's objects. Blank lines are
    skipped.

    Args:
        data: The file's bytes, UTF-8.
        max_columns: Most distinct keys allowed.
        max_cells: Most cells allowed, as records times distinct keys.

    Returns:
        As read_sheet(). Row numbers are line numbers, the first line
        being row 1.

    Raises:
        ValueError: As read_json(), for any line.
    """
    numbered: list[tuple[int, Any]] = []
    cells = 0
    # Split on "\n" only: str.splitlines() also splits on characters
    # such as U+2028, which JSON text may hold unescaped.
    for line_number, line in enumerate(_decode(data).split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = _parse_json(line)
        except ValueError as e:
            raise ValueError(f"line {line_number}: {e}") from None
        # Counted while parsing: holding every record is what takes the
        # memory, however few keys each one has.
        cells += max(len(record), 1) if isinstance(record, dict) else 1
        if cells > max_cells:
            raise _TableTooLarge(
                f"more than {max_cells:,} cells, over the contract's "
                "max_cells"
            )
        numbered.append((line_number, record))
    return _records(numbered, "line", max_columns, max_cells)


def _decode(data: bytes) -> str:
    """Decode a text file as UTF-8, dropping any byte order mark.

    Args:
        data: The file's bytes.

    Returns:
        The text.

    Raises:
        ValueError: The bytes aren't UTF-8.
    """
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"not UTF-8 text (byte {e.start} is invalid); save or export "
            "it as UTF-8"
        ) from None


def _parse_json(text: str) -> Any:
    """Parse JSON text strictly.

    Args:
        text: One JSON document.

    Returns:
        The parsed value.

    Raises:
        ValueError: Invalid JSON, a repeated key in an object, NaN or
            Infinity, a number too large for a float, or nesting too
            deep to parse.
    """
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
            parse_float=_finite_float,
        )
        _check_value(value)
        return value
    except RecursionError:
        raise ValueError("nested too deeply") from None


def _check_value(value: Any) -> None:
    """Refuse values that cannot be preserved in a raw JSON payload.

    The ingestion limit is 128 nested containers, below Python's
    conversion recursion limit. Integers use BigQuery JSON's signed
    INT64 through unsigned UINT64 range.

    Args:
        value: A parsed cell or JSON value.

    Raises:
        ValueError: A value is non-finite, outside the JSON integer
            range, contains a surrogate, or is nested too deeply.
    """
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 128:
            raise ValueError("nested too deeply (maximum 128 levels)")
        if isinstance(current, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in current):
                raise ValueError("text contains an unpaired surrogate")
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("a number is not finite")
        elif isinstance(current, int) and not isinstance(current, bool):
            if not _INT64_MIN <= current <= 2**64 - 1:
                raise ValueError("a number is outside the JSON integer range")
        elif isinstance(current, dict):
            pending.extend((key, depth + 1) for key in current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object, refusing a repeated key.

    json.loads() would keep the last value silently, losing data.

    Args:
        pairs: The object's (key, value) pairs, in order.

    Returns:
        The object.

    Raises:
        ValueError: A key appears twice.
    """
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("an object repeats a key")
        record[key] = value
    return record


def _no_constant(name: str) -> Any:
    """Refuse NaN, Infinity and -Infinity, which JSON doesn't allow.

    Args:
        name: The constant as written.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    """Parse a JSON number with a fraction or exponent.

    Args:
        text: The number as written.

    Returns:
        The number.

    Raises:
        ValueError: It is too large for a float (e.g. 1e400).
    """
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("a number is too large for a float")
    return value


def _records(
    numbered: list[tuple[int, Any]],
    unit: str,
    max_columns: int,
    max_cells: int,
) -> tuple[list[str], list[tuple[int, tuple[Any, ...]]]]:
    """Turn numbered JSON objects into column names and rows.

    Every row gets a value for every key, so the limits are checked
    before the rows are built: a few thousand records with a key each
    would otherwise make a table of millions of cells.

    Args:
        numbered: (row number, parsed value) for each record.
        unit: What a row number counts, for messages: "item" or
            "line".
        max_columns: Most distinct keys allowed.
        max_cells: Most cells allowed, as records times distinct keys.

    Returns:
        As read_sheet().

    Raises:
        ValueError: A record isn't an object, or the table is over a
            limit.
    """
    names: dict[str, None] = {}
    for number, record in numbered:
        if not isinstance(record, dict):
            raise ValueError(
                f"{unit} {number} is {_json_type(record)}, expected an "
                "object"
            )
        names.update(dict.fromkeys(record))
        if len(names) > max_columns:
            raise _TableTooLarge(
                f"more than {max_columns:,} different keys, over the "
                "contract's max_columns"
            )
    if len(numbered) * len(names) > max_cells:
        raise _TableTooLarge(
            f"{len(numbered):,} records with {len(names):,} different keys "
            f"are over the contract's max_cells ({max_cells:,})"
        )
    columns = list(names)
    rows = [
        (number, tuple(record.get(name) for name in columns))
        for number, record in numbered
        if not all(_is_blank(value) for value in record.values())
    ]
    return columns, rows


def _json_type(value: Any) -> str:
    """Name a parsed JSON value's type, for messages.

    Args:
        value: A value from json.loads().

    Returns:
        e.g. "text", "an object".
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "an array"
    return "an object"


def _type_problem(value: Any, data_type: str, file_format: str) -> str | None:
    """Why a non-empty value doesn't fit its column's data_type.

    - An Excel cell has a type of its own, which must match.
    - A CSV field is always text, so it fits if the text reads as the
      type ("12", "-3.5", "2026-10-01"). It still loads as written;
      Dataform types it.
    - A JSON value's own type must match, except a date: JSON has no
      date type, so a date is text in ISO 8601 form, with or without
      a time ("2026-10-01", "2026-10-01T09:30:00Z").

    Args:
        value: A non-empty value.
        data_type: The column's data_type.
        file_format: The contract's file_format.

    Returns:
        The problem, e.g. "is str, expected integer", or None.
    """
    if data_type == "any":
        return None
    if file_format == "csv":
        if data_type == "string":
            return None
        text = str(value).strip()
        fits = {
            "integer": _INTEGER_TEXT.fullmatch(text) is not None,
            "float": _NUMBER_TEXT.fullmatch(text) is not None,
            "date": _is_iso_date(text),
        }[data_type]
        if not fits:
            return f"doesn't read as {_TEXT_LABELS[data_type]}"
        if data_type == "integer":
            digits = text.lstrip("+-").lstrip("0") or "0"
            if len(digits) > 19:
                return _OUT_OF_RANGE
            sign = "-" if text.startswith("-") else ""
            if not _INT64_MIN <= int(sign + digits) <= _INT64_MAX:
                return _OUT_OF_RANGE
        return None
    is_json = file_format in ("json", "jsonl")
    if is_json and data_type == "date":
        if isinstance(value, str) and _is_iso_date(value):
            return None
        return f"is {_json_type(value)}, expected a date as text (YYYY-MM-DD)"
    expected_type, label = _TYPE_CHECKS[data_type]
    if not isinstance(value, expected_type) or isinstance(value, bool):
        found = _json_type(value) if is_json else type(value).__name__
        return f"is {found}, expected {label}"
    if isinstance(value, date) and not _in_date_range(value):
        return f"is outside the years {_MIN_DATE_YEAR} to {_MAX_DATE_YEAR}"
    if (
        data_type == "integer"
        and isinstance(value, int)
        and not _INT64_MIN <= value <= _INT64_MAX
    ):
        # BigQuery's INT64 can't hold it; it would otherwise load as
        # NULL.
        return _OUT_OF_RANGE
    return None


def _in_date_range(value: date) -> bool:
    """Whether a date falls in the years a "date" column accepts.

    Args:
        value: The date (a datetime is a date).

    Returns:
        True if its year is from _MIN_DATE_YEAR to _MAX_DATE_YEAR.
    """
    return _MIN_DATE_YEAR <= value.year <= _MAX_DATE_YEAR


def _is_iso_date(text: str) -> bool:
    """Whether text is a usable ISO 8601 date, or date and time.

    Args:
        text: The text, trimmed.

    Returns:
        True for e.g. "2026-10-01" or "2026-10-01T09:30:00+01:00",
        if it names a real date (and time) in the accepted years.
    """
    if _ISO_DATE_TEXT.fullmatch(text) is None:
        return False
    try:
        if len(text) == 10:
            return _in_date_range(date.fromisoformat(text))
        return _in_date_range(datetime.fromisoformat(text))
    except ValueError:
        return False


def _hashable(value: Any) -> Any:
    """A value the uniqueness check can keep in a set.

    Args:
        value: A non-empty value.

    Returns:
        The value, or for a JSON object or array its canonical JSON
        text.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _select(
    names: list[str],
    rows: list[tuple[int, tuple[Any, ...]]],
    ws_def: Worksheet,
) -> SheetRows:
    """Pick the columns a worksheet loads, in load order.

    Args:
        names: Column names from read_sheet().
        rows: Data rows from read_sheet().
        ws_def: The worksheet's contract.

    Returns:
        SheetRows with the selected columns; a listed column missing
        from the sheet is None in every row.
    """
    index = {name: i for i, name in enumerate(names)}
    if ws_def.column_selection == "listed":
        selected = [col.name for col in ws_def.columns]
    else:
        skip = set(ws_def.excluded)
        selected = [name for name in names if name not in skip]
    return SheetRows(
        columns=selected,
        rows=[
            (
                row_number,
                tuple(
                    (
                        values[index[name]]
                        if name in index and index[name] < len(values)
                        else None
                    )
                    for name in selected
                ),
            )
            for row_number, values in rows
        ],
    )


def formulas_without_values(
    excel_bytes: bytes, sheet_names: list[str]
) -> dict[str, list[tuple[int, str]]]:
    """Find formula cells stored with no cached result.

    Reading values (data_only=True) can't tell such a cell from an
    empty one, so the sheet XML is scanned directly: a <c> element
    with an <f> (formula) child and no <v> (value), or an empty one
    for a non-text result. The scan
    streams the XML and builds no cell objects.

    Args:
        excel_bytes: Raw .xlsx bytes.
        sheet_names: Worksheets to scan.

    Returns:
        Sheet name -> [(row number, cell reference), ...], only for
        sheets that have such cells.

    Raises:
        ContractError: A sheet's XML part can't be located or read.
    """
    scans = _scan_sheets(excel_bytes, sheet_names)
    return {
        name: scan.uncached for name, scan in scans.items() if scan.uncached
    }


@dataclass
class _SheetScan:
    """What a worksheet's XML shows that reading its values can't.

    Attributes:
        uncached: (row number, cell reference) of each formula cell
            stored with no result.
        misplaced_rows: Row numbers that are repeated, or lower than a
            row before them. openpyxl's streaming reader drops those
            rows without a word.
    """

    uncached: list[tuple[int, str]] = field(default_factory=list)
    misplaced_rows: list[int] = field(default_factory=list)


def _scan_sheets(
    excel_bytes: bytes, sheet_names: list[str]
) -> dict[str, _SheetScan]:
    """Scan worksheets' XML directly.

    Args:
        excel_bytes: Raw .xlsx bytes.
        sheet_names: Worksheets to scan.

    Returns:
        Sheet name -> its scan.

    Raises:
        ContractError: A sheet's XML part can't be located or read.
    """
    try:
        with zipfile.ZipFile(BytesIO(excel_bytes)) as archive:
            paths = _sheet_paths(archive)
            found: dict[str, _SheetScan] = {}
            parts = set(archive.namelist())
            for name in sheet_names:
                path = paths.get(name)
                if path is None or path not in parts:
                    # Never read "can't find the sheet" as "no
                    # uncached formulas".
                    raise ContractError(
                        f"Worksheet {name}: cannot locate its part in "
                        f"the workbook to check formulas ({path})"
                    )
                with archive.open(path) as part:
                    found[name] = _scan_sheet(part)
            return found
    except _PARSE_ERRORS as e:
        raise ContractError(
            f"Cannot scan workbook formulas ({type(e).__name__})"
        ) from None


def _local(tag: str) -> str:
    """Strip the namespace from an XML tag.

    Args:
        tag: A tag such as "{http://...}c".

    Returns:
        The local name, such as "c".
    """
    return tag.rsplit("}", 1)[-1]


def _sheet_paths(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map each worksheet name to its part in the .xlsx archive.

    Args:
        archive: The open .xlsx zip.

    Returns:
        Sheet name -> path such as "xl/worksheets/sheet1.xml".
    """
    targets: dict[str, str] = {}
    with archive.open("xl/_rels/workbook.xml.rels") as rels:
        for _, element in ElementTree.iterparse(rels):
            if _local(element.tag) == "Relationship":
                target = element.get("Target", "")
                # Targets are relative to xl/ unless absolute; resolve
                # "./" and "../" as openpyxl does, so every spelling
                # of the same part is scanned.
                joined = (
                    target.lstrip("/")
                    if target.startswith("/")
                    else f"xl/{target}"
                )
                targets[element.get("Id", "")] = posixpath.normpath(joined)
    paths: dict[str, str] = {}
    with archive.open("xl/workbook.xml") as workbook:
        for _, element in ElementTree.iterparse(workbook):
            if _local(element.tag) != "sheet":
                continue
            rel_id = next(
                (v for k, v in element.attrib.items() if _local(k) == "id"),
                "",
            )
            if rel_id in targets:
                paths[element.get("name", "")] = targets[rel_id]
    return paths


def _scan_sheet(part: IO[bytes]) -> _SheetScan:
    """Scan one worksheet part as a stream.

    Args:
        part: The worksheet XML, open for reading.

    Returns:
        Its formulas with no stored value and its misplaced rows, in
        sheet order.

    Raises:
        ValueError: A row number isn't a number.
    """
    cells: list[tuple[int, str]] = []
    misplaced: list[int] = []
    last_row = 0
    for _, element in ElementTree.iterparse(part):
        tag = _local(element.tag)
        if tag == "row":
            # A row with no number follows the one before it.
            number = int(element.get("r") or last_row + 1)
            if number <= last_row:
                misplaced.append(number)
            last_row = max(last_row, number)
            element.clear()
            continue
        if tag != "c":
            continue
        children = {_local(child.tag): child for child in element}
        value = children.get("v")
        # openpyxl and others write an empty <v/> for "no result";
        # only a text result (t="str") can legitimately be empty.
        cached = value is not None and (
            bool(value.text) or element.get("t") == "str"
        )
        if "f" in children and not cached:
            ref = element.get("r", "")
            digits = "".join(ch for ch in ref if ch.isdigit())
            cells.append((int(digits) if digits else 0, ref))
        element.clear()
    return _SheetScan(cells, misplaced)
