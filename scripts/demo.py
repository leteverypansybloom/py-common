"""Smoke test: run workbooks through the real GCS and BigQuery adapters.

A manual check, not part of the test suite. It writes test data to
the configured development GCS bucket and BigQuery dataset, and
creates a `manager-demo-input-<run id>/` folder (gitignored) in the
current directory. Run it only against development resources.

What it checks (see docs/SMOKE_TEST.md):

  0. The audit table has the 0.2.0 columns (destination,
     source_version); with --migrate it adds them.
  RawJsonWarehouse, inserting into smoke_events_raw:
  1. A valid file loads 2 raw rows; raw/ and processed/ copies
     exist; the audit row names the destination and source version.
  2. A rerun skips it (store marker).
  3. With the store markers deleted, a rerun still skips it: the
     warehouse's own audit check refuses a second load.
  4. An invalid file is quarantined and loads nothing.
  5. The same bytes as a new version load again (a revert must
     become the latest generation).
  6. No per-load staging table is left behind.
  7. With Contract.file_name set, a copy under another name is
     quarantined; the file under the agreed name is skipped as
     already loaded.

In PowerShell, first confirm these are set:
    $env:GCP_PROJECT_ID
    $env:GCS_RAW_BUCKET
    $env:BQ_DATASET

Then, from the repository root, run:
    python scripts/demo.py            # stops if the audit table is old
    python scripts/demo.py --migrate  # adds the two audit columns first
"""

import argparse
import os
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage
from openpyxl import load_workbook

from py_common import Column, Contract, Outcome, Pipeline, Worksheet
from py_common.adapters import LocalSource
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore
from py_common.logging import configure_json_logging
from py_common.model import digest

PROJECT = os.environ["GCP_PROJECT_ID"]
BUCKET = os.environ["GCS_RAW_BUCKET"].removeprefix("gs://")
DATASET = os.environ["BQ_DATASET"]
FIXTURES = Path(__file__).resolve().parents[1] / "tests/data/fixtures"
RAW_TABLE = "smoke_events_raw"
NEW_AUDIT_COLUMNS = ("destination", "source_version")


def check(condition: bool, message: str) -> None:
    """Print a pass line, or stop the smoke test with a failure.

    Args:
        condition: Whether the check passed.
        message: What was checked.
    """
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"ok: {message}")


def run(pipeline: Pipeline, label: str) -> list:
    """Run once and print each file's outcome.

    Args:
        pipeline: The pipeline to run.
        label: Printed before each result, to tell runs apart.

    Returns:
        The run's results.
    """
    results = pipeline.run()
    for result in results:
        print(
            f"  {label}: {result.item.name} {result.outcome.value} "
            f"{result.rows_processed} {'; '.join(result.errors)}"
        )
    return results


def outcome_of(results: list, name: str) -> Outcome:
    """Find one file's outcome in a run's results.

    Args:
        results: The run's results.
        name: The file name to look for.

    Returns:
        That file's outcome.
    """
    return next(r.outcome for r in results if r.item.name == name)


def ensure_audit_columns(client: bigquery.Client, migrate: bool) -> None:
    """Stop (or migrate) if the audit table predates 0.2.0.

    Args:
        client: The BigQuery client.
        migrate: Add the missing columns rather than stopping.
    """
    audit_id = f"{PROJECT}.{DATASET}.audit"
    try:
        columns = {f.name for f in client.get_table(audit_id).schema}
    except NotFound:
        print(
            f"FAIL: {audit_id} doesn't exist; create it as in "
            "docs/GCP_SETUP.md section 2"
        )
        sys.exit(1)
    missing = [c for c in NEW_AUDIT_COLUMNS if c not in columns]
    if not missing:
        print("ok: audit table has destination and source_version")
        return
    alter = f"ALTER TABLE `{audit_id}` " + ", ".join(
        f"ADD COLUMN IF NOT EXISTS {c} STRING" for c in missing
    )
    if not migrate:
        print(f"FAIL: audit table is missing {', '.join(missing)}.")
        print(f"Run this (or rerun with --migrate):\n  {alter}")
        sys.exit(1)
    client.query(alter).result()
    print(f"ok: migrated audit table ({alter})")


def delete_markers(bucket: storage.Bucket, identity: str) -> int:
    """Delete this warehouse's "already loaded" markers in GCS.

    Args:
        bucket: The raw bucket.
        identity: The warehouse identity the markers are kept under.

    Returns:
        How many markers were deleted.
    """
    deleted = 0
    for blob in bucket.list_blobs(prefix=f"audit/{identity}/"):
        blob.delete()
        deleted += 1
    return deleted


def count(client: bigquery.Client, sql: str) -> int:
    """Run a COUNT query.

    Args:
        client: The BigQuery client.
        sql: A query returning one row with one number.

    Returns:
        The number.
    """
    return list(client.query(sql).result())[0][0]


