# Prompt: implement the py-common review fixes

> Paste everything below the line into a fresh Claude Code session opened at
> the repo root. It's self-contained, but it expects `REVIEW.md` to be present.

---

You're working in `py-common`, a shared Python library (src layout,
`src/py_common/`) that ingests Excel workbooks into GCP. The flow is: a
`Source` (SharePoint, GCS landing folder or local folder) → `Contract`
validation → an immutable `ObjectStore` (GCS) → a `Warehouse` (BigQuery:
typed MERGE via `BigQueryWarehouse`, or raw JSON tables via
`RawJsonWarehouse`) → Dataform. `Pipeline` (`pipeline.py`) orchestrates it,
and `runner.py` maps outcomes to Cloud Run job exit codes and Pub/Sub retries.

`REVIEW.md` in the repo root is a code review with finding IDs (C1, H1, M3
and so on). Read it in full first. Your job is to implement its findings in
the order below. **The goal that matters most: a file is never loaded into
BigQuery twice, whatever fails and wherever it fails.**

## Ground rules

- **Out of scope; don't touch:** `containers/` (to be deleted separately)
  and `scripts/gcp_auth.py` plus `tests/scripts/` (parked; to be deleted
  later).
- Match the existing style: Google docstrings, 79-character lines,
  `mypy --strict`, comments that explain *why*, `%s` logging args (not
  f-strings), and `py_common.errors` types.
- Core modules (`pipeline.py`, `runner.py`, `contract.py`, `raw.py`,
  `model.py`, `ports.py`) must keep importing **no cloud SDK**.
- **Write tests first** for every behaviour change. A test asserts
  behaviour, such as an outcome, a raised type, rows or a key written,
  rather than only checking that a mock was called. Where SQL changes,
  also assert the parts of the SQL that carry the semantics.
- Work on a branch named `review-fixes`, with **one commit per phase**
  below. Each commit message lists the finding IDs it closes. Don't push
  or open a PR unless I ask.
- After every phase run the following, and keep them all green:
  ```
  pip install -e ".[dev,gcp]"
  pytest -m "not integration"
  ruff check src tests
  black --check src tests
  mypy src/py_common --strict
  ```
- If a fix needs a decision the review left open (marked "unverified", or
  given as options), pick the safer option, note the choice in the commit
  message, and carry on. Stop and ask only if a change would break the
  documented raw-table layout (`docs/RAW_TABLE_LAYOUT.md`) for existing
  Dataform consumers.
- Update the docs (`README.md`, `docs/*.md`) in the same commit as the
  behaviour they describe. In `REVIEW.md`, append "**Fixed in <phase>**"
  to each finding's row.

## Phase 0: tooling (M14)

1. Add a mypy override in `pyproject.toml` for `google.cloud.*` and
   `google.api_core.*` (`ignore_missing_imports = true`) so
   `mypy --strict` passes.
2. Set lower and upper bounds on runtime deps (e.g. `pandas>=2.0,<3`,
   `pyarrow>=15,<27`, `openpyxl>=3.1,<4`, `google-cloud-bigquery>=3.13,<4`,
   `google-cloud-storage>=2.10,<4`). Check that the suite passes on the
   bounds you choose.
3. CI (`.github/workflows/tests.yml`): matrix Python 3.11 and 3.12,
   `actions/checkout@v4`, `actions/setup-python@v5`, and a `pip-audit` step.
   Move the ruff, black and mypy steps before pytest so they fail fast.

## Phase 1: fail-closed storage and correct error types (C1, M10, M11, H1, L9, M3)

1. `GCSObjectStore.get` returns `None` **only** on `NotFound`, and raises
   `ServiceError` on any other `GoogleAPICallError`.
2. `GCSObjectStore.put` raises `ObjectStoreConflict` (not `ValueError`) on
   different content, and wraps `GoogleAPICallError` as `ServiceError`.
   Compare existing objects by `crc32c`/`md5_hash`, not by downloading them
   (M6, partly).
3. Add `tests/unit/test_object_store_contract.py`: one parametrised suite
   run against `LocalObjectStore` and a mocked `GCSObjectStore`, covering
   identical put, conflicting put, missing get and erroring get.
