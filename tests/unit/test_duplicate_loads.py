"""An upload loaded twice is detected.

Each upload loads at most once per table: the commit transaction
checks the audit table first. Two commits of the same upload running
at the same moment could both pass that check (a commit still running
after the pipeline stopped waiting for it, plus a retry). This rare
case is detected, not prevented: check_duplicate_loads()
reads the audit table and logs each upload with more than one LOADED
row, for a log-based alert.
"""

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from py_common.adapters._bq_jobs import DEDUP_COLUMNS
from py_common.runner import check_duplicate_loads

AUDIT = "proj.raw_hww.audit"


class Client:
    """Answers one query with the given rows, and records it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        """Answer every query with these rows.

        Args:
            rows: The rows to return.
        """
        self.rows = rows
        self.sql: list[str] = []

    def query(self, query: str) -> Any:
        """Record a query and return the rows.

        Args:
            query: The SQL.

        Returns:
            A job whose result() gives the rows.
        """
        self.sql.append(query)
        return SimpleNamespace(result=lambda: self.rows)


def test_each_upload_loaded_twice_is_logged_and_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each double load is logged as an error and returned.

    Args:
        caplog: Captures the log.
    """
    client = Client(
        [
            {
                "destination": "proj.raw_hww.was",
                "filename": "was/WAS export.xlsx_1a2b3c4d",
                "source_version": "1712",
                "loads": 2,
            }
        ]
    )

    with caplog.at_level(logging.ERROR, logger="py_common.runner"):
        found = check_duplicate_loads(client, AUDIT)

    assert found == ["proj.raw_hww.was was/WAS export.xlsx_1a2b3c4d 1712"]
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "Duplicate load: was/WAS export.xlsx_1a2b3c4d version 1712 "
        "loaded 2 times into proj.raw_hww.was"
    )


def test_nothing_is_logged_when_every_upload_loaded_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No warnings when nothing loaded twice.

    Args:
        caplog: Captures the log.
    """
    with caplog.at_level(logging.INFO, logger="py_common.runner"):
        assert check_duplicate_loads(Client([]), AUDIT) == []

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_counts_loaded_rows_per_table_and_upload() -> None:
    """The query groups LOADED rows by table and upload."""
    client = Client([])

    check_duplicate_loads(client, AUDIT)

    (sql,) = client.sql
    assert f"FROM `{AUDIT}`" in sql
    assert "outcome = 'LOADED'" in sql
    # The same columns the commit script's "already loaded?" check
    # compares, so the two can't drift apart.
    grouped = ", ".join(("destination", *DEDUP_COLUMNS))
    assert f"GROUP BY {grouped}" in sql
    assert "HAVING COUNT(*) > 1" in sql


@pytest.mark.parametrize(
    "bad", ["audit", "proj.audit", "p.d.t`; DROP TABLE x; --", "p.d.t.u"]
)
def test_only_a_qualified_table_name_is_accepted(bad: str) -> None:
    """Anything but project.dataset.table is refused unrun.

    Args:
        bad: The refused table name.
    """
    client = Client([])

    with pytest.raises(ValueError, match="project.dataset.table"):
        check_duplicate_loads(client, bad)

    assert client.sql == []


def test_a_failed_query_raises() -> None:
    """A query that can't run raises, rather than reporting none."""

    class Broken:
        """A client whose queries fail."""

        def query(self, query: str) -> Any:
            """Fail as a client without access would.

            Args:
                query: The SQL.

            Raises:
                RuntimeError: Always.
            """
            raise RuntimeError("no access")

    with pytest.raises(RuntimeError):
        check_duplicate_loads(Broken(), AUDIT)
