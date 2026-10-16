# Excel to GCS and BigQuery smoke test

This guide explains the end-to-end smoke test: how it creates test Excel
workbooks, what each result demonstrates, and how data moves through the
pipeline.

Run it only against development GCS and BigQuery resources.

## Before you run it (py-common 0.2.0)

1. **Audit table columns.** 0.2.0 records `destination` and
   `source_version` in the audit table. The smoke test checks for
   them and stops with the `ALTER TABLE` to run if they're missing;
   `python scripts/demo.py --migrate` adds them for you (safe to run
   twice). The same migration is in `docs/GCP_SETUP.md`, section 2.
2. **Environment.** `GCP_PROJECT_ID`, `GCS_RAW_BUCKET` (the store
   bucket) and `BQ_DATASET`, and credentials
   (`gcloud auth application-default login`) that can write to both.
3. **Start with a fresh dataset** (or delete `events_final`,
   `smoke_events_raw`, any `events_final_staging` and the `audit`
   rows from earlier runs). 0.2.0 adds `_deleted_at` and
   `_source_file` to `events_final` itself, stages each load into its
   own `events_final_stg_...` table and drops it afterwards.
4. **Optional, before deploying anything:** dry-run the SQL the
   adapters generate against the same project:

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
| 0. Audit schema | `destination`, `source_version` present | The table matches 0.2.0 (or `--migrate` fixed it). |
| 1. Valid file, typed route | `loaded 2`; raw and processed copies; two `DEMO-...` rows; one audit row naming `events_final` | Validate, convert, store, MERGE and audit work end to end. |
| 2. Rerun unchanged file | `skipped 0` | The store marker skips a repeat. |
| 3. Markers deleted, rerun | `skipped 0`, still one audit row | BigQuery's own audit check stops a second load even when the store marker is lost (the main 0.2.0 fix). |
| 4. Invalid workbook | `quarantined 0`, copy under `quarantine/` | Invalid data is kept for investigation and never reaches BigQuery. |
| 5. Valid file, raw JSON route | `loaded 2`, two rows in `smoke_events_raw` | The raw route works live (it hadn't run live before). |
| 6. Markers deleted, rerun | `skipped 0`, still two raw rows | The same generation is never inserted twice. |
| 7. Same bytes, new version | `loaded 2`, four raw rows over two `source_generation`s | A revert becomes the latest generation instead of being skipped. |
| 8. Staging clean-up | No `_stg_` table left for this file | Per-load staging tables are dropped after the load. |
| 9. Same file, one row changed, one removed (typed) | `loaded 1`; the changed row has the new value; the removed row has `_deleted_at` set | Cumulative files update rows, and rows that disappear are marked deleted rather than left looking current. |
| 10. Original file again (typed) | `loaded 2`; the original value is back; the removed row's `_deleted_at` is cleared | Restoring an earlier upload applies it again instead of being skipped. |
| 11. A copy under another name (typed, `file_name` set) | The copy `quarantined 0`; the original `skipped 0` | With `Contract.file_name`, an upload under any other name is quarantined, so it can't become a second "latest" upload. |

## The journey through the system

### 1. A local folder acts as the source

For the demo, Excel files sit in a local folder. `LocalSource` finds `.xlsx`
files, records their filename, modification version and size, then reads their
contents.

This imitates the role SharePoint will eventually play. The pipeline is
designed so that `LocalSource` can later be replaced by a SharePoint adapter
without changing the validation, storage or BigQuery logic.

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
earlier one (step 10 restores an earlier upload). The marker is only a cache: BigQuery checks its own audit
table inside the commit transaction as well, so a lost marker can't cause a
second load (step 3 of the smoke test deletes the markers to prove it).

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
copied to `quarantine/`. It is not converted to Parquet or sent to BigQuery.

This retains evidence of what was received and gives the data owner something
to correct, while protecting the final dataset from invalid data.

### 5. Valid files are kept as raw source data

For a valid file, the original workbook is saved under:

```text
raw/<identity>/<checksum>/<filename>.xlsx
```

`<identity>` is the file's ID at the source (the file name for a local
folder, the item ID for SharePoint) and `<checksum>` is the SHA-256 of
its bytes. If the file is edited and supplied again, the new version is
stored next to the old one, so both are kept.

