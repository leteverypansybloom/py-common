"""Google BigQuery adapter for transactional data loading.

Implements Warehouse protocol. Loads Parquet data with staging,
verification, merge, and audit trail (immutable append-only).

RAP Compliance:
- Reproducible: Same data always produces same result
- Auditable: Every load recorded with checksum, row count, outcome
- Transparent: SQL and audit logic visible; no hidden behavior
"""

import logging
from datetime import datetime, timezone
from io import BytesIO

from google.cloud import bigquery

logger = logging.getLogger("py_common.adapters.bigquery")


class BigQueryWarehouse:
    """Transactional data loading into BigQuery.

    Implements Warehouse protocol. All-or-nothing semantics: the
    final-table merge and the audit record are committed together in
    a single BigQuery script transaction, so a failure partway
    through rolls both back. Never a partial write where data is
    committed with no matching audit record.

    Attributes:
        project_id (str): GCP project ID.
        dataset_id (str): BigQuery dataset name.
        table_id (str): Final table name.
        staging_table_id (str): Staging table ({table_id}_staging).
        audit_table_id (str): Audit table name (default: "audit").
        key_columns (list[str]): Columns identifying a unique logical
            row, used to upsert into the final table. Empty means
            append-only (no deduplication on reprocess).
        client (bigquery.Client): BigQuery client.
    """

    def __init__(
        self,
        project_id: str,
        dataset_id: str,
        table_id: str,
        audit_table_id: str = "audit",
        key_columns: list[str] | None = None,
    ) -> None:
        """Initialize BigQuery adapter.

        Args:
            project_id (str): GCP project ID.
            dataset_id (str): BigQuery dataset name.
            table_id (str): Final table name.
            audit_table_id (str): Audit table name. Defaults to "audit".
            key_columns (list[str] | None): Contract.key_columns used
                to MERGE (upsert) into the final table. None or empty
                means append-only.

        Returns:
            None

        Raises:
            None (credential errors raised on first operation)
        """
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.table_id = table_id
        self.audit_table_id = audit_table_id
        self.key_columns = key_columns or []
        # Staging table uses {table_id}_staging naming convention.
        self.staging_table_id = f"{table_id}_staging"
        self.client = bigquery.Client(project=project_id)

    @property
    def identity(self) -> str:
        """Unique identifier for this warehouse.

        Used as scope for exclusive write lock in distributed systems.
        Format: project.dataset.table (fully qualified name).

        Args:
            None

        Returns:
            str: Fully-qualified table identifier.
        """
        return f"{self.project_id}.{self.dataset_id}.{self.table_id}"

    def load(
        self,
        key: str,
        checksum: str,
        data: bytes,
        rows: int,
    ) -> None:
        """Atomically load Parquet data or fail completely.

        Steps:
        1. Ensure staging table exists
        2. Load Parquet data into staging (truncate first)
        3. Verify row count matches expectation
        4. MERGE into final table (upsert on key_columns, or
           append-only if none configured) and record the audit
           entry together, in a single BigQuery script transaction
        5. Commit that transaction, or roll it back entirely

        The staging load (steps 1-2) is not part of the transaction:
        staging is truncated and reloaded on every run, so a failure
        there simply leaves nothing to merge. Steps 4-5 run as one
        BigQuery job so the final-table write and the audit record
        can never diverge — if the audit INSERT fails, the MERGE is
        rolled back too, instead of leaving committed data with no
        audit trail.

        Args:
            key (str): Unique identifier
                (e.g., "events_2024_01.xlsx").
            checksum (str): SHA-256 checksum for deduplication.
            data (bytes): Parquet-encoded data to load.
            rows (int): Expected row count; verified before committing.

        Returns:
            None

        Raises:
            ValueError: Row count mismatch, schema error, or data issues.
            RuntimeError: Cannot acquire write lock or BigQuery failure.
        """
        logger.debug(
            f"Loading {key}: {len(data)} bytes, {rows} rows, {checksum}"
        )

        # Step 1: Ensure staging table exists.
        self._ensure_staging_table()

        staging_full_id = (
            f"{self.project_id}.{self.dataset_id}.{self.staging_table_id}"
        )
        final_full_id = f"{self.project_id}.{self.dataset_id}.{self.table_id}"
        audit_full_id = (
            f"{self.project_id}.{self.dataset_id}.{self.audit_table_id}"
        )

        # Step 2: Load Parquet into staging. Parquet is self-describing,
        # so BigQuery infers the staging table's schema from the file.
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            # WRITE_TRUNCATE: Empty staging before load (safety).
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )

        load_job = self.client.load_table_from_file(
            BytesIO(data),
            staging_full_id,
            job_config=job_config,
        )
        load_job.result()
        logger.debug("Loaded Parquet into staging table")

        # Step 3: Verify row count.
        count_sql = f"SELECT COUNT(*) as cnt FROM `{staging_full_id}`"
        count_result = self.client.query(count_sql).result()
        actual_rows = list(count_result)[0][0]

        if actual_rows != rows:
            raise ValueError(
                f"Row count mismatch: expected {rows}, got {actual_rows}"
            )

        logger.debug(f"Verified {actual_rows} rows in staging")

        # Step 4: Build the MERGE using the schema BigQuery just
        # inferred for staging, restricted to key_columns actually
        # present in this load's data.
        staging_table = self.client.get_table(staging_full_id)
        columns = [field.name for field in staging_table.schema]
        key_columns = [c for c in self.key_columns if c in columns]

        if key_columns:
            merge_sql = self._build_merge_sql(
                staging_full_id, final_full_id, key_columns, columns
            )
        else:
            # No key columns configured: append-only, matching
            # Contract.key_columns' documented "empty means append".
            merge_sql = (
                f"MERGE INTO `{final_full_id}` AS T "
                f"USING `{staging_full_id}` AS S "
                f"ON FALSE "
                f"WHEN NOT MATCHED THEN INSERT ROW"
            )

        # Steps 4-5: MERGE into final table and record the audit
        # entry as one script transaction (see docstring above).
        transaction_sql = (
            "BEGIN\n"
            "  BEGIN TRANSACTION;\n"
            f"  {merge_sql};\n"
            f"  INSERT INTO `{audit_full_id}` "
            "(filename, checksum, row_count, outcome, timestamp) "
            "VALUES (@filename, @checksum, @row_count, @outcome, "
            "@timestamp);\n"
            "  COMMIT TRANSACTION;\n"
            "EXCEPTION WHEN ERROR THEN\n"
            "  ROLLBACK TRANSACTION;\n"
            "  RAISE USING MESSAGE = @@error.message;\n"
            "END;"
        )

        job_config_txn = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("filename", "STRING", key),
                bigquery.ScalarQueryParameter("checksum", "STRING", checksum),
                bigquery.ScalarQueryParameter("row_count", "INTEGER", rows),
                bigquery.ScalarQueryParameter("outcome", "STRING", "LOADED"),
                bigquery.ScalarQueryParameter(
                    "timestamp",
                    "TIMESTAMP",
                    datetime.now(timezone.utc),
                ),
            ]
        )

        try:
            txn_job = self.client.query(
                transaction_sql, job_config=job_config_txn
            )
            txn_job.result()
        except Exception as e:
            raise RuntimeError(
                f"BigQuery transaction failed for {key}; final table "
                f"merge and audit record were rolled back together: {e}"
            ) from e

        logger.debug(f"Merged data into {self.table_id} and recorded audit")

    def _build_merge_sql(
        self,
        staging_full_id: str,
        final_full_id: str,
        key_columns: list[str],
        columns: list[str],
    ) -> str:
        """Build a MERGE statement upserting staging into final.

        Rows matching on key_columns are updated in place; rows with
        no matching key are inserted. This makes reprocessing a
        changed version of the same logical rows update them instead
        of creating duplicates.

        Args:
            staging_full_id (str): Fully-qualified staging table ID.
            final_full_id (str): Fully-qualified final table ID.
            key_columns (list[str]): Columns identifying a unique row.
            columns (list[str]): All column names in the staging table.

        Returns:
            str: A MERGE statement (no trailing semicolon).
        """
        on_clause = " AND ".join(
            f"T.`{col}` = S.`{col}`" for col in key_columns
        )
        update_columns = [c for c in columns if c not in key_columns]
        insert_columns = ", ".join(f"`{c}`" for c in columns)
        insert_values = ", ".join(f"S.`{c}`" for c in columns)

        when_matched = ""
        if update_columns:
            set_clause = ", ".join(
                f"T.`{c}` = S.`{c}`" for c in update_columns
            )
            when_matched = f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"

        return (
            f"MERGE INTO `{final_full_id}` AS T\n"
            f"USING `{staging_full_id}` AS S\n"
            f"ON {on_clause}\n"
            f"{when_matched}"
            f"WHEN NOT MATCHED THEN "
            f"INSERT ({insert_columns}) VALUES ({insert_values})"
        )

    def _ensure_staging_table(self) -> None:
        """Create staging table if it does not exist.

        Uses minimal schema; actual schema inferred from Parquet load.

        Args:
            None

        Returns:
            None

        Raises:
            RuntimeError: Cannot create table (permission or error).
        """
        staging_full_id = (
            f"{self.project_id}.{self.dataset_id}." f"{self.staging_table_id}"
        )

        try:
            self.client.get_table(staging_full_id)
            logger.debug(f"Staging table {self.staging_table_id} exists")
        except Exception as e:
            logger.debug(
                f"Creating staging table {self.staging_table_id}: {e}"
            )
            # Create empty table; schema comes from Parquet.
            table = bigquery.Table(staging_full_id)
            self.client.create_table(table)
