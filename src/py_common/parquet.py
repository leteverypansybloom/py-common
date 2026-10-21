"""Convert a worksheet's rows to Parquet with a contract-typed schema.

The typed route (BigQueryWarehouse) loads Parquet, and BigQuery takes
the staging table's column types from the Parquet schema. Building
that schema from Column.data_type, rather than letting pandas or
pyarrow infer it from the values, matters twice over:

- A "warn" worksheet can hold an int and "NA" in one column. Inference
  fails on that (ArrowInvalid), so the file would fail and be retried
  forever. Here the value that doesn't fit becomes NULL, with a
  warning.
- A column that is empty this month would be inferred as a null type
  and staged with a different type than last month. Here it keeps its
  declared type.

Columns are named for BigQuery, not as headed in the sheet: see
column_name().

Pure functions only: no cloud SDK imports, and the output is a
function of the inputs alone (no timestamps), so a retry writes
identical bytes.
"""

import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, time
from io import BytesIO
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from py_common.contract import SheetRows, Worksheet, _is_blank
from py_common.errors import ValidationError

#: Longest column name BigQuery accepts.
MAX_COLUMN_NAME = 300

#: Symbols spelled out in column names, so that "Score %" and "Score"
#: stay apart (score_percent, score).
_SPELLED_OUT = {"%": " percent ", "&": " and ", "#": " number ", "+": " plus "}

#: A run of characters that can't be in a column name.
_NOT_NAME = re.compile(r"[^a-z0-9]+")

#: Arrow type for each Column.data_type. Columns not declared in the
#: contract ("all_except" picks them up) are written as strings.
ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "integer": pa.int64(),
    "float": pa.float64(),
    "date": pa.timestamp("us"),
}

_INT64 = (-(2**63), 2**63 - 1)

#: Bad values listed by row number in one warning before "...".
_ROWS_SHOWN = 5


class _Unconvertible(Exception):
    """A value can't be represented in its column's type."""


def column_name(heading: str) -> str:
    """The BigQuery column name for a heading, before numbering.

    BigQuery column names allow only letters, digits and underscores,
    must not start with a digit, ignore case (so "Score" and "score"
    clash) and are at most 300 characters. So:

    - accents are dropped ("Café" -> "cafe");
    - %, &, # and + are spelled out (percent, and, number, plus);
    - every other run of characters that aren't letters or digits
      becomes one "_", with none at either end;
    - letters are lower case;
    - a name starting with a digit gets "col_" in front;
    - it is cut to 300 characters.

    "Date of Enquiry" becomes date_of_enquiry. A name this function
    gives comes back unchanged, so key_columns may be given as the
    headings or as their column names. No name starts with "_", so
    none can take one of BigQuery's reserved prefixes (_TABLE_,
    _PARTITION, ...) or a column the warehouse sets (_loaded_at).

    Args:
        heading: The column's heading, as Contract.read() names it.

    Returns:
        The name, or "" if the heading has no letters or digits.
    """
    spelled = "".join(_SPELLED_OUT.get(char, char) for char in heading)
    plain = "".join(
        char
        for char in unicodedata.normalize("NFKD", spelled)
        if not unicodedata.combining(char)
    )
    name = _NOT_NAME.sub("_", plain.lower()).strip("_")
    if name[:1].isdigit():
        name = f"col_{name}"
    return name[:MAX_COLUMN_NAME].rstrip("_")


def column_names(
    headings: Sequence[str], keys: Iterable[str] = ()
) -> list[str]:
    """BigQuery column names for a worksheet's loaded columns.

    Each heading gets column_name(), or "col_<position>" (1 for the
    first loaded column) if it has no letters or digits. Names that
    still clash are numbered as repeated headings are: the first
    keeps the name, later ones get "_2", "_3", ... left to right,
    skipping names other columns have as their own. Merge keys are
    named first and never numbered, so a key column is always
    column_name(key) and the warehouse can name it without seeing
    the sheet.

    Args:
        headings: The loaded columns' headings, in order.
        keys: Headings of the merge key columns.

    Returns:
        One unique name per heading, in order.

    Raises:
        ValueError: Two key columns get the same name.
    """
    bases = [
        column_name(heading) or f"col_{position}"
        for position, heading in enumerate(headings, start=1)
    ]
    key_set = set(keys)
    used: set[str] = set()
    for heading, base in zip(headings, bases, strict=True):
        if heading in key_set:
            if base in used:
                raise ValueError(
                    f"Two key columns become the BigQuery column '{base}'"
                )
            used.add(base)
    natural = set(bases)
    names: list[str] = []
    for heading, base in zip(headings, bases, strict=True):
        name = base
        if heading not in key_set:
            number = 2
            while name in used or (name != base and name in natural):
                suffix = f"_{number}"
                name = base[: MAX_COLUMN_NAME - len(suffix)] + suffix
                number += 1
            used.add(name)
        names.append(name)
    return names


