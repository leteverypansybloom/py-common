# Excel to GCS and BigQuery smoke test

This guide explains the end-to-end smoke test: how it creates test Excel
workbooks, what each result demonstrates, and how data moves through the
pipeline.

Run it only against development GCS and BigQuery resources.

## Before you run it

1. **Audit table columns.** The audit table records `destination`
   and `source_version`. The smoke test checks for them and stops
   with the `ALTER TABLE` to run if they're missing;
   `python scripts/demo.py --migrate` adds them for you (safe to run
   twice). The table itself is created as in `docs/GCP_SETUP.md`,
   section 2.
2. **Environment.** `GCP_PROJECT_ID`, `GCS_RAW_BUCKET` (the store
   bucket) and `BQ_DATASET`, and credentials
   (`gcloud auth application-default login`) that can write to both.
3. **Start with a fresh dataset** (or delete `smoke_events_raw` and
   the `audit` rows from earlier runs). Each load is staged into its
   own `smoke_events_raw_stg_...` table, which is dropped afterwards.
4. **Optional, before deploying anything:** dry-run the SQL the
   adapter generates against the same project.

   PowerShell:

   ```powershell
   $env:GCP_PROJECT_ID = "my-sandbox"
   pytest -m integration tests/integration/test_bq_dry_run.py
   ```

   Cloud Shell (bash):

   ```bash
   GCP_PROJECT_ID=my-sandbox pytest -m integration tests/integration/test_bq_dry_run.py
   ```

Then, from the repository root:

```bash
python scripts/demo.py --migrate
```

It prints `ok:` for each check and stops at the first `FAIL:`.

## How the smoke-test Excel files are created

The demo creates two local Excel files in a uniquely named folder, for
example:

```text
manager-demo-input-20260923093000/
```

### Valid workbook

The script:

- Copies the known-good fixture: `tests/data/fixtures/events_valid.xlsx`.
- Renames it to include the demo run ID.
- Replaces its event IDs with new values, for example:
  - `DEMO-20260923093000-001`
  - `DEMO-20260923093000-002`
- Sets the attendee values to `10` and `20`.

This gives you a structurally valid workbook with fresh IDs, so its results
cannot be confused with an earlier test run.

### Invalid workbook

Later, the script copies:

```text
tests/data/fixtures/events_missing_column.xlsx
```

This workbook deliberately lacks the required `attendees` column. It tests
that invalid input is caught and isolated before it reaches BigQuery.

## What each test demonstrates

| Step | Expected result | What it proves |
| --- | --- | --- |
| 0. Audit schema | `destination`, `source_version` present | The table has the columns the "already loaded?" check uses (or `--migrate` added them). |
| 1. Valid file | `loaded 2`; raw and processed copies; two rows in `smoke_events_raw`; one audit row naming it | Validate, convert, store, insert and audit work end to end. |
| 2. Rerun unchanged file | `skipped 0` | The store marker skips a repeat. |
| 3. Markers deleted, rerun | `skipped 0`, still two raw rows | BigQuery's own audit check stops a second load even when the store marker is lost. |
| 4. Invalid workbook | `quarantined 0`, copy under `quarantine/` | Invalid data is kept for investigation and never reaches BigQuery. |
| 5. Same bytes, new version | `loaded 2`, four raw rows over two `source_generation`s | A revert becomes the latest generation instead of being skipped. |
| 6. Staging clean-up | No `_stg_` table left for this file | Per-load staging tables are dropped after the load. |
| 7. A copy under another name (`file_name` set) | The copy `quarantined 0`; the original `skipped 0` | With `Contract.file_name`, an upload under any other name is quarantined, so it can't become a second "latest" upload. |

## The journey through the system

### 1. A local folder acts as the source

For the demo, Excel files sit in a local folder. `LocalSource` finds `.xlsx`
files, records their filename, modification version and size, then reads their
contents.

In production the source is a GCS landing bucket (`GCSSource`). The pipeline
is designed so that the source can be swapped without changing the
validation, storage or BigQuery logic.

### 2. The upload's identity and version decide whether it's new

The pipeline identifies an upload by the file's identity at the source and
its version (for `LocalSource`, the file name plus its modification time and
size; for `GCSSource`, the object name and generation). Before downloading,
it looks in GCS for a marker for that upload under
`audit/<warehouse identity>/versions/`:

- If the marker exists, the file is skipped without being downloaded.
- If it does not, the file is downloaded and its SHA-256 checksum, a
  fingerprint of the contents, is computed. The checksum names the stored
  copies and is recorded with the load.

This prevents accidental double loading when the pipeline is rerun with an
unchanged file. A new version always loads, even if its bytes repeat an
earlier one (step 5). The marker is only a cache: BigQuery checks its own
audit table inside the commit transaction as well, so a lost marker can't
cause a second load (step 3 deletes the markers to prove it).

### 3. The Excel workbook is validated against a contract

The `Contract` describes what a valid workbook must contain:

