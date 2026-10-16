# py-common — Code Review

*Commit `caca9e4` on branch `review-fixes`, 15 October 2026. Whole
repository reviewed, not just the latest diff, from three angles at
once: data engineering, data science and software engineering. No
source files were changed. The probes and checks ran in a scratch
directory outside the repo.*

*This is a new file. The earlier reviews, `REVIEW.md` (with its
"Fixed in" annotations) and `REVIEW_chatgpt.md`, are left as they
were. Finding IDs here are new; where an issue was raised before, the
text says so.*

## Orientation

**What it is.** py-common is a small shared Python library (version
0.2.0, about 1,600 statements under `src/`). It ingests Excel
workbooks into Google Cloud for Public Health Wales teams. A
*solution repository* (Healthy Working Wales is the worked example)
supplies the contracts, landing folders, table mappings and Cloud Run
entry points. The users are the data engineers who wire one pipeline
per source. Downstream, the Dataform developers and the analysts and
Power BI reports that read the cleaned tables depend on what it
writes.

**How data moves.** For each file, `Pipeline.run()` does this:

1. Takes a GCS lease lock scoped to the warehouse.
2. Lists the source: `LocalSource` (a folder), `GCSSource` (a landing
   folder, versioned by object generation) or `SharePointSource`
   (Graph, versioned by eTag).
3. Downloads the exact listed version and hashes it with SHA-256.
4. Checks a "loaded" marker in the store bucket, keyed by source
   identity and version.
5. Parses the workbook once, read-only, with `Contract.read()`. This
   validates and selects columns, with quarantine or warn per sheet.
6. Keeps the original under `raw/`, converts, and keeps the processed
   copy under `processed/`.
7. Loads the warehouse, in one of two ways:
   - **Raw JSON route, the recommended one.** `RawJsonWarehouse`
     loads NDJSON (one JSON payload per row, plus `source_file`,
     `source_generation`, `sheet_name`, `row_number`, `checksum`)
     into a per-load staging table per sheet. One BigQuery script
     transaction then checks the `audit` table per destination table
     and `INSERT`s the rows plus one audit row per table.
   - **Typed route.** `BigQueryWarehouse` loads contract-typed
     Parquet and `MERGE`s on the key columns, optionally marking rows
     that disappear from a file as deleted.
8. Writes the marker, then an audit JSON record.

`runner.py` turns per-file outcomes into a Cloud Run job exit code or
a Pub/Sub redelivery. Dataform then picks the latest upload of each
file from the raw tables, casts the JSON payload and publishes the
modelled tables that consumers read. That Dataform layer lives in the
solution repo and is only *documented* here
(`docs/RAW_TABLE_LAYOUT.md`).

