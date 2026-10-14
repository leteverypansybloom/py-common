# GCP setup for the raw JSON route

How to set up Google Cloud for a py-common pipeline that reads Excel
files from a landing bucket and loads them into raw BigQuery tables
(`GCSSource` → `Pipeline(output_format="raw_json")` →
`RawJsonWarehouse`). Written for the Healthy Working Wales (HWW)
project, but nothing here is specific to it.

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
| Cloud Run job + Cloud Scheduler | Runs the pipeline on a schedule | |
| Alerts | Failed runs, warnings, a missing monthly file | Whoever supports the pipeline |

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
IMAGE="<IMAGE_URI>"                 # the solution repo's container image
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
  row_count INT64,
  outcome STRING,
  timestamp TIMESTAMP
)"
```

The raw tables themselves are created by `RawJsonWarehouse` on first
load, partitioned by day of `ingested_at`. Their layout is in
[`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md).

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
| `roles/storage.objectAdmin` | Store bucket | Write copies and audit records; create and delete the run lock |
| `roles/bigquery.dataEditor` | Raw dataset | Create raw and staging tables; insert rows and audit records |
| `roles/bigquery.jobUser` | Project | Run load jobs and the commit transaction |

```bash
gcloud storage buckets add-iam-policy-binding "gs://$LANDING_BUCKET" \
  --member="serviceAccount:$SA" --role="roles/storage.objectViewer"

gcloud storage buckets add-iam-policy-binding "gs://$STORE_BUCKET" \
  --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"

bq add-iam-policy-binding --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataEditor" "$PROJECT:$RAW_DATASET"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
```

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
and a small entry point. An outline for HWW:

```python
"""Load the HWW monthly workbooks into raw BigQuery tables."""

import logging
import os
import sys

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore, GCSSource
from py_common.model import Outcome
from py_common.pipeline import Pipeline

from hww.contracts import PIPELINES  # (contract, tables) per workbook


def main() -> int:
    """Run every HWW pipeline once.

    Returns:
        Exit code: 1 if any file FAILED, so the job run shows failed.
    """
    logging.basicConfig(level=logging.INFO)
    project = os.environ["GCP_PROJECT_ID"]
    store = GCSObjectStore(project, os.environ["STORE_BUCKET"])
    failed = False
    for prefix, contract, tables in PIPELINES:
        pipeline = Pipeline(
            source=GCSSource(project, os.environ["LANDING_BUCKET"], prefix),
            store=store,
            warehouse=RawJsonWarehouse(
                project, os.environ["RAW_DATASET"], tables
            ),
            contract=contract,
            output_format="raw_json",
        )
        results = pipeline.run()
        failed |= any(r.outcome == Outcome.FAILED for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
```

Give each workbook its own landing folder (`prefix`), so each
pipeline lists only the files its contract describes. The container
installs `py-common[gcp]`; see the repository's `Dockerfile`.

## 5. Cloud Run job and schedule

A Cloud Run *job* runs to completion and stops, which suits a batch
that finds a handful of files.

```bash
gcloud run jobs deploy "$JOB" \
  --project="$PROJECT" --region="$REGION" --image="$IMAGE" \
  --service-account="$SA" --tasks=1 --max-retries=0 \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT,LANDING_BUCKET=$LANDING_BUCKET,STORE_BUCKET=$STORE_BUCKET,RAW_DATASET=$RAW_DATASET"

gcloud run jobs execute "$JOB" --project="$PROJECT" --region="$REGION" --wait
```

`--max-retries=0` because a rerun is safe but should be deliberate:
files that loaded are skipped by checksum, and files that failed are
retried on the next scheduled run.

Run it daily. Files arrive monthly, but a daily run picks up a late or
corrected upload the next morning, and a run with nothing new costs
almost nothing.

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
object in the store bucket, and later runs are refused with an error
naming it. Check that no run is active, then delete the
`gs://$STORE_BUCKET/locks/...` object named in the error.

## 6. Alerts

Without alerts, a failed load or a renamed column only shows up when a
dashboard looks wrong. Set up three, all sent to the team's
notification channel:

| Alert | How | Fires when |
|---|---|---|
| Failed run | Log-based alert on `"Processing error for"` or `"Pipeline run failed"` | A file FAILED, or the run itself failed |
| Header or data warning | Log-based alert on `"Contract warning:"` | A `"warn"` worksheet loaded with a mismatch |
| Quarantined file | Log-based alert on `"Validation failed for"` | A `"quarantine"` worksheet rejected a file |
| Missing monthly file | BigQuery scheduled query (below) | No file loaded in 35 days |

A log-based alert, saved as `contract-warning.json` and created with
`gcloud alpha monitoring policies create --policy-from-file=...`:

```json
{
  "displayName": "HWW ingestion: contract warning",
  "combiner": "OR",
  "conditions": [{
    "displayName": "Contract warning logged",
    "conditionMatchedLog": {
      "filter": "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"<CLOUD_RUN_JOB_NAME>\" AND textPayload:\"Contract warning:\""
    }
  }],
  "alertStrategy": {"notificationRateLimit": {"period": "3600s"}},
  "notificationChannels": ["<NOTIFICATION_CHANNEL_ID>"]
}
```

Repeat with the other filters for failures and quarantines. The same
alerts can be created in the console under *Logging → Logs Explorer →
Create alert*.

For the missing-file alert, a log-based alert can't fire on something
that didn't happen. Instead, schedule this query daily in BigQuery
(*Scheduled queries*, with email notification on failure turned on).
It fails, and so notifies, when nothing has loaded for 35 days:

```sql
SELECT IF(
  COUNT(*) = 0,
  ERROR('HWW: no file loaded in the last 35 days'),
  'ok'
)
FROM `<PROJECT_ID>.<RAW_DATASET>.audit`
WHERE outcome = 'LOADED'
  AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 35 DAY)
```

## 7. Check it works

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

## Alternative: run on upload with Eventarc

[`upcoming_work.md`](upcoming_work.md) describes an event-driven
pattern: an upload to the bucket publishes to Pub/Sub, and Eventarc
calls a Cloud Run service for that one file. py-common doesn't build
the trigger, but `Pipeline.process()` handles one file and could sit
behind it. Before choosing it, consider:

- **The lock:** `Pipeline.run()` holds one lock per warehouse for the
  whole run, and the lock never expires. With events, several uploads
  at once would be refused and retried, and a killed instance would
  block every later upload until someone deletes its lock.
- **Loops:** the trigger must be limited to the landing folder, or the
  pipeline's own writes would trigger it again. Separate buckets, as
  above, avoid this.
- **Need:** for monthly files, a daily schedule is simpler and loses
  nothing that matters.

## See also

- [`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md): what the raw tables
  contain.
- [`LOCAL_GCP_AUTHENTICATION.md`](LOCAL_GCP_AUTHENTICATION.md): running
  against GCP from your own machine.
- [`SMOKE_TEST.md`](SMOKE_TEST.md): the end-to-end smoke test for the
  typed (Parquet) route.
