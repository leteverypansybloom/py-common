# py-common: Path to Production

Sep 25, 2026 · @Vondy

## Where it stands today

Phase 1 (core pipeline, local adapters) and phase 2 (GCS + BigQuery adapters) are both complete: 38 unit tests passing, mypy `--strict` clean, `BigQueryWarehouse` merges on `Contract.key_columns` inside one transaction with its audit record.

What's still open, per `docs/INGESTION_FRAMEWORK_GUIDE.md` Section 9 and the code itself:

- **`SharePointSource` is a stub** — every method raises `NotImplementedError`. Nothing can ingest from SharePoint yet.
- **`ObjectStore.lock()` is a deliberate no-op.** Safe only because today's model is one Cloud Run Job at a time; unsafe the moment two runs overlap.
- **No deployment exists.** `deploy/` is an empty placeholder; there's a `Dockerfile` but no Terraform, no Cloud Run job/service definition, no scheduler or trigger.
- **No `SecretStore` adapter** — the protocol exists in `ports.py`, nothing implements it.
- **Failure is silent to the uploader.** A quarantined or failed file writes a detailed audit record to GCS, but nobody is notified — not the data owner, not the team.
- **No monitoring/alerting** — plain-text logging only, no Cloud Monitoring alert policies, no freshness view.

This plan sequences closing those gaps, in roughly the order the framework guide already recommends, plus the four items you asked about specifically.

## Move from local/VS Code to Cloud Run

py-common was written for this from the start — `Pipeline.run()` is already a single batch pass with no long-lived state, which is exactly the shape a Cloud Run Job wants. The library itself needs no code changes to run there; what's missing is the surrounding solution repo and its deployment.

**Job, not Service.** This is a batch run-to-completion workload (discover → validate → load → exit), not something that serves HTTP traffic. Use a **Cloud Run Job**, triggered by Cloud Scheduler (or an event, see below), not a Cloud Run Service sitting behind a URL.

**What changes in practice:**

1. **A separate "solution" repo** (not py-common itself) that depends on py-common, defines the actual `Contract`, wires up `SharePointSource` / `GCSObjectStore` / `BigQueryWarehouse`, and has a `main.py` entry point Cloud Run executes. `py-common`'s own `Dockerfile` only proves the library installs; the solution repo needs its own image built on top of it.
2. **Credentials come from the Cloud Run Job's service account**, not a key file. `gcp_auth.py` already auto-detects this (`method="auto"` → pings the metadata server → falls back to ADC locally), so no code change there — just make sure the deployed job's service account has the right IAM roles (Storage Object Admin on the landing bucket, BigQuery Data Editor + Job User on the dataset, Secret Manager Secret Accessor once that adapter exists).
3. **Config moves out of `.yaml` + local env vars** into Cloud Run environment variables / mounted Secret Manager secrets. `config.py`'s `${VAR}` interpolation still works unchanged, it just resolves against the container's environment instead of a developer's shell.
4. **Terraform for the pieces already designed** in `docs/INGESTION_FRAMEWORK_GUIDE.md` Section 6: the landing bucket, BigQuery datasets, the Cloud Run Job, and its trigger. This is what turns `deploy/` from an empty placeholder into something real — currently the single biggest gap.
5. **Implement `ObjectStore.lock()` for real** (Firestore is the framework guide's recommendation) before relying on Cloud Run to enforce "only one job at a time" — a manual re-run, a retry, or a second trigger source overlapping would otherwise double-process files.

## Event-driven triggering: Pub/Sub vs Eventarc

Today nothing triggers a run except a person. The framework guide's baseline recommendation is **Cloud Scheduler → Cloud Run Job** on a timer, which is the simplest correct answer for a daily/weekly SharePoint export and should ship first regardless of anything below.

True event-driven ("run the moment a file lands") is a later step, and the two GCP options aren't really alternatives — they fit different sources:

|  | **Eventarc** | **Pub/Sub (direct)** |
| --- | --- | --- |
| What it's for | Reacting to a GCP-native event (a new object landing in GCS, an Audit Log entry) | Reacting to a custom message your own code publishes |
| Fits | A SharePoint sync/export job that drops files into a **GCS landing bucket** — Eventarc fires on `google.cloud.storage.object.v1.finalized` and triggers the Cloud Run Job directly | A **Microsoft Graph webhook** subscription on the SharePoint library (push notification on file change), received by a small Cloud Function/Run service that republishes it as a Pub/Sub message |
| Setup cost | Low — a Terraform resource, no code to receive events | Higher — needs a webhook receiver, subscription renewal (Graph webhooks expire and must be renewed), and a publisher |
| Maps to py-common | Nothing changes in the library; the trigger just replaces the Scheduler cron with a GCS-object-created trigger | `SharePointSource.items()` (still to be built) would be the consumer — same interface either way |

**Recommendation:** don't build either yet. Ship Scheduler-driven first (it's already the documented baseline and needs no new infra). Once `SharePointSource` exists and the real shape of the source is known:

