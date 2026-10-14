"""Google BigQuery adapter for raw JSON tables.

Implements the Warehouse protocol for the raw JSON route: it loads
the newline-delimited JSON written by py_common.raw.to_ndjson() into
one raw table per worksheet. Each row keeps where it came from (file,
generation, sheet, row number, checksum) and its values as a JSON
payload. Cleaning and typing happen later, in the transformation
layer (Dataform).

All of one file's rows commit together, across every sheet, along
with the audit record, in a single BigQuery script transaction, the
same shape as BigQueryWarehouse.

RAP Compliance:
- Reproducible: Same NDJSON always produces the same rows
- Auditable: Every load recorded with checksum, row count, outcome
- Transparent: SQL and audit logic visible; no hidden behavior
"""

import json
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

logger = logging.getLogger("py_common.adapters.bigquery_raw")

#: Columns of a staging table: one per field of an NDJSON line.
STAGING_SCHEMA = [
    bigquery.SchemaField("source_file", "STRING"),
    bigquery.SchemaField("source_generation", "STRING"),
    bigquery.SchemaField("sheet_name", "STRING"),
    bigquery.SchemaField("row_number", "INT64"),
    bigquery.SchemaField("checksum", "STRING"),
    bigquery.SchemaField("payload", "JSON"),
]

#: Columns of a raw table: the staging columns plus ingested_at, set
#: by the warehouse at commit time (the audit record's timestamp).
RAW_SCHEMA = [
    *STAGING_SCHEMA,
    bigquery.SchemaField("ingested_at", "TIMESTAMP"),
]

_COLUMNS = ", ".join(field.name for field in STAGING_SCHEMA)


