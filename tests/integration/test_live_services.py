"""Integration tests for live cloud services.

These tests require:
- An Entra application with SharePoint access
- A GCP project with Secret Manager and BigQuery
- Valid credentials in the environment

Marked @pytest.mark.integration and skipped by default.
Run after cloud infrastructure is ready.

```bash
SHAREPOINT_TENANT_ID=... \\
SHAREPOINT_CLIENT_ID=... \\
GCP_PROJECT=... \\
pytest tests/integration/ -m integration
```
"""

import pytest


@pytest.mark.integration
def test_sharepoint_connectivity():
    """Verify connection to SharePoint Online.

    Requires SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID,
    SHAREPOINT_CLIENT_SECRET_NAME environment variables.
    """
    pytest.skip("SharePoint adapter not yet implemented")


@pytest.mark.integration
def test_gcs_object_store():
    """Verify write to Google Cloud Storage.

    Requires GCP_PROJECT, GCS_BUCKET environment variables.
    """
    pytest.skip("GCS adapter not yet implemented")


@pytest.mark.integration
def test_bigquery_warehouse():
    """Verify load to BigQuery staging and final tables.

    Requires GCP_PROJECT, BIGQUERY_DATASET environment variables.
    """
    pytest.skip("BigQuery adapter not yet implemented")


@pytest.mark.integration
def test_end_to_end_local_to_bigquery():
    """Full pipeline: local files → BigQuery.

    Once cloud adapters are implemented, this tests the entire flow.
    """
    pytest.skip("Integration adapters not yet implemented")
