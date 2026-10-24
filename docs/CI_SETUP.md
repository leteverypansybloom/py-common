# Live BigQuery tests in GitHub Actions

*For whoever sets up or looks after py-common's automated tests.*

The unit tests check py-common's BigQuery SQL against a stand-in for
BigQuery. The live tests (`tests/integration/test_bigquery_live.py`)
run it in real BigQuery, in a test dataset, and check what lands:

- each upload loads once, a revert loads again, and a table added
  later loads on its own;
- a failure inside the commit rolls back everything, the "already
  loaded?" check refuses a repeat even with the store's markers
  gone, the documented "latest upload" query reads one load per
  upload, and `check_duplicate_loads()` finds a double load.

They run every night at 03:17 UTC and whenever you press **Run
workflow** (GitHub → Actions → *Live BigQuery tests*). They don't
run on pull requests, so code in a pull request never gets the
credentials.

**Cost.** Each run sends a few dozen small queries, well under 1 GB
in all, so pennies at most, or nothing within BigQuery's free monthly
allowance. Every table a run creates is deleted at the end.

**Signing in without a key.** GitHub Actions gets a short-lived
pass from Google Cloud on each run (Workload Identity Federation),
for one service account that can only use the test dataset. No key
file is created or stored anywhere.

## One-off set-up

Run these once, as someone who can manage IAM in the project
(Cloud Shell works). Use the same region as your other datasets.

```bash
PROJECT="<GCP_PROJECT_ID>"
REGION="<REGION>"                 # e.g. europe-west2
TEST_DATASET="py_common_ci"
REPO="<GITHUB_ORG>/py-common"     # the repository's owner/name
SA_NAME="py-common-ci"
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" \
  --format='value(projectNumber)')"
```

**1. The APIs** (already on if you've followed `GCP_SETUP.md`, apart
perhaps from the last two):

```bash
gcloud services enable bigquery.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com --project="$PROJECT"
```

**2. The test dataset.** Tables expire after a day, so anything a
failed run leaves behind cleans itself up:

```bash
bq --location="$REGION" mk --dataset \
  --default_table_expiration 86400 \
  --description "py-common live tests (GitHub Actions)" \
  "$PROJECT:$TEST_DATASET"
```

**3. A service account that can use only that dataset** (and run
query jobs, which BigQuery grants per project):

```bash
gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
  --display-name="py-common live tests"

bq add-iam-policy-binding --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataEditor" "$PROJECT:$TEST_DATASET"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
```

**4. Let this repository's workflows act as it, without a key.** A
workload identity pool for GitHub, a provider that only admits this
repository, and permission for the repository to use the service
account:

```bash
gcloud iam workload-identity-pools create github --project="$PROJECT" \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc py-common \
  --project="$PROJECT" --location=global \
  --workload-identity-pool=github \
  --display-name="py-common repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository == '$REPO'"

gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --project="$PROJECT" --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
```

If the project already has a `github` pool, skip creating it and add
the provider to it.

**5. Tell GitHub where to sign in.** These are repository
*variables*, not secrets: none of them is a password. Either in
GitHub (Settings → Secrets and variables → Actions → Variables → New
repository variable), or with the `gh` command line:

```bash
gh variable set GCP_PROJECT_ID --repo "$REPO" --body "$PROJECT"
gh variable set BQ_TEST_DATASET --repo "$REPO" --body "$TEST_DATASET"
gh variable set GCP_SERVICE_ACCOUNT --repo "$REPO" --body "$SA"
gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$REPO" \
  --body "projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/py-common"
```

Until `GCP_WORKLOAD_IDENTITY_PROVIDER` is set, the workflow is
skipped rather than failing.

## First run

GitHub runs a scheduled or manual workflow only once it is on the
default branch, so the first run follows merging
`.github/workflows/bigquery-live.yml` into `main`. Then press **Run
workflow** on the Actions tab. A green run took the tests through
real BigQuery; a red one shows which check failed.

To run the tests before that, from your own machine:

```bash
gcloud auth application-default login
GCP_PROJECT_ID="$PROJECT" BQ_TEST_DATASET="$TEST_DATASET" \
  pytest -m integration tests/integration/test_bigquery_live.py -v
```

Your own account then needs the same two roles as the service
account.

## When a run fails

GitHub emails a failed scheduled run to whoever last changed its
schedule, and shows it on the Actions tab.

- **"Sign in to Google Cloud" failed:** a repository variable is
  wrong, or step 4's binding is missing. The error names which.
- **A test failed:** BigQuery didn't do what py-common expects. If
  nothing in py-common changed since the last green run, BigQuery or
  a pinned library may have; either way, look before the next
  release, as the production pipeline runs the same SQL.

## Changing when it runs

The schedule is the `cron` line in
`.github/workflows/bigquery-live.yml` (times are UTC). To also run on
every pull request, add `pull_request:` under `on:`. Pull requests
from branches of this repository would then get the credentials
(still only for the test dataset); pull requests from forks don't.
