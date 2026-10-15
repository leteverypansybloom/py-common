# Full-codebase review: py-common 0.2.0

Reviewed on **15 October 2026**, against the on-disk repository at `C:\Users\eoinv\Downloads\take5\py-common`. Git HEAD: `b2f9059` ("Redo the smoke test for 0.2.0"). This review covers the whole tracked codebase, including `src/`, tests, scripts, container runtime, workflows, packaging, and documentation; it is not a diff review. Repository-relative `file:line` citations refer to this snapshot. Generated build copies, virtual environments, caches, and historical findings in `REVIEW.md` are not treated as maintained source.

## Orientation — purpose, users, and data flow

`py-common` is a shared Excel ingestion library for the Public Health Wales data engineering team and solution repositories such as Healthy Working Wales. Developers supply workbook contracts and wire source, storage, and warehouse adapters together. The documented users downstream are analysts and Power BI consumers; employer identity resolution and reporting models belong to the solution's transformation layer. Sources are local/network folders, a GCS landing folder, or a SharePoint library accessed through Microsoft Graph. [Evidence: `README.md:3`, `docs/INGESTION_FRAMEWORK_GUIDE.md:3`, `docs/RAW_JSON_VS_CONTRACT_VALIDATION.md:12`.]

The intended main flow is **sources → versioned Excel bytes → contract reading and column selection → immutable original and processed copies in GCS → worksheet rows as raw JSON in BigQuery → Dataform validation, typing, quarantine, deduplication and modelling → reporting tables / Power BI**. An alternative route converts the primary worksheet to typed Parquet, stages it, and merges into a BigQuery table, or appends when no merge keys are configured. Raw rows append; staging loads truncate only their own temporary tables. Data writes and successful warehouse audit rows share a transaction. [Evidence: `pipeline.py` below is `src/py_common/pipeline.py`: `:194`, `:206`, `:446`; `src/py_common/adapters/bigquery_raw.py:248`, `:362`; `src/py_common/adapters/bigquery.py:215`, `:262`; `docs/RAW_JSON_VS_CONTRACT_VALIDATION.md:20`.]

The git history runs from initial framework work through live GCP adapters, Graph discovery, configurable headers, raw JSON, and runner helpers. Today's commits add fail-closed storage, warehouse audit deduplication, key checks, typed Parquet, schema reconciliation, lease locks, isolated staging, and JSON logging (`6101b32` through `f95c2c5`). These changes are present in the reviewed source; earlier review findings were not assumed still applicable.

## Executive summary

- **Overall health: C / 5 out of 10 — a credible early-development library for controlled, small Excel feeds, with production correctness and recovery work still needed.** The protocol boundaries and recent improvements are useful foundations. [Evidence: `src/py_common/ports.py:14`, `:48`, `:98`; H01–H07 below.]
- **A typed restore can silently do nothing.** The A → B → A sequence is reported as loaded, loaded, skipped because deduplication remembers all historical content, rather than the logical upload being retried. [H01.]
- **An empty raw replacement leaves old data looking current.** There is no per-sheet upload manifest, so downstream SQL cannot distinguish "latest upload has zero rows" from "no replacement arrived." [H02.]
- **The exactly-once claim is stronger than the implementation.** A still-running commit can outlive the lock, and read-then-append transactions do not establish uniqueness. Changing a raw warehouse's table set also changes the deduplication scope. [H03, H04.]
- **The raw-to-Dataform contract is documented, not enforced end to end here.** In listed/warn mode, a missing source header becomes a present JSON key with null values. The main SQL example provides neither the source-header gate nor row quarantine. A deployed solution may add these; that is **unverified**. [H05.]
- **The recommended alerts can miss real failures.** Their example filters `textPayload`, while the recommended logger emits structured JSON; the failure text list also omits handled warehouse and audit errors. [H06.]
- **Verification is substantially green, but narrower than the coverage number suggests:** 445 passed, 2 skipped, 6 deselected; 96% statement coverage; Ruff and strict mypy passed. Fourteen extra offline scenarios exposed gaps. `pip-audit` was unavailable, so a full installed-dependency vulnerability assessment remains **unverified**. [Verification section; M15.]
- **No Critical finding or committed private key/token was identified in the reviewed text.** Remaining security work includes validating SQL identifiers, restricting authenticated pagination URLs, avoiding data values in logs, tightening allowed dependency versions, and reviewing uploader permissions. This is not a credential-history or deployed-IAM audit. [M11–M14, M16.]

## Findings table

Severity reflects impact under the stated trigger, not a claim that every deployment is affected. **Verified offline** means observed against this snapshot without cloud access. **Code-confirmed** means visible control flow/configuration. Cloud outcomes not exercised are explicitly marked **unverified**.

