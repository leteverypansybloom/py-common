"""M4: typed-route columns get names BigQuery accepts (REVIEW_.md).

Headings became BigQuery column names as written. BigQuery's own
rules (letters, digits and underscores only; case ignored, so
"Score" and "score" clash; at most 300 characters) then rejected
the load, as a ServiceError, so the file FAILED on every retry
instead of loading. Headings are now turned into safe names:
"Date of Enquiry" becomes date_of_enquiry.
"""

import io
from typing import Any
from unittest.mock import Mock, patch

import pyarrow.parquet as pq
import pytest
from _fake_bigquery import FakeBigQueryClient
from _memory_store import MemoryStore
from openpyxl import Workbook

from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.contract import Column, Contract, SheetRows, Worksheet
from py_common.model import Outcome, SourceItem
from py_common.parquet import column_name, column_names, to_parquet
from py_common.pipeline import Pipeline

CASES = [
    ("Date of Enquiry", "date_of_enquiry"),
    ("Participant ID", "participant_id"),
    ("Organisation Industry ", "organisation_industry"),
    ("event_id", "event_id"),
    ("Café", "cafe"),
    ("% complete", "percent_complete"),
    ("R&D", "r_and_d"),
    ("Site #", "site_number"),
    ("18+", "col_18_plus"),
    ("2024 total", "col_2024_total"),
    ("No. of sites (approx.)", "no_of_sites_approx"),
    ("__private", "private"),
    ("_loaded_at", "loaded_at"),
    ("col_B", "col_b"),
    ("Other_2", "other_2"),
]


class TestColumnName:
    @pytest.mark.parametrize("heading, expected", CASES)
    def test_heading_becomes_a_safe_name(
        self, heading: str, expected: str
    ) -> None:
        assert column_name(heading) == expected

    @pytest.mark.parametrize("_, name", CASES)
    def test_a_safe_name_is_left_alone(self, _: str, name: str) -> None:
        assert column_name(name) == name

    @pytest.mark.parametrize("heading", ["?!", "-", "数量", ""])
    def test_no_letters_or_digits_gives_nothing(self, heading: str) -> None:
        assert column_name(heading) == ""

    def test_long_heading_is_cut_to_300(self) -> None:
        assert column_name("a" * 400) == "a" * 300

    def test_never_starts_with_an_underscore(self) -> None:
        # So it can't take a reserved prefix (_TABLE_, _PARTITION...)
        # or a column the warehouse sets (_loaded_at, _deleted_at).
        for heading in ["_TABLE_x", "_partition", "__ROOT__", "_deleted_at"]:
            assert not column_name(heading).startswith("_")


class TestColumnNames:
    def test_names_that_clash_are_numbered(self) -> None:
        names = column_names(["Score", "score", "SCORE %", "Score "])

        assert names == ["score", "score_2", "score_percent", "score_3"]

    def test_numbering_skips_another_column_s_own_name(self) -> None:
        assert column_names(["a", "A", "a_2"]) == ["a", "a_3", "a_2"]

    def test_merge_keys_keep_their_own_names(self) -> None:
        names = column_names(["ID", "id", "Name"], keys=["id"])

        assert names == ["id_2", "id", "name"]

    def test_unusable_heading_is_named_by_position(self) -> None:
        assert column_names(["Name", "?", "数量"]) == [
            "name",
            "col_2",
            "col_3",
        ]

    def test_numbered_long_name_stays_within_300(self) -> None:
        names = column_names(["a" * 400, "A" * 400])

        assert names[1] == "a" * 298 + "_2"
        assert all(len(name) <= 300 for name in names)

    def test_keys_that_clash_are_refused(self) -> None:
        with pytest.raises(ValueError, match="'id'"):
            column_names(["ID", "id"], keys=["ID", "id"])


class TestParquet:
    def test_columns_are_written_under_safe_names(self) -> None:
        sheet = SheetRows(
            columns=["Participant ID", "Date of Enquiry", "Score %"],
            rows=[(2, ("P1", None, 5))],
        )
        worksheet = Worksheet(
            "S",
            [
                Column("Participant ID", "string"),
                Column("Date of Enquiry", "date", nullable=True),
                Column("Score %", "integer"),
            ],
        )

        data = to_parquet(sheet, worksheet, key_columns=["Participant ID"])

        table = pq.read_table(io.BytesIO(data))
        assert table.column_names == [
            "participant_id",
            "date_of_enquiry",
            "score_percent",
        ]
        assert table.column("score_percent").to_pylist() == [5]


def _book(rows: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Signups"
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


CONTRACT = Contract(
    key_columns=["Participant ID"],
    worksheets=[
        Worksheet(
            "Signups",
            [
                Column("Participant ID", "string"),
                Column("Organisation Industry ", "string", nullable=True),
                Column("Number of sites", "integer", nullable=True),
            ],
        )
    ],
)


class TestTypedRoute:
    @pytest.fixture
    def client(self) -> FakeBigQueryClient:
        return FakeBigQueryClient()

    def _warehouse(
        self, client: FakeBigQueryClient, **kwargs: Any
    ) -> BigQueryWarehouse:
        with patch("py_common.adapters.bigquery.bigquery.Client") as cls:
            cls.return_value = client
            return BigQueryWarehouse("p", "d", "signups", **kwargs)

    def test_headings_load_and_merge_under_safe_names(
        self, client: FakeBigQueryClient
    ) -> None:
        data = _book(
            [
                [
                    "Participant ID",
                    "Organisation Industry ",
                    "Number of sites",
                ],
                ["P1", "Retail", 3],
            ]
        )
        source = Mock()
        source.items.return_value = [SourceItem("s.xlsx", "s.xlsx", "1", 1)]
        source.download.return_value = data
        warehouse = self._warehouse(
            client, key_columns=CONTRACT.key_columns, mark_deleted_rows=True
        )

        results = Pipeline(source, MemoryStore(), warehouse, CONTRACT).run()

        assert results[0].outcome == Outcome.LOADED, results[0].errors
        assert client.tables["p.d.signups"][:3] == [
            "participant_id",
            "organisation_industry",
            "number_of_sites",
        ]
        (script,) = client.scripts
        assert "ON T.`participant_id` = S.`participant_id`" in script

    def test_warehouse_refuses_keys_that_clash(
        self, client: FakeBigQueryClient
    ) -> None:
        with pytest.raises(ValueError, match="key_columns"):
            self._warehouse(client, key_columns=["ID", "id"])

    def test_warehouse_refuses_a_key_with_no_letters_or_digits(
        self, client: FakeBigQueryClient
    ) -> None:
        with pytest.raises(ValueError, match="key_columns"):
            self._warehouse(client, key_columns=["?"])

    def test_already_safe_keys_are_unchanged(
        self, client: FakeBigQueryClient
    ) -> None:
        warehouse = self._warehouse(client, key_columns=["event_id"])

        assert warehouse.key_columns == ["event_id"]
