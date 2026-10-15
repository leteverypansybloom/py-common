"""Every load stages into its own short-lived table (review H7).

Two warehouses that share a raw table hold different run locks, so a
shared `{table}_staging` could be truncated and refilled by another
file's load mid-flight. A per-load name removes the sharing.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from google.api_core.exceptions import Forbidden

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.model import SourceItem

CHECKSUM = "0123456789abcdef" * 4
ITEM = SourceItem("f.xlsx", "landing/f.xlsx", "1", 10)
PARQUET_ROWS = 1

_NDJSON = (
    b'{"source_file":"f.xlsx","source_generation":"1","sheet_name":"S",'
    b'"row_number":2,"checksum":"c","payload":{}}\n'
)


def _parquet() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq
    from io import BytesIO

    buffer = BytesIO()
    pq.write_table(pa.table({"id": [1]}), buffer)
    return buffer.getvalue()


def _typed(client: FakeBigQueryClient) -> tuple[Any, bytes, str]:
    with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = BigQueryWarehouse("p", "d", "events")
    return warehouse, _parquet(), "events"


def _raw(client: FakeBigQueryClient) -> tuple[Any, bytes, str]:
    with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = RawJsonWarehouse("p", "d", {"S": "raw_s"})
    return warehouse, _NDJSON, "raw_s"


@pytest.fixture(params=[_typed, _raw], ids=["typed", "raw"])
def setup(
    request: pytest.FixtureRequest,
) -> tuple[FakeBigQueryClient, Any, bytes, str]:
    client = FakeBigQueryClient()
    warehouse, data, table = request.param(client)
    return client, warehouse, data, table


def _staging_name(table: str) -> re.Pattern[str]:
    return re.compile(rf"^p\.d\.{table}_stg_{CHECKSUM[:12]}_[0-9a-f]{{8}}$")


def test_staging_table_is_per_load_and_expires(
    setup: tuple[FakeBigQueryClient, Any, bytes, str],
) -> None:
    client, warehouse, data, table = setup
    before = datetime.now(timezone.utc)

    warehouse.load("k", CHECKSUM, data, 1, ITEM)

    [staging] = [t for t in client.created if "_stg_" in t]
    assert _staging_name(table).match(staging)
    expires = client.expires[staging]
    assert before + timedelta(hours=23) < expires
    assert expires < before + timedelta(hours=25)
    assert f"FROM `{staging}`" in client.scripts[-1] or (
        f"USING `{staging}`" in client.scripts[-1]
    )


def test_staging_table_is_dropped_after_commit(
    setup: tuple[FakeBigQueryClient, Any, bytes, str],
) -> None:
    client, warehouse, data, _ = setup

    warehouse.load("k", CHECKSUM, data, 1, ITEM)

    assert not [t for t in client.tables if "_stg_" in t]


def test_two_loads_never_share_a_staging_table(
    setup: tuple[FakeBigQueryClient, Any, bytes, str],
) -> None:
    client, warehouse, data, _ = setup

    warehouse.load("k", CHECKSUM, data, 1, ITEM)
    warehouse.load("k2", CHECKSUM, data, 1, SourceItem("g", "g", "2", 1))

    staged = [t for t in client.created if "_stg_" in t]
    assert len(staged) == 2 and staged[0] != staged[1]


def test_failed_drop_only_warns(
    setup: tuple[FakeBigQueryClient, Any, bytes, str],
) -> None:
    client, warehouse, data, _ = setup
    client.delete_error = Forbidden("no delete permission")

    with patch("py_common.adapters._bq_jobs.logger") as logger:
        assert warehouse.load("k", CHECKSUM, data, 1, ITEM) is True

    assert any(
        "Could not drop staging table" in c.args[0]
        for c in logger.warning.call_args_list
    )


def test_staging_table_is_dropped_when_load_fails(
    setup: tuple[FakeBigQueryClient, Any, bytes, str],
) -> None:
    client, warehouse, data, _ = setup

    with pytest.raises(ValueError, match="Row count mismatch"):
        warehouse.load("k", CHECKSUM, data, 99, ITEM)

    assert not [t for t in client.tables if "_stg_" in t]