| ID | Severity | Area | file:line | Issue | Recommendation |
|---|---|---|---|---|---|
| H01 | High | Correctness / idempotency | `src/py_common/pipeline.py:350`, `:356`; `src/py_common/adapters/bigquery.py:280` | Typed dedup is destination + historical checksum. A → B → A skips the restore, even after deleting store markers. **Verified offline.** | Deduplicate logical deliveries by namespaced source identity + version; apply an explicit ordering policy to typed updates. Migrate both cache and warehouse keys. |
| H02 | High | Data engineering / consumers | `src/py_common/raw.py:114`; `src/py_common/adapters/bigquery_raw.py:248`, `:275`; `docs/RAW_TABLE_LAYOUT.md:123` | Zero-row sheets produce no raw records or sheet-level version record. A successful empty replacement leaves the previous generation selected; an all-empty first load may not create any raw table. **Verified offline.** | Commit a manifest for every configured sheet, including zero rows, and select latest versions from it; otherwise explicitly quarantine empty snapshots. |
| H03 | High | GCP / concurrency / recovery | `src/py_common/adapters/_bq_jobs.py:61`, `:150`, `:203`; `src/py_common/adapters/gcs.py:257`; `src/py_common/pipeline.py:588` | An indeterminate, still-running commit does not retain ownership or a resumable job identity. A retry can start a second append before the first commits. Leases have no renewal/fencing. Code path confirmed; concurrent live duplication **unverified**. | Resume the same deterministic commit job; persist unresolved job state; serialize/fence commit ownership and keep staging until terminal reconciliation. Do not rely on a SELECT inside an append-only transaction for uniqueness. |
| H04 | High | Data engineering / API design | `src/py_common/adapters/bigquery_raw.py:134`, `:280`, `:374` | Raw identity is the sorted *set of target tables*. Adding a table reloads existing targets; swapping sheet-to-table assignments keeps the same identity and may skip the new mapping. **Verified offline.** | Keep locking identity separate from materialization/dedup identity. Track delivery completion per target and mapping version; migrate existing-target ledger entries when expanding a pipeline. |
| H05 | High | Data quality / Dataform contract | `src/py_common/contract.py:660`, `:671`; `docs/RAW_TABLE_LAYOUT.md:145`, `:155`; `docs/RAW_JSON_VS_CONTRACT_VALIDATION.md:46` | Missing source headers become present null-valued JSON keys, so JSON_KEYS cannot recover source schema drift. The main Dataform example lacks a blocking source-schema gate and row quarantine. Header masking **verified offline**; deployed safeguards **unverified**. | Publish observed/expected headers and contract version in a manifest; require explicit quality dependencies before publishing; implement row quarantine in the solution repo. |
| H06 | High | Reliability / observability / docs | `docs/GCP_SETUP.md:499`, `:515`; `src/py_common/logging.py:123`; `src/py_common/pipeline.py:601`, `:662` | Alert example searches `textPayload`; recommended logs retain message and context as structured JSON. Even text alerts miss `Warehouse service error` and `Audit write failed` because these paths do not emit the listed per-file failure phrase. Code/JSON mismatch confirmed; deployed alert delivery **unverified**. | Alert on structured `outcome="failed"`, plus uncaught run/freshness failures, warnings and quarantines; emit final failures at ERROR; exercise the alert policies with synthetic logs. |
| H07 | High | Correctness / strict data validation | `src/py_common/contract.py:519`; `src/py_common/parquet.py:76`, `:152`; `src/py_common/pipeline.py:207` | A non-nullable integer in quarantine mode can validate as Python int yet overflow INT64. Conversion then replaces it with NULL and only warns, allowing the load. **Verified offline** with 9223372036854775808 in XLSX XML. | Validate destination representability before conversion, and make conversion obey quarantine/warn policy. Route conversion ValidationErrors through quarantine handling. |
| M01 | Medium | Data engineering / lineage | `src/py_common/adapters/gcs.py:463`; `src/py_common/adapters/local.py:80`; `src/py_common/raw.py:118`; `docs/RAW_TABLE_LAYOUT.md:127` | Source identity omits bucket/root namespace; raw metadata retains only basename. Different folder identities with the same name/version/checksum generate identical raw metadata; latest-per-basename SQL can conflate independent files. **Verified offline.** | Add stable `source_id` and full `source_uri` to SourceItem, raw records, manifests and dedup keys. Group versions by full identity. |
| M02 | Medium | Backfill / late data / docs | `src/py_common/adapters/gcs.py:453`, `:503`; `src/py_common/runner.py:244`; `docs/INGESTION_FRAMEWORK_GUIDE.md:179` | Discovery lists live objects only, and download refuses an older retained generation. Upload events trigger a fresh listing rather than fetching their generation. Intermediate versions can therefore be missed. Clearing a store marker no longer forces replay because the warehouse ledger still skips it. | Add an explicit generation-addressed replay/backfill source and a controlled rematerialization mode; correct the obsolete marker-deletion runbook. |
| M03 | Medium | Reproducibility / schema evolution | `src/py_common/pipeline.py:186`, `:218`, `:534`; `src/py_common/model.py:87` | Dedup and processed keys omit contract/converter version. A corrected contract cannot reprocess the same delivery; bypassing markers can instead hit immutable processed-byte conflicts. Persisted audits omit code/config versions. | Version materializations separately from source delivery and include a contract/converter fingerprint in processed keys and audit metadata. Provide an explicit migration/rebuild path. |
| M04 | Medium | Correctness / headings | `src/py_common/contract.py:598`, `:609`, `:614` | A synthesized blank-header name can take a real later header's name. `[blank, col_A]` becomes `[col_A, col_A_2]`, routing a contract's `col_A` to the wrong source column. **Verified offline.** | Reserve all written headings when allocating synthetic names, including the initial synthetic candidate. Add mixed synthetic/explicit collision fixtures. |
| M05 | Medium | Correctness / quarantine / evidence | `src/py_common/contract.py:303`, `:378`; `src/py_common/pipeline.py:194`, `:206`, `:248` | Read-only worksheet parsing errors can arise after `_open()` returns. A malformed numeric cell becomes FAILED with no raw copy, instead of a quarantined invalid workbook. **Verified offline.** | Normalize expected XLSX/XML read errors into ContractError across the full parse; preserve received bytes before parsing, subject to storage success. |
| M06 | Medium | Correctness / local source | `src/py_common/adapters/local.py:103`, `:111` | Local version checking happens only before opening/reading. A write after that check is accepted under the old version; the probe read 11 bytes for a 3-byte discovered item. **Verified offline.** | Open once, compare descriptor metadata before and after reading and the named file's identity; enforce a completed-file publishing convention for network folders. |
| M07 | Medium | Runners / malformed JSON | `src/py_common/runner.py:175`, `:179`, `:228`; `src/py_common/adapters/bigquery_raw.py:315` | Upload parsing checks keys but not field types: `name:null` escapes as AttributeError and is retried. Routing ignores the event bucket. Raw NDJSON similarly permits non-string sheet names to fail outside its documented ValueError normalization. | Validate JSON object shapes, non-empty string bucket/name/generation, sheet_name and row metadata. Route by bucket + folder and reject mismatched sources. Null-name failure **verified offline**. |
| M08 | Medium | Performance / memory | `src/py_common/contract.py:582`, `:620`, `:666`; `src/py_common/raw.py:114`, `:136`; `src/py_common/adapters/bigquery_raw.py:311`, `:262` | Read-only workbook mode still materializes complete row grids, selected rows, NDJSON strings, joined bytes, split lines and per-sheet joined bytes. Sources also download full files before checking duplicates. | Bound workbook/expanded ZIP size; batch rows into file-like outputs or GCS objects and load from URI; add a version-manifest shortcut before download. Peak memory at production sizes is **unverified**. |
| M09 | Medium | GCP cost / partitioning | `docs/RAW_TABLE_LAYOUT.md:23`, `:123`; `src/py_common/adapters/_bq_jobs.py:204`; `src/py_common/adapters/bigquery.py:324`, `:458`; `docs/GCP_SETUP.md:542` | Latest-generation SQL has no ingestion partition predicate and must consider historical uploads; its window/cast does not establish the claimed block pruning. Audit lookups are unclustered by the guide, and typed MERGEs have no partition pruning policy. No explicit query budget is exposed. | Use a small current-upload manifest, cluster the durable ledger appropriately, design typed partition/key clustering per workload, and expose job cost controls/labels. Actual bytes billed **unverified**. |
| M10 | Medium | Schema drift / warehouse validation | `src/py_common/adapters/bigquery_raw.py:337`; `src/py_common/adapters/bigquery.py:327`, `:337` | Existing raw tables are accepted on existence alone. Typed reconciliation compares scalar type names but ignores modes and required `_loaded_at`; created fields discard nested/mode metadata. Incompatible pre-existing tables fail later as service errors rather than clear schema errors. | Validate complete supported raw/final/audit schemas at startup, including reserved metadata columns and modes; reject unsupported nested/repeated Parquet explicitly. Test required/repeated fields and legacy tables. |
| M11 | Medium | Security / SQL construction | `src/py_common/adapters/_bq_jobs.py:205`, `:212`; `src/py_common/adapters/bigquery.py:448`; `src/py_common/adapters/bigquery_raw.py:386` | Data values are parameterized, but configured identifiers are interpolated without centralized validation/escaping. `audit_table_id` enters commit SQL directly. This is a configuration trust boundary, not demonstrated upload-driven SQL injection. Exploitability **unverified**. | Validate project/dataset/table identifiers at construction; quote supported column identifiers through one helper. Reject backticks, statement separators and malformed IDs before any SDK call. |
| M12 | Medium | Security / credentials | `src/py_common/adapters/sharepoint.py:531`, `:577` | An arbitrary absolute `@odata.nextLink` is followed with a newly attached Graph bearer token. **Verified offline** using a fake foreign URL and synthetic token. Attacker influence over a real Graph response is **unverified**. | Require HTTPS and the configured Graph origin/base path for pagination; treat download redirects separately so legitimate preauthenticated file URLs remain supported without forwarding the Graph token. |
| M13 | Medium | Security / logs | `src/py_common/contract.py:498`; `src/py_common/parquet.py:211`; `src/py_common/adapters/bigquery.py:414`; `src/py_common/pipeline.py:215`, `:408` | Duplicate composite-key values and a single rejected typed value are embedded in messages that reach logs/audits. A pasted email or personal identifier can escape the restricted data store into operational logs. Actual sensitive-data exposure **unverified**. | Log row/column/error code/count rather than values; keep sensitive examples only in restricted quarantine records. Review chained token-provider exceptions before logging tracebacks. |
| M14 | Medium | Security / dependency reproducibility | `pyproject.toml:41`, `:51`; `containers/runtime/Dockerfile:39`, `:47`; `containers/speciality_groups/edge/python-dependencies.txt:1` | Requests has no upper bound and permits known-affected versions below 2.32.4. Upper bounds on other direct dependencies are not a deployment lock; container Python/R dependencies and base images remain floating. | Raise the Requests security floor, define a tested compatibility range, and lock deployed Python/R/OS artifacts separately from library compatibility ranges. Audit both Python and built images. Installed Requests is 2.34.2; full installed-package audit **unverified**. |
| M15 | Medium | Testing / semantic coverage | `tests/unit/_fake_bigquery.py:133`, `:179`; `tests/integration/test_bq_dry_run.py:195`; `tests/integration/test_live_services.py:78`, `:87`, `:96`; `tests/integration/test_gcs_bigquery_integration.py:60` | Fake MERGE adds row counts rather than upserting values and always accepts merge keys. Script dry runs do not validate IF bodies. Several live tests always skip or leave the warehouse call commented out; two cloud tests lack integration markers. High coverage does not prove real commit/recovery behavior. | Add value-asserting sandbox tests for actual MERGE, rollback, two concurrent deliveries, empty snapshots, mapping changes and schema errors; mark all live tests. Dry-run extracted DML separately. |
| M16 | Medium | IAM / data access / docs | `docs/GCP_SETUP.md:22`, `:191`, `:201` | The resource overview describes uploaders as write-only, but the provided binding grants `storage.objectUser`, which also permits reading/deleting objects. This broadens access to sensitive landing files and historical versions. Actual deployed bindings **unverified**. | Specify the intended uploader threat/access model; use scoped custom permissions or an upload service, separate source/team boundaries, and protect archival versions independently. |
| M17 | Medium | CI/CD / docs vs reality | `containers/.github/workflows/build-runtime.yml:36`, `:41`, `:129`; `containers/readme.md:209` | The runtime workflow is nested under `containers/.github`, so GitHub will not discover it in this repository. Its paths assume containers is the repo root; its runtime-change matrix rebuilds only two of five groups. | Move/adapt the workflow to root `.github/workflows`, fix build context/path filters, and derive the full group matrix from directories. If this is only vendored reference material, label and maintain it as such. Deployment use **unverified**. |
| M18 | Medium | Runtime security / reproducibility | `containers/runtime/pipeline_executor.py:33`, `:41`, `:105`; `containers/runtime/Dockerfile:57` | A fixed container revision downloads and executes mutable `latest.zip` with the job's credentials, without recording/pinning its generation or digest. Code-bucket writers effectively control job execution; no explicit non-root USER is configured. | Accept an immutable package generation/digest or bake reviewed code into the image; log/verify provenance, tightly control code-bucket writes, and run as non-root. No compromised package is alleged. |
| M19 | Medium | Runtime reliability / performance | `containers/runtime/pipeline_executor.py:105`, `:112`, `:126`, `:140` | R output is buffered entirely until process exit, with no subprocess timeout. Long/noisy or stuck analyses can exhaust memory and withhold diagnostic progress; download failures before returning the temp directory can leak it, and cleanup can override the intended result. | Stream bounded structured output; set execution deadlines and terminate child processes; own TemporaryDirectory at the caller; preserve the primary exception when cleanup fails. Runtime execution **unverified**. |
| M20 | Medium | Local storage correctness | `src/py_common/adapters/local.py:193`, `:202`, `:206` | Local immutable writes use exists/read followed by a non-exclusive write, and the lock is a no-op. Concurrent writers can overwrite each other or expose partial bytes, despite the ObjectStore contract. | Use exclusive creation plus atomic publication/verification and a real local lock, or explicitly restrict the adapter to single-process test use throughout its API/docs. Race outcome **unverified live**. |
| M21 | Medium | Configuration / encoding | `src/py_common/config.py:77`, `:79`; `src/py_common/contract.py:85`, `:149` | YAML can return a truthy list/scalar despite the promised dict; text encoding depends on the platform. Contract construction does not reject duplicate worksheet/column names or unknown data_type strings, so errors/data overwrites surface late. | Read UTF-8 explicitly, require a mapping root, and validate unique names and supported types at construction; keep optional values strongly typed instead of stringifying malformed configuration. |
| M22 | Medium | Reliability / lineage | `src/py_common/model.py:87`; `src/py_common/pipeline.py:106`, `:680`; `src/py_common/adapters/_bq_jobs.py:255` | Persisted file audit records lack the log run_id and BigQuery job IDs; warehouse timestamps are supplied by the client before query execution, rather than server commit time. Historical audits alone cannot reconstruct code/config/run/job provenance or exact commit timing. | Persist run_id, materialization version, commit job reference and distinct source/load timestamps. Use one server timestamp in the transaction for warehouse ingestion metadata. |
| M23 | Medium | Documentation / deployment guidance | `docs/INGESTION_FRAMEWORK_GUIDE.md:337`, `:341`, `:352`; `docs/GCP_SETUP.md:569`; `docs/upcoming_work.md:147`, `:152`, `:379` | The advertised deep reference still says no run ID/JSON logging and non-expiring locks. Freshness docs say identical new uploads are skipped, contradicting raw version dedup. The older CSV prototype reads current bytes but labels them with the event generation and uses string MAX for generations. | Reconcile the deep reference with 0.2.0; archive or prominently label the prototype; download the named generation and order it numerically if retaining it. Keep one authoritative operational runbook. |
| L01 | Low | Legacy authentication script | `scripts/gcp_auth.py:104`, `:108`; `docs/LOCAL_GCP_AUTHENTICATION.md:16` | Windows ADC discovery omits the `gcloud` directory, so the documented default ADC location is not found by the legacy helper. Its module usage example also imports a removed package path. | Retire the unused duplicate helper or delegate to google.auth.default; correct examples while it remains. Production adapters already use ADC directly. |
| L02 | Low | Dead code / consistency | `src/py_common/adapters/bigquery_raw.py:291`, `:293`; `.github/workflows/tests.yml:37`; `scripts/gcp_auth.py:105` | The raw success debug log follows an unconditional return and is unreachable. Lint gates omit scripts/runtime; an additional read-only Ruff run found four issues there. | Move/delete the unreachable log and apply consciously scoped checks to maintained scripts/runtime. These lint issues are maintenance noise, not correctness blockers. |
| L03 | Low | Windows / portable object keys | `src/py_common/pipeline.py:354`, `:531`; `src/py_common/adapters/local.py:190` | Raw versions are inserted verbatim into filesystem keys. Quoted SharePoint eTags are invalid Windows filename components; long nested raw paths also failed six local tests until extended-length paths were used. | Hash/encode opaque version components and keep a short local root or support extended-length paths. Test the documented Windows workflow with realistic eTags. Path-length failure observed; live SharePoint case **unverified**. |

