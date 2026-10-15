# Design Decisions

When project decisions are made they can be added to the decision log. This records the thinking as the project evolves. Focus on recording decisions which someone joining the project might need to know.

Order in reverse chronological order (most recent at top).

Entries are not rewritten once made. When a decision changes, add a
new dated entry saying which earlier entry it replaces, and mark the
earlier entry as superseded.

## 2026-10-15 — Runner helpers for scheduled and upload-triggered runs

### A run with a FAILED file fails

`Pipeline.run()` records a file that fails and carries on with the
next, so a run can return normally with FAILED files. Each solution
repository used to check the results itself. `py_common.runner` now
does: `run_job()` returns exit code 1, and `handle_upload()` raises
`RunFailed` (a new `IngestionError`). That way a Cloud Run job
execution shows as failed, and Pub/Sub delivers an upload event again.
`run_job()` runs every pipeline even after one fails or is refused by
its lock, because the sources are independent. QUARANTINED is not a
failure here: retrying won't fix a bad file, and it has its own alert.

### Upload events run Pipeline.run(), not Pipeline.process()

The HWW team's standard pattern starts a Cloud Run service from
Eventarc when a file lands. `handle_upload()` maps the object's
folder to its pipeline and calls `run()` for that folder. `run()`
takes the warehouse lock and writes the audit record and the
"already loaded" marker. `process()` does neither, so a redelivered
event would have loaded the file again. `GCP_SETUP.md` had suggested
`process()` could sit behind the trigger; it now says not to.

The daily scheduled job stays, as the backstop for lost events (by
default Pub/Sub keeps an undelivered event for 24 hours) and to run
the freshness check. The service runs with one instance and one
request at a time, so files landing together queue instead of
clashing on the lock. The lock still guards the short periods when
Cloud Run runs more instances than its maximum.

**Trade-off**: two ways to start a run means two deployments and two
log sources for the alerts. Benefit: an ad hoc upload loads in
minutes, and the setup matches the team's pattern.

### Missing files are checked per source

The missing-file alert was a BigQuery scheduled query that failed
when nothing had loaded for 35 days. It covered all sources at once,
and its failure email reached only the account that created it. It
is replaced by a `source_freshness` view, with one row per monthly
source read from that source's raw table. The daily job calls
`check_freshness()`, which logs `Missing monthly file:` for each
overdue source, and a log-based alert sends that to the team.
Sources are due by the 7th of the month.

## 2026-10-14 — Workbook layouts and a raw JSON load route

### A raw JSON route alongside the typed MERGE route

The Healthy Working Wales project lands data in BigQuery as raw JSON
and cleans it in Dataform (see
`docs/RAW_JSON_VS_CONTRACT_VALIDATION.md`). py-common provides the
landing and raw load: `Pipeline(output_format="raw_json")` writes
every configured sheet as newline-delimited JSON
(`py_common.raw.to_ndjson`), and `RawJsonWarehouse` inserts each
sheet's rows into its own raw table. A raw table has `source_file`,
`source_generation`, `sheet_name`, `row_number`, `checksum`,
`payload` (JSON) and `ingested_at`, and is partitioned by day of
`ingested_at`. `BigQueryWarehouse` and the Parquet route are
unchanged.

All of a file's rows, across every sheet, are inserted with the
audit record in one script transaction, as `BigQueryWarehouse` does
for its MERGE. Rows are only ever inserted. Every version of a file
stays in the raw table, and Dataform picks the latest
`source_generation`. Values keep their Excel type as far as JSON
allows: numbers stay numbers, and dates become ISO 8601 text. That
way the cleaning layer doesn't have to guess which "01/02/2026" was
a real date.

**Trade-off**: Raw tables grow with every monthly upload of a
cumulative file, though at HWW volumes that is kilobytes. Typing and
deduplication move to Dataform, so they are tested there rather than
here. Benefit: a schema change never stops a load, nothing is
overwritten, and the cleaning SQL can be rerun from the raw tables
at any time.

### Loaded markers and processed copies are kept per destination

The "already loaded" marker moves from `audit/{checksum}` to
`audit/{warehouse identity}/{checksum}`, and processed copies from
`processed/{identity}/{checksum}/...` to
`processed/{warehouse identity}/{identity}/{checksum}/...`. Audit
records gain a `warehouse` field. `raw/` and `quarantine/` keys don't
change: every pipeline stores the same original bytes there, which
`put()` accepts as a repeat.

Before, the first pipeline to load a workbook wrote the marker, and
every other pipeline reading a different sheet of that workbook then
skipped it as a duplicate. Had it not skipped, its different Parquet
would have collided with the first one's under the same key.

