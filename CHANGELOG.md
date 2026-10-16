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
  `audit/{warehouse}/versions/{sha256(identity)}/{version}`.
- **Processed copies are keyed by upload and content:**
  `processed/{warehouse}/{identity}/{checksum}/{version}/{digest}/{name}.parquet`
  (or `.ndjson`), where `{digest}` is the first 16 hex digits of the
  converted bytes' SHA-256. Converting an upload again with a
  different result (a contract fix, a pyarrow upgrade) writes a new
  copy, where it used to fail with `ObjectStoreConflict` on every
  retry; a plain retry reuses its copy.
- **One audit row per destination table.** `RawJsonWarehouse` checks
  and records each raw table separately, so adding a table to its
  mapping loads only that table. A mapped sheet with no rows still
  gets an audit row (`row_count` 0).
- **Empty worksheets are rejected** (quarantined) unless the
  `Worksheet` sets `allow_empty=True`, whatever `on_mismatch` says.
- **`_deleted_at` and `_source_file` are reserved** typed-table
  column names, like `_loaded_at`.
- **Typed tables have BigQuery-safe column names.** Headings used to
  become column names as written, and one BigQuery refuses (a space
  in it, a clash that differs only in case, over 300 characters)
  failed the file on every retry. Now `Date of Enquiry` becomes
  `date_of_enquiry`, by the rules in the README ("Typed column
  names"; `py_common.parquet.column_name()`). Contracts and
  `key_columns` still use the headings; `BigQueryWarehouse` refuses
  key columns that would share a name. Raw JSON payload keys are
  unchanged.
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
- **`Pipeline` checks its configuration when built** and raises
  `ConfigError`: a `BigQueryWarehouse` must merge on the contract's
  `key_columns`, and a `RawJsonWarehouse` must map exactly the
  contract's worksheets.
- **Oversized workbooks are quarantined before parsing:**
  `Contract(max_file_bytes=50 MiB, max_uncompressed_bytes=500 MiB)`
  by default.
- **An already-loaded upload is skipped before it is downloaded**, so
  its `Result` and audit record have no checksum.
- **`configure_json_logging()` removes the root logger's other
  handlers** by default; `replace=False` keeps them.
- **`handle_upload()` ignores an upload to another bucket** than the
  one the folder's `GCSSource` reads.
- **Error messages leave out text that can quote data:** BigQuery
  failures are reported by job ID, status and reason (details: `bq
  show -j <job id>`), and an unreadable sheet by the parser's error
  type only.
- **Dependency floors:** `pyarrow>=23.0.1` and `requests>=2.33.0`
  (pip-audit advisories on the old floors); `defusedxml` is now a
  runtime dependency.

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
- `Contract(file_name=...)`: the name every upload of a workbook must
  have. A file under any other name is quarantined before it is
  parsed, so it can't become a second "latest" upload.
- `runner.check_duplicate_loads()`: for the daily job. Logs
  `Duplicate load:` for any upload the audit table shows loaded twice
  into one table, for a log-based alert (`docs/GCP_SETUP.md`,
  section 7).
- Audit records (`audit/records/*.json`) name the converted copy
  that was loaded (`processed_key`, also on `Result`), the contract it
  was converted with (`contract_fingerprint`, from the new
  `Contract.fingerprint()`) and `py_common_version`, so every load can
  be traced to the contract and library that produced it.
- `GCSObjectStore(lease_seconds=3600)`: the run lock is a lease that
  a later run takes over, atomically, once it has expired.
- `BigQueryWarehouse` creates a missing final table and adds new
  columns; a changed type or removed column quarantines the file.
- `tests/integration/test_bq_dry_run.py`: dry-runs every generated
  SQL statement against a sandbox project.
- `py_common.__all__` lists the supported API; `ConfigError` moved to
  `py_common.errors` (still importable from `py_common.config`).
- `constraints.txt`: the pinned runtime set for deployments. The
  `Dockerfile` builds with it (as a non-root user, on a digest-pinned
  base image), and CI audits it.

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
- Releasing the run lock never raises: a failure is logged
  (`Could not release lock`) and the run's results stand.
- A lock whose times have no time zone is refused, not a `TypeError`.
- Waiting for a staging load is bounded (600 s); a load still running
  is cancelled and the file FAILED for a retry.
- Only parser errors in a worksheet quarantine the file; anything
  else (e.g. `MemoryError`) fails it.

### Known limitations

- **Not strictly exactly-once under every failure.** A commit job
  still running when its client gives up (after ten minutes) isn't
  resumed; a retry starts a new one, and the lock lease isn't renewed
  or fenced. The in-transaction audit check stops ordinary retries
  loading twice, but two commits racing each other could both pass
  it. That is unlikely, so it is detected, not prevented:
  `check_duplicate_loads()` alerts on it, the documented raw "latest
  upload" query reads one load per upload, and a second typed commit
  merges the same values again.
- **Uploads are applied in the order they're processed.** If an
  older version of a file is processed after a newer one, its values
  win on the typed route.
- **A renamed column in a `"warn"` sheet** loads as its old name with
  `null` on every row; Dataform can't tell it from an empty column.
  The warning is logged and kept in the audit JSON, not in BigQuery.
- **Re-pointing a raw table at a different sheet** (swapping the
  mapping) doesn't reload files that table already had.
- Only each landing file's current version is loaded, and there is
  no replay command: to reload a file (after a contract fix, say),
  upload it again. `docs/RUNBOOK.md` covers reloading, undoing and
  removing uploads, and renaming a workbook.
- **A source is tracked by its file name.** On the raw route,
  "latest upload" is per `source_file`, so a cumulative file uploaded
  under a new name each time returns duplicate current rows; on the
  typed route, `mark_deleted_rows` never marks the old name's rows.
  Set `Contract.file_name` so any other name is quarantined, and
  treat renaming a workbook as a one-off change with a clean-up step
  (`docs/RAW_TABLE_LAYOUT.md`).

### Tooling

- Dependencies have upper bounds; CI tests Python 3.11 and 3.12,
  runs ruff/black/mypy before pytest, and runs `pip-audit` on the
  installed set and on `constraints.txt`.
- Pre-commit hooks match CI's versions (black 26.10.1, ruff 0.17.0,
  mypy 2.4.0), and the mypy hook passes in its own environment.
- Every live-cloud test is marked `integration`.
- Live BigQuery tests (`tests/integration/test_bigquery_live.py`) run
  both warehouses in a real test dataset: merges, deleted rows,
  restores, safe column names, schema changes, rollback, the "latest
  upload" query and the duplicate-load check. GitHub Actions runs
  them nightly and on demand, signing in without a stored key
  (`docs/CI_SETUP.md`).

## 0.1.0

First release.
