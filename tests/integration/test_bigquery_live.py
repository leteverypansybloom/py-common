r"""Run the raw JSON warehouse against real BigQuery.

The unit tests run the adapter against a fake client that reads the
SQL text, and test_bq_dry_run.py checks that BigQuery accepts it.
These tests run it for real and check what lands: each upload loaded
once, reverts, the per-table "already loaded?" check, rollback, the
documented "latest upload" query and check_duplicate_loads().

Needs GCP_PROJECT_ID and BQ_TEST_DATASET, an existing dataset the
credentials can write to (roles/bigquery.dataEditor on it, and
roles/bigquery.jobUser on the project). Every table a run creates
starts with a prefix of its own and is deleted afterwards; give the
dataset a default table expiration as a safety net. Skipped unless
both variables are set. GitHub Actions runs it nightly; the set-up
is in docs/CI_SETUP.md. To run it yourself:

```bash
GCP_PROJECT_ID=my-project BQ_TEST_DATASET=py_common_ci \
  pytest -m integration tests/integration/test_bigquery_live.py
```
"""

import io
import itertools
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

bigquery = pytest.importorskip("google.cloud.bigquery")

from openpyxl import Workbook  # noqa: E402

from py_common.adapters import bigquery_raw  # noqa: E402
from py_common.adapters.bigquery_raw import RawJsonWarehouse  # noqa: E402
from py_common.contract import (  # noqa: E402
    Column,
    Contract,
    Worksheet,
)
from py_common.errors import ObjectStoreConflict  # noqa: E402
from py_common.model import Outcome, SourceItem, digest  # noqa: E402
from py_common.pipeline import Pipeline  # noqa: E402
from py_common.runner import check_duplicate_loads  # noqa: E402

PROJECT = os.environ.get("GCP_PROJECT_ID", "")
DATASET = os.environ.get("BQ_TEST_DATASET", "")
#: Every table this run creates starts with this.
PREFIX = f"it_{uuid.uuid4().hex[:8]}_"
FILE = "WAS export.xlsx"
DOCS = Path(__file__).resolve().parents[2] / "docs"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (PROJECT and DATASET),
        reason="GCP_PROJECT_ID and BQ_TEST_DATASET are not both set",
    ),
]

_numbers = itertools.count()


# ------------------------------------------------------------ helpers


class Uploads:
    """A landing folder holding one file; the test sets its upload."""

    def __init__(self) -> None:
        """Start with no upload."""
        self.data = b""
        self.version = ""

    def upload(self, data: bytes, version: str) -> None:
        """Replace the file with a new upload.

        Args:
            data: The workbook's bytes.
            version: The upload's version.
        """
        self.data, self.version = data, version

    def items(self) -> list[SourceItem]:
        """List the one file.

        Returns:
            Its SourceItem.
        """
        return [SourceItem(FILE, f"was/{FILE}", self.version, len(self.data))]

    def download(self, item: SourceItem) -> bytes:
        """Return the current upload's bytes.

        Args:
            item: The file asked for.

        Returns:
            The bytes.
        """
        return self.data


class Store:
    """In-memory ObjectStore with the write-once rule."""

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        """Return an object's bytes.

        Args:
            key: The object's key.

        Returns:
            The bytes, or None if there is no such object.
        """
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        """Write an object once.

        Args:
            key: The object's key.
            data: The bytes to store.

        Raises:
            ObjectStoreConflict: The key holds different bytes.
        """
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreConflict(key)
        self.objects[key] = data

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        """Hold no lock: one test runs at a time.

        Args:
            key: The lock's key, ignored.

        Yields:
            Nothing.
        """
        yield


def _book(sheets: dict[str, list[list[Any]]]) -> bytes:
    """Build a workbook with the given sheets.

    Args:
        sheets: Sheet name to rows, headings first.

    Returns:
        The .xlsx bytes.
    """
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _full(table: str) -> str:
    """Qualify a table name with the test project and dataset.

    Args:
        table: The table name.

    Returns:
        project.dataset.table.
    """
    return f"{PROJECT}.{DATASET}.{table}"


def _rows(client: Any, sql: str) -> list[dict[str, Any]]:
    """Run a query.

    Args:
        client: The BigQuery client.
        sql: The query.

    Returns:
        Each row as a dict.
    """
    return [dict(row.items()) for row in client.query(sql).result()]


def _count(client: Any, table: str, where: str = "TRUE") -> int:
    """Count a table's rows.

    Args:
        client: The BigQuery client.
        table: The table name.
        where: A filter on the rows counted.

    Returns:
        The count.
    """
    sql = f"SELECT COUNT(*) AS n FROM `{_full(table)}` WHERE {where}"
    return int(_rows(client, sql)[0]["n"])


