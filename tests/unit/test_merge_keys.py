"""Merge keys are validated, never silently dropped.

Contract: key_columns must be listed, non-nullable columns of the
primary worksheet, and a file whose keys repeat is rejected before
anything reaches BigQuery. BigQueryWarehouse: a key column missing
from staging is a ContractError, and duplicate or NULL keys in
staging are a ValidationError (quarantine) found before the MERGE.
"""

from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from openpyxl import Workbook

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ContractError, ValidationError
from py_common.model import SourceItem

ITEM = SourceItem("k.xlsx", "landing/k.xlsx", "1", 10)


def _sheet(**kwargs: Any) -> Worksheet:
    """An "Events" worksheet with id (int), region and count."""
    columns = [
        Column("id", "integer"),
        Column("region", "string"),
        Column("count", "integer", nullable=True),
    ]
    return Worksheet("Events", columns=columns, **kwargs)


def _workbook(rows: list[tuple[Any, ...]]) -> bytes:
    """Events workbook holding `rows` under id/region/count."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    ws.append(["id", "region", "count"])
    for row in rows:
        ws.append(list(row))
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class TestContractKeyColumnsConfig:
    """Contract.__post_init__ rejects unusable key_columns."""

    def test_listed_non_nullable_keys_are_accepted(self) -> None:
        """Listed, required columns are valid keys."""
        contract = Contract(
            key_columns=["id", "region"], worksheets=[_sheet()]
        )
        assert contract.key_columns == ["id", "region"]

    def test_key_not_listed_in_primary_sheet_is_rejected(self) -> None:
        """A key that isn't a listed column is refused, naming it."""
        with pytest.raises(ValueError, match="'idd'.*not a listed column"):
            Contract(key_columns=["idd"], worksheets=[_sheet()])

    def test_nullable_key_is_rejected(self) -> None:
        """A nullable column can't be a key."""
        with pytest.raises(ValueError, match="'count'.*nullable"):
            Contract(key_columns=["count"], worksheets=[_sheet()])

    def test_key_from_a_later_sheet_is_rejected(self) -> None:
        """Only the primary (first) worksheet is merged."""
        other = Worksheet("Other", columns=[Column("other_id", "integer")])
        with pytest.raises(ValueError, match="'other_id'"):
            Contract(key_columns=["other_id"], worksheets=[_sheet(), other])

    def test_keys_without_worksheets_are_rejected(self) -> None:
        """Keys need a worksheet to belong to."""
        with pytest.raises(ValueError, match="no worksheets"):
            Contract(key_columns=["id"])

    def test_no_keys_needs_no_worksheet(self) -> None:
        """Append-only contracts are unaffected."""
        assert Contract().key_columns == []


class TestContractKeyUniqueness:
    """Contract.validate checks the composite key per row."""

    def test_repeated_composite_key_is_quarantined(self) -> None:
        """A repeated (id, region) pair is rejected, naming the rows."""
        contract = Contract(
            key_columns=["id", "region"], worksheets=[_sheet()]
        )
        data = _workbook([(1, "N", 5), (1, "S", 6), (1, "N", 7)])

        with pytest.raises(
            ValidationError,
            match=r"Row 4: Duplicate key in \(id, region\), first on row 2",
        ):
            contract.validate(data, "f.xlsx")

    def test_same_id_different_region_is_fine(self) -> None:
        """Only the whole key has to be unique."""
        contract = Contract(
            key_columns=["id", "region"], worksheets=[_sheet()]
        )
        data = _workbook([(1, "N", 5), (1, "S", 6)])

        assert contract.validate(data, "f.xlsx") == 2

    def test_warn_mode_records_duplicate_key_as_warning(self) -> None:
        """A "warn" sheet records a repeated key as a warning."""
        contract = Contract(
            key_columns=["id"], worksheets=[_sheet(on_mismatch="warn")]
        )
        warnings: list[str] = []

        contract.validate(
            _workbook([(1, "N", 5), (1, "S", 6)]), "f.xlsx", warnings
        )

        assert any("Duplicate key" in w for w in warnings)

    def test_null_key_is_reported_once_as_required(self) -> None:
        """A missing key is reported as required, not also as repeated."""
        contract = Contract(key_columns=["id"], worksheets=[_sheet()])

        with pytest.raises(ValidationError) as info:
            contract.validate(
                _workbook([(None, "N", 5), (None, "S", 6)]), "f.xlsx"
            )

        assert "is required" in str(info.value)
        assert "Duplicate key" not in str(info.value)