**What I read.** README, `docs/*` (GCP setup, raw table layout,
framework guide, usage, getting started, smoke test, auth), DECISIONS,
CHANGELOG, CONTRIBUTING, `pyproject.toml`, the pre-commit config, the
CI workflow, both Dockerfiles, the Cloud Build file, every module in
`src/py_common`, the tests and fakes, and the git log. The log shows
the original framework, the raw-JSON PR (#1), seven review-fix
phases, three follow-up fix commits, a smoke-test rewrite and the
load-policy commit (`caca9e4`). There is no `setup.cfg`,
`requirements*.txt` or lock file. `containers/` and
`scripts/gcp_auth.py` are parked for deletion and were only
inventoried.

## Verification: what I ran

All of this ran on Windows, CPython 3.13.5, in a fresh venv with
`pip install -e ".[dev,gcp]"`. CI uses 3.11 and 3.12; this review did
not run those interpreters.

| Check | Command | Result |
|---|---|---|
| Unit tests + coverage | `pytest -m "not integration" -q -p no:cacheprovider --cov=src/py_common` | **486 passed, 2 skipped, 6 deselected** (16 s); **96 %** statement coverage. Lowest: `adapters/gcs.py` 87 % (lease takeover race paths 283–297, download error paths), `parquet.py` 91 %, `_bq_jobs.py` 94 %. `pipeline.py:121-123`, the "Pipeline run failed" path that an alert depends on, is not covered. |
| Lint | `ruff check src tests` (ruff 0.17.0) | All checks passed. Whole repo: 4 errors, all in parked code (`containers/runtime/*` W292, `scripts/gcp_auth.py` E501). |
| Format | `black --check src tests` (26.10.1) | 56 files unchanged. |
| Types | `mypy src/py_common --strict` (2.4.0) | Success, 19 files. |
| Types, pre-commit hook environment | Fresh venv with only mypy and the hook's stubs, `mypy --strict src/py_common/*.py` | **2 errors**: `google_crc32c` and `google` not found (`gcs.py:36-37`). See L7. Ran with mypy 2.4.0, not the hook's pinned 1.5.0. |
| pyright | not installed | **Not run.** |
| Dependency audit, installed set | `pip-audit --skip-editable` (2.10.1) | Only the venv's own `pip 25.1.1` is flagged. No project dependency (pyarrow 26.0.0, requests 2.34.2, google-cloud-* 3.x, openpyxl 3.1.5) has a known advisory. |
| Dependency audit, declared floors | `pip-audit -r <floors> --no-deps --disable-pip` | **5 advisories**: pyarrow 15.0.0 `PYSEC-2024-161` (fixed in 17.0.0) and `PYSEC-2026-113` (fixed in 23.0.1); requests 2.32.4 `PYSEC-2026-2275` (fixed in 2.33.0). See M3. I did not read the advisory details. |
| Offline probes P1–P11 | A script against the real `Pipeline` and adapters, using the repo's fakes and synthetic XLSX, no cloud calls | All 11 confirmed the suspected behaviour. Results are cited in the findings and listed at the end. |
| XML hardening | `openpyxl.xml.DEFUSEDXML`, `pip show defusedxml`, a runtime-only venv | `defusedxml` arrives in the dev venv only through `py-serializable` (a pip-audit dependency). A runtime-only install of openpyxl and the GCP libraries **does not have it**. See M2. |

Nothing ran against live GCP or SharePoint. Everything about
BigQuery semantics (MERGE values, transaction isolation, dry-run
coverage, Parquet type mapping, column-name rules), IAM conditions
and alert delivery is **unverified** unless stated otherwise.

## Executive summary

- **Overall health: 7 / 10.** A careful, well-tested core that suits
  a supervised pilot of small monthly feeds. Idempotency, error
  classification and observability are now genuinely good. What's
  left is at the edges: what a "file" is, reprocessing, and the
  hand-off to Dataform. It isn't ready for unattended, multi-feed
  production until H1–H3 are addressed or explicitly accepted.
- **Loading twice is now hard.** "Already loaded?" is checked inside
  the BigQuery commit transaction, per destination table and per
  upload. Store reads fail closed, every load stages into its own
  table and the lock is a lease. The one residual path is a commit
  still running when its client gives up, then racing a retry (H3).
  It's documented, low-probability and unverified against live
  BigQuery.
- **New highest risk: a file's name is its identity (H1).** If users
  upload a cumulative file under a new, for example date-stamped,
  name each time, the documented "latest upload" Dataform query
  returns every upload as current. That gives duplicate current rows
  (verified offline). On the typed route, `mark_deleted_rows` never
  marks rows from the old name as deleted. The repo's own sample
  workbook is date-stamped.
- **The agreed "restores load again" policy can fail permanently on
  the typed route (H2).** Processed copies are immutable and keyed
  without a version or converter fingerprint. Restoring a file after
  a contract change (verified) or, very likely, a pyarrow upgrade
  (pyarrow writes its version into the file; not tried across
  versions) raises `ObjectStoreConflict`. The file is then FAILED and
  retried on every run.
- **The raw → Dataform contract is documented, not enforced.** The
  documented `CAST(source_generation AS INT64)` ordering only works
  for GCS sources (M6). Renamed columns in "warn" sheets are invisible
  in the payload (documented). There is no supported way to reprocess
  an upload after fixing a contract (M12).
- **Security is reasonable, with three gaps.** Uploaded workbooks are
  parsed without `defusedxml` and with no size limits in production
  (M2). The declared dependency floors allow versions with known
  advisories (M3). Cell text can leak into quarantine reasons, logs
  and audit records through parser exception messages (M5, verified).
  Values in SQL are parameterised; identifiers come from trusted
  config.
- **One reliability bug confirmed.** `_release_lock` says it "never
  raises", but a 403/503 on delete escapes, fails a run whose files
  all loaded, and leaves the lock behind (M1, verified).
- **Testing is strong offline but can't prove BigQuery semantics.**
  The fakes match SQL text with regexes. The dry-run test cannot
  validate statements inside `IF` blocks, so the MERGE and soft-delete
  SQL are never validated (M10, per BigQuery docs). There's no
  emulator, and the pre-commit mypy hook fails in its own
  environment (L7).
- **Docs lag the code.** Several guides still describe dedup "by
  checksum", "no minimum row count check" and "no delete policy".
  The GCP guide points at a Dockerfile that doesn't install the
  `[gcp]` extra (M8, M9, L15).

## Findings

Severity: **Critical** = silent corruption likely in normal
operation; **High** = wrong data, permanent failure or a stuck
pipeline in plausible conditions; **Medium** = real risk, cost or
maintainability; **Low** = minor or unlikely; **Nit** = style.
Nothing here meets Critical.

| ID | Severity | Area | file:line | Issue | Recommendation |
|---|---|---|---|---|---|
| H1 | High | Data engineering / Dataform contract | `src/py_common/raw.py:118`; `src/py_common/adapters/bigquery.py:509-517`; `docs/RAW_TABLE_LAYOUT.md:136-146, 179-185` | A source's identity is its file name. Raw rows carry only the basename (`source_file`), and the documented "latest upload" query picks the newest generation **per file name**. A cumulative feed uploaded under a new name each time (e.g. `export-2026-10.xlsx`) makes every upload "latest": duplicate current rows (verified, P3). On the typed route, `mark_deleted_rows` only marks rows whose `_source_file` equals this upload's identity, so after a rename the old file's removed rows stay current for ever (by SQL inspection; MERGE unverified live). The repo's sample is `docs/dummy-healthy-working-wales-employer-survey-tool-2026-09-23.xlsx`. | Add a configured logical feed id (`source_id`) plus the full `source_identity` to raw rows and audit. Document and enforce "one cumulative feed = one stable object name" until then. Scope soft deletes to the feed, not the file. Change the documented query to pick the latest *upload per feed*. See the write-up. |
| H2 | High | Correctness / idempotency | `src/py_common/pipeline.py:510-521`; `src/py_common/parquet.py:122-124`; `src/py_common/adapters/gcs.py:194-197` | Processed copies are immutable and keyed `processed/{wh}/{identity}/{checksum}/{name}.parquet`, with no version and no contract or converter fingerprint. Restoring earlier content (the agreed policy) after any change that alters the Parquet bytes raises `ObjectStoreConflict`, so the file is FAILED, retried every run and fires the failed-file alert each time. Verified with a contract change (P2). pyarrow writes `created_by: parquet-cpp-arrow version 26.0.0` into the footer, so an upgrade between the original load and the restore very likely does the same (not tried across versions). On the raw route the key includes the version, but retrying the same version after a contract change hits the same conflict. | Key processed copies by version and a fingerprint of the contract, output format and converter versions, so changed output never reuses a key. See the write-up. |
| H3 | High | Reliability / exactly-once | `src/py_common/adapters/_bq_jobs.py:61-62, 151-154`; `src/py_common/adapters/bigquery.py:200-206`; `src/py_common/adapters/bigquery_raw.py:217-224`; `src/py_common/pipeline.py:574-581`; `src/py_common/adapters/gcs.py:209-218` | A commit still RUNNING when waiting gives up (`IndeterminateCommitError`) is not reconciled. The staging tables are dropped while it may still read them, the lock is released, and the next attempt submits a new transaction. Whether two script transactions can both pass the audit `COUNT(*)` check and both append is **unverified**. The lease isn't renewed or fenced. Low probability; documented in CHANGELOG "Known limitations". Raised before as H03 in `REVIEW_chatgpt.md`. | Use a deterministic job ID per (destination, upload, attempt) and resume that job before starting another. Keep staging until the outcome is known. Test against a sandbox. See the write-up. |
| M1 | Medium | Reliability | `src/py_common/adapters/gcs.py:350-374, 258-261` | `_release_lock` is documented "Never raises" but catches only `NotFound` and `PreconditionFailed`. A 403/503 on delete escapes the `finally`. `Pipeline.run()` then raises after every file has loaded, so results are lost to the caller and the job and Pub/Sub treat the run as failed. The lock stays. If the cause is the `locks/` IAM condition, the takeover (an overwrite, which needs delete) fails too and the pipeline stays blocked (verified, P1). | Catch `Exception`, log it at ERROR with the lock key, and never raise. Add a test with a failing delete. |
| M2 | Medium | Security / performance | `pyproject.toml:15-19`; `src/py_common/contract.py:406-414, 622, 750-768` | Untrusted uploads are parsed by openpyxl, which only guards against XML entity attacks when `defusedxml` is installed. It isn't a dependency, and only the dev environment gets it, through pip-audit, so the tests don't see the production behaviour (verified). There is no limit on file size, decompressed size, rows or cells. `read_sheet` materialises the whole grid, then `_select`, NDJSON and Parquet each copy it. | Add `defusedxml` to the runtime dependencies. Reject files over a configurable size or cell count before parsing (quarantine with a clear reason). Check the zip's uncompressed sizes before opening. |
| M3 | Medium | Security / supply chain | `pyproject.toml:18, 41, 51` | The declared floors `pyarrow>=15` and `requests>=2.32.4` include versions with advisories (pip-audit: pyarrow 15.0.0, PYSEC-2024-161 and PYSEC-2026-113; requests 2.32.4, PYSEC-2026-2275). A constrained resolver can install them, and CI only audits the newest resolution. | Raise the floors (`pyarrow>=23.0.1`, `requests>=2.33.0`). Ship a constraints or lock file for deployments, and audit that file in CI. |
| M4 | Medium | Correctness (typed route) | `src/py_common/contract.py:641-663`; `src/py_common/parquet.py:109-114`; `src/py_common/adapters/_bq_jobs.py:104-109`; `src/py_common/pipeline.py:582-589` | Sheet headings become BigQuery column names without validation. `read_sheet` keeps `Name` and `name` as different columns (verified, P8), but BigQuery names are case-insensitive. Very long survey-question headings or disallowed characters are also likely to be rejected. A rejected load surfaces as a `ServiceError`, so it is FAILED and retried for ever rather than quarantined. BigQuery's exact naming rules for Parquet loads are **unverified**. | Validate or normalise column names for the typed route in `Contract.__post_init__` or `to_parquet` (case-insensitive uniqueness, length, character set). Map a load job's `invalid` reason to `ContractError` so the file is quarantined. |
| M5 | Medium | Security / privacy | `src/py_common/contract.py:313-322`; `src/py_common/adapters/_bq_jobs.py:107-109, 146-150` | The "Cannot read worksheet" quarantine reason embeds the parser's exception text, which includes the cell content. Verified: `could not convert string to float: 'jo.bloggs@example.org'` lands in `Result.errors`, the WARNING log and the audit JSON (P5). BigQuery load and job error texts may echo data in the same way (unverified). This undoes the earlier "no values in messages" fix on one path. | Record the exception class and the sheet/row only, and keep the detail in the access-controlled `quarantine/` evidence. Treat BigQuery `error_result` text the same way before logging it. |
| M6 | Medium | Dataform contract | `src/py_common/raw.py:119`; `src/py_common/adapters/local.py:73-79`; `docs/RAW_TABLE_LAYOUT.md:16, 140-146` | The documented ordering `CAST(source_generation AS INT64)` only works for GCS generations. A `LocalSource` version is `"1792099970012609800-1"` (verified, P3b). SharePoint versions are Graph eTags of the form `"{GUID},3"`. For both, `CAST` errors and `SAFE_CAST` returns NULL, so "latest" breaks for non-GCS sources. | Write an orderable column (e.g. `source_modified_at` TIMESTAMP, or a per-feed sequence) and document ordering by it. At minimum, document that the query is GCS-only. |
| M7 | Medium | Software design | `src/py_common/adapters/bigquery.py:118`; `src/py_common/contract.py:156`; `src/py_common/pipeline.py:79-89` | Merge keys are set twice: on `Contract.key_columns` and again on `BigQueryWarehouse(key_columns=...)`. `Pipeline` doesn't compare them. Passing none to the warehouse silently turns every upload into an append, and different keys merge on the wrong columns. The guide calls this "the one thing to get right as a caller". | Check in `Pipeline.__post_init__` (or build the warehouse from the contract) and refuse a mismatch. |
| M8 | Medium | Docs vs reality | `README.md:95`; `docs/INGESTION_FRAMEWORK_GUIDE.md:151-163, 247-250, 320-322, 481, 487`; `docs/GETTING_STARTED.md:304-313`; `docs/SMOKE_TEST.md:101-112, 211-213, 225-226`; `docs/RAW_JSON_VS_CONTRACT_VALIDATION.md:141, 288`; `DECISIONS_en.md:92, 189, 478` | Many passages still say dedup is "by checksum", markers live at `audit/{wh}/{checksum}`, and "a file re-saved with identical contents is correctly skipped". In code, an identical re-upload now loads, keyed by identity and version. The framework guide also says there is "no minimum row count check" and "no delete handling policy", both now false (empty sheets are rejected, and `mark_deleted_rows` exists), and contradicts itself a few lines later. DECISIONS still records "the lock never expires" without noting the lease replaced it. | Update the passages. Mark superseded DECISIONS entries as superseded, linking to the 0.2.0 entries. |
| M9 | Medium | Docs vs reality / deployment | `docs/GCP_SETUP.md:338-342`; `Dockerfile:4, 9, 23, 39-41` | The GCP guide says the container "installs `py-common[gcp]`, plus `functions-framework`; see the repository's `Dockerfile`". The Dockerfile installs bare `py-common`, with no `[gcp]` extra and no functions-framework, so the GCP adapters can't import. It also runs as root on an unpinned `python:3.11-slim`, and its comment still lists pandas as a base dependency. | Either make the Dockerfile the documented runtime (`[gcp]`, functions-framework, non-root `USER`, pinned digest) or change the guide to say the solution repo's image must add them. |
| M10 | Medium | Testing | `tests/integration/test_bq_dry_run.py:190-215`; `CONTRIBUTING.md:277-283`; `src/py_common/adapters/_bq_jobs.py:195-260` | The dry-run test sends the whole commit script. Per BigQuery's multi-statement documentation (not re-checked live here), a dry run doesn't validate statements inside or after an `IF`. Every MERGE and INSERT, including the new `WHEN NOT MATCHED BY SOURCE` soft delete, sits inside `IF NOT already_loaded_<i> THEN`, so none is validated. CONTRIBUTING says they are. The fake BigQuery client interprets SQL with regexes (`tests/unit/_fake_bigquery.py:164-210`), so it checks text, not semantics. | Dry-run each DML statement on its own, with parameters. Add a sandbox test that runs a real transaction and checks the stored values: upsert, soft delete and restore, rollback on audit failure, a second load as a no-op. |
| M11 | Medium | Performance / reliability | `src/py_common/pipeline.py:186-197, 300-328`; `docs/GCP_SETUP.md:468-469` | Each file is downloaded and hashed *before* the "already loaded" check, even though the marker key no longer uses the checksum. Every run, and every upload event, re-downloads the whole folder (verified, P4: 2 downloads for 1 load). An already-loaded file whose download fails (GCS 503, Graph throttling) is reported FAILED and alerts. | Check the marker before downloading. Leave the in-transaction audit check as the authority. |
| M12 | Medium | Data engineering / backfill | `src/py_common/adapters/gcs.py:442-480`; `src/py_common/pipeline.py:330-353`; `docs/INGESTION_FRAMEWORK_GUIDE.md:179-185` | There is no supported way to reprocess. Only current object versions are listed, markers no longer force a reload, the BigQuery audit rows are authoritative, and processed keys are immutable (H2). Re-loading an upload after a contract fix means hand-deleting audit rows in BigQuery, which is undocumented and easy to get wrong. | Add an explicit replay/rematerialise entry point keyed by a materialisation version (contract fingerprint), plus a documented runbook. |
| L1 | Low | Correctness | `src/py_common/contract.py:788, 802, 300-306` | The formula scan hard-codes `xl/workbook.xml` and its rels path. A workbook whose main part has another name opens fine in openpyxl but is quarantined, even on a "warn" sheet (verified, P6). How often real tools produce this is **unverified**. | Find the workbook part from `_rels/.rels` or `[Content_Types].xml`, as openpyxl does. |
| L2 | Low | Configuration | `src/py_common/adapters/bigquery_raw.py:210-215, 368-384`; `src/py_common/pipeline.py:79-89` | A contract sheet with no table mapped raises `ValueError` on every load, so the file is FAILED and retried for ever (verified, P9). A mapped table with no contract sheet gets a `row_count 0` audit row on every load, and the table is never created. | Check that the contract's sheets and the warehouse mapping match when the `Pipeline` is built, and raise `ConfigError`. |
| L3 | Low | Correctness | `src/py_common/adapters/gcs.py:303, 341-348` | A lock object whose `expires_at` has no time zone makes `lock()` raise `TypeError` (comparing naive and aware datetimes) instead of refusing or taking over (verified, P10). | Normalise parsed times to UTC, or treat a naive timestamp as unreadable (never taken over). |
| L4 | Low | Observability | `src/py_common/logging.py:140-166` | `configure_json_logging()` removes only handlers it installed. After a `logging.basicConfig()`, every line is written twice, once as plain text and once as JSON (verified, P11). | Offer `replace=True` to clear the root handlers, or document calling it first. |
| L5 | Low | GCP / runners | `src/py_common/runner.py:231-232` | `handle_upload` routes by folder and ignores the bucket. An event from another bucket with a matching folder starts a run (verified, P7). | Key pipelines by `(bucket, folder)`, or check the event's bucket against the pipeline's `GCSSource`. |
| L6 | Low | Testing | `tests/integration/test_gcs_bigquery_integration.py:19-25`; `tests/integration/test_end_to_end_gcs_bq.py:15-18`; `tests/integration/test_live_services.py:72-96` | Two live-GCP test classes have no `integration` marker, so they run in the "not integration" selection whenever `GOOGLE_APPLICATION_CREDENTIALS` is set. `test_live_services.py` still says "adapter not yet implemented". | Mark them `integration` and replace or delete the placeholders. |
| L7 | Low | Tooling | `.pre-commit-config.yaml:8-31`; `pyproject.toml:85-87` | The mypy hook's isolated environment can't resolve `google_crc32c` or `google` (verified with a simulated environment), so the hook fails although CI passes. The hook pins (black 23.9.1, ruff v0.1.0, mypy v1.5.0) are far behind CI's (black 26, ruff 0.17, mypy 2.4), so formatting can drift. | Add `google_crc32c` and `google` to the mypy overrides (or the hook's `additional_dependencies`), and bump the hook revisions to match `dev`. |
| L8 | Low | GCP / IAM | `docs/GCP_SETUP.md:22, 191-201` | The overview calls uploaders "write only", but the binding gives them `roles/storage.objectUser`, which can also read and delete everyone's landing files. Whether that matters depends on the DPIA (**unverified**). | State the intended access. Use per-team folders, a custom role, or an upload service if uploaders mustn't see each other's files. |
| L9 | Low | Reliability | `src/py_common/adapters/_bq_jobs.py:104-105` | Staging load jobs are awaited with no timeout. A stuck load holds the run until Cloud Run kills it, after which the lock waits out its lease. | Pass a timeout, and treat a timeout like the commit path does. |
| L10 | Low | Correctness | `src/py_common/contract.py:313-322` | `except Exception` around `read_sheet` turns anything, including `MemoryError` and bugs in our own code, into `ContractError`, so the file is quarantined as "the file's fault". | Catch the parser's exception types (`ValueError`, `KeyError`, `zipfile.BadZipFile`, `xml` parse errors) and let the rest fail. |
| L11 | Low | Correctness | `src/py_common/adapters/local.py:47, 85-112, 193-202, 206-219` | `LocalSource` checks the version and then reads, so a file replaced in between is accepted under the old version (raised before as M06). `LocalObjectStore.put` checks then writes, and its `lock()` does nothing, yet the docstring offers `LocalSource` for "a shared network folder". | Re-stat after reading and compare. Say plainly that the local adapters are single-process and test-only. |
| L12 | Low | Cost | `src/py_common/adapters/bigquery.py:351-365, 509-517`; `docs/GCP_SETUP.md:86-95` | Typed final tables are created unpartitioned and unclustered, and the soft-delete MERGE (`NOT MATCHED BY SOURCE`) reads the whole target on every load. The audit table is unpartitioned and scanned by every commit's check. Fine at pilot volume, but cost grows with history. | Partition or cluster typed tables (and the audit table) on the merge key, `_source_file`/`destination` and time. Monitor bytes billed. |
| L13 | Low | Data science / types | `src/py_common/parquet.py:38, 184-196` | Contract `date` columns are written as naive `timestamp[us]`. Whether BigQuery stages these as DATETIME or TIMESTAMP is **unverified**, and date-only Excel values become midnight datetimes, never `DATE`. Naive Excel times have no time-zone policy, so DST interpretation is up to the solution. | Confirm the staged type in the smoke test. Consider separate `date` and `datetime` contract types, and document that times are local and naive. |
| L14 | Low | Docs / lineage | `src/py_common/adapters/bigquery.py:311`; `src/py_common/adapters/bigquery_raw.py:281`; `docs/RAW_TABLE_LAYOUT.md:21` | `ingested_at` and the audit `timestamp` are taken from the client clock before the job is submitted. The docs call them "when the load committed". | Reword the docs, or use `CURRENT_TIMESTAMP()` inside the transaction. |
| L15 | Low | Docs vs reality | `docs/GCP_SETUP.md:127-130, 405, 455, 489, 497-498, 565-567`; `src/py_common/contract.py:202-203`; `README.md:116-117` | `GCP_SETUP.md:127-130` has a broken code fence: the `bq update` line lost its backslash continuation and the closing fence sits on the same line as prose. The guide says "set up four" alerts but lists five. It says the audit table "records only a file name" (it now has `destination`). It sends readers to the now-archived `upcoming_work.md` for required Pub/Sub permissions. The `Contract.read` docstring claims rows are "not built into a full in-memory grid". The README's lock-permission note predates the conditional IAM. | Fix the text. Move the Pub/Sub permission steps into `GCP_SETUP.md`. |
| L16 | Low | Observability | `src/py_common/pipeline.py:660-693`; `src/py_common/model.py:78-98` | The audit JSON records have no `run_id` and no BigQuery job IDs, so joining a file's audit record to its logs means matching timestamps. | Add `run_id` and `bq_job_ids` to `AuditRecord`. |
| N1 | Nit | Style / dead code | `src/py_common/pipeline.py:300, 438-471`; `src/py_common/contract.py:546`; `src/py_common/model.py:78-85`; `src/py_common/adapters/bigquery.py:189-190`; `scripts/demo.py:61`; `src/py_common/runner.py:289`; `src/py_common/adapters/sharepoint.py:660-668` | Small items: `_is_duplicate`'s `checksum` parameter is unused. `_to_parquet` is only called by tests. `if value is not None` at contract.py:546 is always true. `AuditRecord` says "Immutable" but isn't frozen. `BigQueryWarehouse.load` still documents `ValueError` for `_loaded_at`. The audit `filename` column holds the load key, not the file name. The smoke test calls the store bucket `GCS_RAW_BUCKET`. The freshness log shows the UTC date. `Retry-After` is parsed as seconds only. | Tidy when next touching these files. |

