"""Custom exceptions for ingestion pipeline.

Hierarchy:
- IngestionError: Base exception
  ├─ SourceError: File discovery or download failed
  │  ├─ VersionMismatch: Source changed between discovery and download
  │  └─ NotFound: Source item disappeared
  ├─ ValidationError: Workbook structure or data invalid
  ├─ ContractError: Workbook doesn't match agreed schema
  ├─ ServiceError: Cloud service unavailable or permission denied
  └─ IndeterminateCommitError: Don't know if warehouse committed
"""


class IngestionError(Exception):
    """Base exception for ingestion pipeline."""

    pass


class SourceError(IngestionError):
    """Error reading from source."""

    pass


class VersionMismatch(SourceError):
    """File version changed between discovery and download.

    The pipeline discovered a file at one version but source now
    shows a different version. This prevents processing a
    partially-written file.
    """

    pass


class NotFound(SourceError):
    """Source item no longer exists."""

    pass


class ValidationError(IngestionError):
    """Workbook failed validation against contract rules."""

    pass


class ContractError(ValidationError):
    """Workbook structure doesn't match agreed contract.

    Examples:
    - Missing required worksheet
    - Missing or extra columns
    - Invalid data type for a column
    - Required value is null
    """

    pass


class ServiceError(IngestionError):
    """Cloud service failed (permission denied, unavailable, quota).

    Caller should determine if retry is safe.
    """

    pass


class IndeterminateCommitError(IngestionError):
    """Don't know if warehouse committed data.

    Raised when connection to warehouse fails during commit.
    Caller must check warehouse state before retrying.
    """

    pass
