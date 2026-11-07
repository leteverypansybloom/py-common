"""Read Microsoft Graph list-item JSON Lines with explicit field maps."""

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from py_common.contract import (
    Contract,
    SheetRows,
    _absent_everywhere,
    read_jsonl,
)
from py_common.errors import ConfigError, ValidationError


def _site_time(value: Any, zone: tzinfo) -> Any:
    """Turn a UTC Graph timestamp into the site's local date and time.

    SharePoint stores a date-only value as local midnight converted to
    UTC, so during UK summer time 1 June arrives as 31 May 23:00. Text
    without an offset, or that isn't a timestamp, is left for the
    column's type check.

    Args:
        value: A date column's value.
        zone: The SharePoint site's time zone.

    Returns:
        The local date and time as ISO text without an offset, or the
        value unchanged.
    """
    if not isinstance(value, str):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    try:
        return moment.astimezone(zone).replace(tzinfo=None).isoformat()
    except OverflowError:
        # A placeholder such as 0001-01-01: left for the type check,
        # which refuses a date outside the accepted years.
        return value


def _joined(value: Any, separator: str) -> Any:
    """Join a multi-choice value into one text value.

    Args:
        value: A multi-choice column's value.
        separator: The text placed between choices.

    Returns:
        The joined text, None for an empty list, or a value that isn't
        a list unchanged.

    Raises:
        ValueError: A choice isn't text, or contains the separator.
    """
    if not isinstance(value, list):
        return value
    if any(not isinstance(item, str) or separator in item for item in value):
        raise ValueError("holds a choice that cannot be joined")
    return separator.join(value) if value else None


