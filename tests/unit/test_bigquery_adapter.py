"""Unit tests for BigQueryWarehouse adapter.

Tests define the Warehouse protocol contract without requiring live
BigQuery services. All external dependencies are mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test documents expected behavior
- Transparent: No hardcoded GCP credentials in tests
"""

from types import SimpleNamespace

import pytest
from unittest.mock import Mock, MagicMock, patch

from py_common.adapters.bigquery import BigQueryWarehouse


def _staging_table(*column_names: str) -> Mock:
    """Mock bigquery.Table with a .schema iterable of named fields.

    get_table() is called after the Parquet load to read back the
    inferred staging schema, so tests must give it something
    iterable with `.name` attributes rather than a bare Mock().
    """
    table = Mock()
    table.schema = [SimpleNamespace(name=name) for name in column_names]
    return table


class TestBigQueryWarehouseInit:
    """Test BigQueryWarehouse initialization."""

    def test_init_sets_properties(self):
        """__init__() sets all properties correctly.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If properties not set
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
            )
            assert warehouse.project_id == "test-project"
            assert warehouse.dataset_id == "test-dataset"
            assert warehouse.table_id == "test_table"

    def test_init_sets_staging_table_name(self):
        """__init__() derives staging table name.

        Staging table is {table_id}_staging.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If staging table name not derived
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
            )
            assert warehouse.staging_table_id == "test_table_staging"

    def test_init_default_audit_table(self):
        """__init__() uses default audit_table_id.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If audit_table_id not default
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
            )
            assert warehouse.audit_table_id == "audit"

    def test_init_custom_audit_table(self):
        """__init__() accepts custom audit_table_id.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If custom audit_table_id not used
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
                audit_table_id="compliance_log",
            )
            assert warehouse.audit_table_id == "compliance_log"


