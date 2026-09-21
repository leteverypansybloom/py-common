# Data Ingestion Framework: A Developer's Guide

**Audience:** developers on the Public Health Wales (PHW) data engineering
team, working in this repository (`py-common`) or a project that depends
on it (e.g. the Healthy Working Wales SharePoint → GCP pipeline).

**Purpose:** one plain-language reference that defines the vocabulary of
data ingestion, explains what an "ingestion framework" is expected to do,
and states exactly what `py-common` does today versus what it does not —
with a file and line number for every claim, so nothing here has to be
taken on trust.

**Scope:** this guide covers *ingestion* — getting data from a source
system into governed cloud storage, checked and loaded. It stops where
`py-common` stops: at a validated load into a warehouse table. Identity
resolution, curated ("GOLD") modelling and reporting are out of scope for
this repository and are covered only briefly, in [Section 8](#8-what-sits-outside-py-common),
so the boundary is clear.

**New to this repository?** Start with
[`GETTING_STARTED.md`](GETTING_STARTED.md) instead — a hands-on
walkthrough that gets you running the pipeline before you need this
level of detail. See [`README.md`](README.md) in this folder for how
all the docs here fit together.

---

## Contents

1. [What is data ingestion?](#1-what-is-data-ingestion)
2. [The five stages, and the vocabulary for each](#2-the-five-stages-and-the-vocabulary-for-each)
3. [Framework vs. solution](#3-framework-vs-solution)
4. [What py-common is today](#4-what-py-common-is-today)
5. [Feature-by-feature status](#5-feature-by-feature-status)
6. [The recommended SharePoint → GCP architecture](#6-the-recommended-sharepoint--gcp-architecture)
7. [Choosing features for your scenario](#7-choosing-features-for-your-scenario)
8. [What sits outside py-common](#8-what-sits-outside-py-common)
9. [Priority order for closing the gaps](#9-priority-order-for-closing-the-gaps)
10. [Glossary (A–Z quick reference)](#10-glossary-az-quick-reference)
11. [References](#11-references)

---

## 1. What is data ingestion?

Data ingestion is the **controlled** process of bringing data from a
source system into a place where it can be stored, checked, transformed,
analysed or shared. It is more than "moving data": a proper ingestion
pipeline can always answer four questions about anything it has handled:

- **What arrived?** (a durable, timestamped copy of the original)
- **Was it usable?** (did it pass validation, and against what rules?)
- **Was it processed before?** (so a re-run or a re-sent file doesn't
  create duplicates)
- **How do we recover if something fails?** (from what point, using
  what retained evidence?)

This is the test to apply to any new source before deciding "reading the
file is basically the pipeline." If you can't answer all four questions
about a source once it's live, the ingestion is not finished, however
short the code that reads the file.

`py-common` is one attempt at packaging up the reusable parts of that
work — file discovery, validation, storage, warehouse loading, and the
audit trail — so a new source is (as far as possible) *configuration*,
not new pipeline code. [Section 4](#4-what-py-common-is-today) shows how
far it currently gets.

---

## 2. The five stages, and the vocabulary for each

A useful way to hold the whole subject in your head is five groups:
**connect, capture, make trustworthy, publish, operate**. Below, each
group's key terms are defined in plain language, followed by where (if
anywhere) `py-common` implements the idea today. File references are
exact; where nothing exists, that's stated plainly rather than implied.

### 2.1 Connect — reaching the source and knowing what's there

| Term | Plain-language meaning |
|---|---|
| **Source system** | Where data starts: an API, SharePoint, a database, a spreadsheet, a message queue, a device, or a third-party platform. |
| **Source data** | The actual records, files, messages or responses the source produces. |
| **Connector / adapter** | Reusable code that knows how to talk to *one* type of source or destination — SharePoint, a REST API, SQL Server. |
| **Interface / contract** | The agreed rules for exchanging data: endpoint, fields, formats, authentication, rate limits, errors, expected behaviour. |
| **API** | A defined way for two systems to request or send data to each other, almost always over HTTP. |
| **Authentication** | Proving a system is allowed to connect at all — OAuth, a client secret, a certificate, an API key. |
| **Authorisation / permissions** | What that *authenticated* system is then allowed to read, write or administer. Not the same thing as authentication — you can be who you say you are and still be refused access to a library. |
| **Secrets management** | Storing credentials securely (a secret store) and passing *references* to them through configuration, never the secret value itself. |
| **Connection testing** | Checking credentials, network access and permissions *before* a feed goes live, not on the first production run. |
| **Source discovery** | Listing what objects, files, fields or API resources actually exist at a source, useful when the requested object might change or be unclear. |

**In `py-common` today:**

- The `Source` protocol (`src/py_common/ports.py:14-45`) is the
  connector interface: `items()` lists what's available, `download()`
  fetches one item's bytes.
- `LocalSource` (`src/py_common/adapters/local.py:21-84`) implements it
  for a local folder — this is what the test suite and fixtures use.
- `SharePointSource` (`src/py_common/adapters/sharepoint.py:21-56`)
  is the SharePoint connector **and it is not implemented**: every
  method raises `NotImplementedError`, and the constructor refuses to
  run with an explicit message pointing you at `LocalSource` instead.
  This is the single largest gap for a "SharePoint into GCP" project —
  see [Section 6](#6-the-recommended-sharepoint--gcp-architecture).
- There is no connection-testing step, and no source-discovery helper
  (e.g. "list the drives on this SharePoint site" before you commit to
  a folder path).
- Authentication for GCP is handled separately by `gcp_auth.py`
  (`src/py_common/gcp_auth.py`), which auto-detects Cloud Run/GCE vs. a
  local Application Default Credentials file vs. a service-account
  JSON key. This is **not** wired into the `Source`/`ObjectStore`/
  `Warehouse` adapters — `GCSObjectStore` and `BigQueryWarehouse`
  currently construct their own `storage.Client()` /
  `bigquery.Client()` directly (`adapters/gcs.py:57`,
  `adapters/bigquery.py:64`), relying on ambient Application Default
  Credentials rather than calling `gcp_auth()` first.

### 2.2 Capture — getting the bytes in, once, safely

| Term | Plain-language meaning |
|---|---|
| **Batch ingestion** | Moving a defined set of data at intervals — a daily API extract, a folder of files. |
| **Streaming ingestion** | Processing individual events, continuously or near real time. |
| **Full load** | Extracting the complete relevant dataset — first run, or a deliberate rebuild. |
| **Incremental load** | Extracting only what's changed since the last successful run. |
| **Change data capture (CDC)** | A specialised form of incremental load that reads inserts/updates/deletes straight from a database's change log. |
| **Backfill** | A deliberate load of older, historical data — after a gap, a correction, or a new pipeline release. |
| **Polling** | Asking a source repeatedly whether new data exists. |
| **Webhook** | The source calling *you*, instead of you asking it. |
| **Pagination** | Fetching a large API response in pages, via page numbers, cursors, or "next" links. |
| **Cursor / watermark** | The saved point (timestamp, ID, delta token) from which the next incremental extract resumes. |
| **Checkpoint** | Persisted progress for a pipeline stage; it should advance *only* after the relevant work has actually succeeded. |
| **Landing zone / raw layer** | Storage for the source input in its original form, with timestamp and source context attached — the foundation for audit and recovery. |

**In `py-common` today:**

- Ingestion is **batch, full-listing, no cursor**. `LocalSource.items()`
  (`adapters/local.py:35-54`) lists *every* matching file in the folder
  on every run — there is no "only what changed since last time" at the
  source level. For a local test folder with nine fixtures that's fine;
  for a live SharePoint library with a real history it means listing
  the whole library on every run once `SharePointSource` exists.
- Re-processing is prevented differently: not by a source-side cursor,
  but by a **content hash** check after download. `Pipeline.process()`
  (`src/py_common/pipeline.py:115-131`) computes a SHA-256 of the
  downloaded bytes and skips the file if that exact checksum was
  already recorded as `LOADED` (`store.get(f"audit/{checksum}")`).
  This is a genuinely good property — a file that is re-saved with
  identical contents is correctly skipped, which is stricter than
  tools that key off "last modified" alone — but it is not a
  *watermark*: the pipeline still has to list and often download every
  file, every run, to find that out.
- Version safety at download time: `SourceItem.version`
  (`src/py_common/model.py:24-37`) is compared between discovery and
  download; if a file changed in between, `download()` raises
  `VersionMismatch` (`adapters/local.py:76-81`) and the pipeline records
  the file as `SKIPPED` rather than processing a half-written file
  (`pipeline.py:99-113`).
- The **landing zone** is real: every downloaded file is written
  unchanged to `raw/{item.identity}` before anything else happens
  (`pipeline.py:152`), and a file that fails validation is *also*
  written to `raw/` and to `quarantine/{item.identity}`
  (`pipeline.py:140-141`) before the pipeline gives up on it. So the
  original bytes are always recoverable, even for files that never
  reach the warehouse.
- There is no backfill or replay command. Re-loading old data today
  means manually clearing the `audit/{checksum}` marker for those files
  so the checksum check doesn't skip them again.

### 2.3 Make trustworthy — contracts, schema and quality

| Term | Plain-language meaning |
|---|---|
| **Staging** | A technical working representation of the data, standardised into consistent file formats and columns. |
| **Transformation** | Changing structure or values — parsing a file, renaming fields, standardising dates, mapping codes. |
| **Harmonisation** | Making data from different sources consistent enough to compare or combine. |
| **Mapping** | A documented rule connecting a source field/code to a target field, code or meaning. |
| **Target data model** | The structure data must follow at its destination — a schema, a set of FHIR resources, an API payload shape. |
| **Schema** | The expected fields, types, relationships and constraints in a dataset or message. |
| **Schema drift** | An unplanned change to that structure — a renamed column, an altered type. |
| **Schema registry** | Somewhere the expected schema (and its compatible versions) is stored, so drift can be *detected* rather than discovered by a crash. |
| **Metadata** | Data *about* the pipeline and the data: owners, paths, schemas, schedules, mappings, quality rules, retention, deployment settings. |
| **Metadata-driven ingestion** | A shared runner that behaves according to validated configuration, so a new (supported) feed is set up through metadata, not code changes. |
| **Configuration** | The specific settings one pipeline run uses — connection references, schedule, source object, load method, destination. |
| **Data quality checks** | Tests that decide whether data is fit to move forward: required fields, valid values, uniqueness, counts, freshness. |
| **Validation** | Checking a request, configuration, payload or output against defined rules. |
| **Data profiling** | Examining a source *before* building the pipeline — volumes, formats, nulls, unique values, date coverage, duplicates, anomalies. |
| **Quarantine / dead-letter store** | A controlled place for invalid data or failed messages, with a reason attached and a safe way to replay it later. |

**In `py-common` today:**

- The **contract** is the schema registry, in miniature. `Contract`,
  `Worksheet` and `Column` (`src/py_common/contract.py:16-57`) are
  plain Python dataclasses — no YAML layer yet, so a contract is
  written *in* the calling code, not alongside it as data. Each
  `Column` declares `data_type`, `nullable` and `unique`
  (`contract.py:17-30`).
- `Contract.validate()` (`contract.py:59-188`) checks, in order:
  required worksheets exist, required columns exist, no *unexpected*
  columns exist (an extra column is an error here, not a warning —
  worth confirming this is the policy PHW actually wants; see
  [Section 5](#5-feature-by-feature-status), row "Schema-drift
  detection"), nullability, basic type matching for `integer`/`float`,
  and uniqueness for columns marked `unique`.
- Validation failures raise (`ContractError` for structural problems,
  `ValidationError` for data problems — `errors.py:44-60`) with up to
  five example errors joined into one string
  (`contract.py:177-185`). There is **no** per-row structured output
  (no "row 17, column X, reason Y" table you can query) — just that one
  string, captured in `Result.errors` (`model.py:58`).
- `key_columns` is declared on every `Contract`
  (`contract.py:56`) but, notably, **nothing in `contract.py` or
  `pipeline.py` reads it**. Uniqueness checking uses each column's own
  `unique` flag instead. If the intent was "these are the columns that
  identify a record for merge/upsert purposes," that intent isn't
  acted on anywhere yet — see the BigQuery gap below.
- **No minimum row count check.** A workbook with a valid header row
  and zero data rows validates successfully and reports
  `rows_processed = 0` — the pipeline has no way to notice that a daily
  export came back empty.
- **No data profiling** step anywhere — profiling (understanding a new
  source's volumes, nulls, duplicates before writing its contract) is
  a manual, pre-project activity today, not a `py-common` feature.
- **No metadata-driven onboarding.** There's no request file, no
  registry of "which sources exist, who owns them, what's their
  schedule" — that's currently whatever the calling application
  (outside this repo) wires up itself.

### 2.4 Publish — writing to the destination, safely

| Term | Plain-language meaning |
|---|---|
| **Idempotency** | Designing a retry so processing the same logical input twice doesn't create duplicate or wrong results. |
| **Idempotency key** | A stable identifier sent with a write so the receiving system can recognise "this is a retry of that same operation." |
| **Deduplication** | Detecting and removing/merging repeated records using agreed keys and rules. |
| **Upsert** | Update the existing record if its key exists; insert a new one if it doesn't. |
| **Delete handling** | The agreed policy for when a source record disappears — propagate the deletion, mark inactive, keep history, or investigate. |
| **Retry** | Re-attempting a temporary failure — a network blip, a throttled API call. |
| **Backoff and jitter** | Waiting progressively longer between retries, with some randomness, so you don't hammer a struggling service in lock-step with every other retrying client. |
| **Throttling / rate limiting** | Limits a service imposes on how fast you can call it; your pipeline has to respect them, not just retry blindly into them. |
| **Delivery receipt / acknowledgement** | Evidence that a destination accepted, rejected, or is still processing what you sent it. |
| **Reconciliation** | Comparing counts/keys across source, intermediate and destination to explain any difference and confirm nothing was silently lost. |

**In `py-common` today:**

- The `Warehouse` protocol (`ports.py:97-137`) promises exactly the
  right shape: stage, verify row count, merge on key columns, record
  audit, commit-or-roll-back-everything (`ports.py:118-125`). The
  **implementation doesn't yet deliver that** — see below.
- `BigQueryWarehouse.load()` (`adapters/bigquery.py:81-223`) does:
  load Parquet into a truncated staging table, verify the row count
  matches (`bigquery.py:141-150`), then — the docstring says "MERGE
  into final table (upsert on key columns)" (`bigquery.py:94`) but the
  code that actually runs is a plain
  `INSERT INTO ... SELECT * FROM staging` (`bigquery.py:160-163`), with
  a `TODO` comment admitting it: *"Sample: append-only for now (no
  upsert). TODO: Update to MERGE with key columns when contract
  available."* (`bigquery.py:155-156`). The contract *is* available —
  `key_columns` is right there on the `Contract` object — it just isn't
  passed through to the warehouse today. Concretely: **re-running a
  file that already loaded, with a different checksum for any reason
  (say, a formula recalculated), will duplicate every row in that
  file** in the final table, because there is no upsert. This is the
  most consequential gap in the guide, because it silently
  contradicts both the interface's own contract and the unit tests'
  own class docstring (`tests/unit/test_bigquery_adapter.py:135-145`
  describes "MERGE into final table" as the behaviour under test, but
  the test itself only asserts row-count verification and that an
  audit `INSERT` happened — it never asserts an upsert occurred).
- There is **no idempotency key** sent to BigQuery beyond the
  content-hash dedup already described at the pipeline layer — which
  guards against re-processing an *identical* file, not against a
  changed file being loaded twice for the same logical record.
- **No delete handling policy** exists or is documented — if a row
  disappears from a source export, nothing in `py-common` decides
  whether that should be propagated, ignored, or flagged.
- **No retry, backoff or rate-limiting code anywhere** in the
  repository — not in the BigQuery client calls, not in the (stubbed)
  SharePoint adapter, not in the pipeline. Every external call is a
  single attempt. This matters most for SharePoint/Microsoft Graph,
  which is known to throttle (Airbyte documents this explicitly for
  its own SharePoint connector — see [reference 3](#11-references)).
- **No reconciliation** step — nothing compares "rows in the source
  file" against "rows now queryable in the final table" after a load;
  the row-count check in `BigQueryWarehouse.load()` only checks
  *staging*, before the final `INSERT`.

### 2.5 Operate — knowing it worked, and recovering when it didn't

| Term | Plain-language meaning |
|---|---|
| **Lineage** | A record of where data came from, how it changed, which code/config ran, where it ended up. |
| **Audit trail** | Operational evidence of who/what ran a pipeline, when, with which versions, and what happened. |
| **Observability** | The general ability to understand pipeline behaviour through logs, metrics, traces, status, alerts. |
| **Monitoring** | Collecting and viewing operational signals — failures, duration, volume, freshness, retries, backlog age. |
| **Alerting** | Telling an owner when a condition needs action. |
| **Run ID** | A unique identifier joining logs, quality results, inputs, outputs and status for one execution. |
| **Retention** | How long raw inputs, processed data, logs and error records are kept. |
| **Recovery / replay** | Safely re-running a failed or historical part of the pipeline from retained inputs and recorded state. |
| **Versioning** | Recording versions of code, config, mappings, schemas, reference data, so results are reproducible. |
| **Infrastructure as code (IaC)** | Cloud resources and permissions defined in version-controlled files (e.g. Terraform), not clicked together by hand. |
| **CI/CD** | Automated checks and controlled deployment of code, config and infrastructure changes. |
| **Environment** | A separate deployment context (dev/test/prod) with its own settings and safeguards. |
| **Access control** | Restricting people and services to the minimum data and actions they actually need. |
| **Service-level objective (SLO)** | A measurable expectation — e.g. "daily data is published by 09:00 on 95% of days." |
| **Cost management** | Designing for predictable storage/processing/network/API cost, then watching actual usage against it. |

**In `py-common` today:**

- **Audit trail:** every file processed produces an `AuditRecord`
  (`model.py:75-92`) with timestamp, item identity, filename, version,
  checksum, row count, outcome, errors and processing time, written to
  `audit/records/{timestamp}_{identity}.json` (`pipeline.py:215-247`).
  This is genuinely solid, append-only, and doesn't store the raw data
  itself — only the checksum — so it's safe to keep without extra
  redaction work.
- **Run ID:** there isn't one. Each `Result` and `AuditRecord` is
  per-*file*; there's no record that groups "everything that happened
  in this run" together, so answering "how did last night's run go, as
  a whole?" means scanning individual file records for one time window.
- **Logging:** plain Python `logging` module calls throughout
  (e.g. `pipeline.py:34`, `adapters/gcs.py:19`), text-formatted, not
  structured JSON. This will still show up in Cloud Logging on Cloud
  Run, but you can't filter or alert on a specific field the way you
  can with structured logs.
- **Monitoring/alerting:** none. A failed run currently means an
  uncaught exception propagates out of `Pipeline.run()`
  (`pipeline.py:75-77`); nothing emails, pages, or posts anywhere.
- **Retention:** no retention policy is set or configurable anywhere —
  the landing/raw/quarantine prefixes in `GCSObjectStore` accumulate
  forever until someone sets a bucket lifecycle rule outside this code.
- **Locking:** `ObjectStore.lock()` is meant to be a real exclusive
  lock, and the protocol docstring says so explicitly — *"Lock must
  not auto-expire or steal from long-running processes"*
  (`ports.py:81-83`). `GCSObjectStore.lock()`
  (`adapters/gcs.py:128-153`) is a documented **no-op stub**: it
  acquires nothing and will happily let two overlapping runs both
  believe they hold the warehouse lock. `LocalObjectStore.lock()`
  (`adapters/local.py:134-148`) is the same, appropriately, for local
  testing. This is fine as long as only one instance of the job ever
  runs at a time — but nothing currently enforces that either (no
  Cloud Run concurrency limit is configured in this repo, because no
  deployment configuration exists yet at all — the `deploy/` folder is
  present but empty).
- **Secrets:** the `SecretStore` protocol exists (`ports.py:140-158`)
  but **no adapter implements it** — there is no Secret Manager class
  anywhere in `src/py_common/adapters/`. `gcp_auth.py` handles *GCP
  authentication* (which credential to run as) but that is a different
  problem from *fetching an application secret* (e.g. a SharePoint
  client secret) — the two should not be confused when
  `SharePointSource` is built.
- **IaC / CI/CD:** `.github/workflows/tests.yml` runs pytest, and a
  `Dockerfile` builds the package into a container. There is **no
  Terraform** anywhere in the repository, and the `deploy/` directory
  exists but is empty — so none of "which bucket, which dataset, which
  schedule, which service account" is written down as reviewable code
  yet.
- **Housekeeping note, not a framework gap:** `test_auth.py` at the
  repository root (not under `tests/`) is a manual, one-off script that
  hardcodes a real GCP project id (`ndr-tr-phw-dp-dev`). It isn't
  picked up by `pytest` (wrong location, no `test_` function inside),
  but it's the kind of file worth moving into `scripts/` or deleting
  once its job is done, so a real project identifier doesn't sit
  loosely in the repo history.

---

## 3. Framework vs. solution

These two words get used interchangeably in conversation, and the
distinction is worth holding onto because it decides *where* a piece of
logic should live.

> An **ingestion framework** is the reusable platform capability: shared
> connectors, configuration, security, logging, quality checks and
> deployment patterns.
>
> An **ingestion solution** is one specific implementation using that
> framework: "read daily records from this API and changed Excel files
> from this SharePoint library, validate them, and load them into this
> BigQuery table."

`py-common` is the *framework*. A project that depends on it — for
example a "Healthy Working Wales SharePoint ingestion" repository — is a
*solution*: it should mostly consist of a `config.yaml`, a `Contract`
definition per source, and a thin `main.py` that wires
`SharePointSource` + `GCSObjectStore` + `BigQueryWarehouse` +
`Pipeline` together (see [Section 6](#6-the-recommended-sharepoint--gcp-architecture)).

A practical decision rule for "does this belong in `py-common`, or in the
solution repo that depends on it?":

- **Build it in the framework** when it will be used by more than one
  feed, or it closes a recurring operational risk (retry/backoff logic,
  the audit trail shape, the lock implementation).
- **Keep it in the solution** when it's about the *meaning* of one
  source or destination — field mappings, "what counts as a duplicate
  event," the exact BigQuery table layout for this project.
- **Don't add platform complexity until a real requirement demands
  it** — scale, low latency, many dependencies, high failure
  consequences, or many users. Section 4 of the internal review already
  applies this rule feature-by-feature for a sibling version of this
  codebase [reference 1](#11-references); the same discipline applies
  here.

---

## 4. What py-common is today

In one paragraph, for anyone who hasn't read Section 2: `py-common`
defines four small interfaces (`Source`, `ObjectStore`, `Warehouse`,
`SecretStore` — all in `ports.py`) so that the core pipeline
(`pipeline.py`) never has to import a cloud SDK directly. A `Pipeline`
discovers files from a `Source`, downloads and hashes each one, validates
it against a `Contract`, lands the raw bytes, converts valid files to
Parquet, loads them into a `Warehouse`, and writes an `AuditRecord` for
every attempt — success, skip, quarantine or failure alike. It runs
today against a local folder (`LocalSource` + `LocalObjectStore`); the
production GCP pieces (`GCSObjectStore`, `BigQueryWarehouse`) are written
and unit-tested against mocks, but the SharePoint side
(`SharePointSource`) is an explicit stub, and there is no orchestration,
scheduling, alerting, secrets, or infrastructure code anywhere in the
repository yet.

The test suite (`tests/unit/`, `tests/integration/`) confirms this shape:
unit tests exist for `Contract`, `config.py`, `GCSObjectStore` and
`BigQueryWarehouse` (all mocked); the two integration tests
(`test_end_to_end_gcs_bq.py`, `test_gcs_bigquery_integration.py`) are
marked `integration` and need real GCP credentials to run — they are not
exercised in CI as configured.

---

## 5. Feature-by-feature status

This maps the framework-feature catalogue from the PHW ingestion workshop
material onto what's verifiably true of this repository today. Status
key: **Have** (built and tested), **Partial** (interface or stub exists,
behaviour doesn't match the promise), **Missing** (nothing exists).

| Framework feature | Status | Evidence |
|---|---|---|
| Request/onboarding interface | Missing | No request format, form or API anywhere in the repo. |
| Metadata catalogue | Missing | No store of source/owner/schedule; contracts are Python objects the caller constructs. |
| Connector catalogue | Partial | `Source`/`ObjectStore`/`Warehouse` protocols exist (`ports.py`); only local + (untested-live) GCP adapters are real. |
| Connection testing | Missing | No pre-flight credential/permission check anywhere. |
| Source discovery | Missing | No "list what's available at this source" helper. |
| Raw landing storage | **Have** | `pipeline.py:152`, immutable `raw/{identity}` key per file. |
| Standardisation layer | Partial | Excel → pandas → Parquet conversion exists (`pipeline.py:154-165`); no multi-format (CSV/JSON) reader yet. |
| Schema registry | Partial | `Contract` dataclasses exist (`contract.py`); no external, diffable file format (e.g. YAML) yet. |
| Schema-drift detection | **Have** (strict) | Missing *and* extra columns both raise `ContractError` (`contract.py:92-125`) — confirm this "reject on any drift" policy is actually what's wanted; some tools treat an extra column as a warning, not a failure. |
| Data profiling | Missing | No profiling tool or script. |
| Data quality rule catalogue | Partial | Four rule types exist per column (`nullable`, `data_type`, `unique`, worksheet/column presence); no reusable catalogue across contracts, no thresholds. |
| Mapping/transformation templates | Missing | No mapping-specification format; any renaming/mapping is ad hoc in caller code. |
| Reference-data management | Missing | Not built; not yet needed at this scope. |
| Full-load support | **Have** | `LocalSource.items()` always lists everything (`adapters/local.py:35-54`). |
| Incremental-load support | Missing | No cursor/watermark; see [2.2](#22-capture--getting-the-bytes-in-once-safely). |
| CDC support | N/A | No database sources exist; correctly out of scope for now. |
| File change detection | Partial | Content-hash dedup exists (`pipeline.py:116-131`); no delta/change-token query against the source itself. |
| Scheduling and orchestration | Missing | No scheduler, no orchestrator config; `deploy/` is empty. |
| Event triggering | Missing | Not built; batch-only today. |
| Queueing / buffering | N/A | Not needed at current file volumes. |
| Rate limiting / throttling | Missing | No retry or throttle handling anywhere. |
| Retries and backoff | Missing | Every external call is a single attempt. |
| Idempotency and deduplication | Partial | Content-hash dedup at pipeline level (**have**); warehouse-level upsert (**missing** — see [2.4](#24-publish--writing-to-the-destination-safely)). |
| Checkpointing | Partial | File-level: a crash loses at most one file's work (results are appended per file). No run-level checkpoint. |
| Delivery ledger / receipts | Partial | The audit record *is* a delivery ledger for BigQuery; no equivalent receipt concept for external destination APIs (none exist yet). |
| Quarantine / dead-letter storage | **Have** | `pipeline.py:140-141`, `quarantine/{identity}` key, with the original file also kept in `raw/`. |
| Reconciliation | Missing | No source-count vs. destination-count check after a load completes. |
| Backfill and replay | Missing | No rebuild/replay command; manual procedure only (clear the checksum audit key). |
| Lineage and audit trail | **Have** (file-level) | `model.py:75-92`, written by `pipeline.py:215-247`. No run-level record, no code/config version stamped per record. |
| Logging, metrics, alerts | Partial | Plain-text logging exists; no metrics, no alerting. |
| Run-status API/dashboard | Missing | Nothing queries the audit trail for a status view. |
| Secrets and identity management | Partial | `gcp_auth.py` handles *which GCP credential to run as*; the `SecretStore` protocol (`ports.py:140`) has **no implementation** for application secrets (e.g. a SharePoint client secret). |
| Data classification / policy controls | Missing | Not built; the audit trail's "store checksum, not content" design (`pipeline.py:234-247`) is a reasonable privacy default to build on. |
| Environment management | Partial | `config.py` supports named sections (e.g. `dev`/`prod`) with `${VAR}` interpolation; no enforced separation beyond that convention. |
| Infrastructure as code | Missing | No Terraform; `deploy/` is an empty folder. |
| Testing and release controls | **Have** | `pytest`, `pre-commit` (black/ruff/mypy/detect-secrets), GitHub Actions CI (`.github/workflows/tests.yml`). |
| Cost and capacity controls | Missing | Not applicable yet — no deployment exists to size or budget. |

---

## 6. The recommended SharePoint → GCP architecture

This is the standard "first GCP solution" shape for reading SharePoint
and writing to BigQuery, mapped explicitly onto the protocols that
already exist in this repository, so the next piece of work is additive,
not a redesign.

| Need | Recommended implementation | Maps to |
|---|---|---|
| Source (SharePoint) | A Microsoft Graph-based adapter: site/drive IDs, file metadata, and — once volumes justify it — delta queries for change tracking [reference 4](#11-references) | Implement `SharePointSource` against the existing `Source` protocol (`ports.py:14`); it currently raises `NotImplementedError` (`adapters/sharepoint.py:45`). |
| Raw landing | Cloud Storage, immutable per-run/per-object paths | Already the shape of `GCSObjectStore` (`adapters/gcs.py`) via `pipeline.py:152`. |
| Processing | Python, run as a Cloud Run job (batch-to-completion, not a long-lived service) | `Pipeline.run()` (`pipeline.py:53-79`) is already written as a single batch pass suitable for this. |
| Scheduling | Cloud Scheduler triggering the Cloud Run job directly, or via a Workflows definition if more than one step needs coordinating | Not yet built; belongs in the *solution* repo's `deploy/`, using Terraform, not in `py-common`. |
| Operational state (locks, checkpoints) | Firestore, or another approved store — **not** the no-op `GCSObjectStore.lock()` as currently implemented | `ObjectStore.lock()` (`ports.py:77-94`) needs a real implementation before more than one Cloud Run execution can safely overlap. |
| Warehouse | BigQuery, with a genuine `MERGE` on `Contract.key_columns`, not the current `INSERT` | `BigQueryWarehouse.load()` (`adapters/bigquery.py:81-223`) — the `TODO` at line 156 is exactly this piece of work. |
| Secrets | Secret Manager, referenced (never inlined) from configuration | Needs a new adapter implementing `SecretStore` (`ports.py:140-158`); none exists today. |
| Quality | Reusable checks: schema, required fields, uniqueness, valid codes, record counts, freshness | `Contract.validate()` covers the first four; row-count/freshness checks are not built (see [Section 5](#5-feature-by-feature-status)). |
| Monitoring | Cloud Logging (already reachable via Python `logging`) plus named Cloud Monitoring alerts on failure and on staleness | Not built; needs both a structured-logging change and alert policies defined as IaC. |
| Deployment | Terraform modules, run through the existing CI/CD pattern | `deploy/` exists as a placeholder folder only. |

**Why SharePoint should be built as a Graph-based adapter, not a
synced-folder read:** a document library synced to a local/network path
loses the version/eTag metadata that `SourceItem.version`
(`model.py:24-37`) and the `VersionMismatch` safety check
(`errors.py:27-35`, used at `adapters/local.py:76-81`) depend on.
Reading via the Microsoft Graph API keeps that metadata, and is also the
only route to `listItem: delta` change tracking later
[reference 4](#11-references) — which is the eventual answer to the
"list everything, every run" limitation described in
[2.2](#22-capture--getting-the-bytes-in-once-safely).

---

## 7. Choosing features for your scenario

Not every feature above needs building for every project. Use this table
to decide what actually matters for the feed in front of you.

| Scenario | Prioritise | Usually unnecessary at this stage |
|---|---|---|
| Small daily/weekly Excel export from SharePoint (the current HWW shape) | Real `SharePointSource`; the BigQuery `MERGE`; a minimum-row-count check; a real `ObjectStore.lock()`; a failure alert | An orchestrator (Dagster/Airflow); CDC; streaming; a connector catalogue |
| A future API source (marketing/analytics, third-party) | Pagination, retries with backoff, rate-limit handling, raw-response capture, a cursor/watermark | Streaming, unless the API genuinely pushes events and low latency actually matters |
| Writing out to a destination API (not just BigQuery) | An idempotency key, a delivery ledger with receipts, bounded retries, reconciliation | Assuming a 200 response alone proves the record arrived and was stored correctly downstream |
| Several SharePoint libraries/providers using the same pattern | Metadata-driven onboarding (a request file + registry), a mapping-template format, shared quality rules | A bespoke pipeline per source |
| Personal or otherwise sensitive data (this project has adviser names and attendee emails) | Named ownership, least-privilege service accounts, an explicit decision on what SILVER/GOLD may carry in the clear, safe logging (never log payloads) | Nothing here is "usually unnecessary" — treat all of it as required, early |
| A one-off historical migration | Source profiling, a mapping specification, validation, reconciliation, a signed-off record of what ran | Building general-purpose onboarding machinery for a load that happens once |

---

## 8. What sits outside py-common

`py-common`'s scope stops at a validated load into a RAW/staging table.
The following stages matter to the wider Healthy Working Wales platform
but are **not** implemented in, and arguably should not live inside,
this shared library — they belong in downstream solution repos or a
separate transformation project:

- **Employer/organisation identity resolution** (matching inconsistent
  names like "PHW" / "P.H.W" / "Public Health Wales" to one entity) is a
  fuzzy-matching problem, not an ingestion one. If and when this is
  built, **Splink** is worth evaluating specifically because it's
  open-source, UK-government-built for exactly this kind of
  organisation/person matching, and runs in-process (via DuckDB) rather
  than needing its own infrastructure [reference 6](#11-references).
- **SILVER → GOLD transformation and curation** (business rules,
  aggregation, the analytical model Power BI reads from) is a
  transformation-layer concern. **Dataform** is the natural choice if
  BigQuery stays the only warehouse, since it's now bundled into
  BigQuery at no extra licence cost and is Google's own recommended
  path for a single-warehouse setup [reference 7](#11-references).
- **Declarative data-quality rules at the transformation layer**
  (as opposed to the schema/contract checks `py-common` does at
  ingestion) — **Soda Core** (free, YAML-based) or Dataform/dbt-style
  assertions are the usual choices here
  [reference 8](#11-references).
- **Orchestrating several dependent jobs** (ingest → resolve identity →
  build GOLD → publish) is a job for **Dagster** or **Prefect**, not for
  `py-common` itself, and only once there are enough dependent steps to
  justify an orchestrator at all — a single daily batch job does not
  need one, per [Section 3](#3-framework-vs-solution)'s "don't add
  complexity until it's needed" rule.

None of this changes anything you'd build in `py-common` today; it's
listed so the boundary between "ingestion framework" and "everything
downstream of ingestion" stays visible when scoping future work.

---

## 9. Priority order for closing the gaps

Ordered so each step is small, independently testable, and doesn't
require the steps after it.

1. **Fix the BigQuery write to actually merge on `key_columns`**
   (`adapters/bigquery.py:154-166`), and update
   `tests/unit/test_bigquery_adapter.py` to assert an upsert actually
   happened, not just that a query was issued. This is the gap most
   likely to silently duplicate real data.
2. **Implement `SharePointSource`** against the existing `Source`
   protocol (`adapters/sharepoint.py`), using the Microsoft Graph API
   with `Sites.Selected` permission, per the module's own docstring
   (`sharepoint.py:1-14`). Start without delta queries (full listing is
   fine at current volumes); add `listItem: delta` later if/when
   listing the whole library each run becomes a real cost.
3. **Add a minimum-row-count check** to `Contract` (e.g. an optional
   `min_rows` field, default 1) so an empty export is caught rather than
   silently marked `LOADED` with zero rows.
4. **Implement a real `ObjectStore.lock()`** for GCP (Firestore is the
   natural choice, per [Section 6](#6-the-recommended-sharepoint--gcp-architecture)) —
   today's stub (`adapters/gcs.py:128-153`) will not stop two overlapping
   Cloud Run executions from double-processing.
5. **Add a `SecretStore` adapter for Secret Manager**, implementing the
   existing protocol (`ports.py:140-158`), before any credential (a
   SharePoint client secret, in particular) is wired into configuration.
6. **Write the Terraform for the pieces already designed**: the landing
   bucket (with a retention period, not a locked one, since this is
   personal data and retention rules may need to shorten), the BigQuery
   datasets, the Cloud Run job, and the Cloud Scheduler entry. This is
   also what turns `deploy/` from an empty placeholder into something
   real.
7. **Add one failure alert and one freshness view**: a Cloud Monitoring
   log-based alert on any `FAILED` outcome, and a small BigQuery view
   over the audit-record table answering "when did each source last
   load successfully, and is that overdue?" Both are pure configuration
   once the audit trail exists, which it already does.
8. **Only then**, consider incremental/delta loading, row-level
   quarantine, an orchestrator, or anything else in
   [Section 7](#7-choosing-features-for-your-scenario)'s "usually
   unnecessary at this stage" column — each has its own trigger
   condition, and none of them are blocking today's actual project.

---

## 10. Glossary (A–Z quick reference)

For definitions grouped by workflow stage with worked examples, see
[Section 2](#2-the-five-stages-and-the-vocabulary-for-each). This table
is for fast lookup.

| Term | Meaning |
|---|---|
| Access control | Restricting people/services to the minimum data and actions they need. |
| Alerting | Notifying an owner when a meaningful condition needs action. |
| API | A defined way for systems to request/send data over HTTP. |
| Audit trail | Operational evidence of who/what ran a pipeline, when, and with what result. |
| Authentication | Proving a system is allowed to connect. |
| Authorisation | What an authenticated system may then do. |
| Backfill | A deliberate load of older historical data. |
| Backoff / jitter | Increasing, randomised delay between retries. |
| Batch ingestion | Moving a defined set of data at intervals. |
| CDC | Change data capture — reading DB inserts/updates/deletes from a change log. |
| Checkpoint | Persisted progress that only advances after work succeeds. |
| CI/CD | Automated checks and controlled deployment of changes. |
| Configuration | The specific settings one run uses. |
| Connector / adapter | Reusable code that talks to one source or destination type. |
| Cost management | Designing for predictable spend, then monitoring actual use. |
| Cursor / watermark | The saved resume point for the next incremental extract. |
| Data classification | Identifying sensitivity/handling requirements of data. |
| Data governance | The ownership, policy and accountability that make data trustworthy. |
| Data profiling | Examining a source before building a pipeline for it. |
| Data quality checks | Tests deciding whether data may move forward. |
| Dead-letter store | See Quarantine. |
| Deduplication | Detecting/removing repeated records via agreed keys. |
| Delete handling | The policy for a record that disappears from a source. |
| Delivery receipt | Evidence a destination accepted, rejected, or is processing an item. |
| Destination / sink | Where data is sent. |
| Environment | A separate deployment context (dev/test/prod). |
| Full load | Extracting the complete relevant dataset. |
| IaC | Infrastructure as code — cloud resources defined in version control. |
| Idempotency | A retry of the same input doesn't duplicate or corrupt results. |
| Idempotency key | An identifier letting a receiver recognise a retried write. |
| Incremental load | Extracting only what changed since the last successful run. |
| Interface / contract | The agreed rules for exchanging data. |
| Landing zone / raw layer | Storage for the source input in original form. |
| Lineage | A record of where data came from and how it changed. |
| Mapping | A documented source-to-target field/code rule. |
| Metadata | Data about the pipeline and the data. |
| Metadata-driven ingestion | Behaviour driven by validated config, not code changes. |
| Monitoring | Collecting/viewing operational signals. |
| Observability | The ability to understand pipeline behaviour via logs/metrics/alerts. |
| Orchestration | Starting work in the right order, with dependencies and schedules. |
| Pagination | Fetching a large API response in pages. |
| Polling | Repeatedly asking a source if new data exists. |
| Quarantine / dead-letter | A controlled place for invalid/failed data, with a reason and replay path. |
| Reconciliation | Comparing counts/keys across stages to confirm completeness. |
| Recovery / replay | Safely re-running failed or historical work. |
| Retention | How long data/logs/errors are kept. |
| Retry | Re-attempting a temporary failure. |
| Run ID | A unique identifier joining logs/results for one execution. |
| Schema | Expected fields, types, relationships and constraints. |
| Schema drift | An unplanned structural change at the source. |
| Schema registry | Where expected schemas (and versions) are stored. |
| Secrets management | Storing credentials securely, referenced not inlined. |
| Service-level objective (SLO) | A measurable operational expectation. |
| Source data | The actual records/files/messages a source produces. |
| Source system | Where data starts. |
| Staging | A standardised technical working representation of data. |
| Streaming ingestion | Processing individual events continuously/near real time. |
| Target data model | The structure data must follow at its destination. |
| Throttling / rate limiting | Service-imposed limits on request volume. |
| Transformation | Changing data structure or values. |
| Upsert | Update if the key exists; insert if it doesn't. |
| Validation | Checking a request/config/payload/output against defined rules. |
| Versioning | Recording versions of code, config, mappings, schema, data. |
| Webhook | A source calling your endpoint when something happens. |

---

## 11. References

Numbered where cited above; not exhaustive of everything reviewed.

1. `INGESTION_FRAMEWORK_REVIEW_en.md` — a 52-feature review of an
   earlier, more complete sibling of this codebase (uses
   `ingestion/pipeline.py`, `config/sources.yaml` — a different file
   layout from this repository), written 2026-09-16. Reused here for
   its verdict framework (Have / Add now / Deploy / Later / Skip) and
   its external citations; every "Here:" fact in that document was
   re-verified against *this* repository's actual code before being
   restated above, since the two codebases have since diverged.
2. Workshop material: PHW ingestion framework glossary, feature
   catalogue and scenario tables (pasted into this session), and the
   two Word documents `Data_Ingestion_Framework_Updated(3).docx` and
   `Data_Ingestion_Framework_GCP (1).docx`. The former credits its
   source as the webinar *"Scaling your data ingestion using an
   ingestion framework"* (Liz McQuish and Sai Shri Ram Swami,
   Thorogood) and *The Book of OHDSI*, Chapter 6 "Extract Transform
   Load" (Clair Blacketer and Erica Voss) for its profiling, mapping-
   review and maintenance guidance.
3. Airbyte — Microsoft SharePoint connector, including its note on
   Graph API throttling:
   <https://docs.airbyte.com/integrations/sources/microsoft-sharepoint>
4. Microsoft Graph — `listItem: delta`, for incremental SharePoint list
   change tracking:
   <https://learn.microsoft.com/en-us/graph/api/listitem-delta> (see
   also the Airbyte reference above, which documents delta-query use in
   practice)
5. Google Cloud — Cloud Run jobs on a schedule, and Cloud Storage
   retention policies / Bucket Lock:
   <https://cloud.google.com/run/docs/execute/jobs-on-schedule>,
   <https://cloud.google.com/storage/docs/bucket-lock>
6. Splink — open-source probabilistic record linkage, built and used in
   production by the UK Ministry of Justice:
   <https://moj-analytical-services.github.io/splink/>
7. Google Cloud — Dataform, bundled into BigQuery:
   <https://cloud.google.com/dataform/docs/overview>
8. Soda Core — open-source, YAML-based data quality checks:
   <https://docs.soda.io/soda/core-concepts.html>
9. NHS RAP community of practice — levels of Reproducible Analytical
   Pipelines (Baseline/Silver/Gold), used above as the maturity
   yardstick for CI/CD and scheduled triggers:
   <https://nhsdigital.github.io/rap-community-of-practice/>

All file:line citations elsewhere in this guide point at
`C:\Users\eoinv\Downloads\take5\py-common` as it stood on 2026-09-20;
re-check them against current code before relying on them, since this is
a working repository and will keep changing.