## Detailed write-ups: High findings

### H1: A file's name is treated as the source's identity

**What happens.** A raw row's lineage is `source_file = item.name`
(`raw.py:118`), the basename, plus `source_generation`. The consumer
contract (`RAW_TABLE_LAYOUT.md:136-146`, and the Dataform example at
`:179-185`) chooses the current snapshot with
`MAX(source_generation) OVER (PARTITION BY source_file)`. That is
correct only if a feed keeps **one stable file name** that is
overwritten each time (GCS versioning keeps the history).

Users who add a cumulative file "through time" often save it under a
new, date-stamped name. The repo's own sample is
`dummy-healthy-working-wales-employer-survey-tool-2026-09-23.xlsx`.
Each name then has its own "latest" generation. In the probe (P3),
a September export with ids 1–2 and an October export with ids 1–3
returned **5 current rows, with ids 1 and 2 duplicated**. A
`uniqueKey` assertion would catch it, but without one it reaches
Power BI.

The typed route has the mirror-image problem. `mark_deleted_rows`
scopes deletions with
`WHEN NOT MATCHED BY SOURCE AND T._source_file = @source_file`
(`bigquery.py:509-517`), where `_source_file` is the item identity.
After a rename:
- keys still in the new file move to the new identity (matched rows
  set `_source_file = @source_file`);
