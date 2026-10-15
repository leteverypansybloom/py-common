"""Unit tests for RawJsonWarehouse adapter.

Tests define the Warehouse protocol contract for raw JSON tables
without requiring live BigQuery services. All external dependencies
are mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test documents expected behavior
- Transparent: No hardcoded GCP credentials in tests
"""

import json

import pytest
from unittest.mock import MagicMock, Mock, patch

from google.api_core.exceptions import NotFound

from py_common.errors import ServiceError
from py_common.model import SourceItem
from py_common.adapters.bigquery_raw import (
    RAW_SCHEMA,
    STAGING_SCHEMA,
    RawJsonWarehouse,
)

#: Source file passed to every load().
ITEM = SourceItem("test.xlsx", "landing/test.xlsx", "1", 10)


TABLES = {
    "Account signups": "hww_account_signups",
    "Survey info": "hww_survey_info",
}


def _ndjson(*sheets: str) -> bytes:
    """NDJSON with one minimal row per sheet name given.

    Args:
        *sheets: Sheet name for each row, in order.

    Returns:
        NDJSON bytes, as py_common.raw.to_ndjson() would write.
    """
    return b"".join(
        json.dumps(
            {
                "source_file": "hww.xlsx",
                "source_generation": "1",
                "sheet_name": sheet,
                "row_number": number,
                "checksum": "abc123",
                "payload": {"Org": f"Org {number}"},
            }
        ).encode()
        + b"\n"
        for number, sheet in enumerate(sheets, start=3)
    )


def _load_job(output_rows: int) -> Mock:
    """Mock load job reporting how many rows it staged.

    Args:
        output_rows: Rows the job loaded.

    Returns:
        Mock load job.
    """
    job = Mock()
    job.result.return_value = None
    job.output_rows = output_rows
    return job


class TestRawJsonWarehouseInit:
    """Test RawJsonWarehouse initialization."""

    def test_init_sets_properties(self):
        """__init__() stores project, dataset and table mapping.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If properties not set
        """
        with patch("py_common.adapters.bigquery_raw.bigquery.Client"):
            warehouse = RawJsonWarehouse("p", "raw_hww", TABLES)

            assert warehouse.project_id == "p"
            assert warehouse.dataset_id == "raw_hww"
            assert warehouse.tables == TABLES
            assert warehouse.audit_table_id == "audit"

    def test_init_rejects_no_tables(self):
        """At least one sheet must map to a table.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If ValueError isn't raised
        """
        with patch("py_common.adapters.bigquery_raw.bigquery.Client"):
            with pytest.raises(ValueError, match="at least one table"):
                RawJsonWarehouse("p", "raw_hww", {})

    def test_init_rejects_two_sheets_sharing_a_table(self):
        """Each sheet gets its own raw table.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If ValueError isn't raised
        """
        with patch("py_common.adapters.bigquery_raw.bigquery.Client"):
            with pytest.raises(ValueError, match="its own raw table"):
                RawJsonWarehouse("p", "raw_hww", {"A": "t", "B": "t"})


class TestRawJsonWarehouseIdentity:
    """Test RawJsonWarehouse.identity property."""

    def test_identity_names_every_table_sorted(self):
        """Identity is stable whatever order the mapping is given in.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If identity is wrong
        """
        with patch("py_common.adapters.bigquery_raw.bigquery.Client"):
            warehouse = RawJsonWarehouse(
                "p", "raw_hww", dict(reversed(TABLES.items()))
            )

            assert warehouse.identity == (
                "p.raw_hww.hww_account_signups+hww_survey_info"
            )