def to_parquet(
    sheet: SheetRows,
    worksheet: Worksheet,
    source_name: str = "workbook",
    warnings: list[str] | None = None,
    key_columns: Sequence[str] = (),
) -> bytes:
    """Write one worksheet's rows as Parquet, typed by its contract.

    Args:
        sheet: The rows and column names to write (from
            Contract.extract() or Contract.read()).
        worksheet: The worksheet's contract, for each column's type.
        source_name: File name, for warnings.
        warnings: Where to append one warning per column with values
            that couldn't be converted (mutated). They are loaded as
            NULL. In "quarantine" mode they raise instead.
        key_columns: The contract's merge keys, whose names
            column_names() settles first.

    Returns:
        Parquet bytes, columns in sheet.columns order, named by
        column_names(). Warnings use the headings.
    """
    declared = {column.name: column.data_type for column in worksheet.columns}
    types = [declared.get(name, "string") for name in sheet.columns]
    columns: list[list[Any]] = [[] for _ in sheet.columns]
    bad: dict[int, list[tuple[int, Any]]] = {}

    for row_number, values in sheet.rows:
        cells = zip(values, types, strict=True)
        for index, (value, data_type) in enumerate(cells):
            try:
                columns[index].append(_coerce(value, data_type))
            except _Unconvertible:
                columns[index].append(None)
                bad.setdefault(index, []).append((row_number, value))

    if bad and worksheet.on_mismatch == "quarantine":
        # Validation should already have rejected these; never load a
        # value as NULL on a sheet that asked for strictness.
        raise ValidationError(
            "; ".join(
                _warning(
                    f"{source_name} [{worksheet.name}]",
                    sheet.columns[index],
                    types[index],
                    failures,
                ).replace("; loaded as NULL", "")
                for index, failures in bad.items()
            )
        )
    if warnings is not None:
        for index, failures in bad.items():
            warnings.append(
                _warning(
                    f"{source_name} [{worksheet.name}]",
                    sheet.columns[index],
                    types[index],
                    failures,
                )
            )

    schema = pa.schema(
        [
            pa.field(name, ARROW_TYPES[data_type])
            for name, data_type in zip(
                column_names(sheet.columns, key_columns), types, strict=True
            )
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array(values, type=field.type)
            for values, field in zip(columns, schema, strict=True)
        ],
        schema=schema,
    )
    buffer = BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


def _coerce(value: Any, data_type: str) -> Any:
    """Convert a cell value to its column's Python type.

    Only lossless conversions are made: a whole float to an integer,
    an integer to a float, a number or date to text, a date to
    midnight, numeric or ISO date text to the number or date.

    Args:
        value: Cell value from openpyxl.
        data_type: The column's Column.data_type ("string" for
            undeclared columns).

    Returns:
        The converted value, or None for an empty cell.

    Raises:
        _Unconvertible: The value doesn't fit the type.
    """
    if _is_blank(value):
        return None
    return _CONVERTERS[data_type](value)


def _to_string(value: Any) -> str:
    """Convert a cell to text, with dates and times in ISO format.

    Args:
        value: A non-blank cell value.

    Returns:
        The text.
    """
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _to_integer(value: Any) -> int:
    """Convert a cell to a BigQuery INT64.

    Whole-number floats and numeric text are accepted; True and False
    are not, though Python counts them as ints.

    Args:
        value: A non-blank cell value.

    Returns:
        The integer.

    Raises:
        _Unconvertible: The value isn't a whole number in INT64 range.
    """
    if isinstance(value, bool):
        raise _Unconvertible
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise _Unconvertible from None
    if not isinstance(value, int) or not _INT64[0] <= value <= _INT64[1]:
        raise _Unconvertible
    return value


def _to_float(value: Any) -> float:
    """Convert a cell to a BigQuery FLOAT64.

    Args:
        value: A non-blank cell value.

    Returns:
        The number.

    Raises:
        _Unconvertible: The value is a boolean or isn't numeric.
    """
    if isinstance(value, bool):
        raise _Unconvertible
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            raise _Unconvertible from None
    raise _Unconvertible


def _to_date(value: Any) -> datetime:
    """Convert a cell to a timezone-naive datetime in UTC.

    Args:
        value: A non-blank cell value: a date, a datetime or ISO text.

    Returns:
        The datetime. Aware values are converted to UTC first.

    Raises:
        _Unconvertible: The value isn't a date.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise _Unconvertible from None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time())
    raise _Unconvertible


_CONVERTERS: dict[str, Callable[[Any], Any]] = {
    "string": _to_string,
    "integer": _to_integer,
    "float": _to_float,
    "date": _to_date,
}


def _warning(
    where: str, column: str, data_type: str, failures: list[tuple[int, Any]]
) -> str:
    """Describe a column's unconvertible values in one line.

    Args:
        where: "file [sheet]" prefix.
        column: Column name.
        data_type: The column's type.
        failures: (row number, value) of each bad value.

    Returns:
        The warning text.
    """
    # The bad value itself isn't included: it is data (possibly
    # personal) and this text reaches logs and audit records.
    if len(failures) == 1:
        row, _ = failures[0]
        return (
            f"{where}: '{column}' row {row} is not {data_type}; "
            "loaded as NULL"
        )
    rows = ", ".join(str(row) for row, _ in failures[:_ROWS_SHOWN])
    more = ", ..." if len(failures) > _ROWS_SHOWN else ""
    return (
        f"{where}: '{column}' {len(failures)} values are not {data_type} "
        f"(rows {rows}{more}); loaded as NULL"
    )
