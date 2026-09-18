"""Data models for ingestion pipeline.

SourceItem: File discovered at a source (SharePoint, local folder).
Result: Outcome of processing one file (success, skipped, failed).
Audit: Record of an attempt for compliance and debugging.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256


class Outcome(str, Enum):
    """Result of processing a single file."""

    LOADED = "loaded"
    SKIPPED = "skipped"
    QUARANTINED = "quarantined"
    FAILED = "failed"


@dataclass
class SourceItem:
    """File discovered at source.

    Attributes:
        name: Filename visible at source (e.g., "events.xlsx").
        identity: Unique identifier at source (e.g., SharePoint item ID).
        version: Source's version marker (e.g., eTag, modification time).
        size_bytes: File size at discovery time.
    """

    name: str
    identity: str
    version: str
    size_bytes: int


@dataclass
class Result:
    """Outcome of processing one file.

    Attributes:
        item: The source file processed.
        outcome: Whether loaded, skipped, quarantined or failed.
        checksum: SHA-256 of downloaded bytes for duplicate detection.
        rows_processed: Count of rows validated and loaded.
        errors: Validation or service failures, if any.
        processing_time_seconds: Wall-clock duration.
        warehouse_key: Reference in final table (empty if skipped).
    """

    item: SourceItem
    outcome: Outcome
    checksum: str
    rows_processed: int
    errors: list[str]
    processing_time_seconds: float
    warehouse_key: str = ""


def digest(data: bytes) -> str:
    """Return SHA-256 hex digest of data.

    Args:
        data: Bytes to hash.

    Returns:
        Lowercase hex string.
    """
    return sha256(data).hexdigest()


@dataclass
class AuditRecord:
    """Immutable record of one processing attempt.

    Stored in warehouse audit table or audit log file.
    """

    timestamp: datetime
    item_identity: str
    filename: str
    version: str
    checksum: str
    rows_processed: int
    outcome: Outcome
    errors: list[str]
    processing_time_seconds: float
    warehouse_key: str
