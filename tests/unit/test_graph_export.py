"""A Graph list export with real headings, against its workbook.

docs/dummy_was_database.jsonl holds docs/dummy_was_database.xlsx in the
form GET /sites/{site}/lists/{list}/items?$expand=fields returns: an
envelope around nested fields, SharePoint internal names and system
fields, UTC dates, multi-choice arrays, person lookups and blank values
left out. Reading it must give the workbook's values.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from py_common import (
    Column,
    Contract,
    GraphJsonLinesContract,
    Outcome,
    Pipeline,
    SourceItem,
    ValidationError,
    Worksheet,
)
from py_common.adapters import LocalObjectStore, LocalSource
from py_common.contract import FILE_EXTENSIONS
from py_common.raw import json_value

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"
EXPORT = DOCS / "dummy_was_database.jsonl"
WORKBOOK = DOCS / "dummy_was_database.xlsx"
ROWS = 35
TOPICS_1 = "Topics discussed in session 1"
TOPICS_2 = "Topics discussed in session 2"
FIELD_MAP = {
    "Participant ID": "Participant_x0020_ID",
    "Date of Enquiry": "Date_x0020_of_x0020_Enquiry",
    "Title": "Title",
    "Organisation Industry ": "Organisation_x0020_Industry_x002",
    "Date of Session 1 ": "Date_x0020_of_x0020_Session_x002",
    "Duration of session 1 ": "Duration_x0020_of_x0020_session_",
    TOPICS_1: "Topics_x0020_discussed_x0020_in_",
    "Date of session 2": "Date_x0020_of_x0020_session_x0020",
    "Duration of session 2": "Duration_x0020_of_x0020_session_0",
    "Date Summary 2 sent ": "Date_x0020_Summary_x0020_2_x0020",
    TOPICS_2: "Topics_x0020_discussed_x0020_in_0",
    "Date of Session 3 ": "Date_x0020_of_x0020_Session_x0021",
}
EXCLUDED_FIELDS = [
    "Number_x0020_of_x0020_employees",
    "Sector",
    "Number_x0020_of_x0020_sites",
    "Have_x0020_they_x0020_worked_x00",
    "How_x0020_did_x0020_they_x0020_h",
    "Key_x0020_contact_x0020_",
    "Job_x0020_role",
    "Preferred_x0020_contact_x0020_de",
    "Accessibility_x0020_requirements",
    "Date_x0020_allocated_x0020_to_x0",
    "HWW_x0020_AdviserLookupId",
    "Date_x0020_booking_x0020_request",
    "Date_x0020_summary_x0020_sent_x0",
    "Topics_x0020_discussed_x0020_in_@odata.type",
    "Topics_x0020_discussed_x0020_in_0@odata.type",
    "@odata.etag",
    "id",
    "ContentType",
    "Modified",
    "Created",
    "AuthorLookupId",
    "EditorLookupId",
    "_UIVersionString",
    "Attachments",
    "Edit",
    "LinkTitleNoMenu",
    "LinkTitle",
    "ItemChildCount",
    "FolderChildCount",
    "_ComplianceFlags",
    "_ComplianceTag",
    "_ComplianceTagWrittenTime",
    "_ComplianceTagUserId",
]


def column(heading: str) -> Column:
    """A loaded column, declared as HWW declares the WAS columns.

    Args:
        heading: The column's canonical heading.

    Returns:
        A nullable date, text or (for the ID) unchecked column.
    """
    if heading == "Participant ID":
        return Column(heading, "any", nullable=True)
    if heading.startswith("Date "):
        return Column(heading, "date", nullable=True)
    return Column(heading, "string", nullable=True)


def columns() -> list[Column]:
    """The loaded columns.

    Returns:
        One column per mapped heading.
    """
    return [column(heading) for heading in FIELD_MAP]


def export_contract(**settings: Any) -> GraphJsonLinesContract:
    """The export's contract.

    Args:
        **settings: Settings to replace.

    Returns:
        A contract with the list's mapping and the WAS file's name.
    """
    arguments: dict[str, Any] = {
        "worksheets": [Worksheet("query", columns())],
        "file_name": EXPORT.name,
        "file_format": "jsonl",
        "field_map": FIELD_MAP,
        "excluded_fields": EXCLUDED_FIELDS,
        "site_time_zone": "Europe/London",
        "multi_value_separators": {TOPICS_1: ";#", TOPICS_2: ";#"},
    }
    arguments.update(settings)
    return GraphJsonLinesContract(**arguments)


def workbook_values() -> dict[str, list[Any]]:
    """Each loaded column's values in the workbook, in JSON form.

    Returns:
        Heading -> one value per row.
    """
    contract = Contract(
        worksheets=[Worksheet("query", columns(), column_selection="listed")]
    )
    sheet = contract.extract(WORKBOOK.read_bytes(), WORKBOOK.name)["query"]
    return {
        heading: [json_value(values[index]) for _, values in sheet.rows]
        for index, heading in enumerate(sheet.columns)
    }


def lines_with(change: Any) -> bytes:
    """The export with a change made to its first item.

    Args:
        change: Changes one parsed item in place.

    Returns:
        The changed file's bytes.
    """
    lines = EXPORT.read_text(encoding="utf-8").splitlines()
    item = json.loads(lines[0])
    change(item)
    lines[0] = json.dumps(item)
    return "\n".join(lines).encode()


class RecordingWarehouse:
    """Warehouse that keeps every load instead of writing anywhere."""

    identity = "test.raw.was"

    def __init__(self) -> None:
        """Start with no loads."""
        self.rows: list[dict[str, Any]] = []

    def load(
        self,
        key: str,
        checksum: str,
        data: bytes,
        rows: int,
        item: SourceItem,
    ) -> bool:
        """Record the load's rows.

        Args:
            key: Load identifier.
            checksum: SHA-256 of the source file.
            data: NDJSON bytes.
            rows: Expected row count.
            item: Source file loaded.

        Returns:
            True: every load is new here.
        """
        self.rows += [json.loads(line) for line in data.splitlines()]
        return True


@pytest.fixture(scope="module")
def sheet() -> Any:
    """The export, read through its contract.

    Returns:
        The selected rows.
    """
    return export_contract().read(EXPORT.read_bytes(), EXPORT.name)["query"]


class TestReadingTheExport:
    """The export reads back to the workbook's values."""

    def test_one_row_per_line(self, sheet: Any) -> None:
        """A row's number is its line number.

        Args:
            sheet: The export's selected rows.
        """
        assert [number for number, _ in sheet.rows] == list(range(1, ROWS + 1))

    @pytest.mark.parametrize("heading", list(FIELD_MAP))
    def test_column_matches_the_workbook(
        self, sheet: Any, heading: str
    ) -> None:
        """Dates, text, numbers and blanks all arrive unchanged.

        Args:
            sheet: The export's selected rows.
            heading: The column under test.
        """
        index = sheet.columns.index(heading)
        found = [values[index] for _, values in sheet.rows]
        assert found == workbook_values()[heading]

    def test_multi_choice_joins_as_in_the_workbook(self, sheet: Any) -> None:
        """A list of choices becomes the workbook's separated text.

        Args:
            sheet: The export's selected rows.
        """
        index = sheet.columns.index(TOPICS_2)
        assert sheet.rows[0][1][index] == (
            "Physical Activity ;#Mental Health and Wellbeing"
        )

    def test_one_choice_is_plain_text(self, sheet: Any) -> None:
        """A list holding one choice has no separator, as in the workbook.

        Args:
            sheet: The export's selected rows.
        """
        index = sheet.columns.index(TOPICS_1)
        assert sheet.rows[0][1][index] == (
            "Mental health and wellbeing, burnout etc. "
        )

    def test_similar_internal_names_stay_apart(self, sheet: Any) -> None:
        """Names cut to 32 characters still reach their own column.

        Args:
            sheet: The export's selected rows.
        """
        first = sheet.columns.index("Duration of session 1 ")
        second = sheet.columns.index("Duration of session 2")
        assert sheet.rows[0][1][first] == "Under 90 minutes"
        assert sheet.rows[0][1][second] == "Under 90 mins"

    def test_blank_values_left_out_are_null(self, sheet: Any) -> None:
        """Graph leaves out a blank, which reads as an empty cell.

        Args:
            sheet: The export's selected rows.
        """
        index = sheet.columns.index("Participant ID")
        assert sum(values[index] is None for _, values in sheet.rows) == 1

    def test_envelope_and_excluded_fields_are_not_selected(
        self, sheet: Any
    ) -> None:
        """People, addresses and system fields never reach the rows.

        Args:
            sheet: The export's selected rows.
        """
        assert sheet.columns == list(FIELD_MAP)
        text = json.dumps(sheet.rows)
        assert "example.org" not in text
        assert "Jan Job" not in text


