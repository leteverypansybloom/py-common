"""End-to-end test: workbook rows to GCS, then into a raw table."""

import os

import pytest

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore
from py_common.contract import SheetRows
from py_common.model import SourceItem, digest
from py_common.raw import to_ndjson

HAS_CREDENTIALS = "GOOGLE_APPLICATION_CREDENTIALS" in os.environ


@pytest.mark.integration
@pytest.mark.skipif(not HAS_CREDENTIALS, reason="No GCP credentials")
class TestEndToEndWorkflow:
    """Store NDJSON in GCS, load it into BigQuery, read the audit."""

    def test_ndjson_to_bigquery(self) -> None:
        """Store test rows in GCS and load them into a raw table."""
        item = SourceItem("integration_events.xlsx", "test/events", "1", 0)
        sheets = {
            "Events": SheetRows(
                ["event_id", "attendees"],
                [(2, ("EVT001", 10)), (3, ("EVT002", 20))],
            )
        }
        checksum = digest(b"integration_events")
        data, rows = to_ndjson(sheets, item, checksum)

        project = os.environ["GCP_PROJECT_ID"]
        bucket = os.environ["GCS_RAW_BUCKET"]
        gcs = GCSObjectStore(project, bucket)
        key = "poc_ingestion_raw/test/integration_events.ndjson"
        gcs.put(key, data)
        assert gcs.get(key) == data

        dataset = os.environ["BQ_DATASET"]
        warehouse = RawJsonWarehouse(
            project, dataset, {"Events": "events_raw"}
        )
        load_key = f"{item.identity}_{checksum[:8]}"
        warehouse.load(
            key=load_key, checksum=checksum, data=data, rows=rows, item=item
        )

        from google.cloud import bigquery

        client = bigquery.Client(project=project)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("filename", "STRING", load_key)
            ]
        )
        found = list(
            client.query(
                f"SELECT outcome, row_count FROM `{project}.{dataset}.audit` "
                "WHERE filename = @filename",
                job_config=job_config,
            )
        )

        assert [(r["outcome"], r["row_count"]) for r in found] == [
            ("LOADED", 2)
        ]