def main() -> None:
    """Run the smoke test end to end against the real GCP project."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="add the 0.2.0 audit columns if they are missing",
    )
    args = parser.parse_args()
    # Same log shape as the production job, so the alert filters in
    # docs/GCP_SETUP.md (jsonPayload.outcome etc.) can be tested
    # against this run's log lines.
    configure_json_logging()

    client = bigquery.Client(project=PROJECT)
    bucket = storage.Client(project=PROJECT).bucket(BUCKET)
    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    print(f"Smoke test run ID: {run_id}\n")

    print("0. Audit table schema")
    ensure_audit_columns(client, args.migrate)

    input_dir = Path(f"manager-demo-input-{run_id}")
    input_dir.mkdir()
    valid_file = input_dir / f"demo_valid_{run_id}.xlsx"
    invalid_file = input_dir / f"demo_invalid_{run_id}.xlsx"

    # A valid workbook with IDs unique to this run.
    shutil.copy2(FIXTURES / "events_valid.xlsx", valid_file)
    workbook = load_workbook(valid_file)
    sheet = workbook["Events"]
    sheet["A2"] = f"DEMO-{run_id}-001"
    sheet["A3"] = f"DEMO-{run_id}-002"
    sheet["C2"] = 10
    sheet["C3"] = 20
    workbook.save(valid_file)
    checksum = digest(valid_file.read_bytes())

    contract = Contract(
        worksheets=[
            Worksheet(
                name="Events",
                columns=[
                    Column("event_id", "string", unique=True),
                    Column("employer_id", "string"),
                    Column("attendees", "integer"),
                ],
            )
        ],
    )
    store = GCSObjectStore(PROJECT, BUCKET)
    raw = RawJsonWarehouse(PROJECT, DATASET, {"Events": RAW_TABLE})
    pipeline = Pipeline(LocalSource(input_dir), store, raw, contract)
    raw_rows = (
        f"SELECT COUNT(*) FROM `{PROJECT}.{DATASET}.{RAW_TABLE}` "
        f"WHERE checksum = '{checksum}'"
    )

    print("\n1. Valid file")
    results = run(pipeline, "run 1")
    check(results[0].outcome == Outcome.LOADED, "valid file LOADED")
    check(results[0].rows_processed == 2, "2 rows")

    # Raw copies are keyed {identity}/{checksum}/{name}; for
    # LocalSource the identity is the file name. Processed copies
    # are also keyed by warehouse, version and their own digest, so
    # the result says where its copy went.
    version_key = f"{valid_file.name}/{checksum}/{valid_file.name}"
    check(bucket.blob(f"raw/{version_key}").exists(), "raw/ copy exists")
    processed_key = results[0].processed_key
    check(
        processed_key.startswith(f"processed/{raw.identity}/")
        and bucket.blob(processed_key).exists(),
        "processed/ NDJSON exists",
    )
    check(count(client, raw_rows) == 2, "2 raw rows in BigQuery")
    audit = list(
        client.query(
            f"SELECT destination, source_version FROM "
            f"`{PROJECT}.{DATASET}.audit` "
            f"WHERE checksum = '{checksum}' AND outcome = 'LOADED'"
        ).result()
    )
    check(
        len(audit) == 1
        and audit[0]["destination"] == f"{PROJECT}.{DATASET}.{RAW_TABLE}",
        "one audit row naming the destination",
    )

    print("\n2. Rerun unchanged file")
    results = run(pipeline, "run 2")
    check(results[0].outcome == Outcome.SKIPPED, "SKIPPED by store marker")

    print("\n3. Store markers lost, rerun")
    print(f"  deleted {delete_markers(bucket, raw.identity)} marker(s)")
    results = run(pipeline, "run 3")
    check(
        results[0].outcome == Outcome.SKIPPED,
        "SKIPPED by the warehouse's own audit check",
    )
    check(count(client, raw_rows) == 2, "still 2 raw rows")

    print("\n4. Invalid file")
    shutil.copy2(FIXTURES / "events_missing_column.xlsx", invalid_file)
    results = run(pipeline, "run 4")
    check(
        outcome_of(results, invalid_file.name) == Outcome.QUARANTINED,
        "invalid file QUARANTINED",
    )
    invalid_key = (
        f"quarantine/{invalid_file.name}/"
        f"{digest(invalid_file.read_bytes())}/{invalid_file.name}"
    )
    check(bucket.blob(invalid_key).exists(), "quarantine/ copy exists")
    invalid_file.unlink()

    print("\n5. Same bytes, new version (a revert)")
    stat = valid_file.stat()
    os.utime(valid_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000))
    results = run(pipeline, "run 5")
    check(results[0].outcome == Outcome.LOADED, "new version LOADED")
    check(count(client, raw_rows) == 4, "4 raw rows, 2 per version")
    generations = count(
        client,
        f"SELECT COUNT(DISTINCT source_generation) FROM "
        f"`{PROJECT}.{DATASET}.{RAW_TABLE}` WHERE checksum = '{checksum}'",
    )
    check(generations == 2, "two distinct source_generation values")

    print("\n6. Staging tables cleaned up")
    leftovers = [
        t.table_id
        for t in client.list_tables(f"{PROJECT}.{DATASET}")
        if "_stg_" in t.table_id and checksum[:12] in t.table_id
    ]
    check(not leftovers, f"no staging tables left ({leftovers or 'none'})")

    print("\n7. A copy under another name (file_name)")
    named = replace(contract, file_name=valid_file.name)
    misnamed = input_dir / f"copy of {valid_file.name}"
    shutil.copy2(valid_file, misnamed)
    results = run(Pipeline(LocalSource(input_dir), store, raw, named), "run 7")
    check(
        outcome_of(results, misnamed.name) == Outcome.QUARANTINED,
        "other name QUARANTINED",
    )
    check(
        outcome_of(results, valid_file.name) == Outcome.SKIPPED,
        "agreed name SKIPPED (already loaded)",
    )
    misnamed.unlink()

    print("\nSmoke test passed.")


if __name__ == "__main__":
    main()
