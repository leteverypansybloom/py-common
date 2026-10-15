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
from typing import Any

from google.api_core.exceptions import GoogleAPICallError, NotFound
from google.cloud import bigquery

from py_common.adapters._bq_jobs import (
    commit_parameters,
    commit_script,
    committed,
    create_staging_table,
    drop_staging_table,
    run_query,
    staging_table_id,
    wait_for_load,
)
from py_common.errors import ContractError, ServiceError, ValidationError
from py_common.model import SourceItem

logger = logging.getLogger("py_common.adapters.bigquery")

#: Final-table column set by the warehouse at commit time. Reserved:
#: source data may not contain it.
LOADED_AT_COLUMN = "_loaded_at"

#: Legacy SQL type names the API may report, mapped to the names
#: Parquet loads produce, so a schema diff compares like with like.
_TYPE_ALIASES = {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN"}


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
        item: SourceItem,
    ) -> bool:
        """Atomically load Parquet data once, or fail completely.

        Steps:
        1. Create this load's own staging table
           ({table}_stg_{checksum[:12]}_{random}, expiring in a day)
        2. Load Parquet data into it
        3. Verify row count matches expectation
        4. In one BigQuery script transaction: if the audit table
           already has a LOADED row for this destination and
           checksum, do nothing; otherwise MERGE into the final
           table (upsert on key_columns, or append-only if none
           configured) and record the audit entry. Every inserted or
           updated row gets _loaded_at set to the audit timestamp.
        5. Commit that transaction, or roll it back entirely

        Step 4's check makes load() idempotent: a retry after a
        commit whose confirmation was lost (or whose store marker
        was never written) writes nothing. The same content under a
        new source version is also skipped: a MERGE of identical
        rows would change nothing but _loaded_at.

        The staging load (steps 1-2) is not part of the transaction:
        a failure there simply leaves nothing to merge. The staging
        table is dropped afterwards, whatever happened (best effort;
        it expires anyway). Steps 4-5 run as one
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
            item (SourceItem): Source file loaded; its version is
                recorded in the audit row.

        Returns:
            bool: True if the rows were written, False if this
                content was already loaded here.

        Raises:
            ValueError: Row count mismatch, schema error, data issues,
                or the data has its own _loaded_at column.
            ServiceError: BigQuery reported a failure; nothing was
                committed, so a retry is safe.
            IndeterminateCommitError: The transaction's outcome could
                not be confirmed.
        """
        staging_full_id = (
            f"{self.project_id}.{self.dataset_id}."
            f"{staging_table_id(self.table_id, checksum)}"
        )
        try:
            create_staging_table(self.client, staging_full_id)
            return self._load(key, checksum, data, rows, item, staging_full_id)
        except GoogleAPICallError as e:
            raise ServiceError(f"BigQuery failed for {key}: {e}") from e
        finally:
            drop_staging_table(self.client, staging_full_id)

    def _load(
        self,
        key: str,
        checksum: str,
        data: bytes,
        rows: int,
        item: SourceItem,
        staging_full_id: str,
    ) -> bool:
        """Body of load(); GoogleAPICallErrors are wrapped by load().

        Args:
            key (str): Unique identifier for this load.
            checksum (str): SHA-256 checksum for deduplication.
            data (bytes): Parquet-encoded data to load.
            rows (int): Expected row count.
            item (SourceItem): Source file loaded.
            staging_full_id (str): This load's staging table.

        Returns:
            bool: As load().
        """
        logger.debug(
            "Loading %s: %d bytes, %d rows, %s", key, len(data), rows, checksum
        )

        final_full_id = f"{self.project_id}.{self.dataset_id}.{self.table_id}"
        audit_full_id = (
            f"{self.project_id}.{self.dataset_id}.{self.audit_table_id}"
        )

        # Step 2: Load Parquet into staging. Parquet is self-describing,
        # so BigQuery infers the staging table's schema from the file.
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            # The table is new and empty; WRITE_TRUNCATE lets the
            # Parquet schema replace its empty one.
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )

        load_job = self.client.load_table_from_file(
            BytesIO(data),
            staging_full_id,
            job_config=job_config,
        )
        wait_for_load(load_job, key)
        logger.debug("Loaded Parquet into staging table")

        # Step 3: Verify row count. The load job reports the rows it
        # wrote, so no COUNT(*) query (a round trip, billed at the
        # 10 MB minimum) is needed.
        actual_rows = load_job.output_rows
        if actual_rows != rows:
            raise ValueError(
                f"Row count mismatch: expected {rows}, got {actual_rows}"
            )
        logger.debug("Verified %s rows in staging", actual_rows)

        # Step 4: Build the MERGE using the schema BigQuery just
        # inferred for staging.
        staging_table = self.client.get_table(staging_full_id)
        columns = [field.name for field in staging_table.schema]
        if LOADED_AT_COLUMN in columns:
            # A problem with the file, so quarantine (ContractError),
            # not a retry that can never succeed.
            raise ContractError(
                f"Source data for {key} has a '{LOADED_AT_COLUMN}' "
                "column; that name is reserved for the load timestamp "
                "the warehouse sets"
            )
        # Every configured key must be there: merging on fewer keys
        # overwrites unrelated rows, and on none appends duplicates.
        missing = [c for c in self.key_columns if c not in columns]
        if missing:
            raise ContractError(
                f"{key}: key column(s) {', '.join(missing)} not in the "
                f"data; staging has {', '.join(columns)}"
            )
        key_columns = self.key_columns

        self._reconcile_final_table(staging_table.schema, final_full_id, key)

        if key_columns:
            self._check_keys(staging_full_id, key_columns, key)
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
                f"WHEN NOT MATCHED THEN {self._insert_clause(columns)}"
            )

        # Steps 4-5: check, MERGE and audit as one script transaction
        # (see docstring above). The typed route dedups on content:
        # destination + checksum.
        transaction_sql = commit_script(
            [merge_sql], audit_full_id, dedup_columns=["checksum"]
        )
        job_config_txn = bigquery.QueryJobConfig(
            query_parameters=commit_parameters(
                key,
                checksum,
                rows,
                self.identity,
                item,
                datetime.now(timezone.utc),
            )
        )
        job, result = run_query(
            self.client, transaction_sql, job_config_txn, key
        )
        return committed(job, result, key)

    def _reconcile_final_table(
        self, staging_schema: list[Any], final_full_id: str, key: str
    ) -> None:
        """Make the final table able to take staging's columns.

        - No final table: create it from the staging schema plus
          _loaded_at.
        - New columns in staging (e.g. picked up by "all_except"):
          add them, nullable, so earlier rows read NULL.
        - A column whose type changed, or that the data no longer
          has: refuse the file. Either would break the MERGE with an
          opaque error, and neither can be fixed safely by code.

        Args:
            staging_schema (list[Any]): Staging table's SchemaFields.
            final_full_id (str): Fully-qualified final table ID.
            key (str): Load key, for messages.

        Raises:
            ContractError: A column changed type or was removed; the
                message lists every difference.
        """
        try:
            final = self.client.get_table(final_full_id)
        except NotFound:
            logger.info("Creating final table %s from staging", final_full_id)
            table = bigquery.Table(
                final_full_id,
                schema=[
                    *(
                        bigquery.SchemaField(f.name, f.field_type)
                        for f in staging_schema
                    ),
                    bigquery.SchemaField(LOADED_AT_COLUMN, "TIMESTAMP"),
                ],
            )
            self.client.create_table(table, exists_ok=True)
            return

        final_types = {
            f.name: _type(f.field_type)
            for f in final.schema
            if f.name != LOADED_AT_COLUMN
        }
        staging_types = {f.name: _type(f.field_type) for f in staging_schema}
        changed = [
            f"{name}: {final_types[name]} -> {kind}"
            for name, kind in staging_types.items()
            if name in final_types and final_types[name] != kind
        ]
        removed = [name for name in final_types if name not in staging_types]
        if changed or removed:
            problems = []
            if changed:
                problems.append(f"type changed: {', '.join(changed)}")
            if removed:
                problems.append(f"removed: {', '.join(removed)}")
            raise ContractError(
                f"{key}: data no longer matches {final_full_id} "
                f"({'; '.join(problems)})"
            )

        for name, kind in staging_types.items():
            if name in final_types:
                continue
            logger.warning(
                "Adding column %s %s to %s for %s",
                name,
                kind,
                final_full_id,
                key,
            )
            run_query(
                self.client,
                f"ALTER TABLE `{final_full_id}` "
                f"ADD COLUMN IF NOT EXISTS `{name}` {kind}",
                bigquery.QueryJobConfig(),
                f"{key} schema update",
            )

    def _check_keys(
        self, staging_full_id: str, key_columns: list[str], key: str
    ) -> None:
        """Refuse a file whose merge keys repeat or are NULL.

        A key repeated in staging makes BigQuery reject the MERGE,
        and a NULL key never matches, so the row would be inserted
        again on every load. Both are problems with the file, so they
        raise ValidationError (quarantine) rather than failing the
        load for a retry that can't succeed.

        Args:
            staging_full_id (str): Fully-qualified staging table ID.
            key_columns (list[str]): Merge key columns.
            key (str): Load key, for messages.

        Raises:
            ValidationError: Some key repeats or is NULL; up to five
                are named.
        """
        keys = ", ".join(f"`{c}`" for c in key_columns)
        nulls = " OR ".join(f"`{c}` IS NULL" for c in key_columns)
        sql = (
            f"SELECT {keys}, COUNT(*) AS n, COUNTIF({nulls}) AS null_keys\n"
            f"FROM `{staging_full_id}`\n"
            f"GROUP BY {keys}\n"
            f"HAVING COUNT(*) > 1 OR COUNTIF({nulls}) > 0\n"
            "LIMIT 5"
        )
        _, rows = run_query(
            self.client, sql, bigquery.QueryJobConfig(), f"{key} key check"
        )
        if not rows:
            return
        # Key values are data (possibly personal), and this message
        # reaches logs and audit records, so give counts only.
        null_groups = sum(1 for row in rows if row[len(key_columns) + 1])
        duplicate_groups = len(rows) - null_groups
        raise ValidationError(
            f"{key}: merge keys ({', '.join(key_columns)}) are not "
            f"unique and non-NULL in staging: {duplicate_groups} "
            f"repeated key(s), {null_groups} NULL key group(s) "
            "(first 5 checked)"
        )

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
        of creating duplicates. Both paths set _loaded_at to the
        @timestamp query parameter (the audit record's timestamp).

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
        assignments = [
            f"T.`{c}` = S.`{c}`" for c in columns if c not in key_columns
        ]
        assignments.append(f"T.`{LOADED_AT_COLUMN}` = @timestamp")
        set_clause = ", ".join(assignments)

        return (
            f"MERGE INTO `{final_full_id}` AS T\n"
            f"USING `{staging_full_id}` AS S\n"
            f"ON {on_clause}\n"
            f"WHEN MATCHED THEN UPDATE SET {set_clause}\n"
            f"WHEN NOT MATCHED THEN {self._insert_clause(columns)}"
        )

    @staticmethod
    def _insert_clause(columns: list[str]) -> str:
        """Build the MERGE INSERT clause, adding _loaded_at.

        Args:
            columns (list[str]): Column names in the staging table.

        Returns:
            str: "INSERT (...) VALUES (...)" copying staging columns
                and setting _loaded_at to @timestamp.
        """
        insert_columns = ", ".join(
            f"`{c}`" for c in [*columns, LOADED_AT_COLUMN]
        )
        insert_values = ", ".join(
            [*(f"S.`{c}`" for c in columns), "@timestamp"]
        )
        return f"INSERT ({insert_columns}) VALUES ({insert_values})"


def _type(field_type: str) -> str:
    """Normalise a BigQuery type name for comparison.

    Args:
        field_type (str): SchemaField.field_type.

    Returns:
        str: The type, upper case, with legacy aliases resolved.
    """
    upper = field_type.upper()
    return _TYPE_ALIASES.get(upper, upper)
