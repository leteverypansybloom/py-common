# test_auth.py
import os
from google.auth.transport.requests import Request
import google.auth

# Use cached gcloud credentials
credentials, _ = google.auth.default()
credentials.refresh(Request())

# Set project explicitly
project = "ndr-tr-phw-dp-dev"

print(f"✅ Authenticated")
print(f"Project: {project}")
print(f"Credentials: {type(credentials).__name__}")

# Test BigQuery access
from google.cloud import bigquery
bq = bigquery.Client(project=project, credentials=credentials)
datasets = list(bq.list_datasets(max_results=1))
print(f"BigQuery datasets found: {len(datasets)}")

# Test GCS access
from google.cloud import storage
gcs = storage.Client(project=project, credentials=credentials)
buckets = list(gcs.list_buckets(max_results=1))
print(f"GCS buckets found: {len(buckets)}")