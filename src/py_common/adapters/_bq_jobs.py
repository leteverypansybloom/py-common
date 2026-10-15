"""Run BigQuery jobs and map their failures to py_common errors.

Shared by BigQueryWarehouse and RawJsonWarehouse. The pipeline acts
on two error types from a warehouse, and these helpers are where
they come from:

- ServiceError: BigQuery said the job failed, so a script
  transaction in it did not commit. Safe to retry.
- IndeterminateCommitError: waiting for the job failed (timeout,
  dropped connection) and its final state could not be confirmed.
  The transaction may or may not have committed.

Waiting can fail after the server has committed, so an exception
from job.result() alone proves nothing. The job is re-read by ID to
find out what really happened before anything is reported.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from py_common.errors import IndeterminateCommitError, ServiceError
from py_common.model import SourceItem

logger = logging.getLogger("py_common.adapters.bigquery")

#: Seconds to wait for one BigQuery job before re-checking its state.
JOB_TIMEOUT_SECONDS = 600.0


def run_query(
    client: bigquery.Client,
    sql: str,
    job_config: bigquery.QueryJobConfig,
    what: str,
    timeout: float = JOB_TIMEOUT_SECONDS,
) -> tuple[Any, list[Any]]:
    """Run a query or script and wait for it, confirming its outcome.

    Args:
        client: BigQuery client.
        sql: Query or script text.
        job_config: Job configuration (parameters etc.).
        what: What the job is for, e.g. the load key, for messages.
        timeout: Seconds to wait before re-checking the job's state.

    Returns:
        (job, rows of the job's result).

    Raises:
        ServiceError: The job could not be started, or BigQuery
            reports that it failed.
        IndeterminateCommitError: The job's final state could not be
            confirmed.
    """
    try:
        job = client.query(sql, job_config=job_config)
    except GoogleAPICallError as e:
        raise ServiceError(f"Cannot start BigQuery job for {what}: {e}") from e
    logger.info(
        "Started BigQuery job %s for %s",
        job.job_id,
        what,
        extra={"bq_job_id": job.job_id},
    )

    try:
        return job, list(job.result(timeout=timeout))
    except Exception as e:
        _confirm_after_wait_failed(job, what, e)

    # The job finished cleanly although waiting for it failed: read
    # its result again (it is complete, so this returns at once).
    try:
        return job, list(job.result(timeout=timeout))
    except Exception as e:
        raise IndeterminateCommitError(
            f"BigQuery job {job.job_id} for {what} finished, but its "
            f"result could not be read: {e}"
        ) from e


def wait_for_load(job: Any, what: str) -> None:
    """Wait for a staging load job; it commits nothing final.

    Args:
        job: Load job from load_table_from_file().
        what: What the load is for, for messages.

    Raises:
        ServiceError: BigQuery reports that the load failed.
    """
    logger.info(
        "Started BigQuery load job %s for %s",
        job.job_id,
        what,
        extra={"bq_job_id": job.job_id},
    )
    try:
        job.result()
    except GoogleAPICallError as e:
        raise ServiceError(
            f"BigQuery load job {job.job_id} failed for {what}: {e}"
        ) from e


def _confirm_after_wait_failed(job: Any, what: str, error: Exception) -> None:
    """Re-read a job after waiting for it raised, and decide.

    Returns normally only if the job is DONE with no error.

    Args:
        job: The query job.
        what: What the job is for, for messages.
        error: What waiting raised.

    Raises:
        ServiceError: The job is DONE with an error.
        IndeterminateCommitError: The job isn't DONE, or it couldn't
            be re-read.
    """
    try:
        job.reload()
    except Exception as reload_error:
        raise IndeterminateCommitError(
            f"BigQuery job {job.job_id} for {what}: waiting failed "
            f"({error}) and its state could not be re-read "
            f"({reload_error})"
        ) from error

    if job.state == "DONE" and job.error_result is None:
        logger.warning(
            "BigQuery job %s for %s succeeded although waiting for it "
            "failed: %s",
            job.job_id,
            what,
            error,
            extra={"bq_job_id": job.job_id},
        )
        return
    if job.state == "DONE":
        raise ServiceError(
            f"BigQuery job {job.job_id} failed for {what}: "
            f"{job.error_result}"
        ) from error
    raise IndeterminateCommitError(
        f"BigQuery job {job.job_id} for {what} is still {job.state} "
        f"after waiting failed ({error})"
    ) from error


#: Audit columns written for every load, in order. Each is set from
#: the query parameter of the same name.
AUDIT_COLUMNS = (
    "filename",
    "checksum",
    "destination",
    "source_version",
    "row_count",
    "outcome",
    "timestamp",
)


@dataclass(frozen=True)
class CommitPart:
    """What one destination table gets in a commit.

    Attributes:
        destination: Fully-qualified table ID; the audit row's
            `destination` and the scope of its "already loaded?"
            check.
        writes: DML statements (no trailing semicolon) writing this
            table's rows, e.g. a MERGE or INSERT ... SELECT.
        rows: Rows this table gets, for its audit row.
    """

    destination: str
    writes: list[str]
    rows: int


#: Audit columns besides destination that identify "this upload":
#: the load key (which starts with the source identity) and the
#: source version. Not the checksum: a new upload whose bytes repeat
#: an earlier one (a restore) must load again.
DEDUP_COLUMNS = ("filename", "source_version")


def commit_script(parts: list[CommitPart], audit_full_id: str) -> str:
    """Build the idempotent commit script shared by both warehouses.

    One transaction. For each destination table: check the audit
    table for a LOADED row for this table and this upload; if there
    is none, run the table's writes and record its audit row. The
    check is inside the transaction so the BigQuery audit table,
    committed atomically with the data, is what decides, whatever
    the store marker says. Checking per table means a table added to
    a warehouse later loads without reloading the others.

    The status is selected after the transaction block, so it is the
    script's last statement and therefore the job's result.

    Args:
        parts: One per destination table, in order.
        audit_full_id: Fully-qualified audit table ID.

    Returns:
        A BigQuery script whose result is one row, one column
        `status`: 'LOADED' if any table was written, otherwise
        'ALREADY_LOADED'.
    """
    match = "".join(f"\n      AND {c} = @{c}" for c in DEDUP_COLUMNS)
    columns = ", ".join(AUDIT_COLUMNS)
    declares = "".join(
        f"DECLARE already_loaded_{i} BOOL DEFAULT FALSE;\n"
        for i in range(len(parts))
    )
    blocks = []
    for i, part in enumerate(parts):
        values = ", ".join(
            f"@{c}_{i}" if c in ("destination", "row_count") else f"@{c}"
            for c in AUDIT_COLUMNS
        )
        statements = "".join(f"    {w};\n" for w in part.writes)
        blocks.append(
            f"  SET already_loaded_{i} = (\n"
            "    SELECT COUNT(*) > 0\n"
            f"    FROM `{audit_full_id}`\n"
            f"    WHERE destination = @destination_{i}"
            f"{match}\n"
            "      AND outcome = 'LOADED'\n"
            "  );\n"
            f"  IF NOT already_loaded_{i} THEN\n"
            f"{statements}"
            f"    INSERT INTO `{audit_full_id}` ({columns}) "
            f"VALUES ({values});\n"
            "    SET any_loaded = TRUE;\n"
            "  END IF;\n"
        )
    return (
        f"{declares}"
        "DECLARE any_loaded BOOL DEFAULT FALSE;\n"
        "BEGIN\n"
        "  BEGIN TRANSACTION;\n"
        f"{''.join(blocks)}"
        "  COMMIT TRANSACTION;\n"
        "EXCEPTION WHEN ERROR THEN\n"
        "  ROLLBACK TRANSACTION;\n"
        "  RAISE USING MESSAGE = @@error.message;\n"
        "END;\n"
        "SELECT IF(any_loaded, 'LOADED', 'ALREADY_LOADED') AS status;"
    )


def commit_parameters(
    key: str,
    checksum: str,
    parts: list[CommitPart],
    item: SourceItem,
    timestamp: datetime,
) -> list[bigquery.ScalarQueryParameter]:
    """Query parameters for commit_script().

    Args:
        key: Load key, recorded as the audit filename.
        checksum: SHA-256 of the source file.
        parts: The same parts given to commit_script().
        item: Source file loaded (for its version).
        timestamp: Commit timestamp (audit timestamp, _loaded_at /
            ingested_at).

    Returns:
        The shared parameters, plus destination_<i> and row_count_<i>
        for each part.
    """
    params = [
        bigquery.ScalarQueryParameter("filename", "STRING", key),
        bigquery.ScalarQueryParameter("checksum", "STRING", checksum),
        bigquery.ScalarQueryParameter(
            "source_version", "STRING", item.version
        ),
        bigquery.ScalarQueryParameter("outcome", "STRING", "LOADED"),
        bigquery.ScalarQueryParameter("timestamp", "TIMESTAMP", timestamp),
    ]
    for i, part in enumerate(parts):
        params.append(
            bigquery.ScalarQueryParameter(
                f"destination_{i}", "STRING", part.destination
            )
        )
        params.append(
            bigquery.ScalarQueryParameter(
                f"row_count_{i}", "INTEGER", part.rows
            )
        )
    return params


def committed(job: Any, rows: list[Any], what: str) -> bool:
    """Read commit_script()'s status row.

    Args:
        job: The finished commit job, for messages.
        rows: Its result rows.
        what: What the job was for, for messages.

    Returns:
        True if this job inserted the data, False if the audit table
        showed it was already loaded.

    Raises:
        IndeterminateCommitError: The status is missing or unknown.
    """
    status = rows[0][0] if rows and len(rows[0]) else None
    if status == "LOADED":
        logger.info(
            "BigQuery job %s committed %s",
            job.job_id,
            what,
            extra={"bq_job_id": job.job_id},
        )
        return True
    if status == "ALREADY_LOADED":
        logger.info(
            "BigQuery job %s: %s was already loaded; nothing written",
            job.job_id,
            what,
            extra={"bq_job_id": job.job_id},
        )
        return False
    raise IndeterminateCommitError(
        f"BigQuery job {job.job_id} for {what} returned no load status "
        f"(got {status!r})"
    )


#: How long a staging table lives if its load never drops it.
STAGING_LIFETIME = timedelta(days=1)


def staging_table_id(table_id: str, checksum: str) -> str:
    """A staging table name of this load's own.

    Per load, not per table: two warehouses can share a destination
    table while holding different run locks (RawJsonWarehouse's lock
    is scoped to its whole set of tables), so a shared
    "{table}_staging" could be refilled by another file's load
    between this load's staging and its commit.

    Args:
        table_id: Destination table name.
        checksum: SHA-256 of the source file.

    Returns:
        "{table}_stg_{checksum[:12]}_{8 random hex digits}".
    """
    return f"{table_id}_stg_{checksum[:12]}_{uuid.uuid4().hex[:8]}"


def create_staging_table(
    client: bigquery.Client,
    full_id: str,
    schema: list[bigquery.SchemaField] | None = None,
) -> None:
    """Create a staging table that expires after STAGING_LIFETIME.

    The expiry cleans up after a run that dies before dropping it.

    Args:
        client: BigQuery client.
        full_id: project.dataset.table to create.
        schema: Columns; None lets the first load set them (Parquet).

    Raises:
        ServiceError: The table could not be created.
    """
    table = bigquery.Table(full_id, schema=schema)
    table.expires = datetime.now(timezone.utc) + STAGING_LIFETIME
    try:
        client.create_table(table)
    except GoogleAPICallError as e:
        raise ServiceError(
            f"Cannot create staging table {full_id}: {e}"
        ) from e


def drop_staging_table(client: bigquery.Client, full_id: str) -> None:
    """Drop a staging table; best effort, never raises.

    A failure only leaves the table until it expires, so it must not
    hide the load's own result.

    Args:
        client: BigQuery client.
        full_id: project.dataset.table to drop.
    """
    try:
        client.delete_table(full_id, not_found_ok=True)
    except Exception as e:
        logger.warning(
            "Could not drop staging table %s (it expires by itself): %s",
            full_id,
            e,
        )
