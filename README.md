# py-common: Shared Ingestion Library

Reusable Python library for Excel → Cloud workflows. Handles file discovery, validation, storage, and warehouse loading.

**Status**: Early development. Core pipeline and local testing adapters complete. Cloud adapters (SharePoint, GCS, BigQuery) planned for phase 2.

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

### 1. Set Up Environment

```shell
python -m venv .venv
# On Mac/Linux: source .venv/bin/activate
.venv\Scripts\activate
pip install -e ".[all,dev]"
python -m pre_commit install
pytest
```

### 2. Generate Test Fixtures

```shell
python -c "
from py_common.fixtures import make_fixtures
from pathlib import Path
make_fixtures(Path('tests/data/fixtures'))
"
```

Nine Excel files are created covering all validation rules:
- `events_valid.xlsx` — passes all rules
- `events_missing_sheet.xlsx` — wrong worksheet name
- `events_missing_column.xlsx` — required column missing
- `events_extra_column.xlsx` — unexpected column
- `events_invalid_type.xlsx` — data type mismatch
- `events_missing_value.xlsx` — null in required column
- `events_duplicate.xlsx` — duplicate in unique column

### 3. Run Tests

```shell
pytest tests/
```

All tests use local adapters; no cloud credentials needed.

### 4. Define a Data Contract

```python
from pathlib import Path
from py_common.contract import Column, Contract, Worksheet

contract = Contract(
    key_columns=["event_id"],
    worksheets=[
        Worksheet(
            name="Events",
            columns=[
                Column("event_id", data_type="string", nullable=False),
                Column("employer_id", data_type="string", nullable=False),
                Column("attendees", data_type="integer", nullable=False),
            ],
        )
    ],
)

# Validate an Excel file from the fixtures
excel_bytes = Path("tests/data/fixtures/events_valid.xlsx").read_bytes()
row_count = contract.validate(excel_bytes, "events_valid.xlsx")
print(f"Valid: {row_count} rows")
```

**Note:** Full pipeline orchestration with cloud adapters (SharePoint, GCS, BigQuery) is phase 2.

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
- `put(key, data)` — write object (idempotent)
- `lock(key)` — acquire exclusive write lock

**Warehouse** (protocol)
- Stages, validates, and loads data
- Implements `load(key, checksum, data, rows)`
- Atomically commits or rolls back entire transaction
- Records audit entry with row count and checksum

**Pipeline**
- Orchestrates the workflow
- Handles retry logic, deduplication, error recording
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

Protocols are Python's version of interfaces:

```python
from py_common.ports import Source

# Source is a Protocol (interface)
class MySource:
    def items(self):  # Required method
        ...
    def download(self, item):  # Required method
        ...

# MySource satisfies the protocol (structural typing)
```

This allows:
- Multiple implementations (SharePoint, S3, local folder)
- Swapping implementations without changing core code
- Testing with mocks and local adapters

## Configuration

Configuration is typically YAML with environment variable interpolation:

```yaml
# config.yaml
dev:
  tenant_id: ${SHAREPOINT_TENANT_ID}
  client_id: ${SHAREPOINT_CLIENT_ID}
  project: dev-project
  bucket: dev-bucket

prod:
  tenant_id: ${SHAREPOINT_TENANT_ID}
  client_id: ${SHAREPOINT_CLIENT_ID}
  project: prod-project
  bucket: prod-bucket
```

Load and use:

```python
from py_common.config import load_config, interpolate, require_keys

config = load_config("config.yaml")
dev_cfg = config["dev"]

# Interpolate environment variables only when section is used
dev_cfg = {k: interpolate(v) if isinstance(v, str) else v 
           for k, v in dev_cfg.items()}

require_keys(dev_cfg, ["tenant_id", "client_id", "project"])
```

## Error Handling

```python
from py_common.errors import (
    ContractError,       # Workbook structure wrong
    ValidationError,     # Data violates rules
    ServiceError,        # Cloud service failed
    VersionMismatch,     # Source changed during download
)

try:
    rows = contract.validate(excel_bytes, filename)
except ContractError as e:
    # Schema mismatch; always quarantine
    quarantine_file(e)
except ValidationError as e:
    # Data rule violation; quarantine
    quarantine_file(e)
except ServiceError as e:
    # Cloud service unavailable; retry later
    retry_later()
```

## Testing

### Run Unit Tests

```bash
pytest tests/ -v
```

### Run with Coverage

```bash
pytest tests/ --cov=src/py_common --cov-report=html
```

### Run Only Integration Tests

```bash
pytest tests/ -m integration
```

(Integration tests requiring live services are skipped by default.)

### Generate Fixtures (for development)

```bash
python scripts/make_fixtures.py
```

## What's Implemented

✅ Core pipeline orchestration  
✅ Contract validation  
✅ Configuration with interpolation  
✅ Local adapters (Source, ObjectStore) for testing  
✅ Fixture generation  
✅ Error hierarchy  
✅ Unit tests  

## What's Next (Phase 2)

⏳ SharePoint Online adapter  
⏳ Google Cloud Storage adapter  
⏳ BigQuery adapter  
⏳ Integration tests for cloud services  
⏳ Logging and structured output  

## Dependencies

- openpyxl >= 3.0 — Read/write Excel
- pyyaml >= 6.0 — YAML configuration
- google-cloud-storage >= 2.10 — GCS (phase 2)
- google-cloud-bigquery >= 3.13 — BigQuery (phase 2)
- microsoft-graph-core >= 0.2 — Graph API (phase 2)
- azure-identity >= 1.14 — Azure auth (phase 2)

## Standards

- **PEP 8**: 79-character lines, strict formatting
- **Type hints**: All functions annotated
- **Google docstrings**: Clear, concise documentation
- **TDD**: Tests written before or alongside code
- **RAP**: Reproducible, auditable, peer-reviewed

## License

MIT

## Contact

Questions? Open an issue or contact the data engineering team.