class TestRefusedExports:
    """A changed export stops the file, without repeating its values."""

    def test_new_field_is_refused(self) -> None:
        """A list column nobody has decided about stops the file."""

        def add(item: dict[str, Any]) -> None:
            """Add an unknown field.

            Args:
                item: The list item.
            """
            item["fields"]["Home_x0020_address"] = "1 High Street"

        with pytest.raises(ValidationError) as caught:
            export_contract().read(lines_with(add), EXPORT.name)
        assert "line 1: 1 unrecognised" in str(caught.value)
        assert "High Street" not in str(caught.value)

    def test_new_field_annotation_is_refused(self) -> None:
        """Graph's type annotation is a field like any other."""
        excluded = [n for n in EXCLUDED_FIELDS if "@odata.type" not in n]
        with pytest.raises(ValidationError, match="unrecognised"):
            export_contract(excluded_fields=excluded).read(
                EXPORT.read_bytes(), EXPORT.name
            )

    def test_item_without_fields_is_refused(self) -> None:
        """An export made without $expand=fields is refused."""
        with pytest.raises(ValidationError, match="line 1 needs a fields"):
            export_contract().read(
                lines_with(lambda item: item.pop("fields")), EXPORT.name
            )

    def test_item_without_id_is_refused(self) -> None:
        """Every list item carries its id."""
        with pytest.raises(ValidationError, match="line 1 needs a Graph item"):
            export_contract().read(
                lines_with(lambda item: item.pop("id")), EXPORT.name
            )

    def test_export_under_another_name_is_refused(self) -> None:
        """The fixed file name is checked as for a workbook."""
        with pytest.raises(ValidationError):
            export_contract().read(EXPORT.read_bytes(), "other.jsonl")