- If SharePoint files land via an intermediate sync into GCS → **Eventarc** on the bucket, minimal extra code.
- If you want true "the moment someone uploads in SharePoint" latency → **Pub/Sub** fed by a Graph webhook receiver, more moving parts, only worth it if the daily/weekly cadence genuinely isn't fast enough.

Either way the trigger is infrastructure in the *solution* repo's `deploy/`, not a py-common code change — `Pipeline.run()` doesn't need to know why it was invoked.

## First real production integration

`docs/INGESTION_FRAMEWORK_GUIDE.md` already names the target shape py-common was designed around: *"Small daily/weekly Excel export from SharePoint (the current HWW shape)"* — and lists exactly what's needed to take that from local fixtures to real production use:

1. Real `SharePointSource` (currently a stub)
2. A minimum-row-count check on `Contract` (catches an empty export instead of silently loading zero rows)
3. A real `ObjectStore.lock()`
4. A failure alert

That's a small, closed list — explicitly *not* an orchestrator, CDC, streaming, or a connector catalogue, which the same guide says are out of scope for this shape.

**Suggested rollout for HWW specifically:**

1. Build `SharePointSource` against the Microsoft Graph API (`Sites.Selected` permission, full listing first — delta queries only once volume justifies it, per the module's own docstring and the framework guide's Section 6).
2. Write the HWW `Contract` (worksheets, columns, types, `key_columns`) as a small solution-repo module, not inside py-common.
3. Run it against the Cloud Run Job + Scheduler setup from the section above, pointed at a dev GCP project first.
4. Only promote to prod once the failure-alert and audit-harmonisation work below are in place — otherwise a quarantine on day one is invisible.

Open item: confirm HWW is in fact the intended first pipeline (this doc assumes so, based on the existing docs) and who owns the Entra app registration the SharePoint adapter needs — that registration is the one external dependency this whole section is blocked on.

## Harmonise audit records with other audits in use

py-common's `AuditRecord` (`model.py`) currently has: timestamp, item identity, filename, version, checksum, rows processed, outcome, errors, processing time, warehouse key. Whether that's enough columns, or the right names, depends on what other PHW pipelines already write — which isn't settled yet, so this is a discovery task before it's a code change.

**Discovery steps:**

1. Pull the actual column list from the existing BigQuery `audit` table(s) already in use — including the one py-common's own dev setup already writes to (`rdd_dev_raw.audit`), and any used by the R pipelines (SHRN Primary, CAT-MORT, PHOF) on Vertex AI Workbench.
2. Compare field-by-field against `AuditRecord`: same concept, different name (e.g. "outcome" vs "status")? A field one side has and the other doesn't (run duration, triggering user, source system name)?
3. Decide whether harmonisation means renaming/adding fields in `AuditRecord`, or a downstream BigQuery view that maps py-common's schema onto a shared one — the latter avoids coupling the library's internal model to every consumer's reporting needs.
4. Write the outcome as a new dated entry in `DECISIONS_en.md`, per the project's existing convention.

This naturally overlaps with the framework guide's own "Add one failure alert and one freshness view" step (Section 9, #6) — a shared audit shape is what makes one cross-pipeline monitoring view possible instead of one per pipeline.

## Pull request template

