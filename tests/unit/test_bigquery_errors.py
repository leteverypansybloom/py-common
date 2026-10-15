"""Error contract of both BigQuery warehouses (review H1, M11).

The pipeline acts on ServiceError (retry) and
IndeterminateCommitError (state unknown). These tests drive each
adapter's commit job into every outcome and assert the type raised,
so the pipeline branches that handle them are reachable in
production, not only with mocked warehouses.
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from google.api_core.exceptions import (
    BadRequest,
    Forbidden,
    NotFound,
    ServiceUnavailable,
)

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.errors import IndeterminateCommitError, ServiceError
from py_common.model import SourceItem

_NDJSON = (
    b'{"source_file":"f.xlsx","source_generation":"1","sheet_name":"S",'
    b'"row_number":2,"checksum":"c","payload":{}}\n'
)


ITEM = SourceItem("k.xlsx", "landing/k.xlsx", "1", 10)


def _load(warehouse: Any, data: bytes) -> None:
    """Load one row of `data` for ITEM."""
    warehouse.load("k.xlsx", "abc123", data, 1, ITEM)


def _typed(client: MagicMock) -> Callable[[], None]:
    """Build a BigQueryWarehouse on `client`; return a load call."""
    with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = BigQueryWarehouse("p", "d", "t")
    client.get_table.return_value = SimpleNamespace(
        schema=[SimpleNamespace(name="id", field_type="INTEGER")]
    )
    return lambda: _load(warehouse, b"parquet")


def _raw(client: MagicMock) -> Callable[[], None]:
    """Build a RawJsonWarehouse on `client`; return a load call."""
    with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = RawJsonWarehouse("p", "d", {"S": "t"})
    return lambda: _load(warehouse, _NDJSON)


@pytest.fixture(params=[_typed, _raw], ids=["typed", "raw"])
def setup(
    request: pytest.FixtureRequest,
) -> tuple[MagicMock, Mock, Callable[[], None]]:
    """(client, commit job, load()) for each adapter.

    Every query except the commit script succeeds with one row
    holding 1, which satisfies any row-count or status check.
    """
    client = MagicMock()
    load_job = Mock(output_rows=1, job_id="load-1")
    load_job.result.return_value = None
    client.load_table_from_file.return_value = load_job

    commit = Mock(job_id="job-42")

    def query(sql: str, **_: Any) -> Mock:
        if "BEGIN TRANSACTION" in sql:
            return commit
        other = Mock(job_id="q-1")
        other.result.return_value = [(1,)]
        return other

    client.query.side_effect = query
    return client, commit, request.param(client)


def test_job_reported_failed_raises_service_error_with_job_id(
    setup: tuple[MagicMock, Mock, Callable[[], None]],
) -> None:
    """BigQuery says the script failed → ServiceError naming the job."""
    _, commit, load = setup
    commit.result.side_effect = BadRequest("duplicate key")
    commit.state = "DONE"
    commit.error_result = {"reason": "invalidQuery"}

    with pytest.raises(ServiceError, match="job-42"):
        load()


def test_timeout_then_done_without_error_is_success(
    setup: tuple[MagicMock, Mock, Callable[[], None]],
) -> None:
    """A lost wait on a job that committed is not reported as failed."""
    _, commit, load = setup
    commit.result.side_effect = [
        TimeoutError("wait timed out"),
        [("LOADED",)],
    ]
    commit.state = "DONE"
    commit.error_result = None

    load()

    commit.reload.assert_called_once()


def test_transport_error_then_done_with_error_is_service_error(
    setup: tuple[MagicMock, Mock, Callable[[], None]],
) -> None:
    """Re-read shows the job failed → ServiceError (safe to retry)."""
    _, commit, load = setup
    commit.result.side_effect = ConnectionError("reset by peer")
    commit.state = "DONE"
    commit.error_result = {"reason": "backendError"}

    with pytest.raises(ServiceError, match="job-42"):
        load()


def test_job_still_running_is_indeterminate(
    setup: tuple[MagicMock, Mock, Callable[[], None]],
) -> None:
    """A job not yet DONE may still commit: state is unknown."""
    _, commit, load = setup
    commit.result.side_effect = TimeoutError("wait timed out")
    commit.state = "RUNNING"
    commit.error_result = None

    with pytest.raises(IndeterminateCommitError, match="RUNNING"):
        load()


def test_reload_failure_is_indeterminate(
    setup: tuple[MagicMock, Mock, Callable[[], None]],
) -> None:
    """If the job can't be re-read, nothing is claimed about it."""
    _, commit, load = setup
    commit.result.side_effect = ServiceUnavailable("503")
    commit.reload.side_effect = ConnectionError("still down")

    with pytest.raises(IndeterminateCommitError, match="job-42") as info:
        load()
    assert "rolled back" not in str(info.value)


def test_staging_load_failure_is_service_error(
    setup: tuple[MagicMock, Mock, Callable[[], None]],
) -> None:
    """A failed staging load (nothing committed) is ServiceError."""
    client, _, load = setup
    client.load_table_from_file.return_value.result.side_effect = BadRequest(
        "bad parquet"
    )

    with pytest.raises(ServiceError, match="load-1"):
        load()


class TestEnsureStagingTable:
    """BigQueryWarehouse._ensure_staging_table (review M11)."""

    def _warehouse(self) -> tuple[BigQueryWarehouse, MagicMock]:
        client = MagicMock()
        with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
            cls.return_value = client
            return BigQueryWarehouse("p", "d", "t"), client

    def test_creates_table_only_when_missing(self) -> None:
        """NotFound → the staging table is created."""
        warehouse, client = self._warehouse()
        client.get_table.side_effect = NotFound("no table")

        warehouse._ensure_staging_table()

        client.create_table.assert_called_once()

    def test_other_errors_are_not_masked(self) -> None:
        """A 403 is reported, not hidden behind a create attempt."""
        warehouse, client = self._warehouse()
        client.get_table.side_effect = Forbidden("no access")

        with pytest.raises(ServiceError, match="no access"):
            warehouse._ensure_staging_table()
        client.create_table.assert_not_called()
