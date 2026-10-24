"""A stateful stand-in for google.cloud.bigquery.Client.

Enough of the client for RawJsonWarehouse to run unmodified: tables,
staging loads, and the commit script. The script is not executed as
SQL, but its parts that carry the
idempotency semantics are interpreted from the SQL text itself:

- per destination table, the audit columns compared in its
  "already loaded?" check (every `col = @param` in the COUNT
  subquery);
- each `INSERT INTO target ... FROM staging` moves the staged row
  count into the target;
- `INSERT INTO audit (cols) VALUES (@params)` records an audit row.

So a test that runs the same file twice against one FakeBigQueryClient
sees exactly what BigQuery's audit table would let through, and a
change to the adapter's SQL or parameters changes what it sees.
"""

import json
import re
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from google.api_core.exceptions import NotFound


class _Job:
    """A finished job: result() returns rows or raises."""

    def __init__(
        self,
        job_id: str,
        rows: list[tuple[Any, ...]] | None = None,
        output_rows: int | None = None,
        error: Exception | None = None,
    ) -> None:
        """Create a finished job.

        Args:
            job_id: The job's ID.
            rows: What result() returns.
            output_rows: The rows a load job reports writing.
            error: Recorded as the job's error_result.
        """
        self.job_id = job_id
        self.output_rows = output_rows
        self.state = "DONE"
        self.error_result = None if error is None else {"message": str(error)}
        self._rows = rows or []
        self._wait_error: Exception | None = None

    def result(self, **_: Any) -> list[tuple[Any, ...]]:
        """Return the rows, or raise the error staged for one wait.

        Args:
            **_: Wait arguments such as timeout, ignored.

        Returns:
            The job's rows.
        """
        if self._wait_error is not None:
            error, self._wait_error = self._wait_error, None
            raise error
        return self._rows

    def reload(self) -> None:
        """Do nothing: the state is set directly by the fake client."""
        pass


