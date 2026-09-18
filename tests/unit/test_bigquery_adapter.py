"""Unit tests for BigQueryWarehouse adapter.

Tests define the Warehouse protocol contract without requiring live
BigQuery services. All external dependencies are mocked.

RAP Principles:
- Reproducible: Same test data always produces same results
- Auditable: Each test documents expected behavior
- Transparent: No hardcoded GCP credentials in tests
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from py_common.adapters.bigquery import BigQueryWarehouse


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

        # Mock get_table for staging.
        mock_staging_table = Mock()
        mock_bq_client.get_table.return_value = mock_staging_table

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
        mock_staging_table = Mock()
        mock_bq_client.get_table.return_value = mock_staging_table

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
        mock_staging_table = Mock()
        mock_bq_client.get_table.return_value = mock_staging_table

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
