# File formats: Excel, CSV, JSON and JSON Lines

*For whoever sets up a source that isn't an Excel workbook, such as a
SharePoint list exported to the landing bucket as JSON Lines.*

A contract reads one kind of file, set by `Contract.file_format`:

| `file_format` | Extensions | The file holds | Values load as |
|---|---|---|---|
| `"xlsx"` (default) | `.xlsx`, `.xlsm` | Worksheets, one table each | Excel's cell types ([`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md), "Payload values") |
| `"csv"` | `.csv` | One table | Text, exactly as written; an empty field is `null` |
| `"json"` | `.json` | One table: an array of objects | Their JSON types, nested objects and arrays included |
| `"jsonl"` | `.jsonl`, `.ndjson` | One table: one object per line | As `"json"` |

Everything else works as it does for a workbook: the file-name check,
the size limit, the column checks (`columns`, `column_selection`,
`excluded`, `on_mismatch`, `allow_empty`), quarantine, the raw and
processed copies, the raw table layout and the audit record.

## Setting up a source

A CSV or JSON file holds one table, so its contract has one
`Worksheet`. Its name is yours to choose: it is the raw rows'
`sheet_name` and the key of the warehouse's table mapping.

```python
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSSource
from py_common.contract import FILE_EXTENSIONS, Column, Contract, Worksheet
from py_common.pipeline import Pipeline

ACTIONS = Contract(
    worksheets=[
        Worksheet(
            "items",
            columns=[
                Column("id", "string", unique=True),
                Column("Title", "string"),
                Column("Modified", "date"),
            ],
            # New list columns load without a contract change.
            column_selection="all_except",
            # Personal data the analysis doesn't need.
            excluded=["AuthorLookupId", "EditorLookupId"],
        )
    ],
    file_name="Actions.jsonl",
    file_format="jsonl",
)

pipeline = Pipeline(
    source=GCSSource(
        project,
        landing_bucket,
        "sharepoint/actions",
        extensions=FILE_EXTENSIONS[ACTIONS.file_format],
    ),
    store=store,
    warehouse=RawJsonWarehouse(project, raw_dataset, {"items": "actions"}),
    contract=ACTIONS,
)
```

- **One landing folder per file.** As for workbooks: each pipeline
  lists one folder, and `file_name` quarantines a file under any
  other name.
- **The source's extensions must match the format.** `Pipeline`
  refuses to start (`ConfigError`) if the source picks up an
  extension its contract can't read, e.g. `.csv` files for an
  `"xlsx"` contract. `GCSSource` and `LocalSource` take `extensions`;
  both default to `.xlsx`. `FILE_EXTENSIONS[file_format]` gives the
  right ones.
- **`file_name` must end in one of the format's extensions**, and a
  JSON contract's `header_row` must be 1 (a JSON file has no heading
  row). Both are checked when the contract is built.
- **The contract's fingerprint changes** when `file_format` does, so
  the audit trail shows the switch. A workbook contract keeps the
  fingerprint it had before `file_format` existed.

## CSV

`read_csv()` in `contract.py`:

- UTF-8, with or without a byte order mark (Excel's "CSV UTF-8"
  writes one). Any other encoding is quarantined, naming the first bad
  byte's position: export the file again as UTF-8.
- Comma-separated, with double quotes around a field that holds a
  comma, a quote or a line break. Anything else, such as text after a
  closing quote, is quarantined. So is a row with more fields than the
  heading row, which usually means an unquoted comma has moved every
  value after it into the next column.
- A field holding an Excel error, such as `#REF!`, is quarantined or
  warned about, as in a worksheet.
- Headings are named as in a worksheet: used exactly as written, a
  blank heading over data becomes `col_B`, a repeat gets `_2`.
  `header_row` skips lines above the headings.
- Every value is text. An empty field is `null`, like an empty cell.
  A record of empty fields is skipped, like a blank row.
- `row_number` counts records as a spreadsheet shows them: the
  heading is row 1, so the first data row is row 2. A line break
  inside a quoted field doesn't shift later rows.
- A typed column checks that the text reads as its type, but the text
  still loads as written:

  | `data_type` | Fits | Doesn't fit |
  |---|---|---|
  | `"integer"` | `12`, `-3`, `007` | `1.5`, `1,000`, `12 apples` |
  | `"float"` | `2`, `-1.5`, `.5`, `1e3` | `1,5`, `inf`, `NaN` |
  | `"date"` | `2026-10-01`, `2026-10-01T09:30:00Z` | `01/10/2026`, `2026-02-30` |
  | `"string"` | anything | |

  Surrounding spaces are ignored for the check.

## JSON and JSON Lines

`read_json()` and `read_jsonl()` in `contract.py`:

- UTF-8. A `"json"` file is one array of objects; a `"jsonl"` file
  has one object per line, and blank lines are skipped.
- The columns are every key the objects use, exactly as written, in
  the order they first appear. A key an object leaves out is `null`
  in that row.