**Trade-off**: Markers written before this change are not looked
up, so files that were already loaded load once more after
upgrading. Nothing is in production yet; for a typed table with key
columns, the MERGE makes that reload harmless. Benefit: one workbook
can feed one pipeline per sheet.

### Mismatches can warn instead of quarantine

A worksheet's `on_mismatch` is `"quarantine"` (the default, as
before) or `"warn"`. In `"warn"` mode a header mismatch (missing or
unexpected column) and row-level problems (wrong type, missing
required value, duplicate in a unique column) are recorded as
warnings, and the file still loads. A missing listed column loads as
empty. Warnings are logged at WARNING, kept on `Result.warnings`, and
written to the audit record.

This qualifies "Validation errors quarantine, not fail" below. With
the raw JSON route, the cleaning layer in Dataform quarantines bad
rows one at a time, so rejecting a whole monthly file over one bad
value would hold back every good row with it.

**Trade-off**: In `"warn"` mode, bad data reaches the raw tables, and
the Dataform checks must catch it before it reaches reports.
Warnings only help if someone is alerted to them (see
`docs/GCP_SETUP.md`). Benefit: a renamed column or a stray value no
longer stops a month's data, and the file owner still hears about it.

### Contracts choose which columns are loaded

A worksheet's `column_selection` is `"listed"` (the default) or
`"all_except"`, and `excluded` names columns that are in the sheet
but never loaded. In `listed` mode only the contract's columns are
loaded, and an excluded column is not counted as unexpected. In
`all_except` mode every column except the excluded ones is loaded,
so a column added to a form is picked up without a contract change.
`Contract.extract()` applies the selection; the Parquet conversion
now uses it rather than reading the sheet itself.

Most HWW sources hold far more columns than the analysis needs,
including names, email addresses and contact details. Loading every
column would copy that personal data into BigQuery for no reason,
which works against the data minimisation the DPIA will expect.

**Trade-off**: In `listed` mode a new column is not loaded until
someone adds it to the contract; the header check reports it. In
`all_except` mode a new personal-data column is loaded until someone
excludes it. Benefit: each source can be set to whichever risk fits
it, and the original file is still kept in `raw/` if a column is
needed later.

### Every column gets a name; none are dropped

Headings are read once, by `read_sheet()` in `contract.py`, and both
validation and conversion use its names. A blank heading over a
column with data becomes `col_<letter>` (e.g. `col_B`); a blank
heading over an empty column is ignored as formatting. A repeated
heading keeps its name the first time and becomes `_2`, `_3`, ...
after that, skipping any name the sheet already uses. Headings are
not trimmed. Rows with no values are skipped.

Before, the Parquet conversion built each row with
`dict(zip(headers, row))`, so a repeated heading silently kept only
its last column, and a blank heading's data went nowhere useful. The
HWW survey sheets repeat "Other" many times.

**Trade-off**: A data column with no heading is now reported as an
unexpected column (`col_B`) instead of passing unnoticed, so a
contract may need to list it. Names depend on column position for
blank headings and on order for repeats, so moving such a column
changes its name. Benefit: no data is lost without anyone knowing,
and the names are the same on every run of the same layout.

## 2026-09-30 — Real GCS lock; SharePoint adapter; versioned storage keys; `_loaded_at` set by BigQuery

### GCSObjectStore.lock() is a real lock, not a no-op

Replaces the earlier position, recorded in the README "Concurrency"
section, that the lock could be a no-op because one Cloud Scheduler
job runs at a time. That doesn't stop a manual re-run, a retry or a
second trigger overlapping a scheduled run.

`lock()` now creates a lock object with GCS's atomic create-if-absent
(`if_generation_match=0`). A second run gets `RuntimeError` naming the
holder. The lock never expires and is never stolen, as the
`ObjectStore` protocol requires; release deletes only the lock this
run created.

**Trade-off**: A run killed mid-flight leaves its lock behind, and
later runs are refused until someone deletes it by hand. Benefit:
overlapping runs can't corrupt the staging table, however they start.
No new dependency (Firestore was considered and not needed).

### SharePointSource is real, but authentication is not decided

Supersedes the `SharePointSource` part of "GCP adapters are real, not
stubs" (2026-09-21). `SharePointSource` now lists and downloads files
through Microsoft Graph and is unit-tested against mocked Graph
responses. It has not been run against a real site.

Authentication is injected as a `token_provider` (a function returning
a Graph access token) rather than built in, because IT has not yet
chosen the credential flow. Configuration (`SharePointConfig`) holds
only the location: hostname, site path, library, folder. It rejects
unknown keys, so credentials can't be put there by mistake.

**Consequence**: Before a live run, someone writes the token provider
for the approved flow. The adapter itself should not need to change.

### Plain `requests` instead of the Graph SDK

