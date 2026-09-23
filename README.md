# py-common: Shared Ingestion Library

Reusable Python library for Excel → Cloud workflows. Handles file discovery, validation, storage, and warehouse loading.

**Status**: Early development. The core pipeline, local adapters, and
the GCS and BigQuery adapters are built and unit-tested. The
SharePoint adapter is a stub. See [Adapter status](#adapter-status).

**New to this repo?** See [`docs/README.md`](docs/README.md) for a
guided path: a hands-on getting-started walkthrough, a guide to
whether py-common fits your next ingestion source, and a full
feature-by-feature status reference.

## What It Does

```
Source (discover files)
    ↓
Download (SharePoint, local folder, etc.)
    ↓
Validate (against contract)
    ↓
Store Raw (immutable copy)
    ↓
Convert (to Parquet)
    ↓
Store Processed (immutable copy)
    ↓
Load Warehouse (staging + merge)
    ↓
Audit (record every attempt)
```

## Quick Start

See [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — the single
place for environment setup, generating test fixtures, defining your
first contract and wiring a pipeline. The fixtures themselves are
described in
[`tests/data/fixtures/README.md`](tests/data/fixtures/README.md).

## Architecture

### Core Concepts

**Source** (protocol)
- Discovers files at a location (SharePoint, S3, local folder)
- Downloads exact bytes of a discovered version
- Prevents processing stale or partially-written files

**Contract**
- Defines required worksheets, columns, data types
- Validates structure and business rules
- Deterministic; same file always produces same result

**ObjectStore** (protocol)
- Immutable storage for raw files, processed data, audit records
- `get(key)` — retrieve object by path
- `put(key, data)` — write object; repeating an identical write
  succeeds, but writing different bytes to an existing key raises
  `ObjectStoreConflict`
- `lock(key)` — acquire exclusive write lock (a no-op in the current
  adapters; see [Concurrency](#concurrency))

**Warehouse** (protocol)
- Stages, validates, and loads data
- Implements `load(key, checksum, data, rows)`
- Atomically commits or rolls back entire transaction
- Records audit entry with row count and checksum

**Pipeline**
- Orchestrates the workflow
- Handles deduplication (by checksum) and error recording
- Returns list of Result objects (one per file)

### Concurrency

`GCSObjectStore.lock()` is a **deliberate no-op stub**, not a bug.
PHW's ingestion runs as a single Cloud Scheduler → Cloud Run Job
trigger — one job at a time, never in parallel. Under that model
there is no concurrent writer to lock out.

This stub is **unsafe** if that changes: two processes racing to
stage and merge into the same `{table}_staging` table can corrupt
each other's load. Before enabling any concurrent or manually
overlapping execution, replace `lock()` with real distributed
locking — Firestore document locks are the recommended approach on
GCP — so `store.lock()` genuinely raises `RuntimeError` when another
process already holds it, as the `ObjectStore` protocol promises.

### Protocols vs Implementations

Source, ObjectStore and Warehouse are Python `Protocol`s, so adapters
can be swapped without changing core code. For why, see
[`DECISIONS_en.md`](DECISIONS_en.md); for the technical detail, see
[`docs/INGESTION_FRAMEWORK_GUIDE.md`](docs/INGESTION_FRAMEWORK_GUIDE.md).

## Configuration

Configuration is YAML with environment variable interpolation — see
[`docs/GETTING_STARTED.md`, Configuration](docs/GETTING_STARTED.md#7-configuration).

## Error Handling

```python
from py_common.errors import (
    ValidationError,           # Workbook invalid (includes ContractError)
    ServiceError,              # Cloud service failed
    VersionMismatch,           # Source changed during download
    IndeterminateCommitError,  # Unknown whether warehouse committed
)

try:
    rows = contract.validate(excel_bytes, filename)
except ValidationError as e:
    # Schema or data rule violation; always quarantine
    quarantine_file(e)
```

`ContractError` (wrong worksheets or columns) is a subclass of
`ValidationError`, so one `except` covers both. `Pipeline` already
does this for you: validation failures and warehouse `ServiceError`s
are quarantined, and any other error (including
`IndeterminateCommitError`) marks that file `FAILED` while the run
carries on with the next file. The full hierarchy is in
`src/py_common/errors.py`.

## Testing

See [`CONTRIBUTING.md`, Run Tests Locally](CONTRIBUTING.md#run-tests-locally).

## Adapter status

| Adapter | Implements | Status |
|---|---|---|
| `LocalSource` | Source | ✅ Built, unit-tested |
| `LocalObjectStore` | ObjectStore | ✅ Built, unit-tested |
| `GCSObjectStore` | ObjectStore | ✅ Built, unit-tested against mocks; `lock()` is a no-op |
| `BigQueryWarehouse` | Warehouse | ✅ Built, unit-tested against mocks; `MERGE` on `key_columns` |
| `SharePointSource` | Source | ⏳ Stub — every method raises `NotImplementedError` |
| Secret Manager | SecretStore | ⏳ Not started |

Also built: contract validation, configuration with interpolation,
fixture generation, the error hierarchy, and GCP authentication
helpers (`gcp_auth.py`).

## What's Next

⏳ SharePoint Online adapter  
⏳ Real distributed `lock()` (Firestore)  
⏳ Secret Manager adapter  
⏳ Structured logging and alerting  

The agreed order, with reasoning, is in
[`docs/INGESTION_FRAMEWORK_GUIDE.md`, Section 9](docs/INGESTION_FRAMEWORK_GUIDE.md#9-priority-order-for-closing-the-gaps).

## Dependencies

Core:

- openpyxl >= 3.0 — Read Excel
- pyyaml >= 6.0 — YAML configuration
- pandas >= 2.0 — Excel → DataFrame conversion
- pyarrow >= 15.0 — Parquet output

Optional extras (`pip install -e ".[gcp]"` etc.):

- `gcp`: google-cloud-storage >= 2.10, google-cloud-bigquery >= 3.13
- `sharepoint`: microsoft-graph-core >= 0.2, azure-identity >= 1.14
- `dev`: pytest, black, ruff, mypy, pre-commit, detect-secrets and
  type stubs

## Scripts

`scripts/` holds manual, one-off scripts that aren't part of the
library or the test suite:

- `scripts/demo.py` — runs the fixtures through `GCSObjectStore` and
  `BigQueryWarehouse` against a real development project. Writes to
  real cloud resources.
- `scripts/check_gcp_auth.py` — checks your local GCP credentials can
  reach BigQuery and Cloud Storage.

Both read their project settings from environment variables; see the
header of each file.

## Standards

- **PEP 8**: 79-character lines, strict formatting
- **Type hints**: All functions annotated
- **Google docstrings**: Clear, concise documentation
- **TDD**: Tests written before or alongside code
- **RAP**: Reproducible Analytical Pipelines — reproducible,
  auditable, peer-reviewed

## License

MIT

## Contact

Questions? Open an issue or contact the data engineering team.
