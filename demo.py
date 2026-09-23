# In PowerShell, first confirm these are set:
# $env:GCP_PROJECT_ID
# $env:GCS_RAW_BUCKET
# $env:BQ_DATASET
#
# Then run:
# python -m pytest tests/unit -q
#
# This script writes test data to the configured development GCS bucket
# and BigQuery dataset.

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery, storage
from openpyxl import load_workbook

from py_common.adapters import LocalSource
from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.gcs import GCSObjectStore
from py_common.contract import Column, Contract, Worksheet
from py_common.model import Outcome
from py_common.pipeline import Pipeline


PROJECT = os.environ["GCP_PROJECT_ID"]
BUCKET = os.environ["GCS_RAW_BUCKET"]
DATASET = os.environ["BQ_DATASET"]

run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
input_dir = Path(f"manager-demo-input-{run_id}")
input_dir.mkdir()

valid_file = input_dir / f"demo_valid_{run_id}.xlsx"
invalid_file = input_dir / f"demo_invalid_{run_id}.xlsx"

# Create a valid workbook with unique IDs for this demonstration.
shutil.copy2("tests/data/fixtures/events_valid.xlsx", valid_file)

workbook = load_workbook(valid_file)
sheet = workbook["Events"]
sheet["A2"] = f"DEMO-{run_id}-001"
sheet["A3"] = f"DEMO-{run_id}-002"
sheet["C2"] = 10
sheet["C3"] = 20
workbook.save(valid_file)

contract = Contract(
    key_columns=["event_id"],
    worksheets=[
        Worksheet(
            name="Events",
            columns=[
                Column(
                    "event_id",
                    data_type="string",
                    nullable=False,
                    unique=True,
                ),
                Column(
                    "employer_id",
                    data_type="string",
                    nullable=False,
                ),
                Column(
                    "attendees",
                    data_type="integer",
                    nullable=False,
                ),
            ],
        )
    ],
)

pipeline = Pipeline(
    source=LocalSource(input_dir),
    store=GCSObjectStore(PROJECT, BUCKET),
    warehouse=BigQueryWarehouse(
        PROJECT,
        DATASET,
        "events_final",
        key_columns=contract.key_columns,
    ),
    contract=contract,
)

print(f"Demo run ID: {run_id}")

print("\n1. Valid Excel file")
first_results = pipeline.run()

for result in first_results:
    print(result.item.name, result.outcome.value, result.rows_processed)

assert first_results[0].outcome == Outcome.LOADED
assert first_results[0].rows_processed == 2

print("\n2. Verify GCS artefacts")
storage_bucket = storage.Client(project=PROJECT).bucket(BUCKET)

for key in (
    f"raw/{valid_file.name}",
    f"processed/{valid_file.name}.parquet",
):
    print(f"{key}: {storage_bucket.blob(key).exists()}")

print("\n3. Verify BigQuery rows")
client = bigquery.Client(project=PROJECT)

sql = f"""
SELECT event_id, employer_id, attendees
FROM `{PROJECT}.{DATASET}.events_final`
WHERE event_id LIKE 'DEMO-{run_id}%'
ORDER BY event_id
"""

for row in client.query(sql).result():
    print(dict(row.items()))

print("\n4. Re-run unchanged file")
second_results = pipeline.run()

for result in second_results:
    print(result.item.name, result.outcome.value, result.rows_processed)

assert second_results[0].outcome == Outcome.SKIPPED

print("\n5. Invalid Excel file")
shutil.copy2("tests/data/fixtures/events_missing_column.xlsx", invalid_file)

third_results = pipeline.run()

for result in third_results:
    print(result.item.name, result.outcome.value, result.rows_processed)

invalid_result = next(
    result
    for result in third_results
    if result.item.name == invalid_file.name
)

assert invalid_result.outcome == Outcome.QUARANTINED

quarantine_key = f"quarantine/{invalid_file.name}"

print(f"\n{quarantine_key}: {storage_bucket.blob(quarantine_key).exists()}")
print("\nDemo passed.")