class TestRawJsonWarehouseLoad:
    """Test RawJsonWarehouse.load() method.

    All-or-nothing contract:
    1. Split rows by sheet; refuse unknown sheets
    2. Create missing raw tables
    3. Load each sheet into its own staging table
    4. Verify the total staged row count
    5. Insert into every raw table and record the audit entry in one
       transaction; commit or roll back entirely
    """

    @pytest.fixture
    def mock_bq_client(self):
        """Fixture: Mock BigQuery client whose tables all exist."""
        client = MagicMock()
        client.load_table_from_file.return_value = _load_job(1)
        client.query.return_value.result.return_value = [("LOADED",)]
        return client

    @pytest.fixture
    def warehouse(self, mock_bq_client):
        """Fixture: RawJsonWarehouse with mocked client."""
        with patch(
            "py_common.adapters.bigquery_raw.bigquery.Client"
        ) as mock_client_class:
            mock_client_class.return_value = mock_bq_client
            return RawJsonWarehouse("p", "raw_hww", TABLES)

    def test_each_sheet_is_staged_separately(self, warehouse, mock_bq_client):
        """Each sheet's lines go to its own staging table.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If staging loads are wrong
        """
        data = _ndjson("Account signups", "Survey info")

        warehouse.load(
            key="hww.xlsx", checksum="abc123", data=data, rows=2, item=ITEM
        )

        calls = mock_bq_client.load_table_from_file.call_args_list
        destinations = [c.args[1] for c in calls]
        assert [d.split("_stg_")[0] for d in destinations] == [
            "p.raw_hww.hww_account_signups",
            "p.raw_hww.hww_survey_info",
        ]
        assert len(set(destinations)) == 2
        staged = [c.args[0].getvalue() for c in calls]
        assert b"".join(staged) == data

    def test_staging_load_truncates_with_fixed_schema(
        self, warehouse, mock_bq_client
    ):
        """Staging is emptied first and payload is loaded as JSON.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If the load job config is wrong
        """
        warehouse.load(
            key="hww.xlsx",
            checksum="abc123",
            data=_ndjson("Survey info"),
            rows=1,
            item=ITEM,
        )

        config = mock_bq_client.load_table_from_file.call_args.kwargs[
            "job_config"
        ]
        assert config.write_disposition == "WRITE_TRUNCATE"
        assert config.source_format == "NEWLINE_DELIMITED_JSON"
        assert config.schema == STAGING_SCHEMA

    def test_missing_raw_table_is_created_partitioned(
        self, warehouse, mock_bq_client
    ):
        """A first load creates the raw table, partitioned by day.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If the table isn't created as expected
        """
        mock_bq_client.get_table.side_effect = NotFound("missing")

        warehouse.load(
            key="hww.xlsx",
            checksum="abc123",
            data=_ndjson("Survey info"),
            rows=1,
            item=ITEM,
        )

        [table] = [
            c.args[0]
            for c in mock_bq_client.create_table.call_args_list
            if "_stg_" not in c.args[0].table_id
        ]
        assert table.table_id == "hww_survey_info"
        assert table.schema == RAW_SCHEMA
        assert table.time_partitioning.field == "ingested_at"

    def test_existing_raw_table_is_left_alone(self, warehouse, mock_bq_client):
        """No create call when the raw table already exists.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If create_table is called
        """
        warehouse.load(
            key="hww.xlsx",
            checksum="abc123",
            data=_ndjson("Survey info"),
            rows=1,
            item=ITEM,
        )

        created = [
            c.args[0].table_id
            for c in mock_bq_client.create_table.call_args_list
        ]
        # Only this load's own staging table is created.
        assert [t.split("_stg_")[0] for t in created] == ["hww_survey_info"]

    def test_unknown_sheet_is_refused_before_loading(
        self, warehouse, mock_bq_client
    ):
        """Rows for a sheet with no table load nothing at all.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If ValueError isn't raised first
        """
        data = _ndjson("Survey info", "Newsletter metrics")

        with pytest.raises(ValueError, match="Newsletter metrics"):
            warehouse.load(
                key="hww.xlsx", checksum="abc123", data=data, rows=2, item=ITEM
            )
        mock_bq_client.load_table_from_file.assert_not_called()

    def test_malformed_line_is_refused(self, warehouse):
        """A line that isn't a raw row is a ValueError.

        Args:
            warehouse: RawJsonWarehouse fixture

        Returns:
            None

        Raises:
            AssertionError: If ValueError isn't raised
        """
        with pytest.raises(ValueError, match="Line 1"):
            warehouse.load(
                key="hww.xlsx",
                checksum="abc123",
                data=b"{}\n",
                rows=1,
                item=ITEM,
            )

    def test_row_count_mismatch_stops_before_transaction(
        self, warehouse, mock_bq_client
    ):
        """Staged rows must add up to the expected total.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If ValueError isn't raised or a query runs
        """
        mock_bq_client.load_table_from_file.return_value = _load_job(0)

        with pytest.raises(ValueError, match="Row count mismatch"):
            warehouse.load(
                key="hww.xlsx",
                checksum="abc123",
                data=_ndjson("Survey info"),
                rows=1,
                item=ITEM,
            )
        mock_bq_client.query.assert_not_called()

    def test_inserts_and_audit_run_in_one_transaction(
        self, warehouse, mock_bq_client
    ):
        """Every sheet's insert and the audit row are one job.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If the SQL isn't one transaction
        """
        warehouse.load(
            key="hww.xlsx",
            checksum="abc123",
            data=_ndjson("Account signups", "Survey info"),
            rows=2,
            item=ITEM,
        )

        mock_bq_client.query.assert_called_once()
        sql = mock_bq_client.query.call_args.args[0]
        assert "BEGIN TRANSACTION" in sql
        assert "INSERT INTO `p.raw_hww.hww_account_signups`" in sql
        assert "INSERT INTO `p.raw_hww.hww_survey_info`" in sql
        assert "FROM `p.raw_hww.hww_survey_info_stg_abc123_" in sql
        assert "INSERT INTO `p.raw_hww.audit`" in sql
        assert "COMMIT TRANSACTION" in sql
        assert "ROLLBACK TRANSACTION" in sql

    def test_ingested_at_is_the_audit_timestamp(
        self, warehouse, mock_bq_client
    ):
        """Rows and audit record share one @timestamp parameter.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If ingested_at isn't @timestamp
        """
        warehouse.load(
            key="hww.xlsx",
            checksum="abc123",
            data=_ndjson("Survey info"),
            rows=1,
            item=ITEM,
        )

        sql = mock_bq_client.query.call_args.args[0]
        config = mock_bq_client.query.call_args.kwargs["job_config"]
        params = {p.name for p in config.query_parameters}
        assert "ingested_at) SELECT" in sql
        assert "@timestamp FROM" in sql
        assert params == {
            "filename",
            "checksum",
            "destination",
            "source_version",
            "row_count",
            "outcome",
            "timestamp",
        }

    def test_failed_transaction_raises_service_error(
        self, warehouse, mock_bq_client
    ):
        """A transaction BigQuery reports as failed is a ServiceError.

        More outcomes (timeouts, unknown state) are covered for both
        adapters in test_bigquery_errors.py.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If ServiceError isn't raised
        """
        job = mock_bq_client.query.return_value
        job.result.side_effect = Exception("quota exceeded")
        job.state = "DONE"
        job.error_result = {"reason": "quotaExceeded"}

        with pytest.raises(ServiceError, match="quotaExceeded"):
            warehouse.load(
                key="hww.xlsx",
                checksum="abc123",
                data=_ndjson("Survey info"),
                rows=1,
                item=ITEM,
            )

    def test_file_with_no_rows_still_records_audit(
        self, warehouse, mock_bq_client
    ):
        """An empty sheet loads nothing but the audit row.

        Args:
            warehouse: RawJsonWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If staging is touched or audit missing
        """
        warehouse.load(
            key="hww.xlsx", checksum="abc123", data=b"", rows=0, item=ITEM
        )

        mock_bq_client.load_table_from_file.assert_not_called()
        sql = mock_bq_client.query.call_args.args[0]
        assert "INSERT INTO `p.raw_hww.audit`" in sql