def _warehouse(
    staging_columns: list[str], duplicates: list[tuple[Any, ...]]
) -> tuple[BigQueryWarehouse, MagicMock]:
    """Typed warehouse keyed on id+region over a mocked client.

    Args:
        staging_columns: Columns BigQuery "inferred" for staging.
        duplicates: Rows the duplicate-key query returns.

    Returns:
        (warehouse, client).
    """
    client = MagicMock()
    with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
        cls.return_value = client
        warehouse = BigQueryWarehouse(
            "p", "d", "events", key_columns=["id", "region"]
        )
    client.get_table.return_value = SimpleNamespace(
        schema=[
            SimpleNamespace(name=c, field_type="STRING")
            for c in staging_columns
        ]
    )
    client.load_table_from_file.return_value = Mock(
        job_id="load-1", output_rows=2
    )

    def query(sql: str, **_: Any) -> Mock:
        """Answer the duplicate check, the commit and row counts.

        Args:
            sql: The query text.
            **_: Other query arguments, ignored.

        Returns:
            The job.
        """
        job = Mock(job_id="q")
        if "HAVING" in sql:
            job.result.return_value = duplicates
        elif "BEGIN TRANSACTION" in sql:
            job.result.return_value = [("LOADED",)]
        else:
            job.result.return_value = [(2,)]
        return job

    client.query.side_effect = query
    return warehouse, client


def _scripts(client: MagicMock) -> list[str]:
    """Find the commit scripts load() ran.

    Args:
        client: The mocked BigQuery client.

    Returns:
        The scripts, in order.
    """
    return [
        c.args[0]
        for c in client.query.call_args_list
        if "BEGIN TRANSACTION" in c.args[0]
    ]


class TestWarehouseKeyChecks:
    """BigQueryWarehouse.load refuses keys it can't merge on."""

    def test_missing_key_column_is_contract_error(self) -> None:
        """A renamed key column no longer turns MERGE into append."""
        warehouse, client = _warehouse(["id", "count"], [])

        with pytest.raises(ContractError, match="region"):
            warehouse.load("k", "c", b"parquet", 2, ITEM)
        assert _scripts(client) == []

    def test_duplicate_keys_are_validation_error_with_counts(self) -> None:
        """Counts only: key values are data and reach logs."""
        warehouse, client = _warehouse(
            ["id", "region", "count"], [(1, "N", 2, 0), (7, "S", 3, 0)]
        )

        with pytest.raises(
            ValidationError, match=r"\(id, region\).*2 repeated key"
        ):
            warehouse.load("k", "c", b"parquet", 2, ITEM)
        assert _scripts(client) == []

    def test_null_keys_are_validation_error(self) -> None:
        """NULL never matches in MERGE, so it would insert every time."""
        warehouse, client = _warehouse(
            ["id", "region", "count"], [(None, "N", 1, 1)]
        )

        with pytest.raises(ValidationError, match="NULL"):
            warehouse.load("k", "c", b"parquet", 2, ITEM)
        assert _scripts(client) == []

    def test_clean_keys_merge_on_every_key_column(self) -> None:
        """The MERGE matches on every key column."""
        warehouse, client = _warehouse(["id", "region", "count"], [])

        assert warehouse.load("k", "c", b"parquet", 2, ITEM) is True
        [script] = _scripts(client)
        assert "ON T.`id` = S.`id` AND T.`region` = S.`region`" in script

    def test_duplicate_query_groups_by_every_key(self) -> None:
        """The duplicate check groups by every key column."""
        warehouse, client = _warehouse(["id", "region", "count"], [])

        warehouse.load("k", "c", b"parquet", 2, ITEM)

        [check] = [
            c.args[0]
            for c in client.query.call_args_list
            if "HAVING" in c.args[0]
        ]
        assert "GROUP BY `id`, `region`" in check
        assert "COUNT(*) > 1" in check
        assert "`id` IS NULL OR `region` IS NULL" in check
        assert "LIMIT 5" in check