- Values keep their JSON types: text, numbers, `true` and `false`,
  `null`, and nested objects and arrays, which load into the payload
  unchanged.
- An object whose values are all `null` or blank text is skipped,
  like a blank row.
- `row_number` is the object's position in the array (`"json"`) or
  its line number (`"jsonl"`), counting from 1.
- These are quarantined, with the line or position: invalid JSON, an
  item or line that isn't an object, an object that repeats a key
  (JSON readers usually keep the last value silently), `NaN` or
  `Infinity`, a number too large for a float, and nesting too deep to
  parse.
- A typed column checks the value's JSON type: `"integer"` a whole
  number (not `true`/`false`), `"float"` any number, `"string"`
  text. JSON has no date type, so a `"date"` column takes text in ISO
  8601 form, with or without a time: `"2026-10-01"`,
  `"2026-10-01T09:30:00Z"`.

## What the SharePoint export should write

The SharePoint-to-GCS transfer is separate from py-common. For
py-common to load its files as described above:

1. **One file per list, under a fixed name**, in that list's own
   landing folder, e.g. `gs://<landing>/sharepoint/actions/Actions.jsonl`.
   Overwrite it on every run. Each overwrite is a new GCS generation,
   and py-common loads each generation once.
2. **JSON Lines, UTF-8, one line per list item.** Write each item's
   fields as one JSON object, keys being the columns' internal
   names. Include the item's ID and its Created and Modified times,
   so Dataform can key and order the items.
3. **The whole list every time**, not only the items that changed.
   The raw tables' "latest upload" query
   ([`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md)) treats each upload
   as the complete current state.
4. **Upload each file in one go.** GCS shows an object only once its
   upload has finished, so the pipeline never reads half a file.
5. **An empty list is an empty file.** Set `allow_empty=True` on the
   Worksheet if the list can genuinely be empty; otherwise the file
   is quarantined, as an empty workbook is.
6. **Leave out personal data the analysis doesn't need**, at the
   export if possible, or with the Worksheet's `excluded` list. An
   excluded column is never loaded.
7. **Dates as ISO 8601 text**, as Microsoft Graph returns them
   (`"2026-10-01T09:30:00Z"`), so `"date"` columns can check them.

## In Dataform

The payload holds the file's values as described above:

- **CSV**: every value is a JSON string. Type it in silver, e.g.
  `SAFE_CAST(JSON_VALUE(payload['Count']) AS INT64)`, and
  `SAFE.PARSE_DATE('%Y-%m-%d', LEFT(JSON_VALUE(payload['Date']), 10))`
  for a date.
- **JSON**: numbers are JSON numbers (`LAX_INT64(payload['id'])`);
  dates are strings
  (`SAFE_CAST(JSON_VALUE(payload['Modified']) AS TIMESTAMP)`). Read a
  nested value with a JSON path, e.g.
  `JSON_VALUE(payload['Owner'], '$.Email')`, and an array with
  `JSON_VALUE_ARRAY(payload['Tags'])`.

See [`DATAFORM.md`](DATAFORM.md) for the layers and checks.

## Graph list exports

`GraphJsonLinesContract` reads one list-item envelope per line and maps
SharePoint internal field names and selected envelope metadata to the
contract's columns. Configure `field_map`, `metadata_map` and
`excluded_fields`; no list schema is guessed. An `any` column preserves
lookup identifiers, arrays and nested values without turning them into
text. Keep Graph's column definitions as `schema.json` beside each
export. The original JSON Lines bytes remain the archived source.

A list item's `fields` hold more than the list's columns: SharePoint
adds system fields such as `LinkTitle`, `AuthorLookupId` and
`_UIVersionString`, and a multi-choice column gains a
`<name>@odata.type` entry. Each counts as a field, so `excluded_fields`
lists them. `dummy_was_database.jsonl` in this folder is a complete
example: `dummy_was_database.xlsx` written as Graph would send it, with
`tests/unit/test_graph_export.py` holding its mapping. Its internal
names follow SharePoint's naming rule, and are not from a real list.

`site_time_zone` is the time zone in the SharePoint site's regional
settings. Graph sends dates in UTC, and SharePoint stores a date-only
value as local midnight, so a date column is converted back to this
zone: `2026-05-31T23:00:00Z` loads as `2026-06-01T00:00:00` in
`Europe/London`. A contract with a date column needs this setting.
Check it against a real export: compare a summer date in the list with
the same item in the file.

`multi_value_separators` joins a multi-choice list into one text value,
for a text column that a workbook export holds as separated text:
`{"Topics": ";#"}` loads `["A", "B"]` as `A;#B`. A choice that isn't
text, or that contains the separator, quarantines the file.

## Limits in every format

All source formats reject non-finite numeric values and unpaired Unicode
surrogates. JSON containers are limited to 128 nested levels to bound
conversion recursion. JSON integers must fit BigQuery JSON's signed INT64
through unsigned UINT64 range. Payload object keys are canonicalised;
source row numbers and source-byte checksums remain provenance, so row or
byte permutations do not imply identical complete processed files.