- keys only the old file had stay owned by the old identity, which is
  never loaded again, so they **stay current for ever**.

The MERGE behaviour is inferred from the SQL; it was not run live.

**Fix.** Separate the *feed* from the *file*, and keep an interim rule
until that lands.

```python
# pipeline.py: a stable, configured feed id, e.g. "hww/was".
@dataclass
class Pipeline:
    ...
    source_id: str = ""  # required for raw_json and mark_deleted_rows

# raw.py: carry it and the full identity on every row
record = {
    "source_id": source_id,
    "source_identity": item.identity,   # full object name / item id
    "source_file": item.name,
    "source_generation": item.version,
    ...
}
```

```sql
-- RAW_TABLE_LAYOUT.md: latest *upload* per feed, not per file name
WITH uploads AS (
  SELECT source_id, source_identity, source_generation,
         MAX(ingested_at) AS ingested_at
  FROM `project.raw_hww.hww_was_database`
  GROUP BY 1, 2, 3
),
latest AS (
  SELECT * FROM uploads
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY source_id ORDER BY ingested_at DESC) = 1
)
SELECT r.*
FROM `project.raw_hww.hww_was_database` AS r
JOIN latest USING (source_id, source_identity, source_generation)
```

On the typed route, scope `NOT MATCHED BY SOURCE` by a `_source_id`
column instead of `_source_file`. Adding columns to existing raw
tables needs a migration: `_ensure_raw_table` doesn't evolve the
schema, so add an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` step like
the typed adapter's.

**Interim rule.** Document "one cumulative feed = one object name,
overwritten each time". Have a cheap check warn when a landing folder
holds more than one `.xlsx` for a feed declared cumulative.

### H2: Restores and reprocessing collide with immutable processed copies

**What happens.** `_store_processed` writes
`processed/{warehouse}/{identity}/{checksum}/{name}.parquet` on the
typed route (`pipeline.py:510-521`), and the store refuses different
bytes under an existing key (`gcs.py:194-197`). The Parquet bytes
depend on more than the source bytes:
- the contract (columns, types, exclusions);
- the converter code;
- the **pyarrow version**, which `pq.write_table` writes into the
  footer (`created_by: parquet-cpp-arrow version 26.0.0`, verified).

The agreed policy (CHANGELOG 0.2.0) is that A → B → A loads A again.
The restore of A computes the same checksum as the first A, so it
reuses the key. The probe (P2) loaded A, then B, then restored A
after adding a column to the contract. Result: `failed` with
`ObjectStoreConflict`, and the same on every later run, because the
upload is never marked loaded.

The same happens after a pyarrow upgrade between the original load
and the restore. That case is very likely but not tested, since it
needs two pyarrow versions. On the raw route the key includes the
version, so restores are safe, but retrying the same version after a
contract change hits the conflict.

**Fix.** Make the key reflect everything the output depends on:

```python
# pipeline.py
from importlib.metadata import version as _pkg_version

