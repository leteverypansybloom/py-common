"""BigQueryWarehouse reconciles staging with the final table."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from google.api_core.exceptions import NotFound

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.errors import ContractError
from py_common.model import SourceItem

ITEM = SourceItem("k.xlsx", "landing/k.xlsx", "1", 10)
STAGING = "p.d.events_staging"
FINAL = "p.d.events"


def _table(**fields: str) -> SimpleNamespace:
    """Table with a schema of name -> BigQuery type."""
    return SimpleNamespace(
        schema=[
            SimpleNamespace(name=n, field_type=t, mode="NULLABLE")
            for n, t in fields.items()
        ]
    )


def _warehouse(
    staging: SimpleNamespace, final: SimpleNamespace | None
) -> tuple[BigQueryWarehouse, MagicMock]:
    """Append-only warehouse whose tables have the given schemas."""
    client = MagicMock()
    with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = BigQueryWarehouse("p", "d", "events")

    def get_table(table_id: str) -> Any:
        """Return the final table, or raise NotFound if it isn't set.

        Args:
            table_id: The full table ID.

        Returns:
            The final or staging table.

        Raises:
            NotFound: The final table was asked for and doesn't exist.
        """
        if table_id == FINAL:
            if final is None:
                raise NotFound(table_id)
            return final
        return staging

    client.get_table.side_effect = get_table
    client.load_table_from_file.return_value = Mock(
        job_id="load-1", output_rows=1
    )

    def query(sql: str, **_: Any) -> Mock:
        """Answer the commit, the row count and anything else.

        Args:
            sql: The query text.
            **_: Other query arguments, ignored.

        Returns:
            The job.
        """
        job = Mock(job_id="q")
        if "BEGIN TRANSACTION" in sql:
            job.result.return_value = [("LOADED",)]
        elif "COUNT(*)" in sql:
            job.result.return_value = [(1,)]
        else:
            job.result.return_value = []
        return job

    client.query.side_effect = query
    return warehouse, client


def _sql(client: MagicMock) -> list[str]:
    """List the queries the warehouse ran.

    Args:
        client: The mocked BigQuery client.

    Returns:
        Their SQL, in order.
    """
    return [c.args[0] for c in client.query.call_args_list]


def test_matching_schema_changes_nothing() -> None:
    """INTEGER and INT64 match, so no ALTER TABLE is run."""
    warehouse, client = _warehouse(
        _table(id="INTEGER", name="STRING"),
        _table(id="INT64", name="STRING", _loaded_at="TIMESTAMP"),
    )

    assert warehouse.load("k", "c", b"pq", 1, ITEM) is True
    assert not any("ALTER TABLE" in s for s in _sql(client))


def test_new_column_is_added_before_the_merge() -> None:
    """A column new in staging is added to the final table first."""
    warehouse, client = _warehouse(
        _table(id="INTEGER", notes="STRING"),
        _table(id="INTEGER", _loaded_at="TIMESTAMP"),
    )

    warehouse.load("k", "c", b"pq", 1, ITEM)

    sql = _sql(client)
    [alter] = [s for s in sql if "ALTER TABLE" in s]
    assert alter == (
        f"ALTER TABLE `{FINAL}` ADD COLUMN IF NOT EXISTS `notes` STRING"
    )
    commit = next(i for i, s in enumerate(sql) if "BEGIN TRANSACTION" in s)
    assert sql.index(alter) < commit


def test_changed_type_is_contract_error_with_diff() -> None:
    """A changed type is a ContractError showing old and new types."""
    warehouse, client = _warehouse(
        _table(id="STRING"),
        _table(id="INTEGER", _loaded_at="TIMESTAMP"),
    )

    with pytest.raises(ContractError, match=r"id: INTEGER -> STRING"):
        warehouse.load("k", "c", b"pq", 1, ITEM)
    assert not any("BEGIN TRANSACTION" in s for s in _sql(client))


def test_removed_column_is_contract_error() -> None:
    """A column missing from staging is a ContractError naming it."""
    warehouse, client = _warehouse(
        _table(id="INTEGER"),
        _table(id="INTEGER", region="STRING", _loaded_at="TIMESTAMP"),
    )

    with pytest.raises(ContractError, match="removed: region"):
        warehouse.load("k", "c", b"pq", 1, ITEM)
    assert not any("BEGIN TRANSACTION" in s for s in _sql(client))


def test_missing_final_table_is_created_from_staging() -> None:
    """A missing final table is created from the staging schema."""
    warehouse, client = _warehouse(_table(id="INTEGER", name="STRING"), None)

    warehouse.load("k", "c", b"pq", 1, ITEM)

    created = [c.args[0] for c in client.create_table.call_args_list]
    final = [t for t in created if t.table_id == "events"]
    assert len(final) == 1
    assert [(f.name, f.field_type) for f in final[0].schema] == [
        ("id", "INTEGER"),
        ("name", "STRING"),
        ("_loaded_at", "TIMESTAMP"),
    ]