## Detailed High findings and suggested fixes

The following snippets are proposed changes, not patches applied to the repository. Changes to delivery identity or persisted metadata require a migration and tests; simply changing the code can otherwise reload historical data.

### H01 — Historical content deduplication is not logical-delivery idempotency

**Trigger and impact.** A typed file updates a key's value from A to B; a user then deliberately restores the original A bytes as a new upload. `_loaded_marker_key()` falls back to destination/checksum (`src/py_common/pipeline.py:356`), so A is skipped before validation. Deleting the marker is insufficient: the warehouse uses `dedup_columns=["checksum"]` (`src/py_common/adapters/bigquery.py:281`). B remains current for overlapping keys. This also matters when another source file has since updated those keys. The claim that an identical MERGE would change nothing is only valid when the destination has not changed since that content was loaded.

**Evidence.** The actual pipeline and BigQuery adapter, using the repository's stateful fake client, returned `[loaded, loaded, skipped]` for A → B → A, and still returned skipped after removing destination markers. That establishes the skip path; the fake does not model actual MERGE values, so resulting BigQuery values were not verified live. The existing typed test only exercises A → A (`tests/unit/test_idempotent_loads.py:261`).

**Suggested fix.** Define a delivery by stable source namespace, item identity and version, separate from content integrity. Make both warehouse and cache use that delivery identity. Keep a separately named content-based policy only for feeds explicitly declaring that any historical content is permanently redundant.

