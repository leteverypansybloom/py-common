# py-common: Shared Ingestion Library

Reusable Python library for Excel → Cloud workflows. Handles file discovery, validation, storage, and warehouse loading.

**Status**: Early development. The core pipeline, local adapters, and
the GCS, BigQuery and SharePoint adapters are built and unit-tested
against mocks. There are two load routes: typed tables merged on key
columns, and raw JSON tables for cleaning in Dataform. The SharePoint
adapter has not yet been run against a real site: it waits on IT's
decision on authentication. See [Adapter status](#adapter-status).

**New to this repo?** See [`docs/README.md`](docs/README.md) for a
guided path: a hands-on getting-started walkthrough, a guide to
whether py-common fits your next ingestion source, and a full
feature-by-feature status reference.

## What It Does

```
Source (discover files)
    ↓
Download (SharePoint, GCS landing bucket, local folder)
    ↓
Validate (against contract; quarantine or warn)
    ↓
Store Raw (immutable copy)
    ↓
Convert (Parquet, or JSON rows for raw tables)
    ↓
Store Processed (immutable copy)
    ↓
Load Warehouse (staging, then merge or insert)
    ↓
Audit (record every attempt)
```

Two load routes share the steps above:

- **Typed** (`output_format="parquet"`, the default, with
  `BigQueryWarehouse`): the primary worksheet is merged into a typed
  table on the contract's key columns.
- **Raw JSON** (`output_format="raw_json"`, with `RawJsonWarehouse`):
  every configured worksheet is inserted into its own raw table, one
  JSON payload per row, for cleaning in Dataform. See
  [`docs/RAW_TABLE_LAYOUT.md`](docs/RAW_TABLE_LAYOUT.md) and
  [`docs/GCP_SETUP.md`](docs/GCP_SETUP.md).

## Quick Start

See [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) — the single
place for environment setup, generating test fixtures, defining your
first contract and wiring a pipeline. The fixtures themselves are
described in
[`tests/data/fixtures/README.md`](tests/data/fixtures/README.md).

## Architecture

### Core Concepts

**Source** (protocol)
- Discovers files at a location (SharePoint, GCS bucket, local folder)
- Downloads exact bytes of a discovered version
- Prevents processing stale or partially-written files

**Contract**
- Defines required worksheets, columns, data types
- Validates structure and business rules
- Per worksheet: which row holds the headings (`header_row`), which
  columns are loaded (`column_selection`, `excluded`), and whether a
  mismatch quarantines the file or is recorded as a warning
  (`on_mismatch`)
- Names every column: blank headings become `col_B`, repeated ones
  `Other_2`
- Deterministic; same file always produces same result

**ObjectStore** (protocol)
- Immutable storage for raw files, processed data, audit records
- `get(key)` — retrieve object by path
- `put(key, data)` — write object; repeating an identical write
  succeeds, but writing different bytes to an existing key raises
  `ObjectStoreConflict`
- `lock(key)` — acquire exclusive write lock (real in
  `GCSObjectStore`, a no-op in `LocalObjectStore`; see
  [Concurrency](#concurrency))

**Warehouse** (protocol)
- Stages, validates, and loads data
- Implements `load(key, checksum, data, rows)`
- Atomically commits or rolls back entire transaction
- Records audit entry with row count and checksum

**Pipeline**
- Orchestrates the workflow
- Handles deduplication (by checksum, per destination) and error
  recording
- Returns list of Result objects (one per file), with any warnings

### Concurrency

`GCSObjectStore.lock()` is a real exclusive lock. It creates a small
object under `locks/` in the bucket using GCS's atomic
create-if-absent (`if_generation_match=0`), so if two runs start at
once (a second scheduler trigger, a manual re-run, a retry) only one
proceeds. The other gets a `RuntimeError` naming the holder.

- The lock is a lease. It records when it expires
  (`GCSObjectStore(..., lease_seconds=3600)` by default), and an
  unexpired lock is never taken.
- If a run is killed without releasing it, later runs are refused
  until the lease expires; the next run then takes the lock over
  (atomically, so only one run can) and logs a WARNING naming the
  old holder. `lease_seconds` must exceed the Cloud Run job or
  service timeout.
- Release only deletes the lock this run created.
- The Cloud Run Job's service account needs create and delete
  permission on objects in the bucket.

### SharePoint source

`SharePointSource` (`adapters/sharepoint.py`) lists and downloads
files in one SharePoint folder through Microsoft Graph. It needs two
things:

- **Where the files are** — a `SharePointConfig`, usually from a
  config section:

  ```yaml
  sharepoint:
    hostname: ${SHAREPOINT_HOSTNAME}     # contoso.sharepoint.com
    site_path: ${SHAREPOINT_SITE_PATH}   # /sites/DataSite
    library: Documents
    folder: Incoming                     # optional; library root if omitted
  ```

- **How to authenticate** — a `token_provider`: any function that
  returns a Graph access token. This is deliberately not built yet.
  Once IT approves a credential flow, write that function and pass
  it in; nothing else changes.

```python
source = SharePointSource(
    SharePointConfig.from_mapping(config["sharepoint"]),
    token_provider=approved_token_function,
)
```

Credentials never go in the config: unknown keys such as
`client_secret` are rejected. Files are identified by Graph item ID,
versioned by eTag, and refused with `VersionMismatch` if they change
between listing and download. Throttling (429) and transient 5xx
errors are retried with `Retry-After` or 1s/2s/4s waits. Only files
directly in the folder are read, not sub-folders.

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
does this for you, and carries on with the next file whatever
happens to one:

| Error | Meaning | Outcome |
|---|---|---|
| `ValidationError` (from the contract or the warehouse) | The file itself is wrong | `QUARANTINED`: copied to `quarantine/`, not retried |
| `ServiceError` from the warehouse | A cloud service failed; nothing was committed | `FAILED`: retried by the runner |
| `IndeterminateCommitError` | Unknown whether the warehouse committed | `FAILED`: retried by the runner |
| Anything else | Unexpected | `FAILED` |

The BigQuery adapters raise only these types: a job BigQuery reports
as failed is a `ServiceError` naming the job ID, and if waiting for
the commit job fails (timeout, dropped connection) the job is re-read
by ID before anything is reported. The full hierarchy is in
`src/py_common/errors.py`.

A worksheet with `on_mismatch="warn"` doesn't raise for a header or
row problem. The problem is appended to the `warnings` list passed to
`validate()`, and `Pipeline` logs it at WARNING and keeps it on the
result and the audit record.

## Testing

See [`CONTRIBUTING.md`, Run Tests Locally](CONTRIBUTING.md#run-tests-locally).

## Adapter status

| Adapter | Implements | Status |
|---|---|---|
| `LocalSource` | Source | ✅ Built, unit-tested |
| `LocalObjectStore` | ObjectStore | ✅ Built, unit-tested |
| `GCSSource` | Source | ✅ Built, unit-tested against mocks; reads a landing folder, versions by object generation |
| `GCSObjectStore` | ObjectStore | ✅ Built, unit-tested against mocks; real `lock()` |
| `BigQueryWarehouse` | Warehouse | ✅ Built, unit-tested against mocks; `MERGE` on `key_columns` |
| `RawJsonWarehouse` | Warehouse | ✅ Built, unit-tested against mocks; one raw JSON table per sheet, not yet run live |
| `SharePointSource` | Source | ✅ Built, unit-tested against mocked Graph; not yet run live (auth pending IT) |
| Secret Manager | SecretStore | ⏳ Not started |

Also built: contract validation, the raw JSON conversion (`raw.py`),
configuration with interpolation, fixture generation, the error
and the error hierarchy.

## What's Next

- ⏳ First live run of the raw JSON route ([`docs/GCP_SETUP.md`](docs/GCP_SETUP.md))
- ⏳ SharePoint authentication (token provider), once IT approves a flow
- ⏳ Live SharePoint test: one file, then the full Cloud Run job
- ⏳ Secret Manager adapter
- ✅ Structured JSON logging (`py_common.logging.configure_json_logging()`)
- ⏳ Alert policies as code (manual steps in `docs/GCP_SETUP.md`)

The agreed order, with reasoning, is in
[`docs/INGESTION_FRAMEWORK_GUIDE.md`, Section 9](docs/INGESTION_FRAMEWORK_GUIDE.md#9-priority-order-for-closing-the-gaps).

## Dependencies

Core:

- openpyxl >= 3.1, < 4 — Read Excel
- pyyaml >= 6.0, < 7 — YAML configuration
- pyarrow >= 15, < 27 — Parquet output, typed from the contract

Every runtime dependency has an upper bound, so a fresh install
never picks up a major version the code hasn't been tested against.

Optional extras (`pip install -e ".[gcp]"` etc.):

- `gcp`: google-cloud-storage >= 2.10, < 4; google-cloud-bigquery
  >= 3.13, < 4; google-api-core >= 2.11, < 3
- `sharepoint`: requests >= 2.31 (authentication library to follow IT's decision)
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
- `scripts/gcp_auth.py` — a Python port of the R `gcp_auth.R` helper.
  Not part of the library: the adapters authenticate through Google's
  client libraries (Application Default Credentials). Kept for code
  ported from R; likely to be removed.

The first two read their project settings from environment variables; see the
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
