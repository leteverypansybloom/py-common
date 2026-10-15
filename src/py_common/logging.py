"""Structured JSON logging for Cloud Logging.

Cloud Run sends each line a container writes to stdout to Cloud
Logging, and a line that is a JSON object becomes a structured entry:
`severity` sets the level and `message` the text, and every other
field can be filtered on (e.g. jsonPayload.run_id="..."). So a
failure can be followed across the run lock, one file and its
BigQuery job from the logs alone.

The fields come from two places:

- log_context(), which Pipeline uses to tag everything logged during
  a run (run_id, warehouse) and during one file (item_identity,
  version, checksum), including lines logged by the adapters;
- `extra=` on a single call, e.g. bq_job_id on the line that starts
  a BigQuery job, or outcome on a file's summary line.

No cloud SDK is imported: this is plain `logging` plus `json`.
"""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, TextIO

#: Structured fields copied from a log record into the JSON line,
#: when present.
FIELDS = (
    "run_id",
    "warehouse",
    "item_identity",
    "version",
    "checksum",
    "bq_job_id",
    "outcome",
    "rows",
    "seconds",
)

_context: ContextVar[dict[str, Any]] = ContextVar(
    "py_common_log_context", default={}
)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Tag every record logged inside the block with `fields`.

    Nested blocks add to (and may override) the outer block's fields.
    A field given with `extra=` on a call wins over the context.

    Args:
        **fields: Field name -> value.

    Yields:
        None.
    """
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def add_log_fields(**fields: Any) -> None:
    """Add fields to the innermost log_context() for the rest of it.

    For a value learnt part-way through a block (e.g. a file's
    checksum once it is downloaded). The fields go when the enclosing
    log_context() block ends. Outside any block they last until the
    context is reset, so always call it inside one.

    Args:
        **fields: Field name -> value.
    """
    _context.set({**_context.get(), **fields})


def context_fields() -> dict[str, Any]:
    """The fields log_context() currently adds.

    Returns:
        A copy of the current context.
    """
    return dict(_context.get())


class ContextFilter(logging.Filter):
    """Copy the current log_context() fields onto each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add context fields the record doesn't already have.

        Args:
            record: The record being handled.

        Returns:
            True: never drops a record.
        """
        for name, value in _context.get().items():
            if not hasattr(record, name):
                setattr(record, name, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record, in Cloud Logging's shape."""

    def format(self, record: logging.LogRecord) -> str:
        """Render `record` as a JSON line.

        Args:
            record: The record to format.

        Returns:
            JSON with severity, message, time, logger, any FIELDS
            present, and the traceback as `exception` if there is one.
        """
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "time": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "logger": record.name,
        }
        for name in FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                entry[name] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_json_logging(
    level: int = logging.INFO, stream: TextIO | None = None
) -> logging.Handler:
    """Send all logging to `stream` as Cloud-Logging-compatible JSON.

    Call once at start-up (e.g. in a Cloud Run job's main). Replaces
    any handler an earlier call installed, so calling it twice
    doesn't duplicate lines.

    Args:
        level: Root logger level. Defaults to INFO, so BigQuery job
            IDs and per-file summaries are kept.
        stream: Where to write. Defaults to stdout, which Cloud Run
            forwards to Cloud Logging.

    Returns:
        The handler installed on the root logger.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing.formatter, JsonFormatter):
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    root.addHandler(handler)
    root.setLevel(level)
    return handler
