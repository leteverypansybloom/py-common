# Raw table layout

*For whoever builds the Dataform (cleaning and modelling) layer on top
of the raw tables that py-common loads.*

py-common's raw JSON route (`Pipeline(output_format="raw_json")` with
`RawJsonWarehouse`) writes one raw BigQuery table per worksheet. This
page describes what is in those tables and the rules that decide it,
so the SQL that reads them doesn't have to guess.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `source_file` | STRING | File name at the source, e.g. `dummy_was_database.xlsx`. |
| `source_generation` | STRING | The source's version marker. For `GCSSource` this is the GCS object generation: a number that increases on every upload of the same file name. |
| `sheet_name` | STRING | Worksheet the row came from. |
| `row_number` | INT64 | The row number Excel shows for this row. Use it to point a file owner at a bad row. |
| `checksum` | STRING | SHA-256 of the file's bytes. Joins to the audit table and to the copies kept in the bucket (`raw/<identity>/<checksum>/...`). |
| `payload` | JSON | The row's values, one key per loaded column. |
| `ingested_at` | TIMESTAMP | When the load committed. All rows of one file share it, and it equals the audit record's `timestamp`. |

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
  `audit` table inside the transaction that inserts the rows.
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

Files loaded before py-common 0.2.0 were deduplicated by content
alone, so a revert to content loaded before the upgrade is still
skipped. To load it, delete its `audit/<warehouse>/<checksum>` marker
in the store bucket and run again.

## Payload keys

Keys are column names as py-common reads them from the heading row
(`Worksheet.header_row`, row 1 unless the contract says otherwise):

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
  missing from the sheet is `null` on every row.
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
QUALIFY CAST(source_generation AS INT64) = MAX(
  CAST(source_generation AS INT64)
) OVER (PARTITION BY source_file)
```

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
  QUALIFY CAST(source_generation AS INT64) = MAX(
    CAST(source_generation AS INT64)
  ) OVER (PARTITION BY source_file)
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

- [`RAW_JSON_VS_CONTRACT_VALIDATION.md`](RAW_JSON_VS_CONTRACT_VALIDATION.md):
  why raw JSON plus Dataform.
- [`GCP_SETUP.md`](GCP_SETUP.md): the buckets, datasets and
  permissions these tables live in.
- `src/py_common/raw.py` and `src/py_common/adapters/bigquery_raw.py`:
  the code that writes them.
