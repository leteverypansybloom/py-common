"""Shared ingestion library for Excel → Cloud workflows.

Core concepts:
- Sources discover files (e.g., SharePoint, local folders)
- Contracts validate workbooks against business rules
- ObjectStores preserve raw and processed files
- Warehouses manage staging and audit tables
- Pipelines orchestrate the flow end-to-end

All cloud services are behind Protocol interfaces, so tests run locally.

The names in __all__ are the supported API, versioned by semver (see
CHANGELOG.md). Adapters are imported from py_common.adapters.*, the
runner helpers from py_common.runner and logging set-up from
py_common.logging. Anything else (e.g. AuditRecord, SecretStore,
names starting with "_") may change in any release.
"""

__version__ = "0.2.0"

from py_common.contract import Column, Contract, Worksheet
from py_common.errors import (
    ConfigError,
    ContractError,
    IndeterminateCommitError,
    IngestionError,
    NotFound,
    ObjectStoreConflict,
    RunFailed,
    ServiceError,
    SourceError,
    ValidationError,
    VersionMismatch,
)
from py_common.model import Outcome, Result, SourceItem
from py_common.pipeline import Pipeline
from py_common.ports import ObjectStore, Source, Warehouse

__all__ = [
    "Pipeline",
    "Contract",
    "Worksheet",
    "Column",
    "Outcome",
    "Result",
    "SourceItem",
    "Source",
    "ObjectStore",
    "Warehouse",
    "IngestionError",
    "SourceError",
    "VersionMismatch",
    "NotFound",
    "ValidationError",
    "ContractError",
    "ServiceError",
    "ObjectStoreConflict",
    "IndeterminateCommitError",
    "RunFailed",
    "ConfigError",
]
