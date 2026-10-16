"""Convert workbook rows to newline-delimited JSON for raw tables.

The raw JSON route loads each worksheet's rows into a raw BigQuery
table as a JSON payload, plus columns saying where the row came
from. Cleaning and typing happen later, in the transformation layer
(Dataform), which is why the payload keeps Excel's values as close
to the original as JSON allows.

Pure functions only: no cloud SDK imports, so the conversion is
testable offline and the output is a function of its inputs alone.
"""

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from py_common.contract import SheetRows
from py_common.model import SourceItem

#: Fields of each NDJSON line, in order. The raw table has these
#: columns plus ingested_at, which the warehouse sets at commit time.
ROW_FIELDS = (
    "source_file",
    "source_generation",
    "sheet_name",
    "row_number",
    "checksum",
    "payload",
)


def json_value(value: Any) -> Any:
    """Turn one Excel cell value into a JSON-safe value.

    - None, text, booleans and finite numbers are kept as they are.
    - A date and time becomes "YYYY-MM-DDTHH:MM:SS", a date
      "YYYY-MM-DD" and a time "HH:MM:SS" (ISO 8601). Excel stores
      no time zone, so none is added; a date and time that does
      carry one (openpyxl never gives one today) is converted to
      UTC first, so the output never has an offset.
    - A Decimal becomes a JSON number: an integer if it is whole,
      otherwise a float.
    - A duration becomes an ISO 8601 duration in seconds, e.g.
      "PT5400S".
    - Anything else becomes text.

    Args:
        value: Cell value from openpyxl.

    Returns:
        A value json.dumps() can write.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return (
            int(value) if value == value.to_integral_value() else float(value)
        )
    # datetime is a subclass of date, so it must be checked first.
    if isinstance(value, datetime) and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return _iso_duration(value)
    return str(value)


def _iso_duration(delta: timedelta) -> str:
    """Format a duration as ISO 8601 seconds, e.g. "PT5400S".

    Args:
        delta: Duration from a cell.

    Returns:
        "PT<seconds>S", with a leading "-" if negative.
    """
    seconds = delta.total_seconds()
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    amount = str(int(seconds)) if seconds.is_integer() else str(seconds)
    return f"{sign}PT{amount}S"


def to_ndjson(
    sheets: dict[str, SheetRows],
    item: SourceItem,
    checksum: str,
) -> tuple[bytes, int]:
    """Write every sheet's rows as newline-delimited JSON.

    One line per row, with the fields in ROW_FIELDS. The payload
    holds every selected column (null when the cell is empty), in
    the order SheetRows gives them.

    No load timestamp is written: the same workbook always gives the
    same bytes, so a retry after a failed load writes an identical
    processed/ copy. The warehouse sets ingested_at when it commits.

    Args:
        sheets: Selected rows per worksheet, from Contract.extract().
        item: Source file the rows came from.
        checksum: SHA-256 of the source file's bytes.

    Returns:
        (NDJSON bytes, number of rows written).
    """
    lines = []
    for sheet_name, sheet in sheets.items():
        for row_number, values in sheet.rows:
            record = {
                "source_file": item.name,
                "source_generation": item.version,
                "sheet_name": sheet_name,
                "row_number": row_number,
                "checksum": checksum,
                "payload": {
                    column: json_value(value)
                    for column, value in zip(sheet.columns, values)
                },
            }
            lines.append(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
    data = "".join(f"{line}\n" for line in lines).encode("utf-8")
    return data, len(lines)