4. `BigQueryWarehouse._ensure_staging_table` catches `NotFound` only.
5. Both BigQuery adapters:
   - When the server reports that a job failed, raise `ServiceError`. The
     message must include `job_id`.
   - On a transport error or timeout, `reload()` the job:
     - DONE with no error → success.
     - DONE with an error → `ServiceError`.
     - Anything else, or the reload itself fails → `IndeterminateCommitError`.
   - Never claim "rolled back" unless the job reports an error. Use the
     sketch under H1 in REVIEW.md.
6. Rewrite the `Warehouse` docstring in `ports.py` around the error types
   the pipeline acts on (`ServiceError`, `IndeterminateCommitError`,
   `ValidationError`).
7. In `Pipeline._load_to_warehouse`, a warehouse `ServiceError` gives
   `Outcome.FAILED` (retryable), not `QUARANTINED`. Update
   `tests/unit/test_pipeline.py` and the README's "Error Handling" section.

## Phase 2: idempotent loads (C2, H2, M4); the most important phase

1. Make each warehouse's script transaction idempotent. Inside the
   transaction, skip the insert or MERGE if `audit` already has a `LOADED`
   row for this `destination` and `checksum`. Return a status, and have
   `load()` return `bool` (`True` = inserted, `False` = already loaded).
   Update the `Warehouse` protocol to match.
2. Extend the audit table:
   - Add `destination STRING` (the warehouse identity) and
     `source_version STRING`.
   - Update the `CREATE TABLE` in `docs/GCP_SETUP.md`.
   - Add an idempotent migration snippet:
     `ALTER TABLE … ADD COLUMN IF NOT EXISTS …`.
   - Treat old rows with NULL `destination` as "not loaded here".
3. Dedup key (H2):
   - **Raw JSON route:** key on `identity + version`, so re-uploading
     earlier content under a new generation inserts it again and "latest
     generation" stays correct.
   - **Typed route:** keep `destination + checksum`.
   - The store marker key follows the same rule.
   - Document this in `docs/RAW_TABLE_LAYOUT.md` with the revert example.
4. Treat the store marker as a cache only. Write it **before** the audit
   JSON record. A failed audit or marker write on one file records that
   file as FAILED and the run continues (M4).
5. Tests:
   - Simulate a crash after the commit but before the marker, run again,
     and assert the second load is a no-op. Do this for both warehouses,
     using a fake warehouse that keeps state, not a bare Mock.
   - Simulate a revert: gen1 = A, gen2 = B, gen3 = A on the raw route, and
     assert gen3 is loaded.

## Phase 3: merge correctness (H3, H4)

1. Add a `Contract.__post_init__` check: every `key_columns` entry is a
   listed, `nullable=False` column of `worksheets[0]`.
2. `BigQueryWarehouse.load` raises `ContractError` if any key column is
   missing from staging. Remove the silent filtering.
3. Before the MERGE, query staging for duplicate keys (`GROUP BY … HAVING
   COUNT(*) > 1 LIMIT 5`). If any are found, raise `ValidationError`
   naming them, so the file is quarantined.
4. In `Contract.validate`, add composite-key uniqueness on `key_columns`,
   so quarantine happens before anything reaches BigQuery.

## Phase 4: typed conversion and schema drift (H5, M2)

1. Replace the pandas inference in `Pipeline._to_parquet` with an
   explicit `pyarrow` schema built from `Column.data_type`. Untyped
   `all_except` columns are `string`.
2. Values that can't be coerced become null plus a warning in "warn" mode.
   In "quarantine" mode, validation has already rejected them.
3. Add a regression test: an int and `"NA"` in one column must not raise.
4. Before the MERGE, compare the staging schema with the final table:
   - Add new nullable columns with `ALTER TABLE ADD COLUMN`.
   - If a type changed or a column was removed, raise `ContractError`
     with the diff.
   - If the final table is missing, create it from the staging schema
     plus `_loaded_at`.

## Phase 5: concurrency (H6, H7)

