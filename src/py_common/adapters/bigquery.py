"""Google BigQuery adapter.

Implements Warehouse protocol: Load data and record audit entries
with transactional guarantees.

Configuration (via environment variables):
- GCP_PROJECT: GCP project ID
- BIGQUERY_DATASET: Dataset for staging and final tables
- BIGQUERY_TABLE_STAGING: Staging table (truncated each run)
- BIGQUERY_TABLE_FINAL: Final table (append or merge)
- BIGQUERY_TABLE_AUDIT: Audit table (immutable append)
"""

import logging

logger = logging.getLogger("py_common.adapters.bigquery")


class BigQueryWarehouse:
    """Load data and manage audit records in BigQuery.

    Steps for each load:
    1. Load Parquet into staging table (truncate first)
    2. Verify row count matches expected
    3. Merge into final table on contract key columns
       (or append if no key columns)
    4. Insert audit record
    5. Commit transaction

    Not yet implemented. Requires:
    1. google-cloud-bigquery library
    2. GCP service account with BigQuery permissions
    3. Staging and final tables must exist
    """

    def __init__(self, **config):
        """Initialize BigQuery adapter.

        Args:
            project: GCP project ID
            dataset: BigQuery dataset
            table_staging: Staging table name
            table_final: Final table name
            table_audit: Audit table name
        """
        raise NotImplementedError(
            "BigQueryWarehouse will be implemented in next phase. "
            "For now, tests use mocks and local audit logs."
        )

    @property
    def identity(self):
        """Unique identifier for this warehouse."""
        raise NotImplementedError()

    def load(self, key, checksum, data, rows):
        """Load Parquet data atomically."""
        raise NotImplementedError()
