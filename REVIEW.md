# py-common — Code Review

*Reviewed at commit `a1a608e` (main), 2026-10-15. Whole codebase, not a diff.
Reviewed together from three angles: data engineering, data science and
software engineering. No source files were changed.*

## Orientation

py-common is a small shared Python library (about 3,400 lines of source, 0.1.0)
that Public Health Wales teams use to ingest Excel workbooks into GCP. Its
users are the data engineers who wire up one pipeline per source (HWW, WAS,
peer monitoring and so on) and run it as a Cloud Run job or an
Eventarc-triggered service. Downstream, the Dataform developers and analysts
who read the raw BigQuery tables also depend on it.

Data flow:

```
Source (SharePointSource | GCSSource landing folder | LocalSource)
  → download exact version (eTag / GCS generation) → SHA-256
  → dedup check: store marker audit/{warehouse}/{checksum}
  → Contract.validate (openpyxl; quarantine or warn per worksheet)
  → store raw/  → convert (Parquet of primary sheet | NDJSON of every sheet)
  → store processed/
  → Warehouse.load:
       BigQueryWarehouse: staging (WRITE_TRUNCATE) → MERGE on key_columns + audit row, one script txn
       RawJsonWarehouse:  {table}_staging per sheet → INSERT into raw tables + audit row, one script txn
  → audit record JSON + "loaded" marker in the store bucket
  → Dataform: picks latest source_generation per source_file, SAFE_CASTs JSON payload → consumers / Power BI
```

`runner.py` turns per-file outcomes into exit codes (job) or exceptions
(Pub/Sub redelivery). The git history is short (PR #1 added the raw JSON
route and the runner helpers). The repo also contains an unrelated
`containers/` R-runtime sub-project.

## Tooling run