1. Turn `GCSObjectStore.lock` into a lease:
   - Store `expires_at`, with `lease_seconds` as a constructor argument
     (default 3600, documented as "must exceed the job or service
     timeout").
   - A run may take over an *expired* lock only through
     `upload_from_string(..., if_generation_match=<old generation>)`.
   - Log takeovers at WARNING, with the old holder.
   - Keep the rule "never take over an unexpired lock".
   - Test expiry, takeover and the race where two runs try to take over.
2. Per-load staging tables in both BigQuery adapters:
   `{table}_stg_{checksum[:12]}_{uuid8}`, with a one-day
   `expiration_time`, dropped after the commit (best effort, with a
   warning on failure). Update the IAM notes in `docs/GCP_SETUP.md` if
   needed.

## Phase 6: cost, performance, cleanups (M1, M5, M6, L1–L8, N1–N3, M11)

- **M1:** create raw tables clustered on `source_file, source_generation`.
  Add a `bq update --clustering_fields` line for existing tables to
  `GCP_SETUP.md`.
- **M6:** parse each workbook **once**. Add a
  `Contract.read(excel_bytes, name, warnings) -> dict[str, SheetRows]`
  that validates and extracts in one pass, using `read_only=True`.
  `Pipeline` calls it once. Keep `validate`/`extract` as thin wrappers so
  the public API doesn't break.
- **M5:** warn (in "warn" mode) or raise (in "quarantine" mode) when a
  formula cell has no cached value.
- **L1:** `LocalSource` versions by `st_mtime_ns` plus size.
- **L2:** whitespace-only strings count as null in required checks.
- **L3:** normalise tz-aware datetimes to naive UTC; `Decimal` becomes a
  JSON number.
- **L4:** use `load_job.output_rows` instead of `COUNT(*)`.
- **L5:** use `removeprefix("gs://")`.
- **L7:** validate the freshness view name against
  `^[\w-]+\.[\w-]+\.[\w-]+$`.
- **L8:** require `eventType == OBJECT_FINALIZE`. Check that the
  notification setup in `GCP_SETUP.md` sends attributes.
- **N1–N3:** `%s` logging in the adapters, remove redundant `pass`,
  `StrEnum`.

## Phase 7: observability and API (M12, L6, M13, M7)

1. **M12:**
   - Generate a `run_id` in `Pipeline.run`.
   - Log through `extra={"run_id", "warehouse", "item_identity",
     "version", "checksum", "bq_job_id"}`.
   - Add `py_common.logging.configure_json_logging()`, which emits
     Cloud-Logging-compatible JSON (`severity`, `message`, plus those
     fields).
   - Log BigQuery job IDs at INFO.
   - Emit one summary line per file.
2. **L6:**
   - Export the supported API in `py_common/__init__.py` `__all__`:
     `Pipeline`, `Contract`, `Worksheet`, `Column`, `Outcome`, `Result`,
     `SourceItem`, the ports and the errors.
   - Move `ConfigError` into `errors.py`, and keep a re-export in
     `config.py`.
   - Add `CHANGELOG.md` and bump to `0.2.0`. Phase 2 changed the return
     type of `Warehouse.load` and the audit schema, so call that out as
     breaking.
3. **M13:** add `tests/integration/test_bq_dry_run.py`. It builds every
   SQL statement the adapters generate and runs each one with
   `QueryJobConfig(dry_run=True)` against `$GCP_PROJECT_ID`. Mark it
   `integration` and skip it when the variable is unset. Document how to
   run it in `CONTRIBUTING.md`. Don't add an emulator dependency.
4. **M7:** in `docs/GCP_SETUP.md`:
   - Replace `objectAdmin` on the store bucket with `objectCreator` +
     `objectViewer`.
   - Grant delete only on `locks/`, using an IAM condition
     (`resource.name.startsWith(".../objects/locks/")`).
   - Suggest a retention policy for `raw/` and `audit/`.

## Done means

- Every phase is committed on `review-fixes`, and all checks are green.
- Every C, H and M finding in `REVIEW.md` except M9 is marked fixed, or
  has a one-line reason it wasn't. M9 and L10 are already handled by
  parking `gcp_auth.py`.
- When you finish, report:
  - the commits, one line each;
  - any decisions you made on open questions;
  - anything you couldn't verify without live GCP. Expect to list at
    least: script-transaction behaviour under concurrent `IF EXISTS`,
    clustering on existing tables, and the IAM condition.
