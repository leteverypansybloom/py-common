"""Google Cloud Storage adapter.

Implements ObjectStore protocol: Read and write immutable files
(raw Excel, processed Parquet, audit records).

Configuration (via environment variables):
- GCP_PROJECT: GCP project ID
- GCS_BUCKET_RAW: Bucket for raw Excel files
- GCS_PREFIX_RAW: Prefix within bucket (e.g., "raw/")
- GCS_BUCKET_PROCESSED: Bucket for processed Parquet
- GCS_PREFIX_PROCESSED: Prefix within bucket (e.g., "processed/")
"""

import logging

logger = logging.getLogger("py_common.adapters.gcs")


class GCSObjectStore:
    """Read and write objects in Google Cloud Storage.

    Uses Application Default Credentials for authentication.
    Acquires locks via Cloud Storage generations to prevent
    concurrent writers to the warehouse.

    Not yet implemented. Requires:
    1. google-cloud-storage library
    2. GCP service account with Storage permissions
    """

    def __init__(self, **config):
        """Initialize GCS adapter.

        Args:
            project: GCP project ID
            bucket_raw: Bucket for raw files
            prefix_raw: Prefix within bucket
            bucket_processed: Bucket for processed files
            prefix_processed: Prefix within bucket
        """
        raise NotImplementedError(
            "GCSObjectStore will be implemented in next phase. "
            "For now, use LocalObjectStore for testing."
        )

    def get(self, key):
        """Read object from GCS."""
        raise NotImplementedError()

    def put(self, key, data):
        """Write object to GCS."""
        raise NotImplementedError()

    def lock(self, key):
        """Acquire exclusive lock via GCS generation."""
        raise NotImplementedError()
