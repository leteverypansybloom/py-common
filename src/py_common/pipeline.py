"""Core ingestion pipeline.

Orchestrates:
1. Discover files at source
2. Download and hash each file
3. Validate against contract
4. Store raw file in object store
5. Convert to Parquet, or to JSON rows for a raw table
6. Load into warehouse staging
7. Merge or insert into the destination table(s)
8. Record audit entry
"""

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Literal

from py_common.contract import Contract, SheetRows
from py_common.errors import (
    ConfigError,
    IndeterminateCommitError,
    ServiceError,
    ValidationError,
    VersionMismatch,
)
from py_common.logging import add_log_fields, context_fields, log_context
from py_common.model import AuditRecord, Outcome, Result, SourceItem, digest
from py_common.parquet import to_parquet
from py_common.ports import ObjectStore, Source, Warehouse
from py_common.raw import to_ndjson

logger = logging.getLogger("py_common.pipeline")


def _fields(**more: Any) -> dict[str, Any]:
    """`extra=` for a pipeline log call: the run and file fields.

    The same fields also reach adapter log lines, through the
    ContextFilter that configure_json_logging() installs; passing
    them here as well keeps them on the pipeline's own lines under
    any handler.

    Args:
        **more: Fields for this line only; None values are dropped.

    Returns:
        Field name -> value.
    """
    fields = context_fields()
    fields.update({k: v for k, v in more.items() if v is not None})
    return fields


def _library_version() -> str:
    """py-common's version, for the audit record.

    Imported when needed: py_common/__init__.py imports this module,
    so importing the package at the top would be circular.

    Returns:
        e.g. "0.2.0".
    """
    import py_common

    return py_common.__version__


