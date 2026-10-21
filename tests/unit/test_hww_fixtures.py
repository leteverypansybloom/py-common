"""End-to-end tests of the raw JSON route on the HWW-style fixtures.

Runs Pipeline(output_format="raw_json") over the generated hww_*.xlsx
files with the local adapters, a fake warehouse that records what it
is given, and contracts like the ones the HWW pipelines use.
"""

import json
from pathlib import Path

import pytest

from py_common.adapters import LocalObjectStore, LocalSource
from py_common.contract import Column, Contract, Worksheet
from py_common.model import Outcome, Result, SourceItem
from py_common.pipeline import Pipeline


class RecordingWarehouse:
    """Warehouse that keeps every load instead of writing anywhere."""

    identity = "test.raw_hww.recorded"

    def __init__(self) -> None:
        """Start with no loads."""
        self.loads: list[dict] = []

    def load(
        self,
        key: str,
        checksum: str,
        data: bytes,
        rows: int,
        item: SourceItem,
    ) -> bool:
        """Record the load.

        Args:
            key: Load identifier.
            checksum: SHA-256 of the source file.
            data: NDJSON bytes.
            rows: Expected row count.
            item: Source file loaded.

        Returns:
            True: every load is new here.
        """
        self.loads.append({"key": key, "data": data, "rows": rows})
        return True

    def payloads(self) -> list[dict]:
        """Payloads of every row loaded so far.

        Returns:
            One payload dict per row, in load order.
        """
        return [
            json.loads(line)["payload"]
            for load in self.loads
            for line in load["data"].decode().splitlines()
        ]


def _process(
    folder: Path, store_dir: Path, filename: str, contract: Contract
) -> tuple[Result, RecordingWarehouse]:
    """Run one fixture file through a raw JSON pipeline.

    Args:
        folder: Folder holding the generated fixtures.
        store_dir: Folder for the local object store.
        filename: Fixture file to process.
        contract: Contract to validate against.

    Returns:
        (the file's Result, the warehouse it was loaded into).
    """
    source = LocalSource(folder)
    item: SourceItem = next(i for i in source.items() if i.name == filename)
    warehouse = RecordingWarehouse()
    pipeline = Pipeline(
        source,
        LocalObjectStore(store_dir),
        warehouse,
        contract,
        output_format="raw_json",
    )
    return pipeline.process(item), warehouse


def _peers_contract(on_mismatch: str = "quarantine") -> Contract:
    """Peer-monitoring contract: keep IDs and sector, drop personal data.

    Args:
        on_mismatch: "quarantine" or "warn".

    Returns:
        Contract.
    """
    return Contract(
        worksheets=[
            Worksheet(
                name="query (1)",
                on_mismatch=on_mismatch,
                columns=[
                    Column(
                        name="Participant ID", data_type="string", unique=True
                    ),
                    Column(name="Business Name", data_type="string"),
                    Column(name="Sector", data_type="string"),
                ],
                excluded=["First Name", "Email Address"],
            )
        ]
    )


class TestHwwFixturesHeaderRow:
    """hww_header_row_2.xlsx: headings under a "Last updated" line."""

    def test_rows_load_with_blank_heading_named_by_letter(
        self, source_folder, temp_dir
    ):
        """The sign-up month under the blank heading becomes col_B.

        Args:
            source_folder: Folder of generated fixtures.
            temp_dir: Temporary directory for the object store.
        """
        contract = Contract(
            worksheets=[
                Worksheet(
                    name="Account signups",
                    header_row=2,
                    columns=[
                        Column(name="col_B", data_type="date", nullable=True),
                        Column(name="Org", data_type="string"),
                        Column(name="Size", data_type="string"),
                    ],
                    excluded=["Count"],
                )
            ]
        )

        result, warehouse = _process(
            source_folder,
            temp_dir / "store",
            "hww_header_row_2.xlsx",
            contract,
        )

        assert result.outcome == Outcome.LOADED, result.errors
        assert warehouse.payloads() == [
            {"col_B": "2025-11-01T00:00:00", "Org": "Org 1", "Size": "1000+"},
            {"col_B": None, "Org": "Org 2", "Size": "10-49"},
        ]