class TestBigQueryWarehouseIdentity:
    """Test BigQueryWarehouse.identity property."""

    def test_identity_fully_qualified(self):
        """identity property returns fully-qualified table name.

        Used for distributed locking (unique per warehouse).
        Format: project.dataset.table

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If identity not fully qualified
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="myproject",
                dataset_id="mydataset",
                table_id="mytable",
            )
            expected = "myproject.mydataset.mytable"
            assert warehouse.identity == expected


class TestBigQueryWarehouseLoad:
    """Test BigQueryWarehouse.load() method.

    All-or-nothing contract:
    1. Create staging table if not exists
    2. Load Parquet into staging
    3. Verify row count matches
    4. MERGE into final table
    5. Record audit entry
    6. Commit or roll back entirely (never partial writes)
    """

    @pytest.fixture
    def mock_bq_client(self):
        """Fixture: Mock BigQuery client."""
        return MagicMock()

    @pytest.fixture
    def warehouse(self, mock_bq_client):
        """Fixture: BigQueryWarehouse with mocked client."""
        with patch(
            "py_common.adapters.bigquery.bigquery.Client"
        ) as mock_client_class:
            mock_client_class.return_value = mock_bq_client
            return BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
            )

    def test_load_verifies_row_count(self, warehouse, mock_bq_client):
        """load() verifies row count matches expectation.

        Args:
            warehouse: BigQueryWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If row count not verified
        """
        # Mock query result for row count check (matches expectation).
        mock_query_job = Mock()
        mock_query_result = [(42,)]  # Actual row count
        mock_query_job.result.return_value = mock_query_result
        mock_bq_client.query.return_value = mock_query_job

        # Mock load_table_from_file.
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        # Mock get_table for staging (existence check + schema read).
        mock_bq_client.get_table.return_value = _staging_table("col_a")

        # load() should succeed with matching row count.
        warehouse.load(
            key="test.xlsx", checksum="abc123", data=b"parquet data", rows=42
        )

    def test_load_raises_on_row_count_mismatch(
        self, warehouse, mock_bq_client
    ):
        """load() raises ValueError on row count mismatch.

        Args:
            warehouse: BigQueryWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If ValueError not raised
        """
        # Mock query result with mismatched row count.
        mock_query_job = Mock()
        mock_query_result = [(50,)]  # Different from expected (42)
        mock_query_job.result.return_value = mock_query_result
        mock_bq_client.query.return_value = mock_query_job

        # Mock load_table_from_file.
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        # Mock get_table for staging.
        mock_bq_client.get_table.return_value = _staging_table("col_a")

        # load() should raise on mismatch.
        with pytest.raises(ValueError, match="Row count mismatch"):
            warehouse.load(
                key="test.xlsx",
                checksum="abc123",
                data=b"parquet data",
                rows=42,
            )

    def test_load_records_audit_entry(self, warehouse, mock_bq_client):
        """load() records audit entry after successful merge.

        Args:
            warehouse: BigQueryWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If audit not recorded
        """
        # Mock query result for row count.
        mock_query_job = Mock()
        mock_query_job.result.return_value = [(42,)]
        mock_bq_client.query.return_value = mock_query_job

        # Mock load_table_from_file.
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        # Mock get_table.
        mock_bq_client.get_table.return_value = _staging_table("col_a")

        # Call load().
        warehouse.load(
            key="test.xlsx", checksum="abc123", data=b"parquet data", rows=42
        )

        # Verify audit query was called.
        # Check that query() was called with INSERT INTO ... audit
        calls = [call[0][0] for call in mock_bq_client.query.call_args_list]
        audit_called = any(
            "INSERT INTO" in call and "audit" in call for call in calls
        )
        assert audit_called

    def test_load_merges_and_audits_in_one_transaction(
        self, warehouse, mock_bq_client
    ):
        """load() commits the final-table write and the audit record
        together in a single BigQuery script transaction.

        Fix for issue #1: previously these were two separate query
        jobs, so a final-insert success followed by an audit-insert
        failure left committed data with no audit record. Now both
        statements live inside one BEGIN TRANSACTION / COMMIT
        TRANSACTION script submitted as a single job.

        Args:
            warehouse: BigQueryWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If merge and audit are not one job
        """
        mock_bq_client.get_table.return_value = _staging_table(
            "event_id", "attendees"
        )
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        mock_query_job = Mock()
        mock_query_job.result.return_value = [(1,)]
        mock_bq_client.query.return_value = mock_query_job

        warehouse.load(
            key="test.xlsx", checksum="abc123", data=b"parquet data", rows=1
        )

        sql_calls = [
            call[0][0] for call in mock_bq_client.query.call_args_list
        ]
        txn_calls = [s for s in sql_calls if "BEGIN TRANSACTION" in s]

        # Exactly one job carries the transaction (the other query()
        # call is the plain row-count check).
        assert len(txn_calls) == 1
        assert "MERGE INTO" in txn_calls[0]
        assert "INSERT INTO" in txn_calls[0] and "audit" in txn_calls[0]
        assert "COMMIT TRANSACTION" in txn_calls[0]
        assert "ROLLBACK TRANSACTION" in txn_calls[0]

    def test_load_raises_runtime_error_when_transaction_fails(
        self, warehouse, mock_bq_client
    ):
        """A failed transaction surfaces as RuntimeError, not silently.

        Also proves the merge and audit are one job: the same
        (simulated) failure that would break the audit INSERT is
        what fails the whole job, rather than a separate audit-only
        query call succeeding independently.

        Args:
            warehouse: BigQueryWarehouse fixture
            mock_bq_client: Mocked BigQuery client

        Returns:
            None

        Raises:
            AssertionError: If RuntimeError not raised
        """
        mock_bq_client.get_table.return_value = _staging_table("event_id")
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        mock_count_job = Mock()
        mock_count_job.result.return_value = [(1,)]

        mock_txn_job = Mock()
        mock_txn_job.result.side_effect = Exception("audit insert failed")

        mock_bq_client.query.side_effect = [mock_count_job, mock_txn_job]

        with pytest.raises(RuntimeError, match="transaction failed"):
            warehouse.load(
                key="test.xlsx",
                checksum="abc123",
                data=b"parquet data",
                rows=1,
            )


class TestBigQueryWarehouseMergeSql:
    """Test BigQueryWarehouse._build_merge_sql().

    Fix for issue #2: the final table must be upserted on the
    contract's key columns, not appended to with a plain INSERT
    (which duplicates rows on reprocess).
    """

    @pytest.fixture
    def warehouse(self):
        """Fixture: BigQueryWarehouse with key_columns configured."""
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            return BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
                key_columns=["event_id"],
            )

    def test_init_stores_key_columns(self):
        """__init__() stores key_columns for use in MERGE.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If key_columns not stored
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
                key_columns=["event_id"],
            )
            assert warehouse.key_columns == ["event_id"]

    def test_init_defaults_key_columns_to_empty(self):
        """__init__() defaults key_columns to [] (append-only).

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If default is not an empty list
        """
        with patch("py_common.adapters.bigquery.bigquery.Client"):
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
            )
            assert warehouse.key_columns == []

    def test_merge_upserts_on_key_columns(self, warehouse):
        """MERGE matches on key columns and updates non-key columns.

        This is what makes reprocessing a changed version of the
        same logical row update it in place instead of creating a
        duplicate.

        Args:
            warehouse: BigQueryWarehouse fixture with key_columns

        Returns:
            None

        Raises:
            AssertionError: If MERGE doesn't upsert correctly
        """
        sql = warehouse._build_merge_sql(
            "proj.ds.t_staging",
            "proj.ds.t",
            ["event_id"],
            ["event_id", "employer_id", "attendees"],
        )

        assert "MERGE INTO `proj.ds.t`" in sql
        assert "ON T.`event_id` = S.`event_id`" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "T.`employer_id` = S.`employer_id`" in sql
        assert "T.`attendees` = S.`attendees`" in sql
        # Key columns are the match predicate, not reassigned.
        assert (
            "T.`event_id` = S.`event_id`" not in sql.split("WHEN MATCHED")[1]
        )
        assert "WHEN NOT MATCHED THEN INSERT" in sql

    def test_merge_supports_composite_keys(self, warehouse):
        """MERGE ON clause ANDs together multiple key columns.

        Args:
            warehouse: BigQueryWarehouse fixture with key_columns

        Returns:
            None

        Raises:
            AssertionError: If composite key predicate is wrong
        """
        sql = warehouse._build_merge_sql(
            "proj.ds.t_staging",
            "proj.ds.t",
            ["employer_id", "period"],
            ["employer_id", "period", "score"],
        )

        assert (
            "ON T.`employer_id` = S.`employer_id` "
            "AND T.`period` = S.`period`" in sql
        )

    def test_merge_omits_update_when_all_columns_are_keys(self, warehouse):
        """No WHEN MATCHED clause when there's nothing to update.

        Args:
            warehouse: BigQueryWarehouse fixture with key_columns

        Returns:
            None

        Raises:
            AssertionError: If an empty UPDATE SET is emitted
        """
        sql = warehouse._build_merge_sql(
            "proj.ds.t_staging", "proj.ds.t", ["event_id"], ["event_id"]
        )

        assert "WHEN MATCHED" not in sql
        assert "WHEN NOT MATCHED THEN INSERT" in sql

    def test_load_uses_merge_sql_when_key_columns_configured(self, warehouse):
        """load() builds a real MERGE, not a plain append INSERT,
        when key_columns are configured.

        Args:
            warehouse: BigQueryWarehouse fixture with key_columns

        Returns:
            None

        Raises:
            AssertionError: If load() still does a plain append
        """
        mock_bq_client = warehouse.client
        mock_bq_client.get_table.return_value = _staging_table(
            "event_id", "employer_id", "attendees"
        )
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        mock_query_job = Mock()
        mock_query_job.result.return_value = [(1,)]
        mock_bq_client.query.return_value = mock_query_job

        warehouse.load(
            key="test.xlsx", checksum="abc123", data=b"parquet data", rows=1
        )

        sql_calls = [
            call[0][0] for call in mock_bq_client.query.call_args_list
        ]
        txn_sql = next(s for s in sql_calls if "BEGIN TRANSACTION" in s)

        assert "MERGE INTO" in txn_sql
        assert "ON T.`event_id` = S.`event_id`" in txn_sql
        assert "WHEN MATCHED THEN UPDATE SET" in txn_sql
        assert "SELECT * FROM" not in txn_sql

    def test_load_appends_when_no_key_columns_configured(self):
        """load() stays append-only (MERGE ... WHEN NOT MATCHED only)
        when key_columns is empty, matching the documented "empty
        means append" behaviour and not duplicating pre-existing
        append-only callers' data.

        Args:
            None

        Returns:
            None

        Raises:
            AssertionError: If an upsert is performed with no keys
        """
        mock_bq_client = MagicMock()
        with patch(
            "py_common.adapters.bigquery.bigquery.Client"
        ) as mock_client_class:
            mock_client_class.return_value = mock_bq_client
            warehouse = BigQueryWarehouse(
                project_id="test-project",
                dataset_id="test-dataset",
                table_id="test_table",
            )

        mock_bq_client.get_table.return_value = _staging_table("event_id")
        mock_load_job = Mock()
        mock_load_job.result.return_value = None
        mock_bq_client.load_table_from_file.return_value = mock_load_job

        mock_query_job = Mock()
        mock_query_job.result.return_value = [(1,)]
        mock_bq_client.query.return_value = mock_query_job

        warehouse.load(
            key="test.xlsx", checksum="abc123", data=b"parquet data", rows=1
        )

        sql_calls = [
            call[0][0] for call in mock_bq_client.query.call_args_list
        ]
        txn_sql = next(s for s in sql_calls if "BEGIN TRANSACTION" in s)

        assert "WHEN MATCHED" not in txn_sql
        assert "WHEN NOT MATCHED THEN INSERT ROW" in txn_sql