class RawJsonWarehouse:
    """Transactional loading of raw JSON rows, one table per sheet.

    Implements the Warehouse protocol. Use with
    Pipeline(output_format="raw_json").

    Raw tables are created if missing, partitioned by the day of
    ingested_at. Rows are only ever inserted: every load of every
    file version is kept, and the transformation layer picks the
    latest version (see docs/RAW_TABLE_LAYOUT.md). The audit table
    must already exist, as for BigQueryWarehouse.

    Attributes:
        project_id (str): GCP project ID.
        dataset_id (str): BigQuery dataset for the raw tables.
        tables (dict[str, str]): Worksheet name -> raw table name.
        audit_table_id (str): Audit table name (default: "audit").
        client (bigquery.Client): BigQuery client.
    """

    def __init__(
        self,
        project_id: str,
        dataset_id: str,
        tables: Mapping[str, str],
        audit_table_id: str = "audit",
    ) -> None:
        """Initialize the raw JSON adapter.

        Args:
            project_id (str): GCP project ID.
            dataset_id (str): BigQuery dataset for the raw tables.
            tables (Mapping[str, str]): Raw table name for each
                worksheet the contract reads, e.g.
                {"Account signups": "hww_account_signups"}.
            audit_table_id (str): Audit table name. Defaults to
                "audit".

        Returns:
            None

        Raises:
            ValueError: No tables given, or two sheets share a table.
        """
        if not tables:
            raise ValueError("RawJsonWarehouse needs at least one table")
        if len(set(tables.values())) != len(tables):
            raise ValueError(
                "Each worksheet needs its own raw table, got "
                f"{dict(tables)!r}"
            )
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.tables = dict(tables)
        self.audit_table_id = audit_table_id
        self.client = bigquery.Client(project=project_id)

    @property
    def identity(self) -> str:
        """Unique identifier for this warehouse.

        Scopes the run lock and the pipeline's stored copies.
        Format: project.dataset.table[+table...], tables sorted.

        Returns:
            str: Identifier naming every raw table written.
        """
        names = "+".join(sorted(self.tables.values()))
        return f"{self.project_id}.{self.dataset_id}.{names}"

    def load(
        self,
        key: str,
        checksum: str,
        data: bytes,
        rows: int,
    ) -> None:
        """Atomically insert a file's rows into the raw tables.

        Steps:
        1. Split the NDJSON by sheet; refuse sheets with no table
        2. Create any raw table that doesn't exist yet
        3. Load each sheet's rows into {table}_staging (truncate)
        4. Verify the staged row count matches `rows`
        5. Insert every sheet's rows into its raw table and record
           the audit entry, in one script transaction; commit, or
           roll back everything

        Args:
            key (str): Unique identifier for this load.
            checksum (str): SHA-256 of the source file.
            data (bytes): NDJSON from py_common.raw.to_ndjson().
            rows (int): Expected row count across all sheets.

        Returns:
            None

        Raises:
            ValueError: A line is not valid JSON or names a sheet
                with no table, or the staged row count is wrong.
            RuntimeError: The transaction failed and was rolled back.
        """
        by_sheet = self._split_by_sheet(data)
        unknown = sorted(set(by_sheet) - set(self.tables))
        if unknown:
            raise ValueError(
                f"No raw table configured for sheet(s) "
                f"{', '.join(unknown)} in {key}"
            )

        staged = 0
        for sheet, lines in by_sheet.items():
            table_id = self.tables[sheet]
            self._ensure_raw_table(table_id)
            job_config = bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                schema=STAGING_SCHEMA,
                # WRITE_TRUNCATE: Empty staging before load (safety).
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            )
            load_job = self.client.load_table_from_file(
                BytesIO(b"".join(lines)),
                self._full_id(f"{table_id}_staging"),
                job_config=job_config,
            )
            load_job.result()
            staged += load_job.output_rows or 0
            logger.debug(f"Staged {sheet} for {key}")

        if staged != rows:
            raise ValueError(
                f"Row count mismatch: expected {rows}, got {staged}"
            )

        timestamp = datetime.now(timezone.utc)
        job_config_txn = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("filename", "STRING", key),
                bigquery.ScalarQueryParameter("checksum", "STRING", checksum),
                bigquery.ScalarQueryParameter("row_count", "INTEGER", rows),
                bigquery.ScalarQueryParameter("outcome", "STRING", "LOADED"),
                bigquery.ScalarQueryParameter(
                    "timestamp", "TIMESTAMP", timestamp
                ),
            ]
        )
        try:
            txn_job = self.client.query(
                self._transaction_sql(list(by_sheet)),
                job_config=job_config_txn,
            )
            txn_job.result()
        except Exception as e:
            raise RuntimeError(
                f"BigQuery transaction failed for {key}; raw table "
                f"inserts and audit record were rolled back together: {e}"
            ) from e

        logger.debug(f"Inserted {rows} raw rows for {key}")

    def _split_by_sheet(self, data: bytes) -> dict[str, list[bytes]]:
        """Group NDJSON lines by their sheet_name, keeping order.

        Lines are passed on to BigQuery unchanged; they are parsed
        only to read the sheet name.

        Args:
            data (bytes): NDJSON from py_common.raw.to_ndjson().

        Returns:
            dict[str, list[bytes]]: Lines (with newlines) per sheet.

        Raises:
            ValueError: A line isn't JSON or has no sheet_name.
        """
        by_sheet: dict[str, list[bytes]] = {}
        for number, line in enumerate(data.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                sheet = json.loads(line)["sheet_name"]
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError(
                    f"Line {number} is not a raw JSON row: {e}"
                ) from e
            by_sheet.setdefault(sheet, []).append(line + b"\n")
        return by_sheet

    def _ensure_raw_table(self, table_id: str) -> None:
        """Create a raw table, partitioned by day, if it's missing.

        Args:
            table_id (str): Raw table name.

        Returns:
            None
        """
        full_id = self._full_id(table_id)
        try:
            self.client.get_table(full_id)
        except NotFound:
            logger.info(f"Creating raw table {full_id}")
            table = bigquery.Table(full_id, schema=RAW_SCHEMA)
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="ingested_at",
            )
            self.client.create_table(table, exists_ok=True)

    def _transaction_sql(self, sheets: list[str]) -> str:
        """Build the script inserting every sheet plus the audit row.

        Args:
            sheets (list[str]): Sheets with staged rows, in order.

        Returns:
            str: A BigQuery script with one transaction.
        """
        inserts = "".join(
            f"  INSERT INTO `{self._full_id(self.tables[sheet])}` "
            f"({_COLUMNS}, ingested_at) "
            f"SELECT {_COLUMNS}, @timestamp "
            f"FROM `{self._full_id(self.tables[sheet] + '_staging')}`;\n"
            for sheet in sheets
        )
        return (
            "BEGIN\n"
            "  BEGIN TRANSACTION;\n"
            f"{inserts}"
            f"  INSERT INTO `{self._full_id(self.audit_table_id)}` "
            "(filename, checksum, row_count, outcome, timestamp) "
            "VALUES (@filename, @checksum, @row_count, @outcome, "
            "@timestamp);\n"
            "  COMMIT TRANSACTION;\n"
            "EXCEPTION WHEN ERROR THEN\n"
            "  ROLLBACK TRANSACTION;\n"
            "  RAISE USING MESSAGE = @@error.message;\n"
            "END;"
        )

    def _full_id(self, table_id: str) -> str:
        """Fully-qualify a table name in this dataset.

        Args:
            table_id (str): Table name.

        Returns:
            str: project.dataset.table
        """
        return f"{self.project_id}.{self.dataset_id}.{table_id}"
