# Fixing, reloading and undoing loads

*For whoever supports the pipeline: what to do when a contract was
wrong, an upload was bad, or a workbook is renamed.*

Three rules make all of this simpler:

- **An upload is a file's name plus its GCS version** (generation).
  Each upload loads once into each table.
- **Every new upload loads**, even if its bytes repeat an earlier
  upload's. Uploading a file again is therefore how you reload it;
  there is no separate "reprocess" command.
- **Each upload holds the workbook's full history**, and only the
  latest upload is used. Old uploads never need loading again.

The commands use the names from [`GCP_SETUP.md`](GCP_SETUP.md):
`$LANDING_BUCKET`, `$STORE_BUCKET`, `$PROJECT`, `$RAW_DATASET`. A
file's *identity* is its object name, folder included, e.g.
`was/WAS export.xlsx`.

## Find an upload

- **Its versions:** `gcloud storage ls -a "gs://$LANDING_BUCKET/was/"`
  lists every upload as `...#<generation>`.
- **What happened to it:** the audit record in the store bucket,
  `audit/records/<time>_<random>_<identity>.json`: outcome, errors, checksum,
  the converted copy loaded (`processed_key`) and the contract used
  (`contract_fingerprint`). Or the logs:
  `jsonPayload.item_identity="was/WAS export.xlsx"`.
- **What loaded:** the BigQuery `audit` table, e.g.
  `WHERE filename LIKE 'was/WAS export.xlsx_%' ORDER BY timestamp DESC`
  (`source_version` is the generation).

## The contract was wrong, and the file was quarantined

Fix the contract and deploy. Quarantined files are checked again on
every run, so the next run (the daily job, or the next upload to
that folder) loads it. To load it now, run the job:
`gcloud run jobs execute <JOB> --region <REGION>`.

## The contract was wrong, but the file loaded

Fix the contract and deploy, then upload the same file again. It is a
new upload, so it loads, with the fixed contract. Either the uploader
uploads it again, or you copy the archived original back:

```bash
gcloud storage cp \
  "gs://$STORE_BUCKET/raw/was/WAS export.xlsx/<checksum>/WAS export.xlsx" \
  "gs://$LANDING_BUCKET/was/WAS export.xlsx"
```

- **Raw route:** the new upload is the latest, so Dataform's "latest
  upload" query reads it. The earlier load's rows stay in the raw
  table (it is insert-only), unread.
- **Typed route:** the new upload is merged over the table. A column
  the fix adds is added to the table automatically. A fix that
  removes a column or changes its type is refused (the file is
  quarantined: "data no longer matches") until the table is changed
  to match:
  - removed column: `ALTER TABLE \`<table>\` DROP COLUMN <column>`;
  - changed type: only widening changes (e.g. `INT64` to `NUMERIC`
    or `FLOAT64`) can be made in place, with `ALTER TABLE ... ALTER
    COLUMN ... SET DATA TYPE`. For anything else, copy the table if
    you want its history, drop it, and upload the file again:
    py-common recreates the table from that upload. Only the history
    of `_deleted_at` (which rows were marked deleted, when) is lost.

## An upload was wrong (bad data)

Upload the last good version again; it becomes the latest. The
landing bucket keeps every version, so you can restore it there:

```bash
gcloud storage ls -a "gs://$LANDING_BUCKET/was/WAS export.xlsx"
gcloud storage cp "gs://$LANDING_BUCKET/was/WAS export.xlsx#<good generation>" \
  "gs://$LANDING_BUCKET/was/WAS export.xlsx"
```

- **Raw route:** Dataform reads the restored upload, the newest. The
  bad upload's rows stay in the raw table, unread.
- **Typed route:** the merge puts the good values back. With
  `mark_deleted_rows`, rows that were only in the bad upload are
  marked deleted, and rows the bad upload had dropped are current
  again.

## An upload must be removed completely

For example, personal data loaded by mistake. This is rare and
deliberate, so it is done by hand, by someone with more access than
the pipeline (its service account can't delete stored copies).

1. **Replace it first** with a corrected upload (as above), so the
   folder still has a current file and the typed table has the
   corrected values. If a whole column must go, exclude it in the
   contract, and on the typed route also `DROP COLUMN` it.
2. **Raw tables:** for each raw table of that workbook (one per
   sheet), `DELETE FROM \`<raw table>\` WHERE source_file = '<file
   name>' AND source_generation = '<generation>'`.
3. **Stored copies:** delete the upload's copies in the store bucket.
   Copies are shared by uploads with the same bytes, so check the
   checksum isn't another upload's too:

   ```bash
   gcloud storage rm --recursive \
     "gs://$STORE_BUCKET/raw/<identity>/<checksum>/" \
     "gs://$STORE_BUCKET/processed/*/<identity>/<checksum>/"
   ```

   and `quarantine/<identity>/<checksum>/` if it was quarantined.
4. **The landing version:** `gcloud storage rm
   "gs://$LANDING_BUCKET/<identity>#<generation>"`.
5. **Leave the audit rows and records.** They hold names, checksums
   and counts, no data values, and they stop that upload being loaded
   again.

Deleted data isn't gone at once: BigQuery keeps it for time travel
(7 days by default) and then a 7-day fail-safe period, and GCS soft
delete keeps deleted objects (7 days by default). Check what the
DPIA needs.

## A workbook is renamed on purpose

Change the contract's `file_name`, deploy, then upload under the new
name. Uploads are tracked per name, so the old name needs one
clean-up step:

- **Raw route:** the old name's last upload still counts as "latest"
  for that name. Filter it out in Dataform
  (`WHERE source_file != '<old name>'`) or delete its rows.
- **Typed route with `mark_deleted_rows`:** once the new name has
  loaded, every row still in its file has been moved to the new name
  (`_source_file`). Rows still under the old name are gone from the
  workbook, so mark them:

  ```sql
  UPDATE `<table>`
  SET _deleted_at = CURRENT_TIMESTAMP()
  WHERE _source_file = '<old identity>' AND _deleted_at IS NULL
  ```

## A run's lock is stuck

See [`GCP_SETUP.md`](GCP_SETUP.md), section 5 ("If a run is killed"):
wait for the lease to expire (one hour), or check no run is active
and delete the lock object the error names.

## Old uploads (backfill)

Not needed: each upload holds the workbook's full history, and only
the latest upload is used. Only the current version of each landing
file is loaded; earlier versions stay in the landing bucket's
history, from where any one can be restored as above.