```python
# Conceptual API: SourceItem gains a stable source_id.
delivery_id = digest(json.dumps(
    [item.source_id, item.identity, item.version],
    separators=(",", ":"),
).encode("utf-8"))
marker_key = f"audit/{destination}/deliveries/{delivery_id}"
# Persist delivery_id in the warehouse audit schema as well.
transaction_sql = commit_script(
    [merge_sql], audit_full_id, dedup_columns=["delivery_id"]
)
```

Add a value-asserting A → B → A test and another where a different file updates the same business key between two deliveries. Decide explicitly how historical backfills compare with newer authoritative updates. Changing dedup alone does not add snapshot deletion semantics: the present MERGE updates/inserts, and never deletes removed source rows (`src/py_common/adapters/bigquery.py:461`).

### H02 — Empty snapshots need a manifest independent of data rows

**Trigger and impact.** Generation 1 contains rows; generation 2 has valid headings but zero data rows. `to_ndjson()` writes nothing for the empty sheet (`src/py_common/raw.py:115`), `_split_by_sheet()` cannot discover it, and `_stage_and_commit()` inserts only the audit record. The documented latest-generation query examines raw rows, so it continues serving generation 1. With a multi-sheet workbook, one cleared sheet can retain stale data even while another sheet correctly advances. A first all-empty upload can be marked loaded without creating its raw table.

**Evidence.** The offline raw harness recorded both versions as successful while raw row count stayed one. The existing unit test explicitly accepts a zero-row audit-only load (`tests/unit/test_bigquery_raw_adapter.py:483`), but does not test what consumers see afterward.

**Suggested fix.** Extend the warehouse load input to carry every sheet, observed schema and row count, even when its NDJSON is empty. Commit one manifest row per sheet atomically with raw rows and audit. Downstream selection must start from that manifest:

```sql
-- Proposed schema includes stable source_id, sheet_name and delivery_id.
WITH latest AS (
  SELECT source_id, sheet_name, delivery_id
  FROM `project.raw.upload_manifest`
  WHERE outcome = 'LOADED'
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY source_id, sheet_name
    ORDER BY source_order DESC, delivery_id DESC
  ) = 1
)
SELECT r.*
FROM `project.raw.sheet_rows` AS r
JOIN latest USING (source_id, sheet_name, delivery_id)
```

`source_order` must be defined by the source; ingestion time alone lets a late backfill become current. For GCS uploads it can be the numeric generation for that object. For SharePoint, an opaque eTag is not a sortable version. If empty snapshots are invalid for a feed, a configurable minimum-row rule is a useful immediate safeguard; it must not silently preserve old rows while reporting a successful empty replacement.

### H03 — A detached commit can survive lock release and race its retry

**Failure sequence.** `run_query()` starts a new query without a supplied job ID (`src/py_common/adapters/_bq_jobs.py:61`). If waiting fails and reload reports RUNNING, it raises IndeterminateCommitError (`:150`). The pipeline turns the file into FAILED; normal scope exit releases the GCS lock (`src/py_common/pipeline.py:248`; `src/py_common/adapters/gcs.py:260`). The original server job was not made terminal or durably attached to the next attempt. A retry can therefore submit a new transaction while the old one is still active. Cleanup also attempts to drop its staging tables before the outstanding job is reconciled (`src/py_common/adapters/bigquery_raw.py:220`).

The audit check is a SELECT followed by INSERTs, not a uniqueness constraint. **Inference from documented BigQuery semantics:** two raw transactions can both read "not loaded" and append. Google's [transaction concurrency documentation](https://docs.cloud.google.com/bigquery/docs/transactions) permits concurrent read/append transactions. A keyed typed MERGE can encounter mutation conflicts; that does not establish safety for the raw append path. **A concurrent live-GCP reproduction was not run.**

The lease also has no renewal or fencing token. The claim that a Cloud Run service timeout proves the holder dead (`src/py_common/adapters/gcs.py:81`; `docs/GCP_SETUP.md:389`) is incorrect: Google's [request-timeout documentation](https://docs.cloud.google.com/run/docs/configuring/request-timeout) says a timed-out request's container can continue processing. Expiration can therefore admit another writer while the original still runs.

**Suggested fix.** Persist an attempt/commit-job reference before submission and resume a stable logical commit job on retries. Stable job identity must account for terminal failure versus unknown/running state, retain the same staged inputs, and be reconciled before a new attempt is allowed. For durable concurrency protection, use a fenced ledger/guard mutation or external transactional coordinator; a lock in an arbitrary store bucket is not a warehouse uniqueness constraint.

```python
# Sketch: persisted commit_state owns immutable staging inputs and attempt ID.
job_id = f"py_common_commit_{commit_state.attempt_id}"
try:
    job = client.query(
        sql, job_config=config, job_id=job_id,
        location=location, job_retry=None,
    )
except Conflict:
    job = client.get_job(job_id, location=location)

# On timeout/transport loss, persist unresolved state and resume THIS job.
# Do not drop staging or authorize another commit until it is terminal.
```

Job IDs alone are not a perpetual deduplication ledger. Keep the durable delivery audit, require one shared coordinator, and test process death, still-running jobs, lease takeover, failed submissions and delayed responses with real sandbox jobs. Longer lease duration by itself does not fix this race.

### H04 — Whole-warehouse identity is unstable for per-table delivery state

**Trigger and impact.** A warehouse initially maps `Events → raw_events`. Adding `Other → raw_other` changes identity from `p.d.raw_events` to `p.d.raw_events+raw_other`. The original Events delivery now has a new destination in both marker cache and audit comparison, so it inserts into raw_events again. This occurs even if the added sheet has no rows in this file. Conversely, swapping two sheet assignments preserves the sorted target set, leaving the same identity and an old marker that skips the desired new mapping.

