# Why raw JSON tables

py-common loads every workbook into raw JSON tables in BigQuery, and
Dataform does the cleaning and typing. This page records why, where
validation sits, and how the same pattern scales to a large daily
feed. The code changes are in [`DECISIONS_en.md`](../DECISIONS_en.md).

## Decision: raw JSON + Dataform, with py-common landing the data

For Healthy Working Wales the project combines the two approaches:

- **py-common does the landing and the raw load.** `GCSSource` reads
  the monthly files from a landing bucket; the contract reads each
  worksheet (heading row, blank and repeated headings), loads only
  the columns it selects, and leaves personal-data columns out.
  `Pipeline` and `RawJsonWarehouse` insert every row into one raw
  table per worksheet as a JSON payload.
- **Dataform does the cleaning and modelling.** It picks the latest
  upload of each file, types the values, sends bad rows to a
  quarantine table, merges duplicate employers, and builds the tables
  Power BI reads.

![Raw JSON pattern: file in SharePoint, copied to a bucket, loaded as JSON rows into a raw table, then validated into a staging table with a quarantine table for bad records](raw-json-proposal.png)

Why this, and not typed tables loaded by py-common:

- **Most of the project's work comes after loading:** employer
  deduplication, the data model and the reporting tables. That is SQL
  in BigQuery, which is Dataform's job; py-common stops at the load.
- **Sources will change shape.** Forms and survey exports change
  their columns when questions are edited, and raw JSON absorbs that.
  A typed table would need its schema changed in step with every
  workbook change.
- **One route to build, test, document and run.** Typing and merging
  rows in py-common as well as in Dataform would mean the same rules
  in two places, in two languages.
- **It is the team's documented pattern** (`upcoming_work.md`).
- **py-common still adds what a plain loader lacks:** Excel types
  kept (dates stay dates), checksums and every file version kept, an
  audit record per attempt, data minimisation (excluded columns), and
  header checks before anything loads.

Both approaches cost about nothing at these volumes (a few hundred
rows a month): BigQuery batch loads are free and the free tier covers
the storage and queries. The cost that matters is people's time.

## Where validation sits

1. **In py-common, at load.** Each worksheet's contract checks the
   headings and, for listed columns, the types, required values and
   uniqueness. With `on_mismatch="quarantine"` a file with problems is
   kept in `quarantine/` and loads nothing; with `on_mismatch="warn"`
   it loads and the problems are logged at WARNING and kept in the
   audit record, so an alert tells the file owner straight away (see
   [`GCP_SETUP.md`](GCP_SETUP.md)).
2. **In Dataform: the cleaning.** Typing with `SAFE_CAST`, required
   fields, uniqueness and row-level quarantine. Checks that the
   expected columns exist must *block* the reporting tables, so a
   renamed column can't quietly become blanks in Power BI.
3. **Before the visualisation: a light safety check.** This period's
   data arrived, row counts look reasonable, key fields aren't empty.

What the raw tables contain, and how to read them, is in
[`RAW_TABLE_LAYOUT.md`](RAW_TABLE_LAYOUT.md).

## A large daily feed (millions of rows a day)

The same raw table layout and Dataform checks work at scale, but not
py-common's row-by-row Excel reading. For such a feed:

1. **Check the header row before loading.** Reading the first line of
   the file is cheap. If columns are missing, renamed or unexpected,
   stop and alert before millions of rows are loaded.
2. **Load the raw data with BigQuery itself.** Run a BigQuery load
   job straight from the GCS file into a raw table partitioned by
   load date, rather than building Python row objects, which could
   use many GB of memory in Cloud Run.
3. **Validate rows in SQL and split them.** Process the new partition
   only. Rows that pass go into a typed, partitioned cleaned table;
   rows that fail go into the quarantine table with the file name,
   the reason and the load date.
4. **Set a threshold.** If more than a set share of rows fail (say
   1%), don't publish that day; alert instead. A sudden jump in
   failures usually means a schema or upstream problem.
5. **Run a light check before the visualisation**, as for the small
   feeds.

py-common's outer parts (finding source files, loading each upload
once, keeping the original file, the audit trail and the run lock)
can still be reused for such a feed. Confirm the feed's file format,
the number of columns, and whether each day brings new rows or a full
refreshed copy before sizing it.