All tools ran in a throwaway venv in the session scratchpad, on Python 3.13.5
(3.11 isn't installed locally; CI uses 3.11). I installed `.[dev,gcp]` plus
`pip-audit`, which resolved pandas 3.0.6, pyarrow 26.0.0,
google-cloud-bigquery 3.46.1 and openpyxl 3.1.5.

| Tool | Result |
|---|---|
| `pytest -m "not integration" --cov` | **309 passed, 2 skipped, 4 deselected**, 95% line coverage (`gcp_auth.py` 77%, the rest 91–100%). In the system Python without the `[gcp]` extra, 6 modules fail to collect (`ModuleNotFoundError: google.api_core`). |
| `ruff check src tests` | **2 errors**: E501 at `src/py_common/gcp_auth.py:105` and `:203`. |
| `black --check src tests` | **Fails**: `gcp_auth.py` would be reformatted. |
| `mypy src/py_common --strict` | **2 errors**: `import-untyped` for `google.cloud` at `adapters/bigquery.py:16` and `adapters/bigquery_raw.py:27`. The system env gave 12 errors, including 3 unused `type: ignore` in `gcp_auth.py`. |
| `pip-audit` | No known vulnerabilities in the project's resolved dependencies. Only `pip 25.1.1` (venv tooling, not a project dependency) is flagged. `py-common` itself is skipped as it isn't on PyPI. |

**What this implies (unverified, since I didn't look at CI runs):** the CI
workflow runs ruff, black and `mypy --strict` as separate steps, and with the
current main all three should fail. Either CI is red on main, or it is pinned
to older tool or library versions than a fresh install gets.

Also verified directly: `pd.DataFrame([{'a':1},{'a':'NA'}]).to_parquet()` raises
`ArrowInvalid` (this backs finding H5).

---

**Scope note (post-review):** `containers/` (an R-runtime sub-project) is
out of scope and due to be deleted, so its finding (former M15) has been
removed. `gcp_auth.py` has been moved to `scripts/` (see M9 and L10).

## Executive summary

- **Overall health: 6 / 10 ("solid foundations, unsafe at the edges").** The
  architecture (ports and adapters, immutable store, script transactions,
  generation-pinned downloads, an explicit raw-table contract doc) is well
  above average for a 0.1 library, and the tests are plentiful. But the
  idempotency story has gaps that **will produce duplicate rows in the raw
  tables** in realistic failure modes.
- **Dedup is fail-open.** `GCSObjectStore.get()` swallows every exception and
  returns `None`, so a transient GCS error or an IAM problem on the marker read
  looks like "not loaded yet", and the file is inserted again (C1).
- **The "loaded" marker is written outside the BigQuery transaction**, after
  the commit, with nothing in BigQuery enforcing uniqueness. Any crash, timeout
  or audit-write failure between commit and marker leads to a reload of
  insert-only raw rows on retry (C2).
- **The error contract between warehouses and the pipeline is broken.** The
  BigQuery adapters raise only `RuntimeError` and `ValueError`, so the
  pipeline's `ServiceError` → quarantine and `IndeterminateCommitError` paths
  are dead code against real adapters. A post-commit network drop is reported
  as "rolled back" (H1).
- **The dedup key is the content checksum alone**, not (identity, generation).
  A file reverted to earlier bytes is skipped, and the documented
  "latest generation" Dataform query then serves stale data (H2).
- **The typed MERGE route silently degrades.** Key columns missing from the
  data are dropped, and if none remain it switches to append-only. NULL keys
  never match. Mixed-type columns fail Parquet conversion (H3, H4, H5).
- **Cost and scale:** raw tables are partitioned by `ingested_at` but not
  clustered, and the documented Dataform pattern scans the whole table on
  every run. Workbooks are parsed twice, in non-read-only openpyxl mode
  (M-findings).
- **Observability is plain-text logging** with f-strings in the adapters. There
  is no run ID or structured fields, so diagnosing a failure across the lock,
  file and BigQuery job from logs alone is hard.
- **Tooling hygiene is slipping.** ruff, black and mypy all fail on main,
  dependencies are unbounded (fresh installs get pandas 3.x) and there is no
  lockfile. (The unused `gcp_auth.py` helper, the source of the ruff and
  black failures, has since been moved to `scripts/`.)
- **Tests are mock-heavy for BigQuery.** They assert SQL strings and call
  shapes, not BigQuery semantics. There is no emulator and no SQL dry-run. The
  live integration tests are skipped by default.

---

## Findings

Severity: **Critical** = silent data corruption or duplication likely in
normal operation; **High** = wrong data or a stuck pipeline in plausible
conditions; **Medium** = risk, cost or maintainability; **Low** = minor;
**Nit** = style.

| ID | Severity | Area | file:line | Issue | Recommendation |
|---|---|---|---|---|---|
| C1 | Critical | Correctness / DE | `src/py_common/adapters/gcs.py:97-106`, `src/py_common/pipeline.py:221` | `GCSObjectStore.get()` catches **all** exceptions and returns `None`. `_is_duplicate` treats `None` as "never loaded", so a transient 5xx, a timeout or a 403 on the marker read causes a re-load and duplicate raw rows. | Return `None` only on `NotFound`; raise `ServiceError` otherwise. Make the dedup decision fail-closed. |
| C2 | Critical | DE / idempotency | `src/py_common/pipeline.py:89-93, 491-496`; `src/py_common/adapters/bigquery_raw.py:197-207` | The BigQuery commit and the "loaded" marker aren't atomic. If the process dies, the audit `put` fails, or the run aborts after `warehouse.load` but before the marker is written, the next run inserts all rows again. Raw tables are insert-only, and the BigQuery audit table (which *is* transactional) is never consulted. | Make the warehouse idempotent: inside the script transaction, check `audit` for the checksum (+ destination) and skip if it is present. Treat the store marker as a cache only. See the write-up. |
| H1 | High | Correctness / reliability | `src/py_common/adapters/bigquery.py:239-248`, `src/py_common/adapters/bigquery_raw.py:197-207`, `src/py_common/pipeline.py:411-418` | Adapters wrap every failure in `RuntimeError("…were rolled back together")`. A client timeout or connection loss *after* COMMIT is indistinguishable from a rollback, and the message asserts something unverified. `ServiceError` and `IndeterminateCommitError` are never raised by real adapters, so those pipeline branches (and their tests, `tests/unit/test_pipeline.py:237-280`) exercise behaviour that production can't reach. | Map `google.api_core` errors to `ServiceError`. On an exception from `txn_job.result()`, re-fetch the job by ID: DONE without error → success; error → `ServiceError`; unknown → `IndeterminateCommitError`. Add contract tests that the adapters raise the documented types. |
| H2 | High | DE / correctness | `src/py_common/pipeline.py:231-245`; `docs/RAW_TABLE_LAYOUT.md` ("Latest upload") | Dedup is keyed on `(warehouse, checksum)` only. Upload sequence gen1 = A, gen2 = B, gen3 = A again (a revert): gen3 is skipped, and Dataform's `MAX(source_generation)` keeps returning gen2 (B), which is now **stale**. Two different files with identical bytes: the second never appears under its own `source_file`. | Key the marker on `identity + version + checksum`, or, for the raw route, record a lightweight "generation seen" row even when the content is a duplicate. At minimum, document the behaviour and have Dataform pick the latest by audit timestamp. |
| H3 | High | Correctness | `src/py_common/adapters/bigquery.py:192-206` | `key_columns = [c for c in self.key_columns if c in columns]`. A renamed or misspelled key column is **silently dropped**, and if none remain the MERGE falls back to `ON FALSE` (append-only). The result is duplicates with no warning. A partial key list also merges on fewer columns than intended, so unrelated rows get overwritten. | Raise if any configured key column is absent from staging. Validate `Contract.key_columns` against the primary worksheet's columns in `Contract.__post_init__`. |
| H4 | High | Correctness | `src/py_common/adapters/bigquery.py:276-278` | The `T.k = S.k` join never matches NULL keys, so rows with a NULL key are re-inserted on every load. Duplicate keys inside staging make the MERGE fail ("must match at most one source row"), and the error surfaces as a generic `RuntimeError`. | Use `T.k IS NOT DISTINCT FROM S.k` (or require non-null keys in the contract). Pre-check staging for duplicate keys and raise a `ValidationError` naming the keys, so the file is quarantined. |
| H5 | High | Correctness / DS | `src/py_common/pipeline.py:345-353` | Parquet conversion builds object-dtype columns from raw cell values. A "warn" sheet with `1` and `"NA"` in one column raises `ArrowInvalid` (verified), so the file is FAILED and retried forever. The schema is also inferred per file: a column that is all-NULL in one month becomes `null`/INT64 and conflicts with the final table later. | Build an explicit `pyarrow.schema` from `Column.data_type`, coercing unparseable values to null (with a warning) or quarantining. Don't rely on inference. |
| H6 | High | GCP / reliability | `src/py_common/adapters/gcs.py:155-208`; `docs/GCP_SETUP.md:316, 401` | The lock never expires. Any killed run (Cloud Run timeout, OOM, deploy) blocks **all** subsequent runs and events for that warehouse until someone deletes an object by hand. Events retried by Pub/Sub keep failing until retention expires, so data is silently lost from the event path. | Keep "never steal from a live run" but add a lease: write `expires_at` = start + job timeout + margin, and let a new run take over a lock past its expiry using `if_generation_match=<old gen>`. Alternatively, alert on lock age. |
| H7 | High | DE / concurrency | `src/py_common/adapters/bigquery.py:77`, `src/py_common/adapters/bigquery_raw.py:118, 173`; `src/py_common/pipeline.py:86` | The lock is scoped by `warehouse.identity`, but staging tables are named only by table. Two `RawJsonWarehouse`s that share one raw table (for example one per workbook family writing a common sheet) have different identities, so different locks and the **same `{table}_staging`**. Concurrent WRITE_TRUNCATE loads can stage another file's rows. The row-count check catches some of these cases, but not all. | Use a per-load staging table (`{table}_staging_{key-hash}`, or a session temp table / `_SESSION` in the script) and drop it afterwards, or lock per physical table. |
| M1 | Medium | GCP / cost | `src/py_common/adapters/bigquery_raw.py:248-258`; `docs/RAW_TABLE_LAYOUT.md` | Raw tables are partitioned by `ingested_at` but not clustered, and `require_partition_filter` isn't set. The documented `QUALIFY MAX(source_generation) OVER (PARTITION BY source_file)` scans the whole table on every Dataform run, and the cost grows with history (every monthly upload re-inserts the full cumulative file). | Cluster on `(source_file, source_generation)`. Consider an incremental Dataform model or a "latest" view driven by the audit table. Document the expected growth. |
| M2 | Medium | DE / schema drift | `src/py_common/adapters/bigquery.py:153-166, 184-206` | The final table must already exist with exactly the staging columns. A new or renamed column in the sheet ("all_except" picks it up automatically) breaks the MERGE with an opaque error, and a changed inferred type breaks it too. Nothing creates or evolves the final table. | Diff the staging schema against the final table schema up front. Add new nullable columns (`ALTER TABLE ADD COLUMN`) or raise `ContractError` with the diff. |
| M3 | Medium | Correctness | `src/py_common/pipeline.py:415-418` | A `ServiceError` from the warehouse (transient) is labelled `QUARANTINED`, but nothing is written to `quarantine/` and the runner treats it as "don't retry", so the Pub/Sub event is acknowledged. It is only reloaded by the next daily job, and quarantine alerts fire for an infrastructure problem. | Map warehouse `ServiceError` to `FAILED` (retryable). Keep `QUARANTINED` for file problems only. |
| M4 | Medium | Correctness | `src/py_common/pipeline.py:88-96`, `:490` | If any audit `put` fails, the exception escapes the loop and aborts the run, leaving later files unprocessed, possibly after a successful BigQuery load (see C2). | Catch per-file audit errors, record the file as FAILED and continue. Write the marker before the audit record, or as part of it. |
| M5 | Medium | Correctness | `src/py_common/contract.py:252-258` | `data_only=True` returns `None` for formula cells with no cached value (files saved by non-Excel tools or generated programmatically), so they load silently as NULL or fail "required". | Detect formula cells without a cached value (load once with `data_only=False`, or compare) and warn or quarantine. |
| M6 | Medium | Performance | `src/py_common/contract.py:252-258`; `src/py_common/pipeline.py:131, 323, 345` | Every workbook is fully parsed **twice** (`validate` then `extract`) with `read_only=False`, and `read_sheet` materialises the whole grid. Memory is roughly 10× the file size per parse. Downloads, store puts (`gcs.py:145` re-downloads the existing object to compare) and NDJSON are all held in memory too. | Parse once and return `SheetRows` and the validation result together. Use `read_only=True` (no merged-cell features are used). Compare by `md5_hash`/`crc32c` instead of downloading. |
| M7 | Medium | GCP / IAM | `docs/GCP_SETUP.md:115-118` | `roles/storage.objectAdmin` on the store bucket lets the job overwrite or delete raw, audit and quarantine objects. Immutability is enforced only by code. | Grant `objectCreator` + `objectViewer` on the bucket, and `objectAdmin` only on `locks/` via an IAM condition. Consider a bucket retention policy on `raw/` and `audit/`. |
| M9 | Medium | Design | `scripts/gcp_auth.py` (was `src/py_common/gcp_auth.py`); `adapters/bigquery.py:78`, `adapters/gcs.py:78` | **Partly resolved after review:** `gcp_auth` was a second, unused way to authenticate (module-global state, a hand-rolled OAuth refresh) that had already drifted from gcloud (see L10). Nothing in the library imported it: the adapters use `Client(project=…)`, which calls `google.auth.default()`. It has been moved to `scripts/`, with its tests in `tests/scripts/`. | Delete it once nobody ports R code with it. Optionally let adapters accept a `credentials`/`client` argument for callers that need a non-ADC identity. |
| M10 | Medium | Errors / API | `src/py_common/adapters/gcs.py:149-153`; README "ObjectStore" | `GCSObjectStore.put` raises `ValueError` on conflict, while `LocalObjectStore` raises `ObjectStoreConflict` and the README documents `ObjectStoreConflict`. GCS API errors in `put`/`lock` aren't wrapped in `ServiceError`. | Raise `ObjectStoreConflict` and wrap `GoogleAPICallError` → `ServiceError`. Add a shared protocol-conformance test run against both stores. |
| M11 | Medium | Reliability | `src/py_common/adapters/bigquery.py:330-339` | `_ensure_staging_table` catches **any** exception from `get_table` (including 403 or network errors) and then tries `create_table`, masking the real error. | Catch `NotFound` only, as `bigquery_raw.py:251` already does. |
| M12 | Medium | Observability | `src/py_common/pipeline.py`, `src/py_common/runner.py`, adapters | Logs are free text with no run ID, warehouse, file identity or generation in structured fields, and BigQuery job IDs are never logged. The adapters log at DEBUG, so production logs can't show which BigQuery job failed. The README admits structured logging is missing. | Generate a `run_id` in `Pipeline.run`, use `logger.info(..., extra={...})` with a JSON formatter for Cloud Logging, and log BigQuery job IDs at INFO. Emit one summary record per file. |
| M13 | Medium | Testing | `tests/unit/test_bigquery_adapter.py`, `tests/unit/test_bigquery_raw_adapter.py` | The BigQuery tests assert SQL text and mock call arguments. No test checks that the SQL is valid or that MERGE, NULL or duplicate semantics hold. There is no emulator or dry-run, and the integration tests are skipped by default. | Add a CI job that runs `client.query(sql, job_config=QueryJobConfig(dry_run=True))` against a sandbox project, or use `goccy/bigquery-emulator` for load + MERGE round-trips. Add a scheduled live smoke test. |
| M14 | Medium | Testing / tooling | `.github/workflows/tests.yml:41-53`; `pyproject.toml:13-18, 44-47` | ruff (2), black (1) and `mypy --strict` (2) fail on main as of this review. Deps have no upper bounds or lockfile (a fresh install gets pandas **3.x**, a major version the code wasn't written against). CI runs only 3.11. Actions are on `checkout@v3` and `setup-python@v4`. | Fix the lint and type errors (add a `google.cloud.*` override with `ignore_missing_imports`). Add upper bounds or a constraints/lock file. Test on 3.11 and 3.12. Bump actions. Add `pip-audit` to CI. |
| L1 | Low | Correctness | `src/py_common/adapters/local.py:58, 81` | The version is `int(st_mtime)`, so an edit in the same second as discovery isn't detected. | Use `st_mtime_ns` plus size. |
| L2 | Low | Correctness | `src/py_common/contract.py:350, 465-474` | Whitespace-only strings count as blank for row skipping and headings, but satisfy `nullable=False` and `"string"` checks. | Treat `_is_blank` values as null in the required check. |
| L3 | Low | Correctness | `src/py_common/raw.py:55-56`; `docs/RAW_TABLE_LAYOUT.md` | A tz-aware `datetime` would serialise with an offset, contradicting "no time zone". openpyxl returns naive values today, so this is latent. `Decimal` falls through to a string. | Normalise to naive (or UTC) explicitly. Emit `Decimal` as a number. |
| L4 | Low | Performance | `src/py_common/adapters/bigquery.py:169-177` | An extra `COUNT(*)` query (a round trip, billed at minimum 10 MB) where `load_job.output_rows` is available, as the raw adapter uses. | Use `load_job.output_rows`. |
| L5 | Low | Correctness | `src/py_common/adapters/gcs.py:76, 310` | `bucket_name.replace("gs://", "")` replaces anywhere in the string. | Use `removeprefix("gs://")`. |
| L6 | Low | Design / API | `src/py_common/__init__.py:15-25`; `src/py_common/ports.py:140` | The public API is implicit: `Pipeline`, `Contract`, `Worksheet` and the runner aren't exported, `SecretStore` is an unimplemented protocol, and `AuditRecord` duplicates `Result`. `ConfigError` lives in `config.py`, not `errors.py`. | Export the supported surface in `__all__`, mark the rest private, and move `ConfigError` into `errors.py`. Add a CHANGELOG and semver policy. |
| L7 | Low | Security | `src/py_common/runner.py:264-269` | The view name is interpolated into SQL. The backtick check prevents breaking out of the identifier, but the value is still free-form. | Validate it against `^[\w-]+\.[\w-]+\.[\w-]+$`. |
| L8 | Low | GCP | `src/py_common/runner.py:48, 219` | An empty `eventType` is accepted as a new object. A notification without attributes (or one mis-routed from another topic) for a matching folder triggers a run. Harmless (the run is idempotent-ish) but not intended. | Require `OBJECT_FINALIZE` once notifications are confirmed to include attributes. |
| L10 | Low | Docs vs code | `scripts/gcp_auth.py:104-109` | *Downgraded from Medium (was M8).* The unused helper has the wrong Windows ADC path: `%APPDATA%\application_default_credentials.json` instead of `%APPDATA%\gcloud\...`. This doesn't affect data loading, because the adapters go through `google.auth.default()`, which uses the correct path. | Don't patch it; delete the helper (M9). |
| L9 | Low | Design | `src/py_common/ports.py:118-136` | The Warehouse docstring promises `ValueError`/`RuntimeError` and "truncate staging", which is implementation detail. The pipeline expects `ServiceError`/`IndeterminateCommitError`. The protocol and its consumers disagree (the root of H1). | Rewrite the protocol docstring around the error types the pipeline acts on. |
| N1 | Nit | Style | `src/py_common/adapters/bigquery.py:138, 179, 250`, `gcs.py`, `bigquery_raw.py` | f-string logging (eager formatting; inconsistent with the `%s` style in `pipeline.py`). | Use `%s` args. |
| N2 | Nit | Style | `src/py_common/config.py:21`, `errors.py` | Redundant `pass` after docstrings. | Remove. |
| N3 | Nit | Naming | `src/py_common/model.py:14` | `class Outcome(str, Enum)`. | Use `StrEnum` (3.11+). |

---

## Detailed write-ups: Critical and High

### C1: Dedup read is fail-open

`GCSObjectStore.get` (`adapters/gcs.py:97-106`):

```python
try:
    data: bytes = blob.download_as_bytes()
    ...
except Exception as e:
    logger.debug(f"Object {key} not found or read failed: {e}")
    return None
```

`Pipeline._is_duplicate` (`pipeline.py:221`) then decides "not yet loaded".
With the raw JSON route (insert-only) this duplicates every row of the file
under a new `ingested_at`. The documented Dataform query partitions by
`source_file` and filters on the max generation, and both copies share that
generation, so **duplicates reach consumers**. The failure is logged at DEBUG,
so it is invisible in production.

Fix:

```python
from google.api_core.exceptions import GoogleAPICallError, NotFound

def get(self, key: str) -> bytes | None:
    blob = self.bucket.blob(key)
    try:
        return blob.download_as_bytes()
    except NotFound:
        return None
    except GoogleAPICallError as e:
        raise ServiceError(f"Cannot read gs://{self.bucket_name}/{key}: {e}") from e
```

Add a unit test asserting that a 503 or 403 from `download_as_bytes` raises
instead of returning `None`.

### C2: Commit and "loaded" marker aren't atomic; the warehouse isn't idempotent

The sequence in `Pipeline.run`/`process`:

1. `warehouse.load()`: the BigQuery script commits rows + audit row.
2. `_audit()` → `store.put(audit/records/…)` (`pipeline.py:490`).
3. `store.put(audit/{warehouse}/{checksum})`, the marker (`pipeline.py:494`).

A failure anywhere after step 1 leaves rows committed with no marker:
container killed, Cloud Run timeout, a GCS error in step 2 (which aborts the
whole run, M4), or H1's post-commit timeout. The next run or Pub/Sub
redelivery loads again. BigQuery already has the authoritative record (the
`audit` row with `checksum`, committed atomically with the data), but
`load()` never checks it.

Fix: make the check part of the transaction, so the warehouse is idempotent
whatever the store says:

```sql
BEGIN
  BEGIN TRANSACTION;
  IF EXISTS (
    SELECT 1 FROM `proj.ds.audit`
    WHERE checksum = @checksum AND destination = @destination AND outcome = 'LOADED'
  ) THEN
    COMMIT TRANSACTION;
    SELECT 'ALREADY_LOADED' AS status;   -- caller reads this and returns
  ELSE
    INSERT INTO `proj.ds.hww_x` (...) SELECT ..., @timestamp FROM `proj.ds.hww_x_staging`;
    INSERT INTO `proj.ds.audit` (filename, checksum, destination, row_count, outcome, timestamp)
      VALUES (@filename, @checksum, @destination, @row_count, 'LOADED', @timestamp);
    COMMIT TRANSACTION;
  END IF;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = @@error.message;
END;
```

This needs a `destination` column added to the `audit` table
(`docs/GCP_SETUP.md:86-93`), because one audit table is shared by every
pipeline in the dataset. Also add the source `version`/generation (see H2).
Keep the store marker as a fast-path cache. Unverified: whether BigQuery
script transactions serialise two concurrent `IF EXISTS` checks. Concurrent
DML on the same table conflicts and one transaction aborts, which is
acceptable, but this should be tested live.

### H1: Adapter exceptions don't match what the pipeline acts on

`bigquery.py:239-248` and `bigquery_raw.py:197-207`:

```python
except Exception as e:
    raise RuntimeError(f"BigQuery transaction failed for {key}; ... were rolled back together: {e}") from e
```

- `txn_job.result()` can raise `concurrent.futures.TimeoutError`, or a
  `requests`/transport error, after the server committed. The message then
  states something false, and the pipeline retries into C2.
- Nothing raises `ServiceError` or `IndeterminateCommitError`, so
  `pipeline.py:411-418` is unreachable with real adapters. The README's
  "warehouse ServiceErrors are quarantined" describes behaviour that never
  happens.

Fix sketch:

```python
job = self.client.query(sql, job_config=cfg)
try:
    job.result(timeout=600)
except GoogleAPICallError as e:           # server reported the script failed
    raise ServiceError(f"BigQuery job {job.job_id} failed for {key}: {e}") from e
except Exception as e:                     # transport / timeout: state unknown
    try:
        job.reload()
    except Exception:
        raise IndeterminateCommitError(f"job {job.job_id} for {key}: {e}") from e
    if job.state == "DONE" and job.error_result is None:
        return
    if job.state == "DONE":
        raise ServiceError(f"BigQuery job {job.job_id} failed: {job.error_result}") from e
    raise IndeterminateCommitError(f"job {job.job_id} still {job.state}") from e
```

Combined with C2's in-transaction idempotency, retrying after
`IndeterminateCommitError` becomes safe. Also fix M3: map `ServiceError` to
FAILED (retryable), not QUARANTINED.

### H2: Dedup by content only breaks "latest generation"

`_loaded_marker_key` is `audit/{warehouse}/{checksum}` (`pipeline.py:245`).
Scenario: in January the uploader sends A (gen 100). In February they send B
(gen 200), which was wrong, so they re-upload the January file A (gen 300).
Gen 300 is skipped as a duplicate. Dataform's
`MAX(source_generation) OVER (PARTITION BY source_file)` returns gen 200, so
consumers see the data that was just retracted.

Options:
- Raw route: key the marker on `identity/version`. Re-inserting identical
  content under a new generation is *correct* for an insert-only, history-
  keeping table.
- Or keep content dedup, but write a small `generations` table (source_file,
  generation, checksum, seen_at) on every SKIPPED-duplicate, and have Dataform
  join `latest generation → checksum → rows`.

### H3: Missing key columns silently switch MERGE to append

`bigquery.py:192`:

```python
key_columns = [c for c in self.key_columns if c in columns]
```

Fix:

```python
missing = [c for c in self.key_columns if c not in columns]
if missing:
    raise ContractError(
        f"{key}: key column(s) {', '.join(missing)} not in data; "
        f"staging has {', '.join(columns)}"
    )
key_columns = self.key_columns
```

Also validate in `Contract.__post_init__` that every `key_columns` entry is a
listed, non-nullable column of `worksheets[0]`.

### H4: NULL and duplicate keys in MERGE

- `ON T.k = S.k` never matches when `k` is NULL, so the row is inserted again
  on every load. Use `ON T.k IS NOT DISTINCT FROM S.k`, or better, enforce
  `nullable=False` for key columns in the contract.
- Duplicate keys inside one file make BigQuery reject the MERGE. Detect it
  before the MERGE and quarantine:

```sql
SELECT k1, k2, COUNT(*) c FROM `staging` GROUP BY k1, k2 HAVING c > 1 LIMIT 5
```

Then raise `ValidationError` naming the keys. Alternatively, have
`Contract.validate` check composite-key uniqueness, since `Column.unique` is
only single-column today.

### H5: Parquet conversion fails on mixed types and drifts schema

`pipeline.py:347-352` creates an object-dtype `DataFrame` and lets pyarrow
infer types. Verified: an int and a str in one column raises `ArrowInvalid`.
That is exactly what "warn" mode allows through, so the file becomes FAILED
and the runner retries it every time. Separately, a column that is entirely
empty this month is inferred as `null`, and BigQuery then infers a different
staging type than last month.

Fix: derive an explicit schema from the contract:

```python
_ARROW = {"string": pa.string(), "integer": pa.int64(),
          "float": pa.float64(), "date": pa.timestamp("us")}

def _coerce(value, data_type):
    try: ...  # convert, or return None and append a warning
```

Then `pa.Table.from_pydict(cols, schema=schema)` → `pq.write_table`. For
`"all_except"` columns without a declared type, use `pa.string()`.

### H6: The non-expiring lock is a single point of stoppage

Cloud Run kills containers at `--timeout` (540 s for the service, per
`GCP_SETUP.md`), so a killed run is a normal event, not a rare one. After
one, every event for that folder is refused until a human deletes the lock,
and Pub/Sub drops undelivered events after 24 h (`GCP_SETUP.md`, "The service
is down"). Suggested lease:

```python
holder["expires_at"] = (now + timedelta(seconds=lease_seconds)).isoformat()
try:
    blob.upload_from_string(json.dumps(holder), if_generation_match=0)
except PreconditionFailed:
    current = self.bucket.get_blob(key)
    info = json.loads(current.download_as_bytes())
    if datetime.fromisoformat(info["expires_at"]) > now:
        raise RuntimeError(...)
    blob.upload_from_string(json.dumps(holder),
                            if_generation_match=current.generation)  # atomic takeover
```

Set `lease_seconds` greater than the job or service timeout, so a lease can
only expire once its holder is certainly dead. This keeps the "never steal
from a live run" property.

### H7: Shared staging tables across differently-locked warehouses

The lock key is `digest(warehouse.identity)`. `RawJsonWarehouse.identity` is
the sorted set of its tables (`bigquery_raw.py:118`), so two warehouses that
share one table but differ in others hold different locks. Both
WRITE_TRUNCATE the same `{table}_staging` (`bigquery_raw.py:173`), and
`BigQueryWarehouse` behaves the same way. The row-count check would usually
fail loudly, but if the row counts match, one file's rows commit under the
other's audit entry.

Fix: stage into a per-load table, e.g.
`f"{table}_stg_{checksum[:12]}_{uuid4().hex[:6]}"` with
`expiration_time = now + 1 day`, and drop it after the commit. Or lock per
physical table (one lock per table in sorted order, to avoid deadlock).

---

## Things done well

- **Clean ports and adapters design** (`ports.py`). The core pipeline imports
  no cloud SDK, and local adapters make it fully testable offline.
- **Version-pinned downloads.** GCS uses `if_generation_match`
  (`gcs.py:387-394`), and SharePoint checks the eTag before and after, plus
  the size (`sharepoint.py:342-354`). Partial or overwritten files are refused,
  not loaded.
- **The immutable store uses atomic create-if-absent** (`if_generation_match=0`)
  with an identical-bytes idempotency check, and the content-addressed keys
  `raw/{identity}/{checksum}/{name}` keep every version recoverable.
- **Data and audit commit together.** Each BigQuery script transaction commits
  the data and the audit row as a unit.
- **Deterministic conversion.** There's no timestamp in Parquet or NDJSON, so
  retries write byte-identical processed copies (`raw.py:89-91`,
  `pipeline.py:334-337`).
- **The contract model is careful.** It enforces exact heading names,
  deterministic `col_B`/`Other_2` naming that never collides
  (`contract.py:430-453`), the bool-is-int trap is handled (`contract.py:18-29`),
  sheets are looked up by name, never `wb.active`, and "warn" vs "quarantine"
  is set per sheet.
- **The raw-table contract is documented** for Dataform consumers
  (`docs/RAW_TABLE_LAYOUT.md`), with value-mapping tables and example SQL. Few
  teams do this.
- **The SharePoint adapter is solid.** It has injected auth, rejects secrets in
  config, honours `Retry-After` with bounded waits, detects pagination loops,
  and never logs the token.
- **Runner semantics are thought through.** QUARANTINED vs FAILED maps to
  ack vs retry, one failing pipeline doesn't stop the others, and the
  freshness check fails loudly rather than reporting "nothing missing".
- **The tests are extensive** (309, 95% line coverage), with real `.xlsx`
  fixtures for the edge cases (header on row 2, repeated "Other", personal
  data, missing sheet).
- **DECISIONS_en.md and GCP_SETUP.md explain the *why*.** The ack deadline,
  max-instances, versioning and quarantine-repeat behaviour are all reasoned
  in the docs.

---

## Prioritised action plan

### Quick wins (≤ 1 day each, do first)

1. **C1:** `GCSObjectStore.get` returns `None` only on `NotFound` and raises
   `ServiceError` otherwise. Add a test.
2. **H3:** raise on missing key columns, and validate `key_columns` in
   `Contract`.
3. **M3:** warehouse `ServiceError` becomes FAILED, not QUARANTINED.
4. **M10/M11:** `ObjectStoreConflict` in GCS `put`, and catch `NotFound` only
   in `_ensure_staging_table`.
5. ~~M8~~ (now L10): handled by moving `gcp_auth.py` to `scripts/` (M9).
   Delete it when it's no longer needed.
6. **M14:** make ruff, black and mypy green (a `google.cloud.*` mypy
   override), add upper bounds or a constraints file, add `pip-audit` to CI
   and bump the actions.
7. **M1:** cluster the raw tables on `(source_file, source_generation)`. For
   existing tables, run `bq update --clustering_fields`.
8. **L4, L5, N1:** trivial clean-ups.

### Next (a few days)

9. **C2 + H1:** make the warehouse idempotent (the audit-checked transaction)
   and map adapter errors to `ServiceError`/`IndeterminateCommitError`,
   re-checking job state after a transport error. Add a `destination` (and
   `generation`) column to the audit table. **This is the most important
   change.**
10. **H2:** decide on the dedup key for the raw route (identity + generation),
    and update `RAW_TABLE_LAYOUT.md`.
11. **H6:** lease-based lock with atomic takeover, plus a lock-age alert.
12. **H7:** per-load staging tables.
13. **M4:** a per-file audit failure shouldn't abort the run. Write the marker
    first.
14. **M12:** structured JSON logging with `run_id`, file identity, generation
    and BigQuery `job_id`.

### Larger refactors

15. **H5 + M2:** contract-driven Arrow schema for the typed route, and
    schema-diff/evolution against the final table.
16. **M6:** single-pass `read_only` parse that returns validation and
    extraction together. Hash-based comparison in `put`.
17. **M13:** BigQuery dry-run or emulator tests in CI, and a scheduled live
    smoke test (the raw route has never run live, per the README).
18. **L6, L9:** shrink and stabilise the public API. Optionally add
    `credentials`/`client` injection on the adapters, align the
    `Warehouse` protocol's documented errors, and add a CHANGELOG with semver.
19. **M7:** tighten store-bucket IAM with conditions and a retention
    policy.

---

*Unverified items are marked as such in the text. In particular: the CI run
status on GitHub, BigQuery concurrency behaviour for the proposed
`IF EXISTS` transaction, and runtime behaviour on Python 3.11 (tests ran on
3.13).*