**Evidence.** The offline probe loaded the same file/version, expanded the mapping and loaded again: the existing table row count increased from one to two. A second probe confirmed swapped mappings have identical identities. Per-load staging correctly avoids staging collisions, but does not solve this delivery-state problem (`src/py_common/adapters/_bq_jobs.py:301`).

**Suggested fix.** Give each target materialization a stable identity incorporating the sheet-to-target mapping and processing version. Maintain per-target ledger state, while keeping a distinct lock/coordinator key for an overlapping write set. When adding sheets, migrate completion state for unchanged targets instead of re-keying their history.

```python
target_id = f"{project}.{dataset}.{table}"
materialization_id = digest(json.dumps(
    [target_id, sheet_name, contract_version],
    separators=(",", ":"),
).encode("utf-8"))
# In one transaction: insert only missing target-delivery pairs,
# write their sheet manifests, then record file-level completion.
```

Hashing the complete mapping is better than ignoring it, but **does not by itself fix duplicate rows when the mapping expands**. Include tests for expanding, shrinking, swapping and overlapping target mappings.

### H05 — Source schema evidence disappears before the documented Dataform checks

**Trigger and impact.** A source renames nullable column `expected` to `renamed`. Listed/warn mode logs a warning, then `_select()` outputs the expected key with None (`src/py_common/contract.py:660`, `:671`). The offline JSON payload was exactly `{"expected": null}`. A JSON_KEYS assertion sees the expected schema, and cannot distinguish a renamed heading from a genuinely blank column. A nullable field can silently become all-null without failing nonNull or uniqueness assertions on a different key.

The current raw-layout model includes assertions on participant_id but no observed-header validation, explicit downstream assertion dependencies, or bad-row quarantine (`docs/RAW_TABLE_LAYOUT.md:155`). The older prototype does explicitly depend on `validate_columns` (`docs/upcoming_work.md:423`); it should not be described as wholly ungated. However, its payload-key check is not enough for the current listed projection, and it is not a maintained, tested Dataform project in this repository. **Whether the real HWW solution supplies stronger gates is unverified.**

Google's [Dataform dependency documentation](https://docs.cloud.google.com/dataform/docs/dependencies) also confirms that simply depending on a table does not make that table's assertion failures block its dependents. A ref without assertion dependencies is not a publication gate.

**Suggested fix.** Export the observed original headers, expected headers, missing/unexpected headers, sheet presence, counts and contract version as a manifest. Gate each delivery/sheet, including zero-row sheets, before flattening/publishing; test nullable renamed fields and mixed good/bad files. Keep this transformation implementation in the solution repository, with its contract version tested against this library.

```sql
-- Proposed assertion over upload_manifest, not projected payload keys.
config { type: "assertion" }
SELECT source_id, delivery_id, sheet_name
FROM ${ref("current_upload_manifest")}
WHERE ARRAY_LENGTH(missing_headers) > 0
   OR ARRAY_LENGTH(unexpected_headers) > 0
```

```sql
-- Consumer example: explicitly gate publication.
config {
  type: "table",
  dependencies: ["assert_source_headers"],
  dependOnDependencyAssertions: true
}
SELECT * FROM ${ref("validated_staging")}
```

Add a quarantine model that preserves each invalid row's delivery ID, row number and reason. SAFE_CAST returning NULL is a mechanism for detecting bad values; it is not itself a quarantine or publication policy.

### H06 — Logging and alert configuration no longer agree

**Two independent gaps.** The recommended `configure_json_logging()` outputs severity, message, logger, time and context (`src/py_common/logging.py:123`). The alert JSON still searches `textPayload:"Contract warning:"` (`docs/GCP_SETUP.md:515`), so it does not target that structured message. Google's [structured logging documentation](https://docs.cloud.google.com/logging/docs/structured-logging) describes the JSON payload field model. The actual Python JSON emission was verified offline; Cloud Logging ingestion and notification delivery were not exercised.

Separately, `_load_to_warehouse()` catches ServiceError, logs `Warehouse service error`, and returns FAILED without entering the outer `Processing error for` exception path (`src/py_common/pipeline.py:596`). Audit persistence failures log `Audit write failed`. The documented failed-run phrases (`docs/GCP_SETUP.md:499`) omit both. The per-file summary includes outcome but is emitted at INFO (`src/py_common/pipeline.py:134`).

**Suggested fix.** Emit final file failures at ERROR with structured outcome and identifiers. Use outcome-based alert filters and separate stable event fields for run/freshness failures rather than relying on incidental exception text.

```python
level = logging.ERROR if result.outcome == Outcome.FAILED else logging.INFO
logger.log(level, "%s %s", result.item.name, result.outcome.value,
           extra=_fields(outcome=result.outcome.value,
                         checksum=result.checksum, rows=result.rows_processed))
```

```text
((resource.type="cloud_run_job" AND resource.labels.job_name="JOB") OR
 (resource.type="cloud_run_revision" AND resource.labels.service_name="SERVICE"))
AND (
  jsonPayload.outcome="failed" OR
  jsonPayload.message:"Pipeline run failed" OR
  jsonPayload.message:"Freshness check failed"
)
```

For contract warnings, target `jsonPayload.message:"Contract warning:"`; for quarantines, emit/filter the final structured outcome. Test a warehouse 403, an audit write failure, a lock refusal, a warn-mode header change and a missing monthly source. Do not treat successful job execution as sufficient: quarantines intentionally do not fail the job (`src/py_common/runner.py:124`).

### H07 — Strict validation can still write a required value as NULL

**Trigger and impact.** The strict validator checks `isinstance(value, int)` but not INT64 range (`src/py_common/contract.py:519`). `_to_integer()` knows the range and raises `_Unconvertible` (`src/py_common/parquet.py:152`), but the conversion loop always appends None (`:80`), regardless of worksheet policy or nullable=False. The new final table is created with nullable fields (`src/py_common/adapters/bigquery.py:328`), so the warehouse need not reject this. There is no minimum valid-value check after conversion.

**Evidence.** A minimal XLSX with integer text `9223372036854775808` in a numeric XML cell passed Contract.read() for a non-nullable integer in default quarantine mode. Reading its resulting Parquet produced `value: null`, with only a warning. This is an exported-XLSX edge case, not a claim that Excel normally preserves arbitrary precision beyond its own numeric limits.

**Suggested fix.** Enforce representability in the validation stage, where Pipeline already catches ValidationError and quarantines. Also make the converter fail closed if used independently in quarantine mode.

```python
# During contract validation, alongside the basic type check:
if col_def.data_type == "integer" and valid:
    valid = -(2**63) <= value <= 2**63 - 1
if not valid:
    row_errors.append(
        f"Row {row_idx}: '{col_def.name}' cannot be represented as INT64"
    )
```

```python
# In the converter's _Unconvertible handler:
if worksheet.on_mismatch == "quarantine":
    raise ValidationError(
        f"{source_name} [{worksheet.name}] row {row_number}: "
        f"'{sheet.columns[index]}' cannot be converted to {data_type}"
    ) from None
```

