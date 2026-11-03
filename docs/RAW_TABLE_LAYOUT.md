# Raw table layout

*For whoever builds the Dataform (cleaning and modelling) layer on top
of the raw tables that py-common loads.*

py-common (`Pipeline` with `RawJsonWarehouse`) writes one raw
BigQuery table per worksheet. This
page describes what is in those tables and the rules that decide it,
so the SQL that reads them doesn't have to guess.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `source_file` | STRING | File name at the source, e.g. `dummy_was_database.xlsx`. |
| `source_generation` | STRING | The source's version marker. For `GCSSource` this is the GCS object generation: a number that increases on every upload of the same file name. Other sources' versions aren't numbers (`LocalSource`: `<mtime_ns>-<size>`; SharePoint: an eTag like `"{GUID},3"`), so the "latest upload" query below works for `GCSSource` only. |
| `sheet_name` | STRING | Worksheet the row came from. For a CSV or JSON file, the name of the contract's one `Worksheet`. |
| `row_number` | INT64 | The row number Excel shows for this row. Use it to point a file owner at a bad row. For a CSV file, the record's number as a spreadsheet shows it; for JSON, the object's position in the array; for JSON Lines, its line number ([`FILE_FORMATS.md`](FILE_FORMATS.md)). |
| `checksum` | STRING | SHA-256 of the file's bytes. Joins to the audit table and to the copies kept in the bucket (`raw/<identity>/<checksum>/...`). |
| `payload` | JSON | The row's values, one key per loaded column. |
| `ingested_at` | TIMESTAMP | The load's timestamp: the pipeline's clock just before the commit job starts, written in the commit transaction. All rows of one upload share it, and it equals the audit record's `timestamp`. |

Tables are partitioned by day of `ingested_at` and clustered on
`source_file, source_generation`. Clustering only cuts the bytes a
query reads when it filters on those columns with plain comparisons
(e.g. `WHERE source_file = 'x.xlsx'`). The "latest upload" query
below uses a window function over every row, so it still reads the
whole table; check a query's bytes processed before relying on
clustering, and for large tables consider an incremental Dataform
model or a "latest" view driven by the audit table.

Rows are only ever inserted. Each monthly upload of a file adds all
of that file's rows again, under a new `source_generation`, and earlier
uploads stay in the table. Nothing in the raw table is updated or
deleted by py-common.

**Each generation loads exactly once.** A file is identified by its
source identity (for `GCSSource`, the object name) and generation,
not by its content:

- The same generation is never inserted twice, whatever fails and
  however often the load is retried. The check is made against the
  `audit` table inside the transaction that inserts the rows. The one
  way past it is very unlikely: two commits of the same upload
  running at the same moment (a commit still running after the
  pipeline stopped waiting for it, and a retry's commit starting
  before it ends). The daily job's duplicate-load check alerts if it
  ever happens (`docs/GCP_SETUP.md`, section 7), and the query below
  reads one load per upload, so it never shows the extra rows.
- A new generation always loads, even if its bytes are identical to
  an earlier one. This keeps "latest generation" right when an upload
  is reverted:

  | Upload | Generation | Content | Loaded? |
  |---|---|---|---|
  | January export | 100 | A | yes |
  | February export, wrong | 200 | B | yes |
  | January export again (revert) | 300 | A | **yes** |

  The query below then returns generation 300 (content A). If
  generation 300 were skipped as a repeat of A, it would return 200,
  the upload that was just withdrawn.
- Two different files with identical bytes both load, each under its
  own `source_file`.
- Each raw table is tracked separately. Adding a sheet and table to
  an existing pipeline loads the new table from the next run, without
  loading the existing tables again.

**Empty sheets.** A sheet with headings but no rows is rejected (the
file is quarantined) unless its contract sets `allow_empty=True`.
Without this, an upload that came out empty would insert nothing,
and the "latest upload" query below would keep returning the
previous upload's rows. Only set `allow_empty=True` for a sheet where
"nothing this time" is a real answer, and read such a table with the
audit-table query below ("Latest upload, empty uploads included"):
the shorter query returns the last upload that had rows.

## Payload keys