The `sharepoint` extra now depends on `requests` instead of
`microsoft-graph-core` and `azure-identity`. The adapter makes five
read-only GET calls; the SDK is async and heavier than that needs, and
`azure-identity` would assume a credential flow. An identity library
can be added to the extra once the flow is approved.

**Trade-off**: Pagination, retries and error mapping are ours to
maintain. Benefit: they are small, visible and fully unit-tested.

### Stored copies are keyed by identity and checksum

`raw/`, `quarantine/` and `processed/` keys are now
`{identity}/{checksum}/{name}` instead of `{identity}`. Before, a file
edited in place (same identity, new bytes) collided with its earlier
copy, `put()` raised `ObjectStoreConflict`, and the file was marked
`FAILED` on every run. This is common in SharePoint, where item IDs
survive edits, and also happened with `LocalSource` file names.

**Trade-off**: Every version of a file is kept, so storage grows with
edits (a bucket lifecycle rule can manage that). Keys written before
this change are left where they are. Benefit: edited files load, and
the raw layer holds exactly what was received for each version.

### BigQuery sets `_loaded_at`, not the Parquet file

`_loaded_at` used to be added to the Parquet during conversion, so the
same workbook produced different Parquet bytes on every run. Parquet
is stored under a checksum key before the warehouse load, so a retry
after a failed load (warehouse outage, rolled-back transaction) hit
`ObjectStoreConflict` and the file was marked `FAILED` on every later
run.

Now the Parquet holds only source columns and `BigQueryWarehouse`
sets `_loaded_at` inside the commit transaction, to the audit record's
timestamp. Matched rows get it refreshed on update. A source column
named `_loaded_at` is refused.

**Trade-off**: Parquet is only byte-identical while the pyarrow
version stays the same; upgrading them between a failed load
and its retry can still cause one conflict. `_loaded_at` now means
"committed to BigQuery" rather than "converted". Benefit: failed loads
retry cleanly, and each row's `_loaded_at` joins to its audit entry.

### SharePoint files are identified by item ID and versioned by eTag

The Graph item ID is stable across renames, so a renamed file is not
treated as new. The eTag is checked before and after download; any
change raises `VersionMismatch` and the pipeline skips the file for
this run. Only files directly in the configured folder are read.

## 2026-09-21 — GCS and BigQuery adapters built; warehouse merges on key

### GCP adapters are real, not stubs