class TestPipeline:
    """The export goes through a pipeline as a workbook does."""

    @pytest.fixture
    def run(self, tmp_path: Path) -> tuple[Any, RecordingWarehouse, Path]:
        """Process the export once.

        Args:
            tmp_path: Folder for the landing folder and store.

        Returns:
            The result, the warehouse and the store's folder.
        """
        landing = tmp_path / "landing"
        landing.mkdir()
        (landing / EXPORT.name).write_bytes(EXPORT.read_bytes())
        contract = export_contract()
        source = LocalSource(
            landing, extensions=FILE_EXTENSIONS[contract.file_format]
        )
        (item,) = source.items()
        warehouse = RecordingWarehouse()
        store = tmp_path / "store"
        result = Pipeline(
            source, LocalObjectStore(store), warehouse, contract
        ).process(item)
        return result, warehouse, store

    def test_it_loads_every_row(
        self, run: tuple[Any, RecordingWarehouse, Path]
    ) -> None:
        """Each row names the file, the sheet and its line.

        Args:
            run: The result, the warehouse and the store's folder.
        """
        result, warehouse, _ = run
        assert result.outcome == Outcome.LOADED
        assert len(warehouse.rows) == ROWS
        assert warehouse.rows[0]["source_file"] == EXPORT.name
        assert warehouse.rows[0]["sheet_name"] == "query"
        assert warehouse.rows[0]["row_number"] == 1
        assert set(warehouse.rows[0]["payload"]) == set(FIELD_MAP)

    def test_the_raw_copy_is_the_original(
        self, run: tuple[Any, RecordingWarehouse, Path]
    ) -> None:
        """The archive keeps the envelope and every excluded field.

        Args:
            run: The result, the warehouse and the store's folder.
        """
        _, _, store = run
        copies = [p for p in store.rglob(EXPORT.name) if p.is_file()]
        assert EXPORT.read_bytes() in [p.read_bytes() for p in copies]
