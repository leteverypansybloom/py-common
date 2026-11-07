"""Configurable Microsoft Graph list-export contracts."""

import json
from typing import Any

import pytest

from py_common import Column, GraphJsonLinesContract, Worksheet
from py_common.errors import ConfigError, ValidationError


class TestGraphJsonLinesContractRead:
    """Graph mapping preserves selected values and excludes source fields."""

    @pytest.fixture
    def contract(self):
        """A synthetic schema independent of the migrated HWW lists.

        Returns:
            A contract for one synthetic Graph list.
        """
        return GraphJsonLinesContract(
            worksheets=[
                Worksheet(
                    "items",
                    [
                        Column("item_id", "string"),
                        Column("title", "string"),
                        Column("lookup", "any"),
                        Column("choices", "any"),
                        Column("modified", "date"),
                    ],
                )
            ],
            file_format="jsonl",
            field_map={
                "title": "Title",
                "lookup": "ReviewerLookupId",
                "choices": "Choices",
            },
            metadata_map={"item_id": "id", "modified": "lastModifiedDateTime"},
            excluded_fields=["ContactEmail"],
            site_time_zone="Europe/London",
        )

    def test_maps_fields_metadata_and_multivalue(self, contract):
        """Explicit mappings keep lookup identifiers and multi-value shapes.

        Args:
            contract: Synthetic Graph contract.
        """
        data = json.dumps(
            {
                "id": "42",
                "lastModifiedDateTime": "2026-11-03T14:30:00Z",
                "fields": {
                    "Title": "Example",
                    "ReviewerLookupId": "17",
                    "Choices": ["one", "two"],
                    "ContactEmail": "synthetic@example.invalid",
                },
            }
        ).encode()
        sheet = contract.read(data, "items.jsonl")["items"]
        assert sheet.rows == [
            (
                1,
                (
                    "42",
                    "Example",
                    "17",
                    ["one", "two"],
                    "2026-11-03T14:30:00",
                ),
            )
        ]
        assert "ContactEmail" not in sheet.columns
        assert b"synthetic@example.invalid" in data

    def test_unknown_field_is_private(self, contract):
        """Unknown internal keys never enter error messages.

        Args:
            contract: Synthetic Graph contract.
        """
        data = b'{"id":"42","fields":{"synthetic@example.invalid":1}}'
        with pytest.raises(ValidationError) as caught:
            contract.read(data, "items.jsonl")
        assert "synthetic@example.invalid" not in str(caught.value)

    def test_missing_mapping_fails_setup(self):
        """A production list cannot run with guessed field mappings."""
        with pytest.raises(ConfigError, match="internal field mapping"):
            GraphJsonLinesContract(
                worksheets=[Worksheet("items", [Column("title", "string")])],
                file_format="jsonl",
            )

    def test_nullable_graph_field_can_be_absent(self):
        """Graph omits blank optional fields without losing the item id."""
        contract = GraphJsonLinesContract(
            worksheets=[
                Worksheet(
                    "items",
                    [
                        Column("id", "string"),
                        Column("optional", "any", nullable=True),
                    ],
                )
            ],
            file_format="jsonl",
            field_map={"optional": "SyntheticOptional"},
            metadata_map={"id": "id"},
        )
        rows = contract.read(b'{"id":"42","fields":{}}', "items.jsonl")
        assert rows["items"].rows == [(1, ("42", None))]

    def test_field_in_no_item_is_warned_of(self) -> None:
        """A mapped field no item holds loads as None, with a warning."""
        contract = GraphJsonLinesContract(
            worksheets=[
                Worksheet(
                    "items",
                    [
                        Column("id", "string"),
                        Column("optional", "any", nullable=True),
                    ],
                )
            ],
            file_format="jsonl",
            field_map={"optional": "SyntheticOptional"},
            metadata_map={"id": "id"},
        )
        data = b'{"id":"1","fields":{}}\n{"id":"2","fields":{}}\n'
        warnings: list[str] = []

        rows = contract.read(data, "items.jsonl", warnings)

        assert rows["items"].rows == [(1, ("1", None)), (2, ("2", None))]
        assert warnings == [
            "items.jsonl [items]: column 'optional' is in no record, so it "
            "loads as empty in every row; check the export still includes it"
        ]

    def test_field_in_some_items_is_not_warned_of(self) -> None:
        """A field that only some items hold is an ordinary blank."""
        contract = GraphJsonLinesContract(
            worksheets=[
                Worksheet(
                    "items",
                    [
                        Column("id", "string"),
                        Column("optional", "any", nullable=True),
                    ],
                )
            ],
            file_format="jsonl",
            field_map={"optional": "SyntheticOptional"},
            metadata_map={"id": "id"},
        )
        data = (
            b'{"id":"1","fields":{}}\n'
            b'{"id":"2","fields":{"SyntheticOptional":"x"}}\n'
        )
        warnings: list[str] = []

        contract.read(data, "items.jsonl", warnings)

        assert warnings == []