The second change also needs Pipeline to catch conversion ValidationError and call `_quarantine_invalid`, rather than treating it as a generic FAILED (`src/py_common/pipeline.py:207`, `:248`). Add boundary tests for min/max INT64 and one outside each bound, nullable columns, warn mode and key columns. Do not include the rejected value itself in public logs (M13).

## Verification — commands, results, and limits

All executable checks ran against a copy of the **120 tracked on-disk files** under this chat's `work/snapshot`, using the repository's existing `.venv\Scripts\python.exe`. `PYTHONPATH` selected the copied `src`; bytecode was disabled; caches, coverage and temporary outputs stayed in the chat workspace. The original repository was not installed into or modified by these checks. All 120 original tracked files were SHA-256 checked against the starting baseline afterward: **0 changed**. Git log, ls-files and rev-parse succeeded; `git status`/explicit work-tree attempts failed in the restricted execution environment, so I do not assert that the original checkout was clean before review.

Environment: Windows, CPython **3.13.5**. The declared Python floor is 3.11 and CI targets 3.11/3.12 (`pyproject.toml:12`; `.github/workflows/tests.yml:15`); those interpreter versions were **not tested in this session**. Existing installed packages were used, rather than resolving a fresh deployment lock.

| Check | Invocation / scope | Result |
|---|---|---|
| Pytest | `python -m pytest tests -m "not integration" -q -p no:cacheprovider --basetemp=../pytest-temp3 --cov=src/py_common --cov-report=term-missing` | **Exit 0: 445 passed, 2 skipped, 6 deselected in 11.27s.** Statement coverage 96%; not branch coverage. |
| Ruff | `python -m ruff check src tests --no-cache` | **Exit 0: All checks passed!** |
| mypy | `python -m mypy src/py_common --strict --cache-dir=../mypy-cache` | **Exit 0: Success: no issues found in 19 source files.** |
| Extra Ruff | `python -m ruff check scripts containers --no-cache --output-format=concise` | **Exit 1: 4 errors**, listed below; no fixes applied. |
| Black | `python -m black --check src tests`, then attempts with one worker / a writable cache | CLI attempts produced no diagnostics and were interrupted when they stalled in this environment. A sequential **Black API check** using line length 79, PY311 and `fast=False` checked **53 Python files: 0 would be reformatted; exit 0**. The API result is not presented as a successful CLI run. |
| pip-audit | `python -m pip_audit --skip-editable` | **Exit 1: No module named pip_audit.** No audit report produced; installed vulnerability status **unverified**. No packages installed to add the tool. |
| Extra edge-case probes | Standalone offline Python script using the copied real pipeline/adapters, repository stateful fake, synthetic workbooks and patched I/O | **Exit 0: 14 scenarios reproduced**, summarized below. No cloud requests. |
| Text secret scan | Filename-only pattern search in `src scripts tests containers .github` for private-key blocks and common Google/GitHub token formats | No candidate files returned. This does not cover git history, arbitrary secret formats, workbook data, external stores or deployed credentials. |

The first pytest run reported **10 failed, 428 passed, 2 skipped, 6 deselected, 25 errors** because `tempfile.TemporaryDirectory()` used a sandbox temp path that Python could not access/clean up. Redirecting TEMP/TMP into `work/` removed those permission failures. The next run reported **6 failed, 439 passed, 2 skipped, 6 deselected**, all failing on long raw processed paths in the HWW local fixtures. An extended-length Windows temp path (`\\?\...\work\t`) resolved those six failures, producing the final green run above. These were not silently discarded; the portable-path limitation is L03. No source edits were used to obtain the pass.

Final coverage output:

```text
Name                                     Stmts   Miss  Cover   Missing
----------------------------------------------------------------------
src/py_common/__init__.py                    7      0   100%
src/py_common/adapters/__init__.py           2      0   100%
src/py_common/adapters/_bq_jobs.py          74      5    93%   62-63, 80-81, 291
src/py_common/adapters/bigquery.py         107      1    99%   178
src/py_common/adapters/bigquery_raw.py      88      2    98%   190, 313
src/py_common/adapters/gcs.py              167     21    87%   184-185, 187, 282-285, 290-296, 318-319, 521-524, 557-559
src/py_common/adapters/local.py             62      4    94%   68-69, 101, 219
src/py_common/adapters/sharepoint.py       231     10    96%   149, 203, 269, 443, 526, 556, 662-663, 684-685
src/py_common/config.py                     39      2    95%   82-83
src/py_common/contract.py                  238      5    98%   481, 584, 711, 717-718
src/py_common/errors.py                     16      0   100%
src/py_common/fixtures.py                   48      0   100%
src/py_common/logging.py                    47      0   100%
src/py_common/model.py                      41      0   100%
src/py_common/parquet.py                    84      8    90%   138, 159, 167, 172-175, 179
src/py_common/pipeline.py                  178      3    98%   121-123
src/py_common/ports.py                      17      0   100%
src/py_common/raw.py                        38      1    97%   61
src/py_common/runner.py                     81      0   100%
----------------------------------------------------------------------
TOTAL                                     1565     62    96%
445 passed, 2 skipped, 6 deselected in 11.27s
```

The additional lint output was:

```text
containers/runtime/pipeline_executor.py:141:36: W292 No newline at end of file
containers/runtime/pipeline_runner.py:77:11: W292 No newline at end of file
scripts/gcp_auth.py:105:80: E501 Line too long (82 > 79)
scripts/gcp_auth.py:203:80: E501 Line too long (101 > 79)
Found 4 errors.
```

Tool versions: pytest 9.1.1; pytest-cov 7.1.0; Ruff 0.16.8; mypy 2.3.1; Black 26.5.1. Runtime versions inspected: openpyxl 3.1.5; PyYAML 6.0.3; PyArrow 25.0.1; Requests 2.34.2; google-cloud-storage 3.14.1; google-cloud-bigquery 3.45.2; google-api-core 2.38.0.

### Additional offline probes

| Scenario | Observed result | Finding |
|---|---|---|
| Typed A → B → A | loaded, loaded, skipped | H01 |
| Typed restore after deleting marker cache | Still skipped by warehouse ledger | H01 |
| Empty raw replacement | LOADED; audit versions 1 and 2; raw rows stay 1 | H02 |
| Add a raw target to the mapping | Same delivery LOADED again; existing table rows 1 → 2 | H04 |
| Swap sheet assignments between two target tables | Warehouse identities equal | H04 |
| Blank heading followed by literal `col_A` | Synthesized column gets `col_A`; real named column becomes `col_A_2` | M04 |
| Rename a nullable listed source header in warn mode | Payload remains `{"expected": null}` | H05 |
| Different folder identities, equal basename/version/content | NDJSON metadata/output identical | M01 |
| Upload JSON with `name: null` | AttributeError escapes handle_upload | M07 |
| Foreign Graph pagination URL | Synthetic bearer Authorization attached to foreign request | M12 |
| Required INT64 overflow in quarantine mode | Contract read succeeds; Parquet value NULL; warning only | H07 |
| Malformed XLSX numeric cell after lazy open | FAILED; zero raw copies | M05 |
| Local file replaced between metadata check and read | Discovered size 3, returned size 11 | M06 |
| Structured contract warning | JSON has logger, message, severity and time fields | H06 |

These are narrow reproductions, not a substitute for cloud semantic tests. In particular, the fake counts inserted rows and parses limited SQL text; it does not implement BigQuery MERGE, snapshot isolation, quotas, IAM or transaction concurrency.

