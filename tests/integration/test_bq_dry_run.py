r"""Dry-run every SQL statement the BigQuery adapters generate.

The unit tests check SQL text against mocks; this checks BigQuery
accepts it. Each adapter's load() runs against a
recording client, which answers like BigQuery would and keeps every
statement with its parameters. A scratch dataset then gets every
table those statements name, and each statement is sent with
QueryJobConfig(dry_run=True): parsed, planned and checked against the
real tables, but never run, so it costs nothing.

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

from py_common.adapters.bigquery import BigQueryWarehouse  # noqa: E402
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
TYPED_STAGING = [
    bigquery.SchemaField("id", "INT64"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("notes", "STRING"),
]
TYPED_FINAL = [
    bigquery.SchemaField("id", "INT64"),
    bigquery.SchemaField("name", "STRING"),
    bigquery.SchemaField("_loaded_at", "TIMESTAMP"),
]


class Recorder:
    """Stands in for bigquery.Client; records every query.

    Answers so that load() walks every SQL path: no duplicate keys,
    a LOADED status, and a final table missing the staging table's
    "notes" column (so the ALTER TABLE is generated too).
    """

    def __init__(self, dataset: str) -> None:
        """Start recording.

        Args:
            dataset: The dataset the statements name.
        """
        self.dataset = dataset
        self.statements: list[tuple[str, Any]] = []

    def get_table(self, table_id: str) -> Any:
        """Return the typed tables' schemas; nothing else exists.

        Args:
            table_id: The full table ID.

        Returns:
            An object with a schema list, like a bigquery.Table.

        Raises:
            NotFound: Any other table.
        """
        schema = TYPED_STAGING if "_stg_" in table_id else TYPED_FINAL
        if table_id.endswith(".events") or "_stg_" in table_id:
            return SimpleNamespace(schema=schema)
        raise NotFound(table_id)

    def create_table(self, table: Any, exists_ok: bool = False) -> None:
        """Do nothing: tables are created for real later.

        Args:
            table: The table to create.
            exists_ok: Ignored.
        """
        pass

    def delete_table(self, table_id: str, not_found_ok: bool = False) -> None:
        """Do nothing: the scratch dataset is deleted whole.

        Args:
            table_id: The table to delete.
            not_found_ok: Ignored.
        """
        pass

    def load_table_from_file(self, file: Any, table_id: str, **_: Any) -> Any:
        """Pretend to stage one row.

        Args:
            file: The data, ignored.
            table_id: The staging table.
            **_: Load options, ignored.

        Returns:
            A finished load job reporting one row.
        """
        # Every load stages the one row the callers below expect.
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
        if "BEGIN TRANSACTION" in sql:
            rows = [("LOADED",)]
        else:
            rows = []
        return SimpleNamespace(job_id="q", result=lambda **_: rows)


def _statements(dataset: str) -> list[tuple[str, Any]]:
    """Every statement both adapters generate for one load."""
    recorder = Recorder(dataset)
    with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
        cls.return_value = recorder
        BigQueryWarehouse(PROJECT, dataset, "events", key_columns=["id"]).load(
            "k", CHECKSUM, b"PAR1", 1, ITEM
        )
        BigQueryWarehouse(PROJECT, dataset, "events").load(
            "k", CHECKSUM, b"PAR1", 1, ITEM
        )
        BigQueryWarehouse(
            PROJECT,
            dataset,
            "events",
            key_columns=["id"],
            mark_deleted_rows=True,
        ).load("k", CHECKSUM, b"PAR1", 1, ITEM)
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
    """Create every table the statements name, with the right schema."""
    named = set()
    for text in sql:
        named.update(re.findall(rf"`{PROJECT}\.{dataset}\.(\w+)`", text))
    for table in named:
        if table == "audit":
            schema = AUDIT_SCHEMA
        elif table == "events":
            # As after the ALTER TABLE: a dry run doesn't apply it,
            # and the MERGE that follows writes the new column.
            schema = [
                *TYPED_STAGING,
                bigquery.SchemaField("_loaded_at", "TIMESTAMP"),
                bigquery.SchemaField("_deleted_at", "TIMESTAMP"),
                bigquery.SchemaField("_source_file", "STRING"),
            ]
        elif table.startswith("events_stg_"):
            schema = TYPED_STAGING
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
    """BigQuery accepts every statement the adapters generate.

    Args:
        client: The BigQuery client.
        dataset: The scratch dataset.
    """
    statements = _statements(dataset)
    kinds = {
        "commit script": any("BEGIN TRANSACTION" in s for s, _ in statements),
        "key check": any("HAVING" in s for s, _ in statements),
        "schema update": any("ALTER TABLE" in s for s, _ in statements),
        "deleted-row marking": any(
            "NOT MATCHED BY SOURCE" in s for s, _ in statements
        ),
    }
    assert all(kinds.values()), kinds
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


def test_recorder_sees_the_typed_route_merge(dataset: str) -> None:
    """Guard: the statements really include the MERGE we ship."""
    sql = "\n".join(s for s, _ in _statements(dataset))

    assert "MERGE INTO" in sql
    assert "ON T.`id` = S.`id`" in sql
