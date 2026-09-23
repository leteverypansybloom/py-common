"""Check local GCP credentials can reach BigQuery and Cloud Storage.

A manual, one-off check, not part of the test suite. It uses your
Application Default Credentials (from `gcloud auth application-default
login`) and lists at most one dataset and one bucket.

In PowerShell, first set:
    $env:GCP_PROJECT_ID

Then, from the repository root, run:
    python scripts/check_gcp_auth.py
"""

import os

import google.auth
from google.auth.transport.requests import Request
from google.cloud import bigquery, storage

project = os.environ["GCP_PROJECT_ID"]

credentials, _ = google.auth.default()
credentials.refresh(Request())

print("Authenticated")
print(f"Project: {project}")
print(f"Credentials: {type(credentials).__name__}")

bq = bigquery.Client(project=project, credentials=credentials)
datasets = list(bq.list_datasets(max_results=1))
print(f"BigQuery datasets found: {len(datasets)}")

gcs = storage.Client(project=project, credentials=credentials)
buckets = list(gcs.list_buckets(max_results=1))
print(f"GCS buckets found: {len(buckets)}")