Modelled on the [ONS research-and-development template](https://github.com/ONSdigital/research-and-development/blob/develop/.github/pull_request_template.md), adapted for py-common's own standards (PEP 8/79 chars via `black`+`ruff`, `mypy --strict`, TDD, Google docstrings, `detect-secrets`). Save as `.github/pull_request_template.md`:

```markdown
## What does this PR do?

- 
- 

## Closes / relates to

Closes #

## Code checklist

- [ ] Code runs locally without errors
- [ ] Branch is up to date with `main`/`develop`, conflicts resolved
- [ ] New/changed behaviour matches the linked issue's requirements
- [ ] `pyproject.toml` dependencies updated if new packages were added
- [ ] Config/`.env.example` updated if new environment variables were introduced

### Clean code

- [ ] PEP 8 / 79-character lines (`black --check --line-length=79`)
- [ ] `ruff check` passes with no new warnings
- [ ] No duplicated logic that should be a shared function
- [ ] Full type hints on all new/changed functions (`mypy --strict` passes)

## Documentation checklist

- [ ] All new/changed functions have Google-style docstrings (Args, Returns, Raises)
- [ ] `README.md` / `docs/` updated if behaviour, setup, or the adapter status table changed
- [ ] `DECISIONS_en.md` updated if this PR makes or supersedes a design decision

## Data checklist

- [ ] No real data, credentials, or secrets committed
- [ ] `detect-secrets` pre-commit hook passes (`.secrets.baseline` updated if a new false positive was audited)
- [ ] Test fixtures used are synthetic (`py_common.fixtures.make_fixtures()`), not real PHW data

## Testing checklist

- [ ] New tests added for new/changed behaviour (TDD — test written alongside or before the code)
- [ ] `pytest tests/ -v --cov=src/py_common` passes locally
- [ ] Integration tests updated/skipped appropriately if GCP-facing behaviour changed

## For the reviewer

- [ ] Dependencies install cleanly (`pip install -e ".[dev,gcp]"`)
- [ ] Docstrings are clear and in Google format
- [ ] Tests are meaningful, not just coverage padding
- [ ] Code runs and does what the PR description says

## Post-review

- [ ] Author has responded to all review comments
- [ ] Reviewer approves merge

---
*Review comments: be specific — point to the bug, the missing test, the unclear docstring, or the style issue. Critical and clear, not mean.*
```

## Other production-readiness gaps

From `docs/INGESTION_FRAMEWORK_GUIDE.md` Section 9 and a read of the code, not already covered above:

| Gap | Why it matters before prod |
| --- | --- |
| `SecretStore` adapter for Secret Manager | No credential (SharePoint client secret especially) should go into config as plain text; the protocol exists in `ports.py`, nothing implements it |
| Minimum-row-count check on `Contract` | An empty export currently validates fine and loads 0 rows silently — looks like success, isn't |
| One failure alert | A Cloud Monitoring log-based alert on any `FAILED` outcome — pure config once structured logging exists, and the audit trail already has everything it needs |
| One freshness view | A BigQuery view over the audit table answering "when did each source last load, and is that overdue?" |
| Notify the uploader on quarantine | Flagged directly by your colleague's question in the earlier review — today a bad upload's error message only reaches a JSON file in GCS, nobody tells the person who submitted it |
| Raw-file overwrite edge case | `ObjectStore.put()` rejects a second write to the same key with different bytes; a corrected re-upload of the *same* SharePoint item (same identity, new content) will currently fail rather than update — worth a fix before SharePoint goes live |
| `gcp_auth.py` unit tests | Flagged in an earlier code review as "zero tests on a 428-line module" — still outstanding |
| Structured logging | Plain-text `logging` calls today; JSON/structured logs are what makes the failure alert and freshness view queryable |
| Security review | Before real PHW data flows through it: IAM roles scoped to least privilege, `detect-secrets` baseline audited, dependency scanning in CI |

## Suggested phased roadmap

Sequenced so each phase is independently shippable and doesn't require later phases.

**Phase 3a — Deployable (no new features)**

1. PR template (quick, unblocks better review on everything after this)
2. Terraform for landing bucket, BigQuery datasets, Cloud Run Job, Cloud Scheduler
3. Solution repo skeleton with `main.py`, wired to `LocalSource` + real GCS/BigQuery adapters, deployed to dev
4. Real `ObjectStore.lock()` (Firestore)

**Phase 3b — HWW live**

5. `SharePointSource` implementation (blocked on Entra app registration)
6. Minimum-row-count check on `Contract`
7. HWW `Contract` written, run end-to-end against dev
8. Failure alert (Cloud Monitoring) — ship before prod, not after

**Phase 3c — Operate with confidence**

9. Audit-schema discovery + harmonisation
10. Freshness view
11. Uploader notification on quarantine
12. `SecretStore` adapter; SharePoint client secret moved out of plain config
13. `gcp_auth.py` unit tests; structured logging; security review

**Phase 4 — Event-driven (only once 3b is stable in prod)**

14. Eventarc or Pub/Sub trigger, per the earlier comparison — replaces the Scheduler cron, doesn't change py-common itself

Raw-file-overwrite edge case (see previous section) should land wherever it's cheapest — realistically alongside item 5, since it only bites once SharePoint re-uploads are real.

## Open questions

- **Is HWW the right first target?** This plan assumes so, based on the one mention in `docs/INGESTION_FRAMEWORK_GUIDE.md` — confirm before starting Phase 3b.
- **Who owns the Entra app registration** the SharePoint adapter needs? It blocks item 5 and everything after it in Phase 3b.
- **Which audit schema(s) exist already** to harmonise against — discovery step 1 in that section needs someone who can see the R pipelines' BigQuery tables.
- **Solution repo:** new repo, or a folder inside an existing one? Affects where Terraform and `main.py` live.
- **Alerting destination:** where should the failure alert and uploader notification actually go — email, Teams, Slack? Not yet decided anywhere in the existing docs.
