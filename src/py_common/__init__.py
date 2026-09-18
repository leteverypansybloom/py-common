"""Shared ingestion library for Excel → Cloud workflows.

Core concepts:
- Sources discover files (e.g., SharePoint, local folders)
- Contracts validate workbooks against business rules
- ObjectStores preserve raw and processed files
- Warehouses manage staging and audit tables
- Pipelines orchestrate the flow end-to-end

All cloud services are behind Protocol interfaces, so tests run locally.
"""

__version__ = "0.1.0"

from py_common.model import Outcome, Result, SourceItem
from py_common.ports import ObjectStore, Source, Warehouse

__all__ = [
    "Outcome",
    "Result",
    "SourceItem",
    "ObjectStore",
    "Source",
    "Warehouse",
]
