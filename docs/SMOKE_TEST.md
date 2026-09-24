# Excel to GCS and BigQuery smoke test

This guide explains the end-to-end smoke test: how it creates test Excel
workbooks, what each result demonstrates, and how data moves through the
pipeline.

Run it only against development GCS and BigQuery resources.

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

| Test | Expected result | What it proves |
| --- | --- | --- |
| Unit tests | All tests pass locally | Core logic behaves as expected without GCP. |
| Valid Excel file | `loaded 2` | The pipeline can validate, convert, store and load two rows. |
| GCS check | Raw `.xlsx` and processed `.parquet` objects exist | The original and processed versions are retained. |
| BigQuery check | Two new `DEMO-...` rows appear | The data reached the final destination table. |
| `_loaded_at` check | Both rows have a timestamp | The pipeline records when the data was loaded. |
| Rerun unchanged file | `skipped 0` | The same file content is not loaded twice. |
| Invalid workbook | `quarantined 0` | Invalid data is retained for investigation and does not reach BigQuery. |

## The journey through the system

### 1. A local folder acts as the source

For the demo, Excel files sit in a local folder. `LocalSource` finds `.xlsx`
files, records their filename, modification version and size, then reads their
contents.

This imitates the role SharePoint will eventually play. The pipeline is
designed so that `LocalSource` can later be replaced by a SharePoint adapter
without changing the validation, storage or BigQuery logic.

### 2. A checksum identifies the exact file content

The pipeline calculates a SHA-256 checksum, which is a unique fingerprint of
the file contents. It looks in GCS for an `audit/<checksum>` marker:

- If the marker exists, the file is skipped.
- If it does not exist, processing continues.

This prevents accidental double loading when the pipeline is rerun with an
unchanged file.

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
raw/<filename>.xlsx
```

Keeping the raw file means you can show what was supplied, rerun the process,
investigate a problem, or change transformation logic without asking the
source system to resend the file.

### 6. The primary worksheet is converted to Parquet

The pipeline reads the `Events` sheet into a table-like structure and creates:

```text
processed/<filename>.xlsx.parquet
```

It adds `_loaded_at` at this stage using a UTC timestamp.

Parquet is efficient for analytics and works well with BigQuery. The original
Excel workbook remains unchanged in `raw/`, while the Parquet file is the
processed, query-ready version.

At present, the pipeline converts the first configured worksheet to Parquet.
It can validate more than one worksheet, but does not yet create a separate
Parquet output for every worksheet.

### 7. Parquet is loaded through a BigQuery staging table

The Parquet data is first loaded into:

```text
events_final_staging
```

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
- `_loaded_at` holds the pipeline's UTC load timestamp.

The final-table change and BigQuery audit entry are committed together. This
avoids a successful-looking data load without a corresponding audit record.

### 9. Audit records provide traceability

GCS receives:

- A detailed audit JSON record for each outcome: loaded, skipped, quarantined
  or failed.
- A short checksum marker for successfully loaded files, used for duplicate
  protection.

BigQuery receives a warehouse audit record for successful loads.

## Key design choices

- **Keep raw and processed data:** Preserve the original Excel workbook for
  traceability and use Parquet for efficient downstream analytics.
- **Validate before loading:** Stop invalid input contaminating the final
  table.
- **Quarantine rather than discard:** Retain invalid files so problems can be
  understood and fixed.
- **Use content-based duplicate protection:** Prevent the same bytes being
  loaded more than once.
- **Use unique event IDs for merging:** Allow corrected versions of an event
  to update the existing row instead of creating duplicates.
- **Use a staging table:** Check incoming data before it reaches the final
  table.
- **Use UTC timestamps:** Avoid ambiguity across machines, regions and
  daylight-saving changes.
- **Keep components separate:** Source, validation, storage and warehouse
  loading can evolve independently.

## Current constraint

The GCS lock is currently a no-op. The design is safe only when one pipeline
run happens at a time, such as a single Cloud Scheduler-triggered Cloud Run
job.

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

`events_final_staging` is used temporarily while loading. It is not intended
for users or downstream analysis.

```text
Excel -> raw GCS -> processed Parquet in GCS -> events_final_staging -> events_final
                                                          \
                                                           -> audit metadata
```

The smoke-test table has four columns because the source workbook contains
`event_id`, `employer_id` and `attendees`, and the pipeline adds `_loaded_at`.
If a real workbook has additional columns, they should also be designed into
the BigQuery destination table.
