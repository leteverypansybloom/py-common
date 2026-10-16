"""Custom exceptions for ingestion pipeline.

Hierarchy:
- IngestionError: Base exception
  ├─ SourceError: File discovery or download failed
  │  ├─ VersionMismatch: Source changed between discovery and download
  │  └─ NotFound: Source item disappeared
  ├─ ValidationError: Workbook structure or data invalid
  │  └─ ContractError: Workbook doesn't match agreed schema
  ├─ ServiceError: Cloud service unavailable or permission denied
  ├─ ObjectStoreConflict: Key exists with different content
  ├─ IndeterminateCommitError: Don't know if warehouse committed
  ├─ RunFailed: A pipeline run finished with FAILED files
  └─ ConfigError: Configuration file is missing or malformed

ContractError is a ValidationError, so callers that catch
ValidationError (e.g. Pipeline.process(), which quarantines it)
catch ContractError too. This is deliberate: the README says schema
mismatches should always quarantine, same as data-validation
failures.
"""

from py_common.model import Result


class IngestionError(Exception):
    """Base exception for ingestion pipeline."""


class SourceError(IngestionError):
    """Error reading from source."""


class VersionMismatch(SourceError):
    """File version changed between discovery and download.

    The pipeline discovered a file at one version but source now
    shows a different version. This prevents processing a
    partially-written file.
    """


class NotFound(SourceError):
    """Source item no longer exists."""


class ValidationError(IngestionError):
    """Workbook failed validation against contract rules."""


class ContractError(ValidationError):
    """Workbook structure doesn't match agreed contract.

    Examples:
    - Missing required worksheet
    - Missing or extra columns
    - Invalid data type for a column
    - Required value is null
    """


class ServiceError(IngestionError):
    """Cloud service failed (permission denied, unavailable, quota).

    Caller should determine if retry is safe.
    """


class ObjectStoreConflict(IngestionError):
    """Object store key already exists with different content.

    ObjectStore.put() is idempotent for identical bytes, but two
    different payloads under the same key would silently corrupt
    the immutable store if allowed.
    """


class IndeterminateCommitError(IngestionError):
    """Don't know if warehouse committed data.

    Raised when connection to warehouse fails during commit.
    Caller must check warehouse state before retrying.
    """


class ConfigError(IngestionError):
    """Configuration file is missing or malformed."""


class RunFailed(IngestionError):
    """A pipeline run finished, but one or more files FAILED.

    Pipeline.run() records a failed file and moves on to the next, so
    the run itself returns normally. py_common.runner raises this
    afterwards, so that a Cloud Run job exits non-zero and an upload
    event is delivered again.

    Attributes:
        results: The FAILED results, in run order.
    """

    def __init__(self, results: list[Result]) -> None:
        """Name the failed files in the message.

        Args:
            results: The FAILED results, in run order.
        """
        names = ", ".join(result.item.name for result in results)
        super().__init__(f"{len(results)} file(s) failed: {names}")
        self.results = results
