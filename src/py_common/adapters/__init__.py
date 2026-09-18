"""Adapter implementations for different cloud services.

Each adapter implements one or more of the ports (protocols):
- Source: Discover and download files
- ObjectStore: Read/write immutable storage
- Warehouse: Load and audit data
- SecretStore: Retrieve secrets

Test adapters (LocalSource, LocalStore) enable full pipeline testing
without cloud services or credentials.
"""

from py_common.adapters.local import LocalObjectStore, LocalSource

__all__ = ["LocalSource", "LocalObjectStore"]