def dated_contract(**settings: Any) -> GraphJsonLinesContract:
    """Build a contract with a date and a multi-choice column.

    Args:
        **settings: Contract settings to add.

    Returns:
        A contract for one synthetic Graph list.
    """
    return GraphJsonLinesContract(
        worksheets=[
            Worksheet(
                "items",
                [
                    Column("held", "date", nullable=True),
                    Column("topics", "string", nullable=True),
                ],
            )
        ],
        file_format="jsonl",
        field_map={"held": "Held", "topics": "Topics"},
        **settings,
    )


def first_row(contract: GraphJsonLinesContract, fields: Any) -> Any:
    """Read one synthetic list item and return its selected values.

    Args:
        contract: The contract to read with.
        fields: The item's fields object.

    Returns:
        The values of the item's row.
    """
    data = json.dumps({"id": "1", "fields": fields}).encode()
    return contract.read(data, "items.jsonl")["items"].rows[0][1]


class TestSiteTimeZone:
    """Date columns are converted from UTC to the site's time zone."""

    def test_summer_midnight_keeps_its_day(self) -> None:
        """Local midnight in UK summer time stays on the entered date."""
        contract = dated_contract(site_time_zone="Europe/London")
        row = first_row(contract, {"Held": "2026-05-31T23:00:00Z"})
        assert row[0] == "2026-06-01T00:00:00"

    def test_winter_midnight_keeps_its_day(self) -> None:
        """In winter the UK is on UTC, so the date is unchanged."""
        contract = dated_contract(site_time_zone="Europe/London")
        row = first_row(contract, {"Held": "2026-01-15T00:00:00Z"})
        assert row[0] == "2026-01-15T00:00:00"

    def test_text_without_an_offset_is_unchanged(self) -> None:
        """A value already in local time isn't moved."""
        contract = dated_contract(site_time_zone="Europe/London")
        assert first_row(contract, {"Held": "2026-06-01"})[0] == "2026-06-01"

    def test_date_column_needs_a_time_zone(self) -> None:
        """A date column can't run without the site's time zone."""
        with pytest.raises(ConfigError, match="site_time_zone"):
            dated_contract()

    def test_unknown_time_zone_is_refused(self) -> None:
        """A misspelt zone fails at start-up."""
        with pytest.raises(ConfigError, match="Unknown"):
            dated_contract(site_time_zone="Europe/Atlantis")


class TestMultiValueSeparators:
    """Multi-choice lists become the text a workbook export holds."""

    @pytest.fixture
    def contract(self) -> GraphJsonLinesContract:
        """A contract joining topics with the SharePoint separator.

        Returns:
            The contract.
        """
        return dated_contract(
            site_time_zone="Europe/London",
            multi_value_separators={"topics": ";#"},
        )

    def test_choices_are_joined(
        self, contract: GraphJsonLinesContract
    ) -> None:
        """A list of choices becomes one separated text value.

        Args:
            contract: The contract.
        """
        row = first_row(contract, {"Topics": ["Stress", "Sleep"]})
        assert row[1] == "Stress;#Sleep"

    def test_single_text_value_is_unchanged(
        self, contract: GraphJsonLinesContract
    ) -> None:
        """A single choice column still loads.

        Args:
            contract: The contract.
        """
        assert first_row(contract, {"Topics": "Stress"})[1] == "Stress"

    def test_empty_list_is_blank(
        self, contract: GraphJsonLinesContract
    ) -> None:
        """No choices is a blank cell.

        Args:
            contract: The contract.
        """
        assert first_row(contract, {"Topics": []})[1] is None

    def test_bad_choice_quarantines_without_its_value(
        self, contract: GraphJsonLinesContract
    ) -> None:
        """A choice that can't be joined is refused, and not echoed.

        Args:
            contract: The contract.
        """
        with pytest.raises(ValidationError) as caught:
            first_row(contract, {"Topics": ["private;#value"]})
        assert "private" not in str(caught.value)

    def test_only_text_columns_can_be_joined(self) -> None:
        """A separator on another kind of column fails at start-up."""
        with pytest.raises(ConfigError, match="separators"):
            dated_contract(
                site_time_zone="Europe/London",
                multi_value_separators={"held": ";#"},
            )