@dataclass
class GraphJsonLinesContract(Contract):
    """Map Graph fields and metadata to a canonical one-table contract.

    Original exports are archived by Pipeline without modification.
    Only mapped fields reach the selected payload. Unmapped source
    fields must be explicitly excluded by their internal names.

    Attributes:
        field_map: Canonical heading to SharePoint internal field name.
        metadata_map: Canonical heading to Graph envelope property,
            such as id, createdDateTime or lastModifiedDateTime.
        excluded_fields: Internal fields that must not be loaded.
        site_time_zone: The SharePoint site's time zone, such as
            Europe/London. Required when the contract has a date
            column: date columns are converted from UTC to this zone.
        multi_value_separators: Canonical heading of a text column to
            the separator that joins a multi-choice value into text.
    """

    field_map: dict[str, str] = field(default_factory=dict)
    metadata_map: dict[str, str] = field(default_factory=dict)
    excluded_fields: list[str] = field(default_factory=list)
    site_time_zone: str = ""
    multi_value_separators: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Require an explicit mapping and JSON Lines format.

        Raises:
            ConfigError: The Graph schema is absent or ambiguous.
        """
        super().__post_init__()
        if any(
            not isinstance(mapping, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key.strip()
                or not value.strip()
                for key, value in mapping.items()
            )
            for mapping in (self.field_map, self.metadata_map)
        ):
            raise ConfigError(
                "Graph mappings need nonblank string keys and values"
            )
        if not isinstance(self.excluded_fields, list) or any(
            not isinstance(name, str) or not name.strip()
            for name in self.excluded_fields
        ):
            raise ConfigError("Graph excluded fields must be a list of names")
        if self.file_format != "jsonl" or len(self.worksheets) != 1:
            raise ConfigError("Graph exports need one JSON Lines worksheet")
        if not self.field_map:
            raise ConfigError("Configure the Graph internal field mapping")
        destinations = list(self.field_map) + list(self.metadata_map)
        if len(destinations) != len(set(destinations)):
            raise ConfigError("Graph field and metadata targets overlap")
        expected = {column.name for column in self.worksheets[0].columns}
        if set(destinations) != expected:
            raise ConfigError("Graph mappings must match the contract columns")
        if set(self.field_map.values()) & set(self.excluded_fields):
            raise ConfigError("A Graph field is both mapped and excluded")
        kinds = {c.name: c.data_type for c in self.worksheets[0].columns}
        if "date" in kinds.values() and not self.site_time_zone:
            raise ConfigError(
                "Graph date columns need the SharePoint site_time_zone"
            )
        if self.site_time_zone:
            try:
                ZoneInfo(self.site_time_zone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigError(
                    f"Unknown Graph site_time_zone {self.site_time_zone!r}"
                ) from None
        if any(
            kinds.get(column) != "string" or not separator
            for column, separator in self.multi_value_separators.items()
        ):
            raise ConfigError(
                "Multi-value separators need a text column and a separator"
            )

    def read(
        self,
        excel_bytes: bytes,
        source_name: str,
        warnings: list[str] | None = None,
    ) -> dict[str, SheetRows]:
        """Validate the original export and apply its configured mappings.

        Args:
            excel_bytes: Original Graph JSON Lines bytes.
            source_name: Agreed export filename.
            warnings: Contract warnings, if supplied.

        Returns:
            One canonical worksheet, retaining original line numbers.

        Raises:
            ValidationError: The export structure or selected values
                are invalid, or an unrecognised internal field appears.
        """
        self._check_file_name(source_name)
        self._check_size(excel_bytes, source_name)
        try:
            names, rows = read_jsonl(
                excel_bytes,
                max_columns=self.max_columns,
                max_cells=self.max_cells,
            )
        except ValueError:
            raise ValidationError(
                f"{source_name}: invalid Graph JSON Lines export"
            ) from None
        worksheet = self.worksheets[0]
        columns = [column.name for column in worksheet.columns]
        selected = []
        allowed = set(self.field_map.values()) | set(self.excluded_fields)
        dates = [c.name for c in worksheet.columns if c.data_type == "date"]
        zone = ZoneInfo(self.site_time_zone) if dates else None
        seen_fields: set[str] = set()
        seen_metadata: set[str] = set()
        for number, values in rows:
            record = dict(zip(names, values, strict=True))
            if (
                not isinstance(record.get("id"), str)
                or not record["id"].strip()
            ):
                raise ValidationError(
                    f"{source_name}: line {number} needs a Graph item id"
                )
            fields = record.get("fields")
            if not isinstance(fields, dict):
                raise ValidationError(
                    f"{source_name}: line {number} needs a fields object"
                )
            unexpected = set(fields) - allowed
            required = {
                column.name
                for column in worksheet.columns
                if not column.nullable
            }
            missing = {
                internal
                for canonical, internal in self.field_map.items()
                if canonical in required and internal not in fields
            }
            missing_metadata = {
                name
                for canonical, name in self.metadata_map.items()
                if canonical in required and name not in record
            }
            if unexpected or missing or missing_metadata:
                raise ValidationError(
                    f"{source_name}: line {number}: "
                    f"{len(unexpected)} unrecognised field(s), "
                    f"{len(missing) + len(missing_metadata)} missing field(s)"
                )
            seen_fields.update(fields)
            seen_metadata.update(record)
            mapped = {
                **{
                    key: fields.get(name)
                    for key, name in self.field_map.items()
                },
                **{
                    key: record.get(name)
                    for key, name in self.metadata_map.items()
                },
            }
            if zone is not None:
                for key in dates:
                    mapped[key] = _site_time(mapped[key], zone)
            for key, separator in self.multi_value_separators.items():
                try:
                    mapped[key] = _joined(mapped[key], separator)
                except ValueError as error:
                    raise ValidationError(
                        f"{source_name}: line {number}: '{key}' {error}"
                    ) from None
            selected.append((number, tuple(mapped[key] for key in columns)))
        found = warnings if warnings is not None else []
        # A field SharePoint leaves out of an item reads as None, so one
        # dropped from the list would otherwise look like one nobody
        # filled in. Said whatever on_mismatch is.
        if selected:
            header = f"{source_name} [{worksheet.name}]"
            absent = [
                canonical
                for canonical, name in self.field_map.items()
                if name not in seen_fields
            ] + [
                canonical
                for canonical, name in self.metadata_map.items()
                if name not in seen_metadata
            ]
            found.extend(
                _absent_everywhere(header, canonical)
                for canonical in columns
                if canonical in absent
            )
        checked = self._checked(
            columns, selected, worksheet, source_name, found, True
        )
        return {worksheet.name: checked}
