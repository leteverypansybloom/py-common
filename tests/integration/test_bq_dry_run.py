r"""Dry-run every SQL statement the BigQuery adapter generates.

The unit tests check SQL text against mocks; this checks BigQuery
accepts it. RawJsonWarehouse.load() runs against a recording client,
which answers like BigQuery would and keeps every statement with its
parameters. A scratch dataset then gets every table those statements
name, and each statement is sent with QueryJobConfig(dry_run=True):
parsed, planned and checked against the real tables, but never run,
so it costs nothing.

Needs GCP_PROJECT_ID and credentials allowed to create a dataset and
run query jobs there (roles/bigquery.dataEditor + jobUser). Skipped
when GCP_PROJECT_ID is unset. The dataset is deleted afterwards and
expires after a day regardless.

```bash
GCP_PROJECT_ID=my-sandbox pytest -m integration \
  tests/integration/test_bq_dry_run.py
```
"""

import os
import re
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

bigquery = pytest.importorskip("google.cloud.bigquery")
from google.api_core.exceptions import NotFound  # noqa: E402

from py_common.adapters.bigquery_raw import (  # noqa: E402
    RAW_SCHEMA,
    STAGING_SCHEMA,
    RawJsonWarehouse,
)
from py_common.model import SourceItem  # noqa: E402

PROJECT = os.environ.get("GCP_PROJECT_ID", "")
ITEM = SourceItem("f.xlsx", "landing/f.xlsx", "1", 10)
CHECKSUM = "ab" * 32

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PROJECT, reason="GCP_PROJECT_ID is not set"),
]

AUDIT_SCHEMA = [
    bigquery.SchemaField("filename", "STRING"),
    bigquery.SchemaField("checksum", "STRING"),
    bigquery.SchemaField("destination", "STRING"),
    bigquery.SchemaField("source_version", "STRING"),
    bigquery.SchemaField("row_count", "INT64"),
    bigquery.SchemaField("outcome", "STRING"),
    bigquery.SchemaField("timestamp", "TIMESTAMP"),
]


class Recorder:
    """Stands in for bigquery.Client; records every query.

    Answers so that load() walks every SQL path: no table exists yet,
    each load stages one row, and the commit reports LOADED.
    """

    def __init__(self) -> None:
        """Start recording."""
        self.statements: list[tuple[str, Any]] = []

    def get_table(self, table_id: str) -> Any:
        """Report every table missing, so load() creates them.

        Args:
            table_id: The full table ID.

        Raises:
            NotFound: Always.
        """
        raise NotFound(table_id)

    def create_table(self, table: Any, exists_ok: bool = False) -> None:
        """Do nothing: tables are created for real later.

        Args:
            table: The table to create.
            exists_ok: Ignored.
        """

    def delete_table(self, table_id: str, not_found_ok: bool = False) -> None:
        """Do nothing: the scratch dataset is deleted whole.

        Args:
            table_id: The table to delete.
            not_found_ok: Ignored.
        """

    def load_table_from_file(self, file: Any, table_id: str, **_: Any) -> Any:
        """Pretend to stage one row.

        Args:
            file: The data, ignored.
            table_id: The staging table.
            **_: Load options, ignored.

        Returns:
            A finished load job reporting one row.
        """
        return SimpleNamespace(
            job_id="load", output_rows=1, result=lambda **_: None
        )

    def query(self, sql: str, job_config: Any = None) -> Any:
        """Record the statement and answer as BigQuery would.

        Args:
            sql: The statement.
            job_config: Carries its parameters.

        Returns:
            A finished job.
        """
        self.statements.append((sql, job_config))
        rows = [("LOADED",)] if "BEGIN TRANSACTION" in sql else []
        return SimpleNamespace(job_id="q", result=lambda **_: rows)


def _statements(dataset: str) -> list[tuple[str, Any]]:
    """Return every statement the adapter generates for one load.

    Args:
        dataset: The dataset the statements name.

    Returns:
        (SQL, job config) per statement, in order.
    """
    recorder = Recorder()
    with patch("py_common.adapters.bigquery_raw.bigquery.Client") as cls:
        cls.return_value = recorder
        RawJsonWarehouse(PROJECT, dataset, {"S": "raw_s"}).load(
            "k",
            CHECKSUM,
            b'{"sheet_name":"S"}\n',
            1,
            ITEM,
        )
    return recorder.statements


@pytest.fixture(scope="module")
def client() -> Any:
    """Return a real BigQuery client for the test project.

    Returns:
        The client.
    """
    return bigquery.Client(project=PROJECT)


@pytest.fixture(scope="module")
def dataset(client: Any) -> Iterator[str]:
    """Create a scratch dataset, deleted afterwards.

    Args:
        client: The BigQuery client.

    Yields:
        The dataset's name.
    """
    name = f"py_common_dry_run_{uuid.uuid4().hex[:8]}"
    ds = bigquery.Dataset(f"{PROJECT}.{name}")
    ds.default_table_expiration_ms = 24 * 3600 * 1000
    client.create_dataset(ds)
    try:
        yield name
    finally:
        client.delete_dataset(ds, delete_contents=True, not_found_ok=True)


def _create_tables(client: Any, dataset: str, sql: list[str]) -> None:
    """Create every table the statements name, with the right schema.

    Args:
        client: The BigQuery client.
        dataset: The scratch dataset.
        sql: The statements.

    Raises:
        AssertionError: A statement names a table this test doesn't
            know the schema of.
    """
    named = set()
    for text in sql:
        named.update(re.findall(rf"`{PROJECT}\.{dataset}\.(\w+)`", text))
    for table in named:
        if table == "audit":
            schema = AUDIT_SCHEMA
        elif table.startswith("raw_s_stg_"):
            schema = STAGING_SCHEMA
        elif table == "raw_s":
            schema = RAW_SCHEMA
        else:
            raise AssertionError(f"unexpected table {table}")
        client.create_table(
            bigquery.Table(f"{PROJECT}.{dataset}.{table}", schema=schema),
            exists_ok=True,
        )


def test_every_generated_statement_passes_a_dry_run(
    client: Any, dataset: str
) -> None:
    """BigQuery accepts every statement the adapter generates.

    Args:
        client: The BigQuery client.
        dataset: The scratch dataset.
    """
    statements = _statements(dataset)
    assert any("BEGIN TRANSACTION" in s for s, _ in statements)
    _create_tables(client, dataset, [s for s, _ in statements])

    failures = []
    for sql, config in statements:
        params = getattr(config, "query_parameters", None) or []
        dry = bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params
        )
        try:
            client.query(sql, job_config=dry)
        except Exception as e:  # report every failure, not just one
            failures.append(f"{e}\n---\n{sql}")

    assert not failures, "\n\n".join(failures)


def test_recorder_sees_the_insert_into_the_raw_table(dataset: str) -> None:
    """Guard: the statements really include the insert we ship.

    Args:
        dataset: The scratch dataset.
    """
    sql = "\n".join(s for s, _ in _statements(dataset))

    assert f"INSERT INTO `{PROJECT}.{dataset}.raw_s`" in sql
