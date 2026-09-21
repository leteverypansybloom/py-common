"""End-to-end test: Parquet → GCS → BigQuery."""

import os
from datetime import datetime
from io import BytesIO

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from py_common.adapters.gcs import GCSObjectStore
from py_common.adapters.bigquery import BigQueryWarehouse


HAS_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS" in os.environ


@pytest.mark.skipif(not HAS_CREDENTIALS, reason="No GCP credentials")
class TestEndToEndWorkflow:
    """Full pipeline: Create data → GCS → BigQuery."""

    def test_parquet_to_bigquery(self):
        """Create test Parquet, upload to GCS, load to BigQuery."""
        # Step 1: Create test Parquet in memory
        data = {
            "event_id": ["EVT001", "EVT002"],
            "employer_id": ["EMP001", "EMP002"],
            "attendees": [10, 20],
            "_loaded_at": [datetime.now(), datetime.now()],
        }
        table = pa.table(data)

        # Write to BytesIO (memory, not disk)
        buf = BytesIO()
        pq.write_table(table, buf)
        parquet_bytes = buf.getvalue()

        # Step 2: Upload to GCS
        project = os.getenv("GCP_PROJECT_ID")
        bucket = os.getenv("GCS_RAW_BUCKET")
        gcs = GCSObjectStore(project, bucket)

        key = "poc_ingestion_raw/test/integration_events.parquet"
        checksum = "test-checksum-123"
        gcs.put(key, parquet_bytes)

        # Verify upload
        retrieved = gcs.get(key)
        assert retrieved == parquet_bytes

        # Step 3: Load to BigQuery
        dataset = os.getenv("BQ_DATASET")
        bq = BigQueryWarehouse(project, dataset, "events_final")

        bq.load(key=key, checksum=checksum, data=parquet_bytes, rows=2)

        # Step 4: Query audit table to verify
        query = f"""
        SELECT * FROM `{project}.{dataset}.audit`
        WHERE filename = @filename
        """
        from google.cloud import bigquery

        client = bigquery.Client(project=project)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("filename", "STRING", key)
            ]
        )
        result = client.query(query, job_config=job_config)
        rows = list(result)

        assert len(rows) > 0
        assert rows[0][0] == key  # filename
        assert rows[0][3] == "LOADED"  # outcome