def _materialization(self) -> str:
    """Short fingerprint of what shapes the processed bytes."""
    parts = [
        json.dumps(asdict(self.contract), sort_keys=True, default=str),
        self.output_format,
        _pkg_version("py-common"),
        _pkg_version("pyarrow") if self.output_format == "parquet" else "",
    ]
    return digest("\x1f".join(parts).encode())[:12]

def _store_processed(self, item, checksum, processed):
    suffix = ".ndjson" if self.output_format == "raw_json" else ".parquet"
    key = (
        f"processed/{self.warehouse.identity}/{item.identity}/"
        f"{checksum}/{item.version}/{self._materialization()}/"
        f"{item.name}{suffix}"
    )
    self.store.put(key, processed)
```

Record the same fingerprint in the audit JSON (it's also the hook a
replay API needs; see M12). Add a test that runs A, B, then A with a
different contract and asserts the restore loads.

### H3: A commit still running when the client gives up isn't reconciled

**What happens.** `run_query` submits the script without a job ID
(`_bq_jobs.py:61-62`). If waiting fails and a reload shows the job
still RUNNING, it raises `IndeterminateCommitError`
(`_bq_jobs.py:151-154`). Then:
- the adapter's `finally` drops the staging tables
  (`bigquery.py:205-206`, `bigquery_raw.py:222-224`) while the job may
  still read them;
- the pipeline marks the file FAILED (`pipeline.py:574-581`) and
  releases the lock;
- the next run or Pub/Sub retry builds new staging tables and submits
  a new commit.

If the first job is still running, both transactions evaluate the
audit `COUNT(*)` check. Whether BigQuery's snapshot isolation lets
two INSERT-only transactions both see "not loaded" and both append is
**unverified**. If it does, raw rows are duplicated. The lease
(`gcs.py:209-218`) isn't renewed or fenced, so a service container
still working after its request timed out can overlap a takeover.
CHANGELOG lists this as a known limitation. It needs a slow commit
(over 600 s) or a lost connection, so the probability is low.

**Fix sketch.** Make the commit job resumable:

```python
# _bq_jobs.py
from google.api_core.exceptions import Conflict