@dataclass
class Pipeline:
    """Orchestrate file processing with cloud services.

    Attributes:
        source: Source to discover and download files.
        store: Object store for raw and processed files.
        warehouse: Warehouse for staging and final tables.
        contract: Workbook validation rules.
        output_format: What the warehouse is given. "parquet" (the
            default) is the primary worksheet as Parquet, for
            BigQueryWarehouse. "raw_json" is every configured
            worksheet as newline-delimited JSON rows, for
            RawJsonWarehouse.
    """

    source: Source
    store: ObjectStore
    warehouse: Warehouse
    contract: Contract
    output_format: Literal["parquet", "raw_json"] = "parquet"

    def __post_init__(self) -> None:
        """Reject a configuration that can't load correctly.

        Checked when the pipeline is built, so a mistake fails the
        deployment instead of failing (or silently mis-loading) every
        file at run time.

        Raises:
            ValueError: output_format is not "parquet" or "raw_json".
            ConfigError: The warehouse's merge keys differ from the
                contract's, or a raw warehouse's sheet-to-table
                mapping doesn't match the contract's worksheets.
        """
        if self.output_format not in ("parquet", "raw_json"):
            raise ValueError(
                f"output_format must be 'parquet' or 'raw_json', "
                f"got {self.output_format!r}"
            )
        if isinstance(self.contract, Contract):
            self._check_keys_match()
            self._check_tables_match()

    def _check_keys_match(self) -> None:
        """The typed warehouse merges on the contract's key columns.

        They are configured twice (Contract.key_columns and
        BigQueryWarehouse(key_columns=...)). A warehouse given none
        would append every upload again; one given other columns
        would merge on the wrong ones.

        Raises:
            ConfigError: The two lists differ.
        """
        keys = getattr(self.warehouse, "key_columns", None)
        if self.output_format != "parquet" or not isinstance(keys, list):
            return
        if list(keys) != list(self.contract.key_columns):
            raise ConfigError(
                f"Warehouse {self.warehouse.identity} merges on "
                f"key_columns {keys!r}, but the contract's key_columns "
                f"are {self.contract.key_columns!r}. Pass "
                "key_columns=contract.key_columns to the warehouse."
            )

    def _check_tables_match(self) -> None:
        """A raw warehouse maps exactly the contract's worksheets.

        A sheet with no table would fail every load; a table with no
        sheet would get an empty audit row on every load and never be
        created.

        Raises:
            ConfigError: The mapping has missing or extra sheets.
        """
        tables = getattr(self.warehouse, "tables", None)
        if self.output_format != "raw_json" or not isinstance(tables, Mapping):
            return
        sheets = [ws.name for ws in self.contract.worksheets]
        missing = [name for name in sheets if name not in tables]
        extra = [name for name in tables if name not in sheets]
        if missing or extra:
            problems = []
            if missing:
                problems.append(f"no table for sheet(s) {missing!r}")
            if extra:
                problems.append(f"table(s) for unknown sheet(s) {extra!r}")
            raise ConfigError(
                f"Warehouse {self.warehouse.identity}: "
                f"{'; '.join(problems)}. Map exactly the contract's "
                f"worksheets: {sheets!r}."
            )

    def run(self) -> list[Result]:
        """Process all current files at source.

        Iterates discovered files and processes each one. If any
        file is quarantined or fails, processing continues.

        Returns:
            One Result per discovered file.

        Raises:
            RuntimeError: Another process holds the warehouse lock.
            ServiceError: Source listing failed.
        """
        results: list[Result] = []
        lock_key = f"locks/{digest(self.warehouse.identity.encode())}"
        run_id = uuid.uuid4().hex

        # Everything logged during the run, by the pipeline or an
        # adapter, carries run_id and warehouse (py_common.logging).
        with log_context(run_id=run_id, warehouse=self.warehouse.identity):
            logger.info("Run %s started", run_id, extra=_fields())
            try:
                with self.store.lock(lock_key):
                    for item in self.source.items():
                        with log_context(
                            item_identity=item.identity, version=item.version
                        ):
                            result = self._audit_or_fail(self.process(item))
                            self._log_summary(result)
                        results.append(result)
            except Exception as e:
                logger.exception("Pipeline run failed: %s", e, extra=_fields())
                raise

        return results

    @staticmethod
    def _log_summary(result: Result) -> None:
        """Log the one summary line for a file.

        Args:
            result: The file's final result.
        """
        # Alerts match on the outcome field (docs/GCP_SETUP.md,
        # section 7), so a failure is also an ERROR whatever path led
        # to it, and a quarantine a WARNING.
        level = {
            Outcome.FAILED: logging.ERROR,
            Outcome.QUARANTINED: logging.WARNING,
        }.get(result.outcome, logging.INFO)
        logger.log(
            level,
            "%s %s: %d rows in %.2fs",
            result.item.name,
            result.outcome.value,
            result.rows_processed,
            result.processing_time_seconds,
            extra=_fields(
                checksum=result.checksum or None,
                outcome=result.outcome.value,
                rows=result.rows_processed,
                seconds=round(result.processing_time_seconds, 3),
            ),
        )

    def process(self, item: SourceItem) -> Result:
        """Process a single file; caller must hold warehouse lock.

        Args:
            item: SourceItem from source discovery.

        Returns:
            Result with outcome (loaded, skipped, quarantined, failed).
        """
        with log_context(item_identity=item.identity, version=item.version):
            return self._process(item)

    def _process(self, item: SourceItem) -> Result:
        """Body of process(), inside the file's log context.

        Args:
            item: SourceItem from source discovery.

        Returns:
            As process().
        """
        start_time = perf_counter()
        errors: list[str] = []
        warnings: list[str] = []
        outcome = Outcome.LOADED
        checksum = ""
        rows_processed = 0
        warehouse_key = ""
        processed_key = ""

        try:
            # The marker is keyed by identity and version, so an upload
            # already loaded here is skipped before it is downloaded.
            if self._is_duplicate(item):
                return self._make_result(
                    item, Outcome.SKIPPED, checksum, 0, errors, start_time
                )

            data = self._download(item)
            if data is None:
                return self._make_result(
                    item, Outcome.SKIPPED, checksum, 0, errors, start_time
                )

            checksum = digest(data)
            add_log_fields(checksum=checksum)

            # One parse validates and reads every worksheet; the
            # conversion below works from these rows.
            try:
                sheets = self.contract.read(data, item.name, warnings=warnings)
            except ValidationError as e:
                errors = self._quarantine_invalid(item, data, checksum, e)
                return self._make_result(
                    item,
                    Outcome.QUARANTINED,
                    checksum,
                    0,
                    errors,
                    start_time,
                )

            self._store_raw(item, data, checksum)
            try:
                processed, rows_processed = self._convert(
                    item, sheets, checksum, warnings
                )
            except ValidationError as e:
                errors = self._quarantine_invalid(item, data, checksum, e)
                return self._make_result(
                    item, Outcome.QUARANTINED, checksum, 0, errors, start_time
                )

            # Logged at WARNING so an alert can be raised on them: in
            # "warn" mode the file loads, so nothing else would show
            # that the layout or data has changed.
            for warning in warnings:
                logger.warning(
                    "Contract warning: %s", warning, extra=_fields()
                )
            processed_key = self._store_processed(item, checksum, processed)

            warehouse_key = f"{item.identity}_{checksum[:8]}"
            outcome = self._load_to_warehouse(
                item,
                data,
                warehouse_key,
                checksum,
                processed,
                rows_processed,
                errors,
            )

            if outcome == Outcome.LOADED:
                logger.info(
                    "Loaded %s (%d rows, %s)",
                    item.name,
                    rows_processed,
                    checksum[:8],
                    extra=_fields(),
                )
            if outcome in (Outcome.LOADED, Outcome.SKIPPED):
                # The warehouse has the file either way: record that
                # in the marker cache before anything else can fail.
                self.store.put(self._loaded_marker_key(item), b"processed")
            if outcome == Outcome.SKIPPED:
                rows_processed = 0

        except Exception as e:
            if outcome != Outcome.QUARANTINED:
                outcome = Outcome.FAILED
            if str(e) not in errors:
                errors.append(str(e))
            logger.exception(
                "Processing error for %s: %s", item.name, e, extra=_fields()
            )

        return self._make_result(
            item,
            outcome,
            checksum,
            rows_processed,
            errors,
            start_time,
            warehouse_key,
            warnings,
            processed_key,
        )

    def _download(self, item: SourceItem) -> bytes | None:
        """Download source bytes, or None if the version moved on.

        Args:
            item: SourceItem from source discovery.

        Returns:
            File bytes, or None if the source version changed since
            discovery (caller should treat this as a skip).
        """
        try:
            return self.source.download(item)
        except VersionMismatch:
            logger.info(
                "Skipping %s: version changed during download",
                item.name,
                extra=_fields(),
            )
            return None

    def _is_duplicate(self, item: SourceItem) -> bool:
        """Check the marker cache for this upload in this warehouse.

        Only a cache: a miss goes on to the warehouse, whose load()
        checks its own audit table inside the commit transaction and
        is the authority. A store read error raises (fail-closed)
        rather than reading as a miss.

        Args:
            item: SourceItem being processed.

        Returns:
            True if the marker says this file was already loaded
            into this pipeline's warehouse.

        Raises:
            ServiceError: The marker could not be read.
        """
        key = self._loaded_marker_key(item)
        if self.store.get(key):
            logger.info(
                "Skipping %s: already loaded (marker %s)",
                item.name,
                key,
                extra=_fields(),
            )
            return True
        return False

    def _loaded_marker_key(self, item: SourceItem) -> str:
        """Key of the marker saying this upload was loaded here.

        Scoped to the destination (warehouse identity): one workbook
        can feed several pipelines, one per worksheet or table, and
        the first to load it must not make the others skip it.

        Within a destination it follows the warehouses' own dedup
        rule: the upload (source identity and version), not its
        bytes. Every new upload loads, even one that repeats earlier
        content: a restore must win again on the typed route, and be
        the latest generation on the raw route.

        Args:
            item: SourceItem being processed.

        Returns:
            "audit/{warehouse}/versions/{sha256(identity)}/{version}".
        """
        identity = digest(item.identity.encode())
        return (
            f"audit/{self.warehouse.identity}/versions/"
            f"{identity}/{item.version}"
        )

    @staticmethod
    def _version_key(item: SourceItem, checksum: str) -> str:
        """Object key suffix for one content version of one item.

        Keyed by identity and checksum so a file edited in place
        (same identity, new bytes) is stored beside earlier versions
        rather than colliding with them, and every version stays
        recoverable. The source filename comes last so stored copies
        stay recognisable when the identity is an opaque ID (e.g. a
        SharePoint item ID).

        Args:
            item: SourceItem being processed.
            checksum: SHA-256 of the downloaded bytes.

        Returns:
            "{identity}/{checksum}/{name}".
        """
        return f"{item.identity}/{checksum}/{item.name}"

    def _quarantine_invalid(
        self,
        item: SourceItem,
        data: bytes,
        checksum: str,
        error: ValidationError,
    ) -> list[str]:
        """Store raw + quarantine copies after a validation failure.

        Args:
            item: SourceItem that failed validation.
            data: Raw downloaded bytes to preserve for triage.
            checksum: SHA-256 of data.
            error: The validation failure.

        Returns:
            Single-element errors list describing the failure.
        """
        logger.warning(
            "Validation failed for %s: %s", item.name, error, extra=_fields()
        )
        key = self._version_key(item, checksum)
        self.store.put(f"raw/{key}", data)
        self.store.put(f"quarantine/{key}", data)
        return [str(error)]

    def _store_raw(self, item: SourceItem, data: bytes, checksum: str) -> None:
        """Store the raw downloaded bytes.

        Args:
            item: SourceItem being processed.
            data: Raw file bytes.
            checksum: SHA-256 of data.
        """
        self.store.put(f"raw/{self._version_key(item, checksum)}", data)

    def _convert(
        self,
        item: SourceItem,
        sheets: dict[str, SheetRows],
        checksum: str,
        warnings: list[str] | None = None,
    ) -> tuple[bytes, int]:
        """Convert the workbook's rows into what the warehouse loads.

        Args:
            item: SourceItem being processed.
            sheets: Rows per worksheet, from Contract.read().
            checksum: SHA-256 of the source bytes.
            warnings: Where Parquet conversion reports values loaded
                as NULL (mutated).

        Returns:
            (bytes to load, rows the warehouse should find in them).
            For Parquet that is the primary worksheet's rows; for raw
            JSON it is the rows of every configured worksheet.
        """
        if self.output_format == "raw_json":
            return to_ndjson(sheets, item, checksum)
        primary = self.contract.worksheets[0]
        parquet = self._sheets_to_parquet(sheets, item.name, warnings)
        return parquet, len(sheets[primary.name].rows)

    def _to_parquet(
        self,
        data: bytes,
        source_name: str = "workbook",
        warnings: list[str] | None = None,
    ) -> bytes:
        """Convert the primary worksheet to Parquet bytes.

        Looks up the primary worksheet by the name the contract
        validated, not wb.active: the workbook's active sheet has no
        guaranteed relation to which sheet the contract expects. Only
        the columns the contract selects are written.

        The output is a pure function of the input bytes (no load
        timestamp), so a retry after a failed warehouse load writes
        identical bytes to the same processed/ key. The warehouse
        stamps _loaded_at when it commits.

        Column types come from the contract (py_common.parquet), not
        from the values, so mixed values in a "warn" sheet load (bad
        ones as NULL, with a warning) and an empty column keeps its
        type.

        Args:
            data: Raw Excel bytes.
            source_name: File name, for warnings.
            warnings: Where values loaded as NULL are reported
                (mutated).

        Returns:
            Parquet-encoded bytes of the primary worksheet.
        """
        sheets = self.contract.extract(data, source_name)
        return self._sheets_to_parquet(sheets, source_name, warnings)

    def _sheets_to_parquet(
        self,
        sheets: dict[str, SheetRows],
        source_name: str = "workbook",
        warnings: list[str] | None = None,
    ) -> bytes:
        """Write the primary worksheet's rows as typed Parquet.

        Args:
            sheets: Rows per worksheet, from Contract.read().
            source_name: File name, for warnings.
            warnings: Where values loaded as NULL are reported
                (mutated).

        Returns:
            Parquet-encoded bytes of the primary worksheet.
        """
        primary = self.contract.worksheets[0]
        return to_parquet(
            sheets[primary.name],
            primary,
            source_name,
            warnings,
            key_columns=self.contract.key_columns,
        )

    def _store_processed(
        self, item: SourceItem, checksum: str, processed: bytes
    ) -> str:
        """Store the converted bytes (Parquet or NDJSON).

        The store is write-once, so the key must change whenever the
        bytes do. It is "processed/{warehouse}/{identity}/{checksum}/
        {version}/{digest}/{name}.parquet" (or ".ndjson"):

        - the warehouse: pipelines reading different sheets of one
          workbook write different bytes;
        - the version: each upload has its own copy, even when its
          bytes repeat an earlier upload's (a restore);
        - the digest, the first 16 hex digits of the converted bytes'
          SHA-256: converting the same upload again with a different
          result (a contract fix, a library upgrade) writes a new
          copy instead of failing with ObjectStoreConflict on every
          retry, while a plain retry reuses its copy.

        Args:
            item: SourceItem being processed.
            checksum: SHA-256 of the source bytes.
            processed: Converted bytes to store.

        Returns:
            The key written.
        """
        extension = "ndjson" if self.output_format == "raw_json" else "parquet"
        key = (
            f"processed/{self.warehouse.identity}/{item.identity}/"
            f"{checksum}/{item.version}/{digest(processed)[:16]}/"
            f"{item.name}.{extension}"
        )
        self.store.put(key, processed)
        return key

    def _load_to_warehouse(
        self,
        item: SourceItem,
        data: bytes,
        warehouse_key: str,
        checksum: str,
        processed: bytes,
        rows_processed: int,
        errors: list[str],
    ) -> Outcome:
        """Load the converted bytes into the warehouse.

        Args:
            item: SourceItem being processed (for logging).
            data: Raw source bytes, kept in quarantine/ if the
                warehouse rejects the file as invalid.
            warehouse_key: Unique identifier for this load.
            checksum: SHA-256 of the source bytes.
            processed: Converted Parquet or NDJSON data to load.
            rows_processed: Expected row count for verification.
            errors: Errors list to append to on failure (mutated).

        Returns:
            Outcome.LOADED on success. Outcome.SKIPPED if the
            warehouse found it already loaded (a retry after a lost
            commit confirmation). Outcome.FAILED if the warehouse
            service failed (nothing committed; a retry is safe), so
            the runner retries it. Outcome.QUARANTINED if the
            warehouse found the file itself invalid.

        Raises:
            IndeterminateCommitError: Commit state could not be
                confirmed; caller must treat this as a failure.
        """
        try:
            inserted = self.warehouse.load(
                key=warehouse_key,
                checksum=checksum,
                data=processed,
                rows=rows_processed,
                item=item,
            )
            if not inserted:
                logger.info(
                    "Skipping %s: the warehouse audit shows it already "
                    "loaded",
                    item.name,
                    extra=_fields(),
                )
                return Outcome.SKIPPED
            return Outcome.LOADED
        except IndeterminateCommitError:
            logger.error(
                "Cannot confirm warehouse commit for %s",
                item.name,
                extra=_fields(),
            )
            errors.append("Warehouse commit indeterminate")
            raise
        except ServiceError as e:
            # A service problem, not a file problem: FAILED is
            # retried (Pub/Sub redelivery, next job run), whereas
            # QUARANTINED would be acknowledged and raise the
            # bad-file alert.
            logger.error("Warehouse service error: %s", e, extra=_fields())
            errors.append(f"Warehouse error: {e}")
            return Outcome.FAILED
        except ValidationError as e:
            errors.extend(self._quarantine_invalid(item, data, checksum, e))
            return Outcome.QUARANTINED

    def _make_result(
        self,
        item: SourceItem,
        outcome: Outcome,
        checksum: str,
        rows_processed: int,
        errors: list[str],
        start_time: float,
        warehouse_key: str = "",
        warnings: list[str] | None = None,
        processed_key: str = "",
    ) -> Result:
        """Assemble a Result with elapsed processing time.

        Args:
            item: SourceItem processed.
            outcome: Final outcome.
            checksum: SHA-256 of source bytes, if computed.
            rows_processed: Rows validated and loaded.
            errors: Errors encountered, if any.
            start_time: perf_counter() value at start of process().
            warehouse_key: Reference in final table, if loaded.
            warnings: Problems tolerated in "warn" mode, if any.
            processed_key: Key of the stored converted copy, if any.

        Returns:
            Populated Result.
        """
        return Result(
            item=item,
            outcome=outcome,
            checksum=checksum,
            rows_processed=rows_processed,
            errors=errors,
            processing_time_seconds=perf_counter() - start_time,
            warehouse_key=warehouse_key,
            warnings=list(warnings or []),
            processed_key=processed_key,
        )

    def _audit_or_fail(self, result: Result) -> Result:
        """Write the audit record; a failure fails this file only.

        The loaded marker was already written by process(), so even
        when this write fails a retry finds the file loaded.

        Args:
            result: Processing result to record.

        Returns:
            `result`, or a FAILED copy of it if the record could not
            be written.
        """
        try:
            self._audit(result)
            return result
        except Exception as e:
            logger.exception(
                "Audit write failed for %s: %s",
                result.item.name,
                e,
                extra=_fields(),
            )
            return replace(
                result,
                outcome=Outcome.FAILED,
                errors=[*result.errors, f"Audit write failed: {e}"],
            )

    def _audit(self, result: Result) -> None:
        """Write audit record to object store.

        Args:
            result: Processing result to record.
        """
        record = AuditRecord(
            timestamp=datetime.now(timezone.utc),
            item_identity=result.item.identity,
            filename=result.item.name,
            version=result.item.version,
            checksum=result.checksum,
            rows_processed=result.rows_processed,
            outcome=result.outcome,
            errors=result.errors,
            processing_time_seconds=result.processing_time_seconds,
            warehouse_key=result.warehouse_key,
            warnings=result.warnings,
            warehouse=self.warehouse.identity,
            processed_key=result.processed_key,
            contract_fingerprint=(
                self.contract.fingerprint()
                if isinstance(self.contract, Contract)
                else ""
            ),
            py_common_version=_library_version(),
        )

        audit_json = json.dumps(
            asdict(record),
            default=str,
            sort_keys=True,
        )
        # isoformat()'s colons are invalid in Windows filenames, so
        # replace them (2026-09-22T14-30-45.123456+00-00, not
        # 14:30:45.123456+00:00) before using the timestamp as a key.
        timestamp_key = record.timestamp.isoformat().replace(":", "-")
        # The time orders the records; the random part keeps two from
        # one clock tick apart (Python before 3.13 reads the Windows
        # clock about every 15 ms), which the write-once store would
        # otherwise refuse.
        audit_key = (
            f"audit/records/{timestamp_key}_{uuid.uuid4().hex[:8]}_"
            f"{result.item.identity}.json"
        )
        self.store.put(audit_key, audit_json.encode())
