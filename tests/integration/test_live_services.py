"""Integration tests for live cloud services.

These tests require:
- Read access to a SharePoint test folder (Graph Sites.Selected)
- A GCP project with Secret Manager and BigQuery
- Valid credentials in the environment

Marked @pytest.mark.integration and skipped by default.
Run after cloud infrastructure is ready.

```bash
SHAREPOINT_HOSTNAME=contoso.sharepoint.com \\
SHAREPOINT_SITE_PATH=/sites/DataSite \\
SHAREPOINT_LIBRARY=Documents \\
SHAREPOINT_FOLDER=Test \\
SHAREPOINT_TEST_ACCESS_TOKEN=... \\
GCP_PROJECT=... \\
pytest tests/integration/ -m integration
```

SHAREPOINT_TEST_ACCESS_TOKEN is a short-lived Graph token obtained
by whatever route IT approves. It is only for this manual check;
production code gets tokens from a token provider, never an
environment variable.
"""

import os

import pytest


@pytest.mark.integration
def test_sharepoint_connectivity() -> None:
    """List the test folder and download one file from SharePoint.

    Read-only. Skips unless the SHAREPOINT_* variables in the module
    docstring are set.

    Raises:
        AssertionError: Folder empty or download incomplete.
    """
    names = [
        "SHAREPOINT_HOSTNAME",
        "SHAREPOINT_SITE_PATH",
        "SHAREPOINT_LIBRARY",
        "SHAREPOINT_TEST_ACCESS_TOKEN",
    ]
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(f"Set {', '.join(missing)} to run")

    from py_common.adapters.sharepoint import (
        SharePointConfig,
        SharePointSource,
    )

    config = SharePointConfig(
        hostname=os.environ["SHAREPOINT_HOSTNAME"],
        site_path=os.environ["SHAREPOINT_SITE_PATH"],
        library=os.environ["SHAREPOINT_LIBRARY"],
        folder=os.environ.get("SHAREPOINT_FOLDER", ""),
    )
    token = os.environ["SHAREPOINT_TEST_ACCESS_TOKEN"]
    source = SharePointSource(config, token_provider=lambda: token)

    items = source.items()
    assert items, f"No Excel files found in {config.describe()}"
    data = source.download(items[0])
    assert len(data) == items[0].size_bytes


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
