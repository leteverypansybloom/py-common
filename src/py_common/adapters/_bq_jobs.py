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
from typing import Any

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery

from py_common.errors import IndeterminateCommitError, ServiceError

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
    logger.info("Started BigQuery job %s for %s", job.job_id, what)

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
    logger.info("Started BigQuery load job %s for %s", job.job_id, what)
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
