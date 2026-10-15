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

Pure functions only: no cloud SDK imports, and the output is a
function of the inputs alone (no timestamps), so a retry writes
identical bytes.
"""

from collections.abc import Callable
from datetime import date, datetime, time, timezone
from io import BytesIO
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from py_common.contract import SheetRows, Worksheet, _is_blank

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


def to_parquet(
    sheet: SheetRows,
    worksheet: Worksheet,
    source_name: str = "workbook",
    warnings: list[str] | None = None,
) -> bytes:
    """Write one worksheet's rows as Parquet, typed by its contract.

    Args:
        sheet: The rows and column names to write (from
            Contract.extract() or Contract.read()).
        worksheet: The worksheet's contract, for each column's type.
        source_name: File name, for warnings.
        warnings: Where to append one warning per column with values
            that couldn't be converted (mutated). They are loaded as
            NULL. In "quarantine" mode validation has already
            rejected such files, so this is the "warn" mode path.

    Returns:
        Parquet bytes, columns in sheet.columns order.
    """
    declared = {column.name: column.data_type for column in worksheet.columns}
    types = [declared.get(name, "string") for name in sheet.columns]
    columns: list[list[Any]] = [[] for _ in sheet.columns]
    bad: dict[int, list[tuple[int, Any]]] = {}

    for row_number, values in sheet.rows:
        for index, (value, data_type) in enumerate(zip(values, types)):
            try:
                columns[index].append(_coerce(value, data_type))
            except _Unconvertible:
                columns[index].append(None)
                bad.setdefault(index, []).append((row_number, value))

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
            for name, data_type in zip(sheet.columns, types)
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array(values, type=field.type)
            for values, field in zip(columns, schema)
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
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _to_integer(value: Any) -> int:
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
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise _Unconvertible from None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
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
    if len(failures) == 1:
        row, value = failures[0]
        return (
            f"{where}: '{column}' row {row} is not {data_type} "
            f"({value!r}); loaded as NULL"
        )
    rows = ", ".join(str(row) for row, _ in failures[:_ROWS_SHOWN])
    more = ", ..." if len(failures) > _ROWS_SHOWN else ""
    return (
        f"{where}: '{column}' {len(failures)} values are not {data_type} "
        f"(rows {rows}{more}); loaded as NULL"
    )
