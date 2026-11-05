# Dataform on py-common's raw tables

*For whoever builds and runs the cleaning and modelling layer of a
project that lands its files with py-common.*

py-common stops at the raw load: one raw JSON table per worksheet,
plus the audit table ([`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md)).
Everything after that, typing values, applying business rules,
merging duplicates and building the tables reports read, belongs in
GCP Dataform. This page is the pattern to follow; the HWW project's
`dataform/` folder and its `docs/dataform.md` are a worked example.

## Layers

| Layer | Dataset (example) | Holds | Written by |
|---|---|---|---|
| Bronze | `raw_<project>` | The raw tables and `audit` | py-common only. Dataform declares them and only reads. |
| Silver | `<project>_silver` | Reference tables (mappings), the load each source reads, one cleaned and typed row per source row, data quality views | Dataform |
| Gold | `<project>_gold` | The tables reports read, with fixed schemas and keys | Dataform |
| Checks | `<project>_assertions` | Dataform assertions | Dataform |

Report readers get access to gold only.

## Reading bronze

- **Select the load from the audit table**, not from the raw table
  alone, so an upload with no rows counts as the newest upload
  ([`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md), "Latest upload, empty
  uploads included"). Put the selection in one silver view (one row
  per raw table: file, generation, load time, row count) and join each
  staging model to it.
- **Read one agreed file name per workbook**, the contract's
  `file_name`, so an old name's last upload is never read.
- **Check the read is complete**: an assertion that the selected
  load's row count equals the audit row's `row_count`, and that every
  raw table has a selected load.
- **Read payload values by their exact key**: `payload['Organisation
  Industry ']` keeps the heading's trailing space. A blank heading is
  `col_B`, a repeated one `Other_2`.
- **CSV and JSON sources**: a CSV file's values are all strings; a
  JSON file's keep their JSON types, nested objects and arrays
  included ([`FILE_FORMATS.md`](FILE_FORMATS.md), "In Dataform").
- **Tidy and type in silver**: `JSON_VALUE` for text, then trim and
  turn blanks into `NULL`; `SAFE.PARSE_DATE('%Y-%m-%d', ...)` on the
  first ten characters for a column the contract types as a date. Keep
  each rule in one JavaScript function in `includes/` so every model
  applies it the same way.

## Layout

```
workflow_settings.yaml   project, location, datasets, settings as vars
includes/                shared SQL snippets and settings
definitions/
  sources/               declare() each raw table and audit
  reference/             mapping tables
  staging/               the selected loads, one model per source
  intermediate/          joins shared by several gold tables
  gold/                  the reporting tables
  assertions/            stops and output checks
```

Mapping files kept as CSV in the project's repository can't be read
by Dataform. Generate a JavaScript file from them (a short Node script
with no dependencies), build the reference tables from it, commit
both, and have CI fail when the generated file is stale.

## Stops and checks

- **Stops** are assertions on inputs: a missing load, an empty source,
  a broken mapping, a repeated id. Make every gold table depend on
  them (`dependencies: [...]` and `dependOnDependencyAssertions: true`
  in its config), so a failing stop leaves the previous gold tables in
  place.
- **Output checks** run on each gold table: required columns, unique
  keys, column names and types against the agreed schema (from the
  dataset's `INFORMATION_SCHEMA.COLUMNS`). Keep each check to one
  table: a check reading several gold tables, combined with
  `dependOnDependencyAssertions`, makes a dependency cycle.
- **Warnings** (unmapped values, unexpected columns) go to a silver
  view, one row per kind of issue, instead of failing the run.
- If an earlier implementation exists, load its outputs into an
  `expected` dataset and add assertions that compare each gold table
  with it both ways (`EXCEPT DISTINCT`) and by row count.

Gold tables are replaced one by one, not in one transaction. A run
that fails part-way can leave a mix of new and previous tables until
the next run; the stops make sure bad inputs never get that far.

## In GCP

**Repository root.** Dataform reads `workflow_settings.yaml` from the
root of the connected Git repository. If the project is a folder of a
larger repository, publish that folder as a branch whose root is the
folder, and connect Dataform to the branch:

```bash
git push origin "$(git subtree split --prefix dataform main):refs/heads/dataform"
```

Make changes on `main` and publish again; never commit to the branch.

**Service account.** Run workflows as a dedicated service account
with:

| Role | On |
|---|---|
| `roles/bigquery.jobUser` | The project that runs the queries |
| `roles/bigquery.dataViewer` | The raw dataset |
| `roles/bigquery.dataEditor` | The silver, gold and assertions datasets |

The Dataform service agent
(`service-<project-number>@gcp-sa-dataform.iam.gserviceaccount.com`)
needs `roles/iam.serviceAccountTokenCreator` on that account, and
`roles/secretmanager.secretAccessor` on the secret holding the Git
token. The pipeline's own service account doesn't need access to the
Dataform datasets, nor Dataform's to the store bucket.

**Schedule.** A release configuration compiles the branch (with the
project, a dataset suffix for development and any variables as
compilation overrides); a workflow configuration runs it on a
schedule, after the ingestion job's. Alert on failed workflow runs as
on failed ingestion runs ([`GCP_SETUP.md`](GCP_SETUP.md), section 7).

**Location.** Create the Dataform repository and the datasets in the
same location as the raw dataset.

**Execution account.** A custom Dataform execution account needs the
Service Account User and Service Account Token Creator roles granted to
the Dataform service agent on that account
([access control](https://docs.cloud.google.com/dataform/docs/access-control)).