class TestHwwFixturesRepeatedHeadings:
    """hww_repeated_other.xlsx: "Other" appears twice."""

    def test_both_other_columns_are_loaded(self, source_folder, temp_dir):
        """The second "Other" is loaded as Other_2, not dropped.

        Args:
            source_folder: Folder of generated fixtures.
            temp_dir: Temporary directory for the object store.
        """
        contract = Contract(
            worksheets=[
                Worksheet(
                    name="Survey info",
                    column_selection="all_except",
                    columns=[Column(name="Org", data_type="string")],
                )
            ]
        )

        result, warehouse = _process(
            source_folder,
            temp_dir / "store",
            "hww_repeated_other.xlsx",
            contract,
        )

        assert result.outcome == Outcome.LOADED, result.errors
        first = warehouse.payloads()[0]
        assert list(first) == [
            "Org",
            "Q4 Option A",
            "Other",
            "Q7 Option A",
            "Other_2",
        ]
        assert first["Other"] is None
        assert first["Other_2"] == "Yes"


class TestHwwFixturesPersonalData:
    """hww_personal_data.xlsx: names and emails stay out of BigQuery."""

    def test_excluded_columns_are_not_loaded(self, source_folder, temp_dir):
        """No payload holds a first name or an email address.

        Args:
            source_folder: Folder of generated fixtures.
            temp_dir: Temporary directory for the object store.
        """
        result, warehouse = _process(
            source_folder,
            temp_dir / "store",
            "hww_personal_data.xlsx",
            _peers_contract(),
        )

        assert result.outcome == Outcome.LOADED, result.errors
        for payload in warehouse.payloads():
            assert set(payload) == {
                "Participant ID",
                "Business Name",
                "Sector",
            }
        assert b"example.org" not in warehouse.loads[0]["data"]


class TestHwwFixturesHeaderMismatch:
    """hww_header_mismatch.xlsx: "Sector" renamed "Business Sector"."""

    def test_warn_mode_loads_and_records_the_warning(
        self, source_folder, temp_dir
    ):
        """The file loads, Sector is empty, and the warning is kept.

        Args:
            source_folder: Folder of generated fixtures.
            temp_dir: Temporary directory for the object store.
        """
        result, warehouse = _process(
            source_folder,
            temp_dir / "store",
            "hww_header_mismatch.xlsx",
            _peers_contract(on_mismatch="warn"),
        )

        assert result.outcome == Outcome.LOADED, result.errors
        assert "Missing column 'Sector'" in result.warnings[0]
        assert "Unexpected columns: Business Sector" in result.warnings[0]
        assert [p["Sector"] for p in warehouse.payloads()] == [None, None]

    def test_quarantine_mode_rejects_the_file(self, source_folder, temp_dir):
        """Nothing is loaded and the file goes to quarantine.

        Args:
            source_folder: Folder of generated fixtures.
            temp_dir: Temporary directory for the object store.
        """
        store_dir = temp_dir / "store"

        result, warehouse = _process(
            source_folder,
            store_dir,
            "hww_header_mismatch.xlsx",
            _peers_contract(on_mismatch="quarantine"),
        )

        assert result.outcome == Outcome.QUARANTINED
        assert warehouse.loads == []
        assert list((store_dir / "quarantine").rglob("*.xlsx"))

    @pytest.mark.parametrize("on_mismatch", ["warn", "quarantine"])
    def test_matching_file_has_no_warnings(
        self, source_folder, temp_dir, on_mismatch
    ):
        """The unrenamed file loads cleanly in either mode.

        Args:
            source_folder: Folder of generated fixtures.
            temp_dir: Temporary directory for the object store.
            on_mismatch: Mode under test.
        """
        result, _ = _process(
            source_folder,
            temp_dir / "store",
            "hww_personal_data.xlsx",
            _peers_contract(on_mismatch=on_mismatch),
        )

        assert result.outcome == Outcome.LOADED, result.errors
        assert result.warnings == []
