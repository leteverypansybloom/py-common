# GCP setup for the raw JSON route

How to set up Google Cloud for a py-common pipeline that reads Excel
files from a landing bucket and loads them into raw BigQuery tables
(`GCSSource` → `Pipeline(output_format="raw_json")` →
`RawJsonWarehouse`). Written for the Healthy Working Wales (HWW)
project, but nothing here is specific to it.

A daily Cloud Run job runs the pipelines (section 5). Optionally, an
upload can also start a run within a minute or so (section 6). Both
call the same code, in `py_common.runner`.

> These are manual `gcloud` and `bq` steps, so the setup can be
> understood and tried by hand. Terraform (issue #13) is expected to
> replace them; when it does, this page becomes the description of
> what the Terraform creates.

## What gets created

| Resource | Purpose | Who can access it |
|---|---|---|
| Landing bucket | Monthly Excel files are uploaded here | Uploaders (write only), the pipeline (read) |
| Store bucket | The pipeline's own copies: `raw/`, `quarantine/`, `processed/`, `audit/`, `locks/` | The pipeline only |
| Raw dataset | One raw table per worksheet, plus the audit table | The pipeline (write), Dataform (read) |
| Service account | Identity the pipeline runs as | |
| Cloud Run job + Cloud Scheduler | Runs the pipelines daily and checks for missing monthly files | |
| Cloud Run service + Pub/Sub + Eventarc (optional) | Runs the pipeline for a folder when a file is uploaded to it | |
| Freshness view | One row per monthly source: when it last loaded, and whether it is overdue | The pipeline (read) |
| Alerts | Failed runs, warnings, quarantines, a missing monthly file | Whoever supports the pipeline |

The landing and store buckets are separate on purpose. The pipeline
writes to the store on every run, and if that were the landing folder
it would list its own copies as new files.

## Configuration

Replace every value in angle brackets before running anything.

```bash
PROJECT="<PROJECT_ID>"              # project for buckets, BigQuery and the job
REGION="europe-west2"
LANDING_BUCKET="<LANDING_BUCKET>"   # e.g. hww-landing-dev
STORE_BUCKET="<STORE_BUCKET>"       # e.g. hww-store-dev
RAW_DATASET="<RAW_DATASET>"         # e.g. raw_hww
SA_NAME="<SERVICE_ACCOUNT_NAME>"    # e.g. hww-ingest
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
JOB="<CLOUD_RUN_JOB_NAME>"          # e.g. hww-ingest
SERVICE="<CLOUD_RUN_SERVICE_NAME>"  # e.g. hww-ingest-on-upload (section 6)
TOPIC="<PUBSUB_TOPIC_NAME>"         # e.g. hww-landing-uploads (section 6)
IMAGE="<IMAGE_URI>"                 # the solution repo's container image
FRESHNESS_VIEW="$PROJECT.$RAW_DATASET.source_freshness"
UPLOADERS="<GROUP_EMAIL>"           # e.g. group:hww-uploaders@example.org
DATAFORM_SA="<DATAFORM_SERVICE_ACCOUNT_EMAIL>"
```

## 1. Buckets

```bash
gcloud storage buckets create "gs://$LANDING_BUCKET" \
  --project="$PROJECT" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention

gcloud storage buckets update "gs://$LANDING_BUCKET" --versioning

gcloud storage buckets create "gs://$STORE_BUCKET" \
  --project="$PROJECT" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention
```

**Object versioning** on the landing bucket keeps every upload of a
file, even when the same name is uploaded again each month. It is
the "version control" for the source files (issue #24). Each upload
gets a new *generation*, which `GCSSource` uses as the file's version
and which ends up in the raw tables as `source_generation`. Add a
lifecycle rule to delete old versions only once the retention period
in the DPIA (issue #7) is agreed.

The store bucket does not need versioning. py-common never overwrites
an object there; it refuses different bytes under an existing key.

## 2. Raw dataset and audit table

```bash
bq --location="$REGION" mk --dataset "$PROJECT:$RAW_DATASET"

bq query --use_legacy_sql=false "
CREATE TABLE IF NOT EXISTS \`$PROJECT.$RAW_DATASET.audit\` (
  filename STRING,
  checksum STRING,
  destination STRING,
  source_version STRING,
  row_count INT64,
  outcome STRING,
  timestamp TIMESTAMP
)"
```

The audit table is what makes a load happen at most once. Each
warehouse checks it inside the same transaction that writes the
rows, and writes nothing if it already has a `LOADED` row for this
`destination` table and this upload (the same `filename`, which is
the load key, and `source_version`). There is one audit row per
destination table: a `RawJsonWarehouse` writing three raw tables
records three. The `audit/` markers in the store bucket are only a
cache in front of it.

**An existing 0.1.x audit table** needs the two new columns (safe to
run twice):

```bash
bq query --use_legacy_sql=false "
ALTER TABLE \`$PROJECT.$RAW_DATASET.audit\`
  ADD COLUMN IF NOT EXISTS destination STRING,
  ADD COLUMN IF NOT EXISTS source_version STRING"
```

Rows written before the upgrade have `destination` NULL and count as
"not loaded here", so files already loaded by 0.1.x would load again.
Start 0.2.0 with a fresh dataset and store bucket where you can.

The raw tables themselves are created by `RawJsonWarehouse` on first
load, partitioned by day of `ingested_at` and clustered on
`source_file, source_generation`, the columns the "latest upload"
query filters on. Raw tables created before py-common 0.2.0 are not
clustered; add it to each (it applies to data written from then on):

```bash
bq update --clustering_fields=source_file,source_generation \
  "$PROJECT:$RAW_DATASET.<raw table>"
```

Their layout is in [`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md).

**Restrict this dataset.** Even with personal columns excluded by the
contracts, the raw tables hold employer names and free text, and the
DPIA (issue #7) should say who may see them. Grant access to the
dataset (step 3) rather than the project, and point analysts and
Power BI at the cleaned Dataform datasets, not at the raw one.

## 3. Service account and permissions

```bash
gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
  --display-name="HWW ingestion pipeline"
```

| Role | On | Why |
|---|---|---|
| `roles/storage.objectViewer` | Landing bucket | List and download the uploaded files |
| `roles/storage.objectCreator` | Store bucket | Write copies, audit records and markers (create only: it can't overwrite or delete them) |
| `roles/storage.objectViewer` | Store bucket | Read markers and compare an existing copy's hashes |
| `roles/storage.objectUser`, with a condition | `locks/` in the store bucket only | Release the run lock (delete) and take over an expired one (overwrite, which GCS counts as a delete) |
| `roles/bigquery.dataEditor` | Raw dataset | Create raw tables; create and drop each load's staging table (`{table}_stg_...`, expiring after a day if a run dies before dropping it); insert rows and audit records |
| `roles/bigquery.jobUser` | Project | Run load jobs and the commit transaction |

```bash
gcloud storage buckets add-iam-policy-binding "gs://$LANDING_BUCKET" \
  --member="serviceAccount:$SA" --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://$STORE_BUCKET" \
  --member="serviceAccount:$SA" --role="roles/storage.objectCreator"

gcloud storage buckets add-iam-policy-binding "gs://$STORE_BUCKET" \
  --member="serviceAccount:$SA" --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://$STORE_BUCKET" \
  --member="serviceAccount:$SA" --role="roles/storage.objectUser" \
  --condition="title=locks-only,expression=resource.name.startsWith(\"projects/_/buckets/$STORE_BUCKET/objects/locks/\")"

bq add-iam-policy-binding --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataEditor" "$PROJECT:$RAW_DATASET"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
```

With these roles the job can add to `raw/`, `processed/`,
`quarantine/` and `audit/` but never change or delete what is there,
so the store's immutability no longer rests on the code alone. IAM
conditions need uniform bucket-level access, which section 1 turns
on. Check the condition once after granting it: a first run should
log `Released lock`, not `Could not release lock`.

**Retention.** Consider keeping `raw/` and `audit/` for the period
the DPIA sets even against someone with more access than the job. A
bucket retention policy applies to every object in the bucket,
including `locks/`, and would stop the lock being released, so it
needs the lock in a bucket of its own (py-common keeps both in one
store bucket today). Until then, keep soft delete on (the default, 7
days; raise it with `gcloud storage buckets update
--soft-delete-duration`) so a deleted copy can be restored.

People who upload files need only `roles/storage.objectCreator` on the
landing bucket: they can add files but not read, overwrite or delete
them. GCS treats uploading over an existing name as a delete plus a
create, so if they upload under the same file name each month, give
them `roles/storage.objectUser` instead. With versioning on, the
earlier upload is still kept as a non-current version. Dataform needs `roles/bigquery.dataViewer` on the raw
dataset.

```bash
gcloud storage buckets add-iam-policy-binding "gs://$LANDING_BUCKET" \
  --member="$UPLOADERS" --role="roles/storage.objectUser"

bq add-iam-policy-binding --member="serviceAccount:$DATAFORM_SA" \
  --role="roles/bigquery.dataViewer" "$PROJECT:$RAW_DATASET"
```

## 4. The pipeline code

py-common is a library; the solution repository holds the contracts
and three small files. An outline for HWW, with one pipeline per
landing folder:

```python
# hww/pipelines.py
"""The HWW pipelines: one per landing folder."""

import os

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore, GCSSource
from py_common.pipeline import Pipeline

from hww.contracts import PIPELINES  # (prefix, contract, tables) each


def build_pipelines() -> dict[str, Pipeline]:
    """Build one pipeline per landing folder.

    Returns:
        Pipeline per landing folder (prefix), e.g. "was/".
    """
    project = os.environ["GCP_PROJECT_ID"]
    store = GCSObjectStore(project, os.environ["STORE_BUCKET"])
    return {
        prefix: Pipeline(
            source=GCSSource(project, os.environ["LANDING_BUCKET"], prefix),
            store=store,
            warehouse=RawJsonWarehouse(
                project, os.environ["RAW_DATASET"], tables
            ),
            contract=contract,
            output_format="raw_json",
        )
        for prefix, contract, tables in PIPELINES
    }
```

`configure_json_logging()` writes each log line as JSON, which Cloud
Logging turns into a structured entry: `severity`, `message`, and
fields to filter on — `run_id` (one per `Pipeline.run()`),
`warehouse`, `item_identity`, `version`, `checksum` and `bq_job_id`.
Each file ends with one summary line that also has `outcome`, `rows`
and `seconds`. For example, everything about one file in one run:
`jsonPayload.run_id="..." AND jsonPayload.item_identity="was/x.xlsx"`.

```python
# hww/job.py: the daily Cloud Run job (the image's default command)
"""Load every HWW workbook, then check for missing monthly files."""

import logging
import os
import sys

from google.cloud import bigquery

from py_common.logging import configure_json_logging
from py_common.runner import check_freshness, run_job

from hww.pipelines import build_pipelines


def main() -> int:
    """Run every pipeline, then the freshness check.

    Returns:
        Exit code: 1 if a file FAILED, a run didn't finish, or the
        freshness check couldn't run, so the execution shows failed.
    """
    configure_json_logging()
    code = run_job(build_pipelines().values())
    try:
        check_freshness(
            bigquery.Client(project=os.environ["GCP_PROJECT_ID"]),
            os.environ["FRESHNESS_VIEW"],
        )
    except Exception:
        logging.exception("Freshness check failed")
        code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
```

```python
# main.py: the upload-triggered Cloud Run service (section 6)
"""Load a landing folder when a file is uploaded to it."""

import logging

import functions_framework
from cloudevents.http import CloudEvent

from py_common.logging import configure_json_logging
from py_common.runner import handle_upload

from hww.pipelines import build_pipelines

configure_json_logging()
PIPELINES = build_pipelines()


@functions_framework.cloud_event
def on_upload(event: CloudEvent) -> None:
    """Run the pipeline for the folder the upload landed in.

    Args:
        event: Eventarc Pub/Sub event carrying a GCS notification.
    """
    handle_upload(event.data, PIPELINES)
```

What the `py_common.runner` helpers add to `Pipeline.run()`:

- `Pipeline.run()` records a file that fails as a FAILED result and
  carries on, so a run can return normally with failed files.
  `run_job()` returns exit code 1 in that case, and `handle_upload()`
  raises `RunFailed`, so the job execution shows failed and Pub/Sub
  delivers the event again.
- `run_job()` runs every pipeline even after one fails or is refused
  by its lock: each source is independent.
- A QUARANTINED file doesn't fail either: retrying won't fix it, and
  it has its own alert (section 7).
- `check_freshness()` logs each overdue source for the missing-file
  alert (section 7).

Give each workbook its own landing folder (`prefix`), so each
pipeline lists only the files its contract describes. Build the
solution's image `FROM` the one this repository's `Dockerfile`
builds: it has `py-common[gcp]` at the versions pinned in
`constraints.txt` and runs as a non-root user. The solution's image
adds its own code and, for the service, `functions-framework`, which
loads `main.py` from the working directory.

`Pipeline` checks its configuration when it is built: a
`BigQueryWarehouse` must merge on the contract's `key_columns`, and
a `RawJsonWarehouse` must map exactly the contract's worksheets. A
mismatch raises `ConfigError` at start-up instead of failing every
file.

## 5. Cloud Run job and schedule

A Cloud Run *job* runs to completion and stops, which suits a batch
that finds a handful of files.

```bash
gcloud run jobs deploy "$JOB" \
  --project="$PROJECT" --region="$REGION" --image="$IMAGE" \
  --service-account="$SA" --tasks=1 --max-retries=0 \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT,LANDING_BUCKET=$LANDING_BUCKET,STORE_BUCKET=$STORE_BUCKET,RAW_DATASET=$RAW_DATASET,FRESHNESS_VIEW=$FRESHNESS_VIEW"

gcloud run jobs execute "$JOB" --project="$PROJECT" --region="$REGION" --wait
```

`--max-retries=0` because a rerun is safe but should be deliberate:
files that loaded are skipped (by the store marker, and failing that
by the warehouse's audit check), and files that failed are retried on
the next scheduled run.

Run it daily. Files arrive monthly, but a daily run picks up a late or
corrected upload the next morning, and a run with nothing new costs
almost nothing. Keep it if you add section 6: it loads any file whose
upload event was lost, and it runs the freshness check.

```bash
gcloud iam service-accounts create "$JOB-scheduler" --project="$PROJECT"

gcloud run jobs add-iam-policy-binding "$JOB" \
  --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:$JOB-scheduler@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http "$JOB-daily" \
  --project="$PROJECT" --location="$REGION" \
  --schedule="0 6 * * *" --time-zone="Europe/London" \
  --http-method=POST \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB:run" \
  --oauth-service-account-email="$JOB-scheduler@$PROJECT.iam.gserviceaccount.com"
```

**If a run is killed** (for example, a timeout) it can leave its lock
object in the store bucket. Later runs are refused with an error
naming it until its lease expires (one hour by default,
`GCSObjectStore(lease_seconds=...)`), and the first run after that
takes it over and logs `Took over expired lock` at WARNING. Keep
`lease_seconds` above the job's task timeout (10 minutes unless set
with `--task-timeout`) and the service's `--timeout`,
so a lease outlasts any normal run. This makes a stolen lock
unlikely, not impossible: a Cloud Run *service* container can keep
working after its request times out, and the lease isn't renewed or
fenced. The warehouse's in-transaction audit check is the second line
of defence; two commits racing each other is still possible (see
`CHANGELOG.md`, Known limitations). To unblock sooner, check
that no run is active, then delete the `gs://$STORE_BUCKET/locks/...`
object named in the error.

## 6. Run on upload with Eventarc (optional)

The daily job is enough to load monthly files. Add this when an ad
hoc upload should load within minutes rather than the next morning,
or to follow the team's bucket → Pub/Sub → Eventarc → Cloud Run
pattern (from the team's earlier guide, now archived as
[`upcoming_work.md`](upcoming_work.md)).

A new object in the landing bucket publishes a notification to
Pub/Sub. Eventarc delivers it to the Cloud Run service, where
`handle_upload()` finds the pipeline for the object's folder and runs
it.

**The service runs the whole pipeline, not one file.**
`handle_upload()` calls `Pipeline.run()`, which takes the warehouse
lock, lists the folder, and writes the audit record and the "already
loaded" marker. Don't call `Pipeline.process()` on the uploaded file
instead: it does none of that. Eventarc delivers events at least
once, so a repeated delivery would insert the file's rows again. With
`run()`, a repeat finds the file already loaded and skips it.

```bash
gcloud storage buckets notifications create "gs://$LANDING_BUCKET" \
  --topic="projects/$PROJECT/topics/$TOPIC" \
  --payload-format=json --event-types=OBJECT_FINALIZE

gcloud run deploy "$SERVICE" \
  --project="$PROJECT" --region="$REGION" --image="$IMAGE" \
  --service-account="$SA" --no-allow-unauthenticated \
  --max-instances=1 --concurrency=1 --timeout=540 \
  --command="functions-framework" \
  --args="--target=on_upload,--signature-type=cloudevent" \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT,LANDING_BUCKET=$LANDING_BUCKET,STORE_BUCKET=$STORE_BUCKET,RAW_DATASET=$RAW_DATASET"

gcloud run services add-iam-policy-binding "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:$SA" --role="roles/run.invoker"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/eventarc.eventReceiver"

gcloud eventarc triggers create "$SERVICE-upload" \
  --project="$PROJECT" --location="$REGION" \
  --destination-run-service="$SERVICE" \
  --destination-run-region="$REGION" \
  --event-filters="type=google.cloud.pubsub.topic.v1.messagePublished" \
  --transport-topic="projects/$PROJECT/topics/$TOPIC" \
  --service-account="$SA"

SUBSCRIPTION=$(gcloud eventarc triggers describe "$SERVICE-upload" \
  --project="$PROJECT" --location="$REGION" \
  --format="value(transport.pubsub.subscription)")
gcloud pubsub subscriptions update "$SUBSCRIPTION" --ack-deadline=600
```

`notifications create` also creates the topic if it doesn't exist
and lets Cloud Storage publish to it. Whoever runs it needs at least
`pubsub.topics.create`, `pubsub.topics.get`, `pubsub.topics.publish`
and `pubsub.topics.getIamPolicy` on the topic's project, and
`storage.buckets.update` on the landing bucket (the list from the
team's earlier guide). Ask the Data Engineering team to grant them.

| Setting | Why |
|---|---|
| `--event-types=OBJECT_FINALIZE` | Only new uploads start a run, not deletes or metadata changes. `handle_upload()` ignores other event types too, and a message with no `eventType` attribute: Cloud Storage always sets it (with `bucketId`, `objectId` and others) whatever `--payload-format` is, so such a message didn't come from this notification. |
| Separate landing and store buckets (section 1) | The pipeline's own writes go to the store bucket, which has no notification, so they can't start another run. |
| `--max-instances=1 --concurrency=1` | One run at a time. When several files land together, later events wait their turn instead of being refused by the lock. Cloud Run can briefly exceed the maximum, for example during a deploy; the lock then refuses the extra run, and Pub/Sub retries it. |
| `--timeout=540`, `--ack-deadline=600` | The acknowledgement deadline starts at 10 seconds, so a longer run would be delivered again while it was still going. Keep the service timeout below the deadline. |
| Trigger created with `gcloud` | A trigger created with `gcloud` retries failed deliveries, waiting 10 seconds at first and up to 10 minutes. One created from the Cloud Run page in the console doesn't retry by default. |

What happens when:

- **Several files land together:** the first event's run loads every
  file in the folder; the later events find them loaded and skip them.
- **A file FAILED:** `handle_upload()` raises, and the event is
  retried.
- **A file is quarantined:** the event is not retried. But every later
  run of that folder (the next upload, the daily job) checks the file
  again and quarantines it again, by design (`DECISIONS_en.md`,
  "Validation errors quarantine, not fail"). So the quarantine alert
  repeats until the uploader replaces or deletes the file.
- **The service is down:** Pub/Sub keeps an undelivered event for 24
  hours by default, then drops it. The daily job loads the file
  anyway.
- **A run is killed** (for example at the timeout): its lock stays, as
  for the job (section 5), and events are refused until the lease
  expires (one hour by default) or the lock is deleted. Pub/Sub keeps
  redelivering them meanwhile, and the failed-run alert fires on each
  refusal.
- **An unreadable event, or an upload to a folder with no pipeline:**
  logged and acknowledged, not retried. This includes sub-folders,
  which `GCSSource` doesn't read, and uploads to another bucket whose
  folder has the same name: `handle_upload()` checks the event's
  bucket against the one the folder's `GCSSource` reads.

The team's earlier guide (`upcoming_work.md`, archived) puts Pub/Sub
and Cloud Run in an app project and the bucket in a data project. The same commands work across the two:
create the topic, service and trigger in the app project, and grant
the service account its roles on the data project's buckets and
dataset.

## 7. Alerts

Without alerts, a failed load or a renamed column only shows up when a
dashboard looks wrong. Set up five, all sent to the team's
notification channel, for example the team mailbox or a Teams
channel's email address:

The job and service log JSON (`configure_json_logging()`, section 5),
so the log text is in `jsonPayload.message`, not `textPayload`, and
each file ends with one summary line carrying `jsonPayload.outcome`
(an ERROR for a failed file, a WARNING for a quarantined one,
whatever path led there). Match on those fields:

| Alert | Filter (after the resource part below) | Fires when |
|---|---|---|
| Failed file | `jsonPayload.outcome="failed"` | Any file FAILED, from any cause (service error, unknown commit, audit write, ...) |
| Failed run | `severity>=ERROR AND (jsonPayload.message:"Pipeline run failed" OR jsonPayload.message:"Freshness check failed" OR jsonPayload.message:"Could not release lock")` | A run failed or was refused by the lock, the freshness check couldn't run, or a run couldn't release its lock (the next runs would be refused until its lease expires) |
| Header or data warning | `jsonPayload.message:"Contract warning:"` | A `"warn"` worksheet loaded with a mismatch, or a value loaded as NULL |
| Quarantined file | `jsonPayload.outcome="quarantined"` | A file was rejected, by the contract or the warehouse |
| Missing monthly file | `jsonPayload.message:"Missing monthly file:"` | From the 8th of the month, a monthly source with nothing loaded since the 1st; repeats daily until it loads |

Each is a log-based alert. This one is saved as
`contract-warning.json` and created with
`gcloud alpha monitoring policies create --policy-from-file=...`:

```json
{
  "displayName": "HWW ingestion: contract warning",
  "combiner": "OR",
  "conditions": [{
    "displayName": "Contract warning logged",
    "conditionMatchedLog": {
      "filter": "((resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"<CLOUD_RUN_JOB_NAME>\") OR (resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"<CLOUD_RUN_SERVICE_NAME>\")) AND jsonPayload.message:\"Contract warning:\""
    }
  }],
  "alertStrategy": {"notificationRateLimit": {"period": "3600s"}},
  "notificationChannels": ["<NOTIFICATION_CHANNEL_ID>"]
}
```

The filter matches the job's logs and the service's: a job logs as
`cloud_run_job`, a service as `cloud_run_revision`. Without section 6,
drop the service half. Repeat with the other filters. If a job
logs plain text instead (no `configure_json_logging()`), use
`textPayload:"..."` with the message texts; the outcome filters need
JSON logging.

**Test each alert once** before relying on it. The smoke test
(`docs/SMOKE_TEST.md`, `scripts/demo.py`) logs JSON like the
production job; run it as a Cloud Run job so its lines carry the
`cloud_run_job` resource, then check each filter in the Logs Explorer
and that its notification arrives:

| Alert | How to trigger it |
|---|---|
| Quarantined file | The smoke test's invalid workbook (step 4) |
| Header or data warning | Upload a workbook with an extra column to a `"warn"` source |
| Failed file | Run once with the job's BigQuery access removed, or point it at a dataset with no `audit` table |
| Failed run | Start a second run while one holds the lock |
| Missing monthly file | Temporarily set the freshness view's due day so a source is overdue |

The quarantine check alone doesn't prove the others. The same
alerts can be created in the console under *Logging → Logs Explorer →
Create alert*.

### Missing monthly files

A log-based alert can't fire on something that didn't happen, so the
daily job checks. `check_freshness()` reads a view with one row per
monthly source and logs `Missing monthly file: <source> (last loaded
<date>)` for each overdue one. The view reads each source's own raw
table, which is the simplest thing to name per source (the audit
table would also do, by its `destination` column).

```bash
bq query --use_legacy_sql=false "
CREATE OR REPLACE VIEW \`$FRESHNESS_VIEW\` AS
WITH loads AS (
  SELECT 'signups' AS source, MAX(ingested_at) AS last_loaded_at
  FROM \`$PROJECT.$RAW_DATASET.<SIGNUPS_RAW_TABLE>\`
  UNION ALL
  SELECT 'was', MAX(ingested_at)
  FROM \`$PROJECT.$RAW_DATASET.<WAS_RAW_TABLE>\`
),
month AS (
  SELECT DATE_TRUNC(CURRENT_DATE('Europe/London'), MONTH) AS first_day
)
SELECT
  source,
  last_loaded_at,
  CURRENT_DATE('Europe/London') > DATE_ADD(first_day, INTERVAL 6 DAY)
    AND (last_loaded_at IS NULL
         OR DATE(last_loaded_at, 'Europe/London') < first_day) AS overdue
FROM loads CROSS JOIN month"
```

- **One `UNION ALL` line per monthly source.** For a workbook with
  several sheets, one sheet's table is enough. Leave out sources that
  arrive only now and then, such as event sign-ups.
- **Due by the 7th.** `INTERVAL 6 DAY` makes a source overdue from the
  8th. Change it to move the due day.
- **Add a source after its first file has loaded.** The view can't
  name a raw table that doesn't exist yet.
- **What counts as arrived:** a file that loaded. A quarantined upload
  doesn't count (its own alert has fired). A new upload always
  loads, even with the same bytes as last month's, so it counts.

The view also gives each source's "last updated" date, which the
dashboard can show. Dataform can publish it with the cleaned tables,
as the raw dataset is restricted.

This replaces an earlier BigQuery scheduled query that raised an
error when nothing had loaded for 35 days. That query covered all
sources at once, so one source loading hid another that was missing.
And a scheduled query's failure email goes only to the account that
created it ([BigQuery docs](https://cloud.google.com/bigquery/docs/transfer-run-notifications)),
not to the team.

## 8. Check it works

1. Generate fixtures (see [`GETTING_STARTED.md`](GETTING_STARTED.md))
   and upload one, e.g.
   `gcloud storage cp tests/data/fixtures/hww_personal_data.xlsx gs://$LANDING_BUCKET/peers/`.
2. Run the job: `gcloud run jobs execute "$JOB" --region="$REGION" --wait`.
3. Check the rows arrived and the excluded columns didn't:

   ```bash
   bq query --use_legacy_sql=false "
   SELECT source_file, row_number, TO_JSON_STRING(payload) AS payload
   FROM \`$PROJECT.$RAW_DATASET.<RAW_TABLE>\`
   ORDER BY ingested_at DESC LIMIT 10"
   ```

4. Run the job again: the logs should say the file was skipped as
   already processed, and the table should have no new rows.
5. With section 6: upload a different fixture to the same folder. The
   service's logs should show `Upload gs://...: running ...`, then a
   `Run for ...` line counting one file loaded.
6. With section 6: upload two files to the same folder at once. One
   run should load both, the other event should skip them, and the
   raw table should hold each file's rows once. This hasn't yet been
   tried against live GCP.
7. Query the freshness view: `bq query --use_legacy_sql=false "SELECT *
   FROM \`$FRESHNESS_VIEW\`"`. After the 7th, a source with no load
   this month shows `overdue = true`, and the next job run logs
   `Missing monthly file:` for it.

## See also

- [`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md): what the raw tables
  contain.
- [`LOCAL_GCP_AUTHENTICATION.md`](LOCAL_GCP_AUTHENTICATION.md): running
  against GCP from your own machine.
- [`SMOKE_TEST.md`](SMOKE_TEST.md): the end-to-end smoke test for the
  typed (Parquet) route.
- [`upcoming_work.md`](upcoming_work.md): the team's Cloud Run guide
  for data landing in a bucket.