> **Partly superseded** on 2026-09-30: `SharePointSource` is no longer
> a stub (see "SharePointSource is real, but authentication is not
> decided").

Supersedes "Phase 1: Local testing only" (2026-09-18).
`GCSObjectStore` and `BigQueryWarehouse` are implemented and
unit-tested against mocks (commits `6658cfd`, `de40c07`).
`SharePointSource` is still a stub that raises `NotImplementedError`,
pending the Entra app registration.

**Consequence**: The unit tests import `google.cloud`, so developers
install the `gcp` extra (`pip install -e ".[dev,gcp]"`) even when
working offline. No credentials are needed to run them.

### BigQueryWarehouse merges on the contract's key columns

`BigQueryWarehouse` takes `key_columns` (normally
`Contract.key_columns`) and `MERGE`s staging into the final table on
them, updating matching rows and inserting new ones. With no key
columns it appends. The `MERGE` and the audit `INSERT` run in one
BigQuery transaction.

**Benefit**: Re-loading a changed version of a file updates its rows
rather than duplicating them. The content-checksum check in
`Pipeline` still skips byte-identical files before they reach the
warehouse.

## 2026-09-18 — py-common Library Inception

### Protocols, not abstract base classes

Source, ObjectStore, and Warehouse are defined as Protocols
(structural typing) rather than abstract base classes. This allows
multiple implementations to satisfy the interface without explicit
inheritance, and makes testing with mocks easier.

**Trade-off**: Requires Python 3.8+ for `Protocol` (the project as a
whole requires 3.11+; see `pyproject.toml`). Benefit: Implementations
are simpler and more flexible.

### Contract is separate from warehouse

Validation rules are a Contract (a data class), not part of
the Warehouse interface. This means the same contract can work
with different warehouse implementations, and validation is
repeatable offline.

**Trade-off**: Contracts must be provided by caller. Benefit: Core
pipeline code never imports or depends on contract formats.

### Fixtures are generated and committed

The seven test Excel files (7 KB total) are generated by
`py_common.fixtures.make_fixtures()` and committed to the repository. This makes it easy for
reviewers to see exactly which scenarios are tested without
reading Python code.

**Trade-off**: Small binary files in git. Benefit: Fixtures are
reproducible, regenerable, and serve as regression tests.

### Configuration is YAML with environment variable interpolation

Configuration format is YAML (human-readable, multi-environment).
Environment variables are referenced as `${VARIABLE_NAME}` and
replaced only when that section is used. This allows developers
to test SharePoint connectivity without setting GCP variables.

**Trade-off**: Manual interpolation in caller code. Benefit: Loose
coupling; no implicit assumptions about which variables exist.

### ObjectStore requires idempotent put()

The put(key, data) method must succeed if identical data already
exists at that key. This is essential for retryable, distributed
systems where a network failure can't be distinguished from
success + network loss.

**Invariant**: put(k, d); put(k, d) must succeed both times.

### Files identified by item ID, versions by eTag then checksum

At Source discovery, each file gets a unique identity and version.
The pipeline uses this to:
1. Skip if eTag unchanged (source hasn't been touched)
2. Skip if checksum unchanged (metadata-only changes)
3. Detect and retry if version changed during download

**Benefit**: Prevents processing stale or partial files. Allows
rerun-safety without additional state.

### Validation errors quarantine, not fail

A workbook that fails contract validation is stored in a
quarantine/ prefix and marked as QUARANTINED outcome. The job
continues processing other files. Only our-side errors (service
failures) mark the run as FAILED.

**Benefit**: Quarantine is an expected, non-critical outcome.
Data owners can review and resubmit. Platform does not alert on
quarantine, only on FAILED.

### Audit records include checksum, not full data

Every attempt is recorded with filename, version, checksum, row
count, and errors. We do NOT store the actual workbook contents
or individual row data in audit logs.

**Benefit**: Audit is lightweight, privacy-respecting, and safe
to log without redaction rules.

### No cloud SDK imports in core pipeline

The pipeline.py, contract.py, config.py, and model.py files
import no cloud libraries (no google-cloud-*, no azure-*, no
msgraph-*). All cloud access goes through adapters behind protocols.

**Benefit**: Core code is testable offline with LocalSource and
LocalObjectStore. Dependencies are explicit and swappable.

### Phase 1: Local testing only

> **Superseded** on 2026-09-21 by "GCP adapters are real, not stubs".
> Only `SharePointSource` is still a stub.

SharePointSource, GCSObjectStore, and BigQueryWarehouse are
stubbed (raise NotImplementedError). This ensures all tests pass
locally before cloud infrastructure exists.

**Benefit**: New team can start writing ingestion logic
immediately. Cloud implementations follow once Entra app and
GCP project are provisioned.

### Warehouse.load() is all-or-nothing

The warehouse.load() method either:
1. Loads to staging
2. Verifies row count
3. Merges/appends to final table
4. Records audit entry
5. Commits transaction

Or: Raises exception and rolls back everything. No partial writes.

**Benefit**: Guarantee: if load() succeeds, the data is committed
and audited. If it raises ServiceError, nothing was written; if it
raises IndeterminateCommitError, the outcome is unknown.

### Parquet types come from the contract (0.2.0)

The typed route used to let pandas infer each Parquet column's type
from the values. A "warn" worksheet with `1` and `"NA"` in one column
then failed conversion (`ArrowInvalid`) and was retried forever, and
a column empty this month was staged with a different type than last
month. `py_common.parquet.to_parquet()` now writes the schema the
contract declares (undeclared "all_except" columns as strings) and
loads a value that doesn't fit as NULL, with a warning.
`BigQueryWarehouse` compares the staged schema with the final table:
it creates a missing final table, adds new nullable columns, and
quarantines a file whose column types changed or columns disappeared.

### Warehouse.load() is idempotent (0.2.0)

Steps 3-4 run only if the audit table has no `LOADED` row for this
destination and file, checked inside the same transaction; otherwise
load() writes nothing and returns False. The object store's "loaded"
marker is written after the commit, so a crash, timeout or store
error between the two used to mean a second load on retry (review
C2). Now the warehouse's own audit table, committed atomically with
the data, is the authority and the marker is only a cache.

The raw JSON route identifies a file by identity + version, the
typed route by content checksum: see docs/RAW_TABLE_LAYOUT.md.

### RAP Principles: Reproducibility

- Configuration is external, not hardcoded
- Fixtures are committed and regenerable
- Tests are deterministic (same input → same output)
- Decision log explains "why" not just "what"
- Code is peer-reviewed before merge

### RAP Principles: Auditability

- Every file processing attempt is recorded with timestamp, identity,
  checksum, row count, and outcome
- Audit entries include errors for failed attempts
- Audit records cannot be modified (append-only)
- Audit is queryable by checksum (detects re-runs)

### RAP Principles: Transparency

- No hardcoded values (all from environment)
- Public schema defined in contracts (YAML or Python)
- Test fixtures are committed (not generated on-the-fly)
- Code is readable and well-commented
- Documentation is plain language, not academic

**Standards**: PEP 8, type hints, Google docstrings, TDD, RAP (Reproducible Analytical Pipelines — reproducible, auditable, peer-reviewed)