Keeping the raw file means you can show what was supplied, rerun the process,
investigate a problem, or change transformation logic without asking the
source system to resend the file.

### 6. The primary worksheet is converted to Parquet

The pipeline reads the `Events` sheet into a table-like structure and creates:

```text
processed/<warehouse identity>/<identity>/<checksum>/<version>/<digest>/<filename>.xlsx.parquet
```

It holds only the worksheet's own columns, so the same workbook always
produces the same Parquet bytes, and a failed load is retried with the
same copy. `<digest>` is the start of the Parquet file's own SHA-256:
if the contract or a library changes what the file converts to, the new
result is stored next to the old one instead of clashing with it.

Parquet is efficient for analytics and works well with BigQuery. The original
Excel workbook remains unchanged in `raw/`, while the Parquet file is the
processed, query-ready version.

The typed route converts the first configured worksheet to Parquet. To load
several worksheets, either run one pipeline per worksheet (processed copies
and duplicate markers are kept per warehouse, so they don't clash), or use the
raw JSON route, which loads every configured worksheet into its own raw table
(see `docs/RAW_TABLE_LAYOUT.md`).

### 7. Parquet is loaded through a BigQuery staging table

The Parquet data is first loaded into a staging table of its own:

```text
events_final_stg_<checksum[:12]>_<random>
```

It expires after a day and is dropped as soon as the load finishes.

The pipeline checks that the staging table has the expected number of rows
before it updates the final table. This provides an early check that the
conversion and warehouse load have not silently lost or added rows.

### 8. Data is merged into the final table and audited

The staging data is merged into:

```text
events_final
```

using `event_id` as the key:

- A new event ID is inserted.
- An existing event ID is updated rather than duplicated.
- `_loaded_at` is written in the same transaction, set to the same UTC
  timestamp as the audit entry (the pipeline's clock just before the commit
  job starts).

The final-table change and BigQuery audit entry are committed together. This
avoids a successful-looking data load without a corresponding audit record.

### 9. Audit records provide traceability

GCS receives:

- A detailed audit JSON record for each outcome: loaded, skipped, quarantined
  or failed.
- A short marker for each loaded upload (identity and version), used to
  skip it on later runs.

BigQuery receives a warehouse audit record for successful loads.

## Key design choices

- **Keep raw and processed data:** Preserve the original Excel workbook for
  traceability and use Parquet for efficient downstream analytics.
- **Validate before loading:** Stop invalid input contaminating the final
  table.
- **Quarantine rather than discard:** Retain invalid files so problems can be
  understood and fixed.
- **Load each upload once:** The same upload (file version) is never loaded
  twice, but a new upload always loads, even one that restores earlier
  content.
- **Use unique event IDs for merging:** Allow corrected versions of an event
  to update the existing row instead of creating duplicates.
- **Use a staging table:** Check incoming data before it reaches the final
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

The actual loaded data is in `events_final`. For the smoke test, each Excel
row becomes a row in that table:

| event_id | employer_id | attendees | _loaded_at |
| --- | --- | ---: | --- |
| `DEMO-...-001` | `EMP001` | 10 | UTC timestamp |
| `DEMO-...-002` | `EMP002` | 20 | UTC timestamp |

`events_final` is the final usable data table. `audit` is separate and records
metadata about each successful load, including filename, checksum, row count,
outcome and time.

The `events_final_stg_...` tables exist only during a load. They are not
intended for users or downstream analysis.

```text
Excel -> raw GCS -> processed Parquet in GCS -> events_final_stg_... -> events_final
                                                          \
                                                           -> audit metadata
```

The smoke-test table has six columns: the workbook's `event_id`,
`employer_id` and `attendees`, plus three the warehouse sets. `_loaded_at`
is when the row was last loaded; `_source_file` is the file it came from;
`_deleted_at` is NULL while the row is in that file's newest upload, and
set when a later upload no longer has it (`mark_deleted_rows=True`, which
the smoke test turns on). Current rows are `WHERE _deleted_at IS NULL`.
Source columns with those three names are refused.
If a real workbook has additional columns, they should also be designed into
the BigQuery destination table.