### External behavior checked against primary documentation

- **BigQuery script dry runs stop at IF** and do not validate its body or later statements. Therefore the actual MERGE/INSERT statements inside `commit_script()` need separate validation or execution; a passing script dry run does not establish them. [Google BigQuery multi-statement query documentation](https://docs.cloud.google.com/bigquery/docs/multi-statement-queries). Repository evidence: `src/py_common/adapters/_bq_jobs.py:210`; `tests/integration/test_bq_dry_run.py:195`.
- **The allowed Requests range includes a known advisory**, CVE-2024-47081 / GHSA-9hjg-9r4m-mvj7, affecting versions below 2.32.4 under its malicious-URL/.netrc conditions. The installed 2.34.2 is outside that advisory's affected range. This is not a claim that the installed environment is generally vulnerability-free. [Requests maintainer advisory](https://github.com/psf/requests/security/advisories/GHSA-9hjg-9r4m-mvj7). Repository evidence: `pyproject.toml:51`.
- **Cluster pruning depends on suitable filters**, and functions/column comparisons can prevent it. The documented all-history window query should not be sold as reading only current blocks without execution-plan evidence. [Google clustered-table query guidance](https://docs.cloud.google.com/bigquery/docs/querying-clustered-tables). Repository evidence: `docs/RAW_TABLE_LAYOUT.md:23`, `:125`.
- **Workflow discovery uses root `.github/workflows`**. [GitHub workflow syntax documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax). The nested `containers/.github/workflows/build-runtime.yml:1` is not active here just because it exists.
- **Storage Object User includes read and delete permissions**, relevant to the write-only overview discrepancy. [Google Cloud Storage IAM roles](https://docs.cloud.google.com/storage/docs/access-control/iam-roles). Repository evidence: `docs/GCP_SETUP.md:22`, `:201`.

### Coverage of the requested review areas

**Correctness.** Beyond the findings, dataclass lists use `default_factory` and function list parameters generally use None; I found no ordinary mutable-default-argument defect in the reviewed core (`src/py_common/contract.py:79`, `:146`; `src/py_common/model.py:63`). The logging ContextVar's default dict is not mutated in place by its helpers (`src/py_common/logging.py:44`, `:62`, `:80`). Empty rows, duplicate headers, missing columns, null merge keys and uncached formulas have behavioral tests. The synthetic-name collision, overflow, lazy parse errors and local read race are gaps in those cases, rather than a claim those features are entirely untested.

**Dates, timezones and scientific interpretation.** Raw conversion emits ISO strings and normalizes aware datetimes to UTC before removing timezone information (`src/py_common/raw.py:66`); typed dates use a timezone-naive microsecond Arrow timestamp (`src/py_common/parquet.py:37`, `:176`). Naive Excel values have no source timezone policy, so their interpretation across London DST transitions is a solution-level contract to define, not something this code can infer. A real workbook's timezone assumptions and BigQuery date/time round trip were **unverified**. The freshness SQL's month boundary explicitly uses Europe/London, and its `> first_day + 6 days` rule starts overdue status on the 8th as documented (`docs/GCP_SETUP.md:549`, `:554`). It measures ingestion this month, not whether records belong to the expected business month; retain a separate business-period completeness check.

**Data engineering.** Raw tables are append-only; typed output is update/insert MERGE when keys are passed and append otherwise (`src/py_common/adapters/bigquery_raw.py:362`; `src/py_common/adapters/bigquery.py:262`). Only staging uses WRITE_TRUNCATE. Typed snapshot deletion and ordering of conflicting late files are not implemented: source iteration order can determine the last update (`src/py_common/pipeline.py:114`; `src/py_common/adapters/bigquery.py:461`). The caller independently supplies contract and warehouse keys; forgetting the latter selects append behavior even if the contract declares keys (`src/py_common/adapters/bigquery.py:93`; `src/py_common/contract.py:146`). These policies should be explicit in the solution's feed contract and in any future replay API (H01, M02, M03).

**GCP and runners.** Separate landing/store buckets, conditional lock operations and per-load staging are appropriate. SDK calls do have their own retry defaults, so it would be inaccurate to say the library has no GCP retries simply because it does not pass `retry=`. There is no explicit bounded end-to-end deadline: commit waiting defaults to 600 seconds, staging load waiting supplies no timeout, and the service example allows 540 seconds (`src/py_common/adapters/_bq_jobs.py:32`, `:104`; `docs/GCP_SETUP.md:423`). This reinforces H03's need for durable pending-job reconciliation. SharePoint has bounded exponential retries, although Retry-After is treated only as numeric seconds and capped at 60 (`src/py_common/adapters/sharepoint.py:650`); test the organization's actual throttling behavior before its first live use. The guide creates buckets/dataset/compute in europe-west2 (`docs/GCP_SETUP.md:41`, `:59`, `:84`); no deployed location, regional quota or IAM state was inspected, so region consistency in a real deployment remains **unverified**.

**Software design and public API.** The supported exports and 0.x compatibility policy are explicit (`src/py_common/__init__.py:12`, `:39`; `CHANGELOG.md:3`). `Contract.read()` is the shared parsing boundary, and conversion modules are separate from storage. The main design weakness is conflating destination identity with locking, deduplication, processed artifacts and materialization version (H04/M03). The separate R-package executor and legacy authentication utility have their own deployment/dependency concerns and should either be maintained/tested as separate components or clearly marked reference material (M17–M19, L01). Optional SDK client injection would make testing and deployment policy configuration easier than patching constructors (`src/py_common/adapters/bigquery.py:94`; `src/py_common/adapters/gcs.py:101`); SharePoint already demonstrates injected HTTP/token dependencies (`src/py_common/adapters/sharepoint.py:239`).

**Reliability and observability.** A normal file's run, item, checksum and query-job logs can generally be correlated when JSON logging is configured. Run and failure isolation are useful, but the persisted audit lacks those same run/job identifiers (M22) and the alert configuration needs correction (H06). A failure during source listing has no file result to audit, while runner exception propagation supplies the process-level signal (`src/py_common/pipeline.py:121`; `src/py_common/runner.py:147`). Operators should be able to distinguish "not submitted", "staging failed", "commit pending", "committed but marker failed" and "audit persistence failed" without guessing from a single generic FAILED outcome.

**Testing.** The suite includes real workbook/Parquet content assertions and stateful failure-path tests, not only mock call counts. The offline suite is useful and fast. Missing semantic coverage includes two concurrent real commits, a pending server job surviving its client, typed A → B → A values, per-sheet zero-row snapshots, mapping migrations, mixed synthetic/real headings, INT64 bounds, real date/time round trips, and malformed nested JSON types (M15 and probe table). There is **no configured local BigQuery emulator**: local tests use mocks/fakes, and the dry-run test creates a real scratch dataset when explicitly configured (`tests/integration/test_bq_dry_run.py:141`). An actual sandbox integration lane is the strongest available repository path; its required cloud writes were not run during this review.

**Security.** Config uses `yaml.safe_load`, raw rows/events use JSON, and the core has no pickle/eval deserialization path (`src/py_common/config.py:78`; `src/py_common/adapters/bigquery_raw.py:315`; `src/py_common/runner.py:176`). The runtime invokes R using an argument list, not shell=True (`containers/runtime/pipeline_executor.py:105`). Its purpose is executing trusted code, so a code-bucket permission is an execution permission (M18); no archive-based arbitrary-code vulnerability independent of that trust boundary was established. No live account credentials were read or printed. The security issues found are identified in M11–M14/M16/M18, with trust assumptions stated rather than speculative exploit claims.

## Things done well

- **The core does not depend on cloud SDK calls.** Small structural protocols allow local execution and targeted adapters (`src/py_common/ports.py:14`, `:48`, `:98`; `src/py_common/pipeline.py:32`).
- **Final writes and the successful warehouse audit share a transaction.** This avoids the common "data committed, audit insert failed" split for a completed script (`src/py_common/adapters/_bq_jobs.py:202`, `:212`, `:215`). It is valuable even though uniqueness under concurrent append needs stronger coordination.
- **Store reads fail closed and GCS writes use generation preconditions.** A permission error is not interpreted as a missing dedup marker; conflicting immutable bytes are rejected (`src/py_common/adapters/gcs.py:126`, `:170`, `:194`).
- **Per-load staging has unique names, expiry and best-effort cleanup.** Independent loads no longer share one mutable staging table (`src/py_common/adapters/_bq_jobs.py:317`, `:337`, `:347`). Pending commit cleanup still needs the H03 distinction.
- **Key validation is substantive.** The contract rejects unusable declared merge keys; the warehouse rejects missing, null and duplicate staged keys before MERGE (`src/py_common/contract.py:169`; `src/py_common/adapters/bigquery.py:252`, `:398`).
- **Column selection is deliberate and defaults to an allowlist.** Required named sheets, configurable header rows, duplicate-heading preservation, exclusions, and row-number metadata address real analyst spreadsheets rather than assuming machine-perfect files (`src/py_common/contract.py:299`, `:552`, `:659`; `src/py_common/raw.py:121`). Keeping original files separately preserves evidence.
- **Conversion is deterministic within a fixed dependency/config version.** Parquet columns use declared types and omit the ingestion timestamp; raw JSON is UTF-8 with non-ASCII preserved and non-finite float handling (`src/py_common/parquet.py:95`; `src/py_common/raw.py:55`, `:129`).
- **Failures are isolated at useful boundaries.** A file failure does not stop later files, run_job attempts independent pipelines, and FAILED results produce non-zero runner status (`src/py_common/pipeline.py:118`; `src/py_common/runner.py:124`, `:144`).
- **Authentication is generally placed correctly.** Production GCP adapters use ADC; Graph authentication is injected; unknown SharePoint config keys are rejected rather than accepting credential fields (`src/py_common/adapters/bigquery.py:94`; `src/py_common/adapters/sharepoint.py:188`, `:255`). The GCP guide scopes store delete/overwrite permissions to locks rather than all archived data (`docs/GCP_SETUP.md:164`).
- **CI quality gates and a documented API now exist.** Ruff, Black, strict mypy, pip-audit and a Python matrix precede unit tests, and the changelog explains the load API/schema migration (`.github/workflows/tests.yml:15`, `:35`, `:47`; `CHANGELOG.md:16`). The core checks passed here; M15 explains the remaining semantic blind spots.

## Prioritised action plan

### Quick wins — small changes with focused behavioral tests

1. **Repair and exercise alert filters first (H06).** Use structured outcomes and stable event fields, emit file failures at ERROR, and verify synthetic warehouse/audit/lock/quarantine/freshness logs produce the expected notifications. Completion criterion: an intentionally failed file cannot leave both job status and the team's alerts green.
2. **Prevent silent data corruption (H07, M04, M05).** Add destination range checks, apply quarantine policy during conversion, reserve written headings before synthesizing names, and normalize lazy parser failures. Completion criterion: required overflow is quarantined; a literal `col_A` is never reassigned; malformed workbooks retain their original bytes and a diagnostic reason.
3. **Validate the boundaries (M07, M11, M12, M21).** Require typed upload/config/NDJSON shapes, bucket-aware routing, permitted SQL identifiers, Graph pagination origin checks and UTF-8 config reads. Completion criterion: malformed inputs fail with a documented actionable exception before cloud mutation; foreign nextLink receives no bearer token.
4. **Reduce operational data leakage and supply-chain ambiguity (M13, M14, M16).** Remove raw key/value examples from ordinary logs, raise Requests' minimum supported security version, inspect deployed uploader access and generate deployment locks. Check installed packages with pip-audit when available; do not infer security from upper bounds alone. Replace long-lived build service-account keys with federated CI credentials where the platform supports them (`containers/.github/workflows/build-runtime.yml:85`).
5. **Align docs and component ownership (M17, M23, L01–L03).** Correct the 0.2.0 feature/replay/freshness descriptions, clearly separate the old CSV prototype, fix or quarantine the nested workflow, and retire the unused credential helper. Keep the documented Windows path/eTag workflow in a platform test lane. These are useful changes but do not close the delivery-state design issues.

### Larger refactors — agree semantics before implementation

1. **Redesign delivery/materialization state and migrate it together (H01, H03, H04, M01–M03).** Separate source namespace, logical delivery, target materialization, lock/coordinator scope and processing version. Resume pending commits using durable job references; fence overlapping writers; track completion per target. Provide an explicit replay API rather than instructing operators to delete authoritative records. Document append/upsert/snapshot and late-arrival ordering policies. Completion criterion: A → B → A works, unchanged retries are no-ops, expanded mappings only load new targets, and two concurrent deliveries cannot duplicate raw records.
2. **Add per-sheet manifests and the tested consumer contract (H02, H05).** Carry zero-row sheets, observed headers, row counts, source order and contract version through the same transaction as data. Use manifests for latest-version selection and freshness, with an independently checked business-period arrival rule. Build versioned Dataform assertions/quarantine/publication dependencies in the solution repo. Completion criterion: an intentionally empty replacement removes the previous snapshot from current consumers, and a renamed nullable source column blocks or quarantines according to an explicit policy.
3. **Build a real semantic integration lane (M10, M15).** Use isolated expiring sandbox datasets and synthetic GCS objects; execute actual transactions, inspect stored values, force an audit failure to prove rollback, exercise concurrent append, and test missing/changed metadata schemas. Keep cheap extracted-statement dry runs, recognizing their limits. Run the declared Python versions and Windows path cases. Completion criterion: acceptance tests test warehouse behavior, not a replica of generated SQL logic.
4. **Bound work and costs before scaling (M08, M09, M19).** Establish supported file/expanded-workbook limits, batch processing and storage-backed load inputs, manifest-based discovery, typed partition/cluster policies, query labels/budgets where supported, and end-to-end deadlines with pending-job reconciliation. Stream subprocess diagnostics and bound child lifetime. Benchmark representative wide workbooks and retained upload history; no measured scalability claim is made in this review.
5. **Make deployment artifacts auditable (M18, M22).** Pin code-package generations/digests and image/dependency versions, use non-root containers, persist run/job/config identifiers, and validate IAM/regions/retention/alerts from infrastructure code in the solution deployment. Completion criterion: a historical audit identifies the exact input version, transformation version and commit job, and the same job revision cannot silently execute a different `latest.zip`.

The remaining work should be accepted against these scenarios, rather than a higher line-coverage target. No source-file changes or cloud writes were made as part of this review.
