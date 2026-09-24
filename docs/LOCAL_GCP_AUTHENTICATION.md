# Local GCP authentication

This guide explains how to authenticate a local development machine so
`py-common` can access Google Cloud Storage (GCS) and BigQuery.

It applies when you run Python from a terminal or from VS Code on your
own machine. It does not apply to Cloud Run: a deployed job should use
a dedicated service account instead.

## How local authentication works

The Google Cloud Python libraries use **Application Default Credentials
(ADC)**. On Windows, the Google Cloud CLI normally stores ADC here:

```text
C:\Users\<your-user>\AppData\Roaming\gcloud\application_default_credentials.json
```

This file contains credentials for the Google account you choose during
sign-in. Do not create or edit it by hand, commit it to Git, or share
its contents.

In the current codebase, `GCSObjectStore` and `BigQueryWarehouse` create
Google Cloud clients directly. Those clients automatically look for ADC;
they do not currently call `py_common.gcp_auth`.

## Create or refresh the credentials file

1. Ask IT to install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install).
2. In PowerShell or the VS Code integrated terminal, run:

   ```powershell
   gcloud auth application-default login
   ```

3. Complete the browser sign-in using the work Google account that has
   access to the intended GCP project.

The command creates the file if it is missing, or updates it if you
sign in again. If the file already exists and is for the correct account,
you do not need to run the command again.

## Test the credentials

First, check that ADC can obtain a token:

```powershell
gcloud auth application-default print-access-token
```

Expect a long token-like value. Do not copy it into a message, issue or
commit.

Then set the resource locations for the current PowerShell session:

```powershell
$env:GCP_PROJECT_ID = "YOUR_PROJECT_ID"
$env:GCS_RAW_BUCKET = "YOUR_BUCKET_NAME"
$env:BQ_DATASET = "YOUR_DATASET_NAME"
```

`GCS_RAW_BUCKET` may be either `your-bucket-name` or
`gs://your-bucket-name` when passed to `GCSObjectStore`. For general
Google Cloud commands, prefer the bucket name without `gs://`.

The following command checks read access using the same Google Cloud
Python libraries used by the GCS and BigQuery adapters. It does not
write, alter or delete anything:

```powershell
@'
import os
from google.cloud import bigquery, storage

project = os.environ["GCP_PROJECT_ID"]
bucket_name = os.environ["GCS_RAW_BUCKET"].removeprefix("gs://")
dataset_name = os.environ["BQ_DATASET"]

storage.Client(project=project).get_bucket(bucket_name)
dataset = bigquery.Client(project=project).get_dataset(dataset_name)

print(f"GCS OK: {bucket_name}")
print(f"BigQuery OK: {dataset.full_dataset_id}")
'@ | python
```

Expected output is similar to:

```text
GCS OK: example-dev-bucket
BigQuery OK: example-project:example_dataset
```

## Troubleshooting

### The token command fails

Run:

```powershell
gcloud auth application-default login
```

again, ensuring that you select the right work account.

### GCS or BigQuery access is denied

ADC proves who you are, not what you are permitted to do. Ask the GCP
administrator to check that your account has the required roles on the
specific bucket and dataset.

## Production deployment

Do not copy the local ADC file into a container or Cloud Run service.
Cloud Run should run under a dedicated service account with narrowly
scoped GCS and BigQuery permissions.
