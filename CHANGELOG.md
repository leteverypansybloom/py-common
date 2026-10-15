# Changelog

All notable changes to py-common. The supported API is what
`py_common.__all__` exports, plus the adapters in `py_common.adapters.*`,
`py_common.runner` and `py_common.logging`. Versions follow
[semantic versioning](https://semver.org/): while the major version
is 0, a minor release (0.x.0) may break that API, and says how below.

## 0.2.0 — 2026-10-15

Implements the code review in `REVIEW.md`. The aim: a file is never
loaded into BigQuery twice, whatever fails and wherever it fails.

### Breaking

- **`Warehouse.load()` takes the source item and returns `bool`.**
  Signature: `load(key, checksum, data, rows, item) -> bool`; `True`
  if the rows were written, `False` if the warehouse's audit table
  shows the file already loaded. Custom warehouses must add the
  parameter, return a bool, and should make the same in-transaction
  check (see `ports.Warehouse`).
- **Audit table schema.** Two new columns, `destination STRING` (the
  destination table) and `source_version STRING`. Create the table as
  in `docs/GCP_SETUP.md` (section 2), or add the columns to an
  existing one with the `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  given there. Rows from 0.1.x have `destination` NULL and count as
  "not loaded here", so start with a fresh dataset where possible.
- **"Already loaded" means this upload, on both routes.** A file is
  identified by its source identity and version (e.g. GCS
  generation), not its bytes: every new upload loads, even if it
  repeats an earlier one, so a restore (A, then B, then A again)
  puts A back. The same upload never loads twice. Store markers are
  `audit/{warehouse}/versions/{sha256(identity)}/{version}`; raw-route
  `processed/` copies include the version.
- **One audit row per destination table.** `RawJsonWarehouse` checks
  and records each raw table separately, so adding a table to its
  mapping loads only that table. A mapped sheet with no rows still
  gets an audit row (`row_count` 0).
- **Empty worksheets are rejected** (quarantined) unless the
  `Worksheet` sets `allow_empty=True`, whatever `on_mismatch` says.
- **`_deleted_at` and `_source_file` are reserved** typed-table
  column names, like `_loaded_at`.
- **Warehouse `ServiceError` is `FAILED` (retried), not
  `QUARANTINED`.** A `ValidationError` from the warehouse is
  quarantined.
- **Error types.** `GCSObjectStore.put()` raises `ObjectStoreConflict`
  (was `ValueError`); `GCSObjectStore.get()` raises `ServiceError` on
  any failure other than "not found" (was `None`); the BigQuery
  adapters raise `ServiceError` / `IndeterminateCommitError` (were
  `RuntimeError`); a missing merge key column is `ContractError`.
- **`Contract` refuses unusable `key_columns`** at construction: each
  must be a listed, `nullable=False` column of the first worksheet.
- **Files with formulas that have no saved value** are quarantined
  ("quarantine" worksheets) or warned about ("warn").
- **`BigQueryWarehouse.staging_table_id` is gone**; each load stages
  into its own `{table}_stg_{checksum[:12]}_{random}` table.
- **`LocalSource` versions are `"{st_mtime_ns}-{st_size}"`** (were
  whole seconds).
- **Upload events need `eventType=OBJECT_FINALIZE`**; a message
  without the attribute is ignored.
- **`check_freshness()` accepts only `project.dataset.view`.**
- **pandas is no longer a runtime dependency** (Parquet is written
  with pyarrow); it moved to the `dev` extra.

### Added

- `Contract.read()`: validate and extract in one read-only parse.
- `py_common.logging.configure_json_logging()`: Cloud Logging JSON
  with `run_id`, `warehouse`, `item_identity`, `version`, `checksum`
  and `bq_job_id` fields, and one summary line per file.
- `BigQueryWarehouse(mark_deleted_rows=True)`: for cumulative files,
  rows missing from a file's newest upload get `_deleted_at` set
  (and cleared if they come back); only rows from the same file
  (`_source_file`) are affected. Rows are never physically deleted.
- `Worksheet(allow_empty=True)` for sheets where no rows is valid.
- `GCSObjectStore(lease_seconds=3600)`: the run lock is a lease that
  a later run takes over, atomically, once it has expired.
- `BigQueryWarehouse` creates a missing final table and adds new
  columns; a changed type or removed column quarantines the file.
- `tests/integration/test_bq_dry_run.py`: dry-runs every generated
  SQL statement against a sandbox project.
- `py_common.__all__` lists the supported API; `ConfigError` moved to
  `py_common.errors` (still importable from `py_common.config`).

### Fixed

- Dedup is fail-closed and enforced by BigQuery inside the commit
  transaction, so a lost marker, a crash after commit or a dropped
  connection can't load a file twice.
- Parquet columns are typed from the contract: mixed values in a
  "warn" column no longer fail conversion, and empty columns keep
  their type.
- Duplicate or NULL merge keys quarantine the file before the MERGE.
- A failed audit or marker write fails only that file; the run goes
  on.
- Raw tables are clustered on `source_file, source_generation`.
- Smaller fixes: whitespace-only cells count as empty, tz-aware
  datetimes become UTC and `Decimal` a JSON number in raw rows, typed
  row counts come from the load job, `gs://` is stripped only as a
  prefix.

### Known limitations

- **Not strictly exactly-once under every failure.** A commit job
  still running when its client gives up isn't resumed; a retry
  starts a new one, and the lock lease isn't renewed or fenced. The
  in-transaction audit check stops ordinary retries loading twice,
  but two commits racing each other could both pass it.
- **Uploads are applied in the order they're processed.** If an
  older version of a file is processed after a newer one, its values
  win on the typed route.
- **A renamed column in a `"warn"` sheet** loads as its old name with
  `null` on every row; Dataform can't tell it from an empty column.
  The warning is logged and kept in the audit JSON, not in BigQuery.
- **Re-pointing a raw table at a different sheet** (swapping the
  mapping) doesn't reload files that table already had.
- Only each landing file's current version is loaded; there is no
  replay command.

### Tooling

- Dependencies have upper bounds; CI tests Python 3.11 and 3.12,
  runs ruff/black/mypy before pytest, and runs `pip-audit`.

## 0.1.0

First release.