def _outcomes(pipeline: Pipeline) -> list[Outcome]:
    """Run once; each result is printed, which pytest shows on failure."""
    results = pipeline.run()
    for result in results:
        print(result.item.name, result.outcome.value, result.errors)
    return [result.outcome for result in results]


def _documented_latest_query(table: str) -> str:
    """The "latest upload" query from RAW_TABLE_LAYOUT.md, for table."""
    text = (DOCS / "RAW_TABLE_LAYOUT.md").read_text(encoding="utf-8")
    section = text.split("**Latest upload of each file.**", 1)[1]
    sql = section.split("```sql", 1)[1].split("```", 1)[0]
    documented = "`project.raw_hww.hww_was_database`"
    assert documented in sql
    return sql.replace(documented, f"`{_full(table)}`")


# ----------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def client() -> Iterator[Any]:
    """Yield a BigQuery client, then delete this run's tables.

    Yields:
        The client.
    """
    bq = bigquery.Client(project=PROJECT)
    try:
        yield bq
    finally:
        for table in bq.list_tables(f"{PROJECT}.{DATASET}"):
            if table.table_id.startswith(PREFIX):
                bq.delete_table(table, not_found_ok=True)


@pytest.fixture
def name() -> str:
    """A table-name stem of this test's own."""
    return f"{PREFIX}{next(_numbers)}"


@pytest.fixture
def audit(client: Any, name: str) -> str:
    """A fresh audit table, created as docs/GCP_SETUP.md section 2 does."""
    table = f"{name}_audit"
    client.query(
        f"CREATE TABLE IF NOT EXISTS `{_full(table)}` (\n"
        "  filename STRING,\n"
        "  checksum STRING,\n"
        "  destination STRING,\n"
        "  source_version STRING,\n"
        "  row_count INT64,\n"
        "  outcome STRING,\n"
        "  timestamp TIMESTAMP\n"
        ")"
    ).result()
    return table


# --------------------------------------------------------- raw tables

SHEETS = Contract(
    worksheets=[
        Worksheet(
            "Signups",
            [
                Column("Participant ID", "string"),
                Column("Organisation Industry ", "string", nullable=True),
            ],
        ),
        Worksheet("Survey", [Column("Question", "string")]),
    ],
    file_name=FILE,
)
WORKBOOK = _book(
    {
        "Signups": [
            ["Participant ID", "Organisation Industry "],
            ["P1", "Retail"],
            ["P2", None],
        ],
        "Survey": [["Question"], ["Q1"]],
    }
)


def _raw_tables(name: str) -> dict[str, str]:
    """Name this test's raw tables.

    Args:
        name: This test's table-name stem.

    Returns:
        Sheet name to table name.
    """
    return {"Signups": f"{name}_signups_raw", "Survey": f"{name}_survey_raw"}


def test_raw_route_loads_each_upload_once_and_a_revert_again(
    client: Any, name: str, audit: str
) -> None:
    """Each upload loads once, and a reverted file loads again.

    Args:
        client: The BigQuery client.
        name: This test's table-name stem.
        audit: The audit table.
    """
    tables = _raw_tables(name)
    warehouse = RawJsonWarehouse(
        PROJECT, DATASET, tables, audit_table_id=audit
    )
    uploads = Uploads()
    pipeline = Pipeline(uploads, Store(), warehouse, SHEETS)
    signups = tables["Signups"]

    uploads.upload(WORKBOOK, "100")
    assert _outcomes(pipeline) == [Outcome.LOADED]
    assert _count(client, signups) == 2
    assert _count(client, tables["Survey"]) == 1
    # Payload keys are the headings as written, trailing space and all.
    assert _rows(
        client,
        "SELECT JSON_VALUE(payload, '$.\"Organisation Industry \"') AS v "
        f"FROM `{_full(signups)}` WHERE row_number = 2",
    ) == [{"v": "Retail"}]
    # ingested_at is the audit record's timestamp.
    assert _rows(
        client,
        f"SELECT COUNT(*) AS n FROM `{_full(signups)}` AS r "
        f"JOIN `{_full(audit)}` AS a ON a.timestamp = r.ingested_at "
        f"AND a.destination = '{_full(signups)}'",
    ) == [{"n": 2}]

    # The same upload again, markers lost: nothing is inserted.
    again = Pipeline(uploads, Store(), warehouse, SHEETS)
    assert _outcomes(again) == [Outcome.SKIPPED]
    assert _count(client, signups) == 2

    # The same bytes as a new upload (a revert) load again, and the
    # documented query reads only that upload.
    uploads.upload(WORKBOOK, "200")
    assert _outcomes(pipeline) == [Outcome.LOADED]
    assert _count(client, signups) == 4
    latest = _rows(client, _documented_latest_query(signups))
    assert sorted(row["source_generation"] for row in latest) == ["200", "200"]


