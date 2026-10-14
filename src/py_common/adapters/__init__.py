"""Adapter implementations for different cloud services.

Each adapter implements one of the ports (protocols):
- Source: LocalSource, SharePointSource (Microsoft Graph), GCSSource
- ObjectStore: LocalObjectStore, GCSObjectStore
- Warehouse: BigQueryWarehouse (typed MERGE), RawJsonWarehouse (raw JSON)
- SecretStore: no implementation yet

Only the local adapters are exported here, because the cloud adapters
need optional extras; import those from their own modules (e.g.
py_common.adapters.gcs). LocalSource and LocalObjectStore enable
pipeline testing without cloud services or credentials.
"""

from py_common.adapters.local import LocalObjectStore, LocalSource

__all__ = ["LocalSource", "LocalObjectStore"]