def commit_job_id(destination_key: str, attempt: int) -> str:
    return f"pycommon_{digest(destination_key.encode())[:40]}_{attempt}"

def run_commit(client, sql, cfg, destination_key, what):
    attempt = 1
    while True:
        job_id = commit_job_id(destination_key, attempt)
        try:
            job = client.query(sql, job_config=cfg, job_id=job_id,
                               job_retry=None)
        except Conflict:
            job = client.get_job(job_id)        # an earlier attempt
            if job.state != "DONE":
                raise IndeterminateCommitError(
                    f"job {job_id} for {what} still {job.state}")
            if job.error_result is not None:
                attempt += 1                     # it failed: try anew
                continue
        return job, list(job.result(timeout=JOB_TIMEOUT_SECONDS))
```

Here `destination_key` is warehouse identity, load key and source
version. Name the staging tables deterministically per upload, so a
resumed job still finds them. Don't drop staging when the outcome is
indeterminate; let the one-day expiry clean up. Keep the
in-transaction audit check. Verify against a sandbox:
- kill the process mid-commit;
- delay the wait past the timeout;
- run two pipelines on one table at once.

## Review by area

1. **Correctness.**
   - **Edge cases handled well, with behavioural tests:** empty and
     whitespace cells, NaN and infinity, `Decimal`, tz-aware datetimes
     in raw JSON, bool-as-int, repeated and blank headings, case of
     formula cells, INT64 range, empty sheets (now rejected), and the
     merge-key checks.
   - **Gaps:** H2 (restore conflict), M4 (column names), M5 (value
     leakage), L1 (formula-scan path), L3 (naive lease time), L10
     (broad except).
   - **Not found:** no mutable default arguments (dataclasses use
     `default_factory`, functions use `None`, and the logging
     `ContextVar` default is never mutated, `logging.py:44-80`), no
     off-by-one errors (row numbers are `header_row + offset`,
     `contract.py:630`).
   - **Exception swallowing is deliberate and logged:** staging drops
     (`_bq_jobs.py:393-410`) and lock release. Release also swallows
     too little (M1).
   - **Time zones:** the freshness view's London date boundary is
     right (`GCP_SETUP.md:580-587`). Its log line shows the UTC date
     (N1).
2. **Data engineering.**
   - **Idempotency is now strong:** an in-transaction audit check per
     table and per upload, plus the marker cache. Residual risks are
     H3 and H2.
   - **Load semantics are explicit:** truncate only per-load staging,
     append raw rows, MERGE typed rows (with optional soft delete).
   - **Partitioning:** raw tables are partitioned and clustered, but
     the documented query scans everything (L12, documented).
   - **Schema drift:**
     - **raw route:** the payload absorbs new columns;
     - **typed route:** new columns are added, and a type change or a
       removed column quarantines the file.
   - **Late and duplicate data:** only current versions are processed,
     so ordering follows processing order (documented). Identical
     re-uploads append a full copy, by design.
   - **The raw → Dataform contract is documented, not enforced:**
     there are no Dataform definitions or assertions in the repo,
     ordering is GCS-only (M6) and identity is by file name (H1).
   - **Backfill and replay:** missing (M12).
3. **GCP and cloud.**
   - **IAM:** least privilege on the store bucket, with a
     `locks/`-only condition (L8 covers uploaders). Credentials come
     from ADC, there are no secrets in code, and tokens are never
     logged. `requests` drops the Authorization header on Graph's
     cross-host download redirect (behaviour of the `requests`
     library).
   - **Retries:** the Google SDKs' default retries apply. SharePoint
     has bounded retries with timeouts. BigQuery commits re-read the
     job before reporting. Load jobs have no timeout (L9).
   - **Cost:** L12 and M11. Every upload event re-lists and
     re-downloads its folder.
   - **Region:** consistent in the guide (`europe-west2`). Clients
     don't pin a location; BigQuery infers it from the datasets
     (unverified for a multi-region setup).
   - **Runners:** duplicate events are safe (lock plus audit). Failed
     files are retried. Quarantines are acknowledged and alert.
     Routing ignores the bucket (L5). A run that dies holds the lock
     until the lease expires (documented).
4. **Software design.**
   - Ports and adapters keep cloud SDKs out of the core (verified:
     the core modules import none).
   - The public API is explicit (`__all__`, CHANGELOG).
   - Duplicated configuration (M7). A Dockerfile that doesn't match
     the guide (M9). Dead code and naming nits (N1).
   - The warehouse identity still does several jobs (lock scope,
     marker prefix, processed prefix), though dedup is now per table.
5. **Reliability and observability.**
   - **Strengths:** JSON logs with `run_id`, `warehouse`,
     `item_identity`, `version`, `checksum` and `bq_job_id`, one
     summary line per file at the right severity, and alert filters
     on structured fields. Error messages say what to do (empty sheet
     → `allow_empty`, formulas → save in Excel, lock → which object to
     delete).
   - **Gaps:** M1, L4, L16. A failed run *can* usually be diagnosed
     from the logs alone, unless a lock-release error masks it (M1).
6. **Testing.**
   - **Strengths:** 486 tests at 96 %, mostly asserting behaviour
     (outcomes, rows, raised types, which SQL clauses are present),
     with stateful fakes for GCS and BigQuery.
   - **Gaps:**
     - no BigQuery semantics: MERGE values, soft delete, transaction
       races (M10);
     - **no local emulator** (BigQuery has no official one, and
       whether the community emulator supports scripts and
       transactions is unverified); the only real-BigQuery paths are
       the dry run (limited) and the manual smoke test;
     - no tests for the P1–P11 scenarios, the `run()` failure path
       (`pipeline.py:121-123`) or a Windows/Linux path matrix (CI is
       Linux-only, developers use Windows);
     - fixtures don't cover non-standard workbook parts, case-only
       duplicate headings or huge files;
     - unmarked live tests (L6) and a failing pre-commit hook (L7).
7. **Security.**
   - **SQL:** values are always query parameters. Identifiers come
     from trusted config, plus typed-route column names from headings
     after BigQuery has accepted them (backtick-quoted). Injection
     wasn't shown; config identifiers aren't validated (Low, folded
     into M4's recommendation).
   - **Deserialization:** only `json` and `yaml.safe_load`, nothing
     unsafe.
   - **Gaps:** XML and zip hardening (M2), dependency floors (M3),
     data in messages (M5).
8. **Performance.**
   - **Memory:** the workbook is parsed once, read-only, but the grid
     and its copies are held in memory, with no limits (M2).
   - **Redundant work:** a download before the dedup check (M11), and
     NDJSON parsed again just to split by sheet
     (`bigquery_raw.py:308-317`).
   - **Round trips:** about 7–8 BigQuery API calls per typed file and
     3–4 per sheet on the raw route. Fine at the current scale.
9. **Docs vs reality.** M8, M9, L14, L15, and the `Contract.read`
   streaming claim.

## Things done well

- **The idempotency design is right, and now holds end to end.** The
  BigQuery audit table is the authority, checked inside the same
  transaction that writes (`_bq_jobs.py:195-260`). The store marker is
  only a cache, and the stateful-fake tests crash the run between
  commit and marker.
- **Errors are classified by who can fix them.** File problems
  quarantine, service problems are FAILED and retried, and an unknown
  commit is re-read before anything is reported (`_bq_jobs.py:112-154`,
  `ports.py:98-118`).
- **Reads fail closed and writes are conditional.** A permission
  error never looks like "not loaded". Creates use
  `if_generation_match`, and content is compared by hash without a
  download (`gcs.py`).
- **The contract model fits real analyst spreadsheets.** It has a
  configurable header row, stable names for blank and repeated
  headings, an allow-list column selection with exclusions for
  personal data, per-sheet warn or quarantine, composite-key checks,
  formula-cache detection and an empty-sheet guard.
- **Typed conversion is explicit, not inferred** (`parquet.py`). Mixed
  values no longer fail conversion, and empty columns keep their type.
- **Observability fits Cloud Logging.** Structured fields, job IDs at
  INFO, and one summary line per file with severity matched to the
  outcome.
- **The operational docs are unusually good.** The GCP guide explains
  *why* behind the ack deadline, the max-instances setting, the lease
  and the alert filters. The raw-table layout doc gives Dataform
  developers a real contract. The CHANGELOG lists known limitations
  honestly.
- **Tooling hygiene.** ruff, black, strict mypy and pip-audit run in
  CI before the tests, on two Python versions.

## Prioritised action plan

### Quick wins (about a day each or less, with a test)

1. **M1:** make `_release_lock` truly non-raising. Test with a
   failing delete.
2. **M11:** check the marker before downloading.
3. **M2 + M3:** add `defusedxml`, a size/cell limit that quarantines,
   and raise the `pyarrow`/`requests` floors. Audit a constraints file
   in CI.
4. **M5:** remove parser and BigQuery error text from quarantine
   reasons and logs. Test with a synthetic email address.
5. **M7, L2:** check the keys and the sheet-to-table mapping when the
   `Pipeline` is built.
6. **L3, L4, L5, L9, L10:** the small correctness fixes. Each has an
   offline reproduction in the verification table below.
7. **L6, L7:** mark the live tests; fix the hook environment and pins.
8. **M8, M9, L14, L15:** a docs pass. Remove "by checksum" from the
   docs, make the Dockerfile and guide agree, fix the broken code
   fence, and note the GCS-only ordering (M6) until it's fixed.
9. **Interim for H1:** document and check "one cumulative feed = one
   stable object name".

### Larger refactors (agree the design first)

1. **H1 + M6:** add a feed id (`source_id`), the full identity and an
   orderable version to raw rows and audit. Define "latest per feed",
   scope typed soft deletes to the feed, and include a migration.
2. **H2 + M12:** add a materialisation fingerprint to processed keys
   and audit records, and an explicit replay/rematerialise command
   with a runbook.
3. **H3:** resumable commit jobs (deterministic IDs and attempts),
   staging kept while the outcome is unknown, and fencing. Prove it
   in a sandbox with killed and slow commits.
4. **M10:** a real BigQuery integration lane: dry-run each DML
   statement, run transactions in an expiring sandbox dataset and
   assert the stored values (upsert, soft delete, restore, rollback,
   a second load as a no-op).
5. **M4 + L13:** a column-name and type policy for the typed route
   (naming rules, `DATE` vs `DATETIME`), confirmed live.
6. **L12:** partitioning and clustering for typed and audit tables,
   and an incremental "latest" model in Dataform once history grows.

## Probe results (offline, no cloud calls)

| # | Scenario | Observed |
|---|---|---|
| P1 | Lock delete returns 403 during release | `Pipeline.run()` raised `Forbidden` after the file loaded (1 load committed); lock object left behind |
| P2 | Typed: A, B, then A restored after a contract change | `loaded`, `loaded`, **`failed`** (`ObjectStoreConflict` on `processed/...parquet`), and `failed` again next run; footer `created_by = parquet-cpp-arrow version 26.0.0` |
| P3 | Raw: cumulative exports `export-2026-09.xlsx` (ids 1–2) and `export-2026-10.xlsx` (ids 1–3), documented "latest per source_file" | 5 rows, ids `[1, 1, 2, 2, 3]`; duplicates 1 and 2 |
| P3b | Version strings | `LocalSource`: `1792099970012609800-1` (not castable to INT64); SharePoint eTags look like `"{GUID},3"` |
| P4 | Second run over an already-loaded file | outcome `skipped`, but downloads went from 1 to 2 |
| P5 | Corrupt numeric cell holding `jo.bloggs@example.org` | `ContractError: ... could not convert string to float: 'jo.bloggs@example.org'`, so the value is in the reason |
| P6 | Workbook part renamed `xl/book.xml` (openpyxl opens it) | scanner `ContractError` → quarantined even in "warn" mode |
| P7 | Upload event from a different bucket, same folder | the `was/` pipeline ran |
| P8 | Headings `Name` and `name` | both kept as distinct columns |
| P9 | Contract sheet with no raw table mapped | `failed` on run 1 and run 2 ("No raw table configured...") |
| P10 | Lock object with naive `expires_at` | `TypeError: can't compare offset-naive and offset-aware datetimes` |
| P11 | `basicConfig()` then `configure_json_logging()` | root handlers: plain `Formatter` and `JsonFormatter` (every line twice) |