def test_raw_route_loads_a_table_added_later_on_its_own(
    client: Any, name: str, audit: str
) -> None:
    """A table added to the pipeline later loads without the others.

    Args:
        client: The BigQuery client.
        name: This test's table-name stem.
        audit: The audit table.
    """
    tables = _raw_tables(name)
    uploads = Uploads()
    uploads.upload(WORKBOOK, "1")
    first = Contract(worksheets=SHEETS.worksheets[:1], file_name=FILE)
    signups_only = RawJsonWarehouse(
        PROJECT, DATASET, {"Signups": tables["Signups"]}, audit_table_id=audit
    )
    assert _outcomes(Pipeline(uploads, Store(), signups_only, first)) == [
        Outcome.LOADED
    ]

    # Same upload, with the Survey sheet now mapped too: only the new
    # table loads.
    both = RawJsonWarehouse(PROJECT, DATASET, tables, audit_table_id=audit)
    assert _outcomes(Pipeline(uploads, Store(), both, SHEETS)) == [
        Outcome.LOADED
    ]
    assert _count(client, tables["Signups"]) == 2
    assert _count(client, tables["Survey"]) == 1
    assert _count(client, audit, "outcome = 'LOADED'") == 2


def test_a_failure_inside_the_commit_rolls_everything_back(
    client: Any, name: str, audit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error just before COMMIT leaves no rows and no audit record.

    Args:
        client: The BigQuery client.
        name: This test's table-name stem.
        audit: The audit table.
        monkeypatch: Injects the failure into the commit script.
    """
    tables = _raw_tables(name)
    warehouse = RawJsonWarehouse(
        PROJECT, DATASET, tables, audit_table_id=audit
    )
    real = bigquery_raw.commit_script

    def failing(parts: Any, audit_full_id: str) -> str:
        """Build the real commit script with an error before COMMIT.

        Args:
            parts: The tables' parts of the script.
            audit_full_id: The audit table.

        Returns:
            The script.
        """
        # Fail after every table's insert and audit row, just before
        # COMMIT.
        return real(parts, audit_full_id).replace(
            "  COMMIT TRANSACTION;",
            "  SELECT ERROR('injected failure');\n  COMMIT TRANSACTION;",
        )

    monkeypatch.setattr(bigquery_raw, "commit_script", failing)
    uploads = Uploads()
    uploads.upload(WORKBOOK, "1")

    assert _outcomes(Pipeline(uploads, Store(), warehouse, SHEETS)) == [
        Outcome.FAILED
    ]
    assert _count(client, tables["Signups"]) == 0
    assert _count(client, tables["Survey"]) == 0
    assert _count(client, audit) == 0


def test_a_double_load_is_reported_and_hidden_from_the_latest_query(
    client: Any, name: str, audit: str
) -> None:
    """A double load is reported, and the latest query shows it once.

    Args:
        client: The BigQuery client.
        name: This test's table-name stem.
        audit: The audit table.
    """
    table = f"{name}_signups_raw"
    warehouse = RawJsonWarehouse(
        PROJECT, DATASET, {"Signups": table}, audit_table_id=audit
    )
    contract = Contract(worksheets=SHEETS.worksheets[:1], file_name=FILE)
    uploads = Uploads()
    uploads.upload(WORKBOOK, "1")
    assert _outcomes(Pipeline(uploads, Store(), warehouse, contract)) == [
        Outcome.LOADED
    ]

    # What two commits of one upload racing each other would leave:
    # every row and the audit row twice, a moment apart.
    for target, column in ((table, "ingested_at"), (audit, "timestamp")):
        client.query(
            f"INSERT INTO `{_full(target)}` SELECT * REPLACE "
            f"(TIMESTAMP_ADD(`{column}`, INTERVAL 1 SECOND) AS `{column}`) "
            f"FROM `{_full(target)}`"
        ).result()

    latest = _rows(client, _documented_latest_query(table))
    assert len(latest) == 2
    assert len({row["ingested_at"] for row in latest}) == 1

    found = check_duplicate_loads(client, _full(audit))
    assert found == [f"{_full(table)} was/{FILE}_{digest(WORKBOOK)[:8]} 1"]