class FakeBigQueryClient:
    """In-memory BigQuery with commit-script interpretation.

    Attributes:
        tables: Table ID -> list of column names (schema).
        rows: Table ID -> number of rows committed.
        audit: Audit rows committed, as dicts of column -> value.
        lose_next_commit_wait: If set, the next commit script commits
            and then waiting for it raises this (a dropped
            connection after COMMIT).
    """

    def __init__(self) -> None:
        """Start with no tables, rows or audit records."""
        self.tables: dict[str, list[str]] = {}
        self.rows: dict[str, int] = {}
        self.staged: dict[str, int] = {}
        self.audit: list[dict[str, Any]] = []
        self.lose_next_commit_wait: Exception | None = None
        self.scripts: list[str] = []
        self.created: list[str] = []
        self.expires: dict[str, Any] = {}
        self.delete_error: Exception | None = None
        self._jobs = 0

    def _job_id(self) -> str:
        """Return a new, unique job ID.

        Returns:
            The ID.
        """
        self._jobs += 1
        return f"job-{self._jobs}"

    # Tables ---------------------------------------------------------

    def get_table(self, table_id: str) -> Any:
        """Return a table's schema, all STRING columns.

        Args:
            table_id: The full table ID.

        Returns:
            An object with a schema list, like a bigquery.Table.

        Raises:
            NotFound: The table doesn't exist.
        """
        if table_id not in self.tables:
            raise NotFound(table_id)
        return SimpleNamespace(
            schema=[
                SimpleNamespace(name=name, field_type="STRING")
                for name in self.tables[table_id]
            ]
        )

    def create_table(self, table: Any, exists_ok: bool = False) -> None:
        """Create a table, recording its ID and expiry.

        Args:
            table: A bigquery.Table, or anything with the same attributes.
            exists_ok: Ignored: an existing table is kept.
        """
        table_id = getattr(table, "table_id_full", None) or _full_id(table)
        schema = [field.name for field in getattr(table, "schema", [])]
        self.created.append(table_id)
        self.expires[table_id] = getattr(table, "expires", None)
        self.tables.setdefault(table_id, schema)
        self.rows.setdefault(table_id, 0)

    def delete_table(self, table_id: str, not_found_ok: bool = False) -> None:
        """Delete a table, or raise the error staged for deletes.

        Args:
            table_id: The full table ID.
            not_found_ok: Ignored: a missing table is never an error.
        """
        if self.delete_error is not None:
            raise self.delete_error
        self.tables.pop(table_id, None)

    # Loads ----------------------------------------------------------

    def load_table_from_file(
        self, file: BytesIO, table_id: str, job_config: Any = None
    ) -> _Job:
        """Stage NDJSON rows into a table.

        Args:
            file: The data to load.
            table_id: The staging table.
            job_config: Ignored.

        Returns:
            A finished load job reporting the rows written.
        """
        data = file.read()
        lines = [line for line in data.splitlines() if line.strip()]
        count = len(lines)
        columns = list(json.loads(lines[0])) if lines else []
        self.tables[table_id] = columns
        self.staged[table_id] = count
        return _Job(self._job_id(), output_rows=count)

    # Queries --------------------------------------------------------

    def query(self, sql: str, job_config: Any = None) -> _Job:
        """Answer the adapters' queries, running commit scripts.

        Args:
            sql: The query text.
            job_config: Carries the query parameters.

        Returns:
            The job. A commit job may be made to fail its first wait.
        """
        params = {
            p.name: p.value
            for p in getattr(job_config, "query_parameters", None) or []
        }
        if "BEGIN TRANSACTION" not in sql:
            match = re.search(r"COUNT\(\*\).*FROM `([^`]+)`", sql, re.S)
            if match:
                count = self.staged.get(match.group(1), 0)
                return _Job(self._job_id(), rows=[(count,)])
            return _Job(self._job_id(), rows=[])

        self.scripts.append(sql)
        status = self._run_commit_script(sql, params)
        job = _Job(self._job_id(), rows=[(status,)])
        if self.lose_next_commit_wait is not None:
            # Committed, but the client can't confirm it: waiting
            # fails and a re-read doesn't show the job DONE yet.
            job._wait_error = self.lose_next_commit_wait
            job.state = "RUNNING"
            self.lose_next_commit_wait = None
        return job

    def _run_commit_script(self, sql: str, params: dict[str, Any]) -> str:
        """Interpret the commit script's idempotency-bearing parts.

        Each destination table has its own block: a SET of
        already_loaded_<i> from a COUNT(*) check, then an IF holding
        that table's writes and audit row. Blocks are evaluated in
        order, as BigQuery would, so a later block sees audit rows an
        earlier block added.
        """
        blocks = re.findall(
            r"SET (already_loaded_\d+) = \(\s*SELECT COUNT\(\*\) > 0"
            r"\s+FROM `[^`]+`\s+WHERE (.*?)\);\s*"
            r"IF NOT \1 THEN(.*?)END IF;",
            sql,
            re.S,
        )
        assert blocks, "commit script has no already-loaded check"
        any_loaded = False
        for _, condition, body in blocks:
            compared = re.findall(r"(\w+) = @(\w+)", condition)
            literal = re.findall(r"(\w+) = '(\w+)'", condition)

            def matches(
                row: dict[str, Any],
                compared: list[tuple[str, str]] = compared,
                literal: list[tuple[str, str]] = literal,
            ) -> bool:
                """Say whether an audit row matches this block's check.

                Args:
                    row: An audit row.
                    compared: (column, parameter) pairs that must be equal.
                    literal: (column, value) pairs that must be equal.

                Returns:
                    True if every pair matches.
                """
                return all(
                    row.get(col) == params[param] for col, param in compared
                ) and all(row.get(col) == value for col, value in literal)

            if any(matches(row) for row in self.audit):
                continue

            for target, staging in re.findall(
                r"INSERT INTO `([^`]+)`[^;]*?FROM `([^`]+)`",
                body,
                re.S,
            ):
                self.rows[target] = self.rows.get(target, 0) + self.staged.get(
                    staging, 0
                )

            audit_insert = re.search(
                r"INSERT INTO `[^`]+audit` \(([^)]*)\)\s*VALUES \(([^)]*)\)",
                body,
            )
            assert audit_insert, "commit block records no audit row"
            columns = [c.strip() for c in audit_insert.group(1).split(",")]
            values = [v.strip() for v in audit_insert.group(2).split(",")]
            self.audit.append(
                {
                    col: params[val[1:]] if val.startswith("@") else val
                    for col, val in zip(columns, values, strict=True)
                }
            )
            any_loaded = True
        return "LOADED" if any_loaded else "ALREADY_LOADED"


def _full_id(table: Any) -> str:
    """project.dataset.table of a bigquery.Table."""
    return f"{table.project}.{table.dataset_id}.{table.table_id}"