- A worksheet called `Events`.
- Columns called `event_id`, `employer_id` and `attendees`.
- Non-empty required values.
- Integer `attendees` values.
- Unique event IDs.

This separates data rules from the mechanics of moving data. A future feed
can use the same pipeline with a different contract.

### 4. Invalid files are quarantined

If validation fails, the original Excel file is retained under `raw/` and
copied to `quarantine/`. It is not converted or sent to BigQuery.

This retains evidence of what was received and gives the data owner something
to correct, while protecting the raw tables from invalid data.

### 5. Valid files are kept as raw source data

For a valid file, the original workbook is saved under:

```text
raw/<identity>/<checksum>/<filename>.xlsx
```

`<identity>` is the file's ID at the source (the file name for a local
folder, the object name for GCS) and `<checksum>` is the SHA-256 of its
bytes. If the file is edited and supplied again, the new version is stored
next to the old one, so both are kept.

Keeping the raw file means you can show what was supplied, rerun the process,
investigate a problem, or change transformation logic without asking the
source system to resend the file.

### 6. Every worksheet is converted to JSON rows

The pipeline reads each configured worksheet and writes one JSON line per
row (`py_common.raw.to_ndjson()`), stored under:

```text
processed/<warehouse identity>/<identity>/<checksum>/<version>/<digest>/<filename>.xlsx.ndjson
```

Each line holds the row's file, generation, sheet, row number, checksum and
a `payload` of its loaded columns (see `docs/RAW_TABLE_LAYOUT.md`). No load
time is written, so the same workbook always produces the same bytes, and a
failed load is retried with the same copy. `<digest>` is the start of the
copy's own SHA-256: if the contract or a library changes what the file
converts to, the new result is stored next to the old one instead of
clashing with it.

### 7. Rows are loaded through a BigQuery staging table

Each worksheet's rows are first loaded into a staging table of its own:

```text
smoke_events_raw_stg_<checksum[:12]>_<random>
```

It expires after a day and is dropped as soon as the load finishes.

The pipeline checks that the staging tables hold the expected number of rows
before it inserts anything. This provides an early check that the conversion
and warehouse load have not silently lost or added rows.

### 8. Rows are inserted into the raw table and audited

In one transaction, the staged rows are inserted into:

```text
smoke_events_raw
```

with `ingested_at` set to the same UTC timestamp as the audit entry (the
pipeline's clock just before the commit job starts), and the audit row is
written. Rows are only ever inserted: each upload adds its rows under its own
`source_generation`, and Dataform picks the latest upload.

The insert and the BigQuery audit entry are committed together. This avoids
a successful-looking data load without a corresponding audit record.

### 9. Audit records provide traceability

GCS receives:

- A detailed audit JSON record for each outcome: loaded, skipped, quarantined
  or failed.
- A short marker for each loaded upload (identity and version), used to
  skip it on later runs.

BigQuery receives a warehouse audit record for successful loads.

## Key design choices

- **Keep raw and processed data:** Preserve the original Excel workbook for
  traceability, and the JSON rows exactly as they were loaded.
- **Validate before loading:** Stop invalid input reaching the raw tables.
- **Quarantine rather than discard:** Retain invalid files so problems can be
  understood and fixed.
- **Load each upload once:** The same upload (file version) is never loaded
  twice, but a new upload always loads, even one that restores earlier
  content.
- **Clean and type in Dataform:** The raw tables keep Excel's values as they
  are; Dataform casts them and picks the latest upload.
- **Use a staging table:** Check incoming data before it reaches the raw
  table.
- **Use UTC timestamps:** Avoid ambiguity across machines, regions and
  daylight-saving changes.
- **Keep components separate:** Source, validation, storage and warehouse
  loading can evolve independently.

## Current constraint

The GCS lock is a real exclusive lock (a lock object under `locks/` in the
bucket). A second run started while one is active fails with a `RuntimeError`.
The lock is a one-hour lease: if a run is killed without releasing it, the
first run after the lease expires takes it over. To carry on sooner, delete
the lock object named in the error once you have confirmed no run is active.

## Where the loaded data is stored

The loaded data is in `smoke_events_raw`. For the smoke test, each Excel row
becomes a row in that table:

| source_file | source_generation | row_number | payload | ingested_at |
| --- | --- | ---: | --- | --- |
| `demo_valid_....xlsx` | `<mtime_ns>-<size>` | 2 | `{"event_id":"DEMO-...-001",...}` | UTC timestamp |
| `demo_valid_....xlsx` | `<mtime_ns>-<size>` | 3 | `{"event_id":"DEMO-...-002",...}` | UTC timestamp |

The table also has `sheet_name` and `checksum`. `audit` is separate and
records metadata about each successful load, including filename, checksum,
row count, outcome and time.

The `smoke_events_raw_stg_...` tables exist only during a load. They are not
intended for users or downstream analysis.

```text
Excel -> raw GCS -> processed NDJSON in GCS -> smoke_events_raw_stg_... -> smoke_events_raw
                                                          \
                                                           -> audit metadata
```