Keys are column names as py-common reads them from the heading row
(`Worksheet.header_row`, row 1 unless the contract says otherwise).
A CSV file's headings are named the same way. In a JSON or JSON Lines
file the keys are the objects' own keys, exactly as written, in the
order they first appear ([`FILE_FORMATS.md`](FILE_FORMATS.md)).

- **Headings are used exactly as written.** Nothing is trimmed, so
  `'Organisation Industry '` keeps its trailing space. Numeric or date
  headings become text.
- **Blank heading over data:** named after the column letter, e.g.
  `col_B`. A blank heading over an empty column is ignored.
- **Repeated heading:** the first keeps its name, later ones get `_2`,
  `_3`, ... from left to right, skipping names the sheet already uses.
  So two "Other" columns are `Other` and `Other_2`.
- **Which keys appear** depends on the worksheet's `column_selection`:
  - `"listed"`: exactly the contract's columns, in contract order.
  - `"all_except"`: every column except the excluded ones, in sheet
    order.
- **Excluded columns never appear.** Columns in the contract's
  `excluded` list (names, email addresses and so on) are not loaded at
  all.
- **Every loaded column is present on every row.** An empty cell is
  JSON `null`, not a missing key. In `"warn"` mode a listed column
  missing from the sheet is `null` on every row. So if a column is
  renamed in the workbook, the old key is still there, `null` on
  every row, and the new name isn't loaded at all (with
  `column_selection="listed"`). The payload alone can't tell this
  from a column that is genuinely empty: a Dataform assertion that
  fails when a column you rely on is `null` on every row of the
  latest upload catches it. The `Contract warning` log line (and the
  header/data warning alert) names the missing column.
- **Empty rows are skipped,** so a gap in `row_number` means blank rows
  in the sheet.

## Payload values

| Excel value | JSON value | Example |
|---|---|---|
| Text | string | `"Org 1"` |
| Whole number | number | `1234` |
| Decimal | number | `0.25` |
| Decimal (Python `Decimal`) | number: integer if whole | `7`, `1.25` |
| TRUE / FALSE | boolean | `true` |
| Date and time | string, ISO 8601, no time zone (converted to UTC if the value had one) | `"2025-12-16T11:46:00"` |
| Date only | string, ISO 8601 | `"2025-11-17"` |
| Time only | string, ISO 8601 | `"09:30:00"` |
| Duration | string, ISO 8601 seconds | `"PT5400S"` |
| Empty | null | `null` |

Values are not cleaned. A number typed as text in Excel stays a
string, and text like `"NA"` in a number column stays text. Use
`SAFE_CAST` and send failures to a quarantine table.

A CSV file's values are all strings, exactly as written, and an empty
field is `null`. A JSON or JSON Lines file's values keep their JSON
types, nested objects and arrays included. See
[`FILE_FORMATS.md`](FILE_FORMATS.md), "In Dataform", for reading them.

## Warnings and quarantined files

- **Files with warnings still load.** A worksheet with
  `on_mismatch="warn"` loads even with a missing or unexpected column
  or a bad value. The problems are logged at WARNING and kept in the
  audit record in the store bucket (`audit/records/*.json`, field
  `warnings`). They are not in the raw table.
- **Quarantined files load nothing.** A worksheet with
  `on_mismatch="quarantine"` loads no rows from a file with problems.
  The file is copied to `quarantine/` in the store bucket instead.

## Reading the raw tables

**Latest upload of each file.** Each upload of a cumulative file holds
its full history, so take the newest generation per file. Cast the
generation to compare it as a number:

```sql
SELECT *
FROM `project.raw_hww.hww_was_database`
QUALIFY
  CAST(source_generation AS INT64) = MAX(CAST(source_generation AS INT64))
    OVER (PARTITION BY source_file)
  AND ingested_at = MAX(ingested_at)
    OVER (PARTITION BY source_file, source_generation)
```

The second condition keeps a single load of that upload. Every row
of one load has the same `ingested_at`, so if an upload were ever
loaded twice (see "Each generation loads exactly once" above), only
the later load's rows are read.

**Latest upload, empty uploads included.** An upload with no rows
inserts nothing into the raw table, so the query above can't see it.
The audit table records every load, empty ones too, so choose the load
there and read its rows. The audit row's `filename` is the load key,
the file's path in the landing folder, `_` and the first 8 hex digits
of its checksum; `destination` is the raw table's full id:

```sql
WITH selected AS (
  SELECT source_version, timestamp
  FROM `project.raw_hww.audit`
  WHERE outcome = 'LOADED'
    AND ENDS_WITH(destination, '.hww_was_database')
    AND REGEXP_EXTRACT(filename, r'([^/]+)_[0-9a-f]{8}$') = 'dummy_was_database.xlsx'
  ORDER BY timestamp DESC
  LIMIT 1
)
SELECT raw.*
FROM `project.raw_hww.hww_was_database` AS raw
JOIN selected
  ON raw.source_generation = selected.source_version
  AND raw.ingested_at = selected.timestamp
```

It returns no rows when the newest upload was empty, which is the
right answer for a cumulative file. It also reads one file name only,
so an old name's last upload can't come back. Comparing the number of
rows with the audit row's `row_count` catches an incomplete read.

**One file name per workbook.** "Latest" is per `source_file`, the
file's name. If a cumulative file were uploaded under a new name each
time (a date in the name, say), every name would have its own latest
upload and the query would return all of them: duplicate current
rows. So upload each workbook under one unchanging name, and let GCS
versioning keep the history. Set the contract's `file_name` to that
name: an upload under any other name (`WAS export (1).xlsx`, say) is
then quarantined, with a message giving the expected name, and loads
nothing. A `uniqueKey` assertion on the cleaned table (as in the
example below) is a second line of defence.

**Renaming a workbook on purpose.** Change the contract's
`file_name`, then upload under the new name. The old name's last
upload still counts as "latest" for the old name, so filter it out in
Dataform (`WHERE source_file != 'old name.xlsx'`), or delete its rows
from the raw tables once the new name has loaded. Reloading,
undoing and removing uploads are in [`RUNBOOK.md`](RUNBOOK.md).

**Payload fields.** Use `JSON_VALUE` for text and `LAX_` or
`SAFE_CAST` for types. Quote keys that contain spaces or punctuation:

```sql
SELECT
  JSON_VALUE(payload, '$."Participant ID"') AS participant_id,
  SAFE_CAST(JSON_VALUE(payload, '$."Date of Enquiry"') AS DATETIME)
    AS enquiry_at,
  LAX_INT64(payload."Number of sites") AS number_of_sites
FROM ...
```

**Which keys are present.** Useful as a Dataform assertion that the
layout hasn't drifted:

```sql
SELECT DISTINCT key
FROM `project.raw_hww.hww_survey_info`, UNNEST(JSON_KEYS(payload)) AS key
```

## Example Dataform model

`definitions/staging/stg_was_requests.sqlx`:

```sql
config {
  type: "table",
  description: "WAS requests from the latest upload of the WAS export",
  assertions: { nonNull: ["participant_id"], uniqueKey: ["participant_id"] }
}

WITH latest AS (
  SELECT *
  FROM ${ref("hww_was_database")}
  QUALIFY
    CAST(source_generation AS INT64) = MAX(CAST(source_generation AS INT64))
      OVER (PARTITION BY source_file)
    AND ingested_at = MAX(ingested_at)
      OVER (PARTITION BY source_file, source_generation)
)
SELECT
  JSON_VALUE(payload, '$."Participant ID"') AS participant_id,
  JSON_VALUE(payload, '$."Title"') AS organisation,
  SAFE_CAST(JSON_VALUE(payload, '$."Date of Enquiry"') AS DATETIME)
    AS enquiry_at,
  source_file,
  row_number,
  ingested_at
FROM latest
```

## See also

- [`DATAFORM.md`](DATAFORM.md): how to lay out, run and check a
  Dataform project on these tables in GCP.
- [`FILE_FORMATS.md`](FILE_FORMATS.md): how CSV, JSON and JSON Lines
  files are read.
- [`RAW_JSON_VS_CONTRACT_VALIDATION.md`](RAW_JSON_VS_CONTRACT_VALIDATION.md):
  why raw JSON plus Dataform.
- [`GCP_SETUP.md`](GCP_SETUP.md): the buckets, datasets and
  permissions these tables live in.
- `src/py_common/raw.py` and `src/py_common/adapters/bigquery_raw.py`:
  the code that writes them.
