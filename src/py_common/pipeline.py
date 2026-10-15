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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal

from py_common.contract import Contract
from py_common.errors import (
    IndeterminateCommitError,
    ServiceError,
    ValidationError,
    VersionMismatch,
)
from py_common.model import AuditRecord, Outcome, Result, SourceItem, digest
from py_common.parquet import to_parquet
from py_common.ports import ObjectStore, Source, Warehouse
from py_common.raw import to_ndjson

logger = logging.getLogger("py_common.pipeline")


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
        """Reject an unknown output_format when the pipeline is built.

        Raises:
            ValueError: output_format is not "parquet" or "raw_json".
        """
        if self.output_format not in ("parquet", "raw_json"):
            raise ValueError(
                f"output_format must be 'parquet' or 'raw_json', "
                f"got {self.output_format!r}"
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

        try:
            with self.store.lock(lock_key):
                for item in self.source.items():
                    result = self.process(item)
                    results.append(self._audit_or_fail(result))
        except Exception as e:
            logger.exception("Pipeline run failed: %s", e)
            raise

        return results

    def process(self, item: SourceItem) -> Result:
        """Process a single file; caller must hold warehouse lock.

        Args:
            item: SourceItem from source discovery.

        Returns:
            Result with outcome (loaded, skipped, quarantined, failed).
        """
        start_time = perf_counter()
        errors: list[str] = []
        warnings: list[str] = []
        outcome = Outcome.LOADED
        checksum = ""
        rows_processed = 0
        warehouse_key = ""

        try:
            data = self._download(item)
            if data is None:
                return self._make_result(
                    item, Outcome.SKIPPED, checksum, 0, errors, start_time
                )

            checksum = digest(data)
            if self._is_duplicate(item, checksum):
                return self._make_result(
                    item, Outcome.SKIPPED, checksum, 0, errors, start_time
                )

            try:
                rows_processed = self.contract.validate(
                    data, item.name, warnings=warnings
                )
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
            processed, rows_processed = self._convert(
                item, data, checksum, rows_processed, warnings
            )

            # Logged at WARNING so an alert can be raised on them: in
            # "warn" mode the file loads, so nothing else would show
            # that the layout or data has changed.
            for warning in warnings:
                logger.warning("Contract warning: %s", warning)
            self._store_processed(item, checksum, processed)

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
                )
            if outcome in (Outcome.LOADED, Outcome.SKIPPED):
                # The warehouse has the file either way: record that
                # in the marker cache before anything else can fail.
                self.store.put(
                    self._loaded_marker_key(item, checksum), b"processed"
                )
            if outcome == Outcome.SKIPPED:
                rows_processed = 0

        except Exception as e:
            if outcome != Outcome.QUARANTINED:
                outcome = Outcome.FAILED
            if str(e) not in errors:
                errors.append(str(e))
            logger.exception("Processing error for %s: %s", item.name, e)

        return self._make_result(
            item,
            outcome,
            checksum,
            rows_processed,
            errors,
            start_time,
            warehouse_key,
            warnings,
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
                "Skipping %s: version changed during download", item.name
            )
            return None

    def _is_duplicate(self, item: SourceItem, checksum: str) -> bool:
        """Check the marker cache for this file in this warehouse.

        Only a cache: a miss goes on to the warehouse, whose load()
        checks its own audit table inside the commit transaction and
        is the authority. A store read error raises (fail-closed)
        rather than reading as a miss.

        Args:
            item: SourceItem being processed.
            checksum: SHA-256 of the downloaded bytes.

        Returns:
            True if the marker says this file was already loaded
            into this pipeline's warehouse.

        Raises:
            ServiceError: The marker could not be read.
        """
        keys = [self._loaded_marker_key(item, checksum)]
        if self.output_format == "raw_json":
            # Raw-route markers written before identity + version
            # keys (0.1.x) are keyed by checksum, and the audit rows
            # from then have no destination, so only this marker
            # stops those files being inserted again after an
            # upgrade. See docs/RAW_TABLE_LAYOUT.md.
            keys.append(self._content_marker_key(checksum))
        for key in keys:
            if self.store.get(key):
                logger.info(
                    "Skipping %s: already loaded (marker %s)",
                    item.name,
                    key,
                )
                return True
        return False

    def _loaded_marker_key(self, item: SourceItem, checksum: str) -> str:
        """Key of the marker saying this file was loaded here.

        Scoped to the destination (warehouse identity): one workbook
        can feed several pipelines, one per worksheet or table, and
        the first to load it must not make the others skip it.

        Within a destination it follows the warehouse's own dedup
        rule. Typed (Parquet) route: the content checksum, as a
        MERGE of identical rows changes nothing. Raw JSON route: the
        source identity and version, because an insert-only table
        must take every new generation, even one that repeats
        earlier content (a revert), or "latest generation" goes
        stale.

        Args:
            item: SourceItem being processed.
            checksum: SHA-256 of the source bytes.

        Returns:
            "audit/{warehouse}/{checksum}" (typed) or
            "audit/{warehouse}/versions/{sha256(identity)}/{version}"
            (raw JSON).
        """
        if self.output_format == "raw_json":
            identity = digest(item.identity.encode())
            return (
                f"audit/{self.warehouse.identity}/versions/"
                f"{identity}/{item.version}"
            )
        return self._content_marker_key(checksum)

    def _content_marker_key(self, checksum: str) -> str:
        """Checksum-keyed marker: the typed route's, and 0.1.x's.

        Args:
            checksum: SHA-256 of the source bytes.

        Returns:
            "audit/{warehouse identity}/{checksum}".
        """
        return f"audit/{self.warehouse.identity}/{checksum}"

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
        logger.warning("Validation failed for %s: %s", item.name, error)
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
        data: bytes,
        checksum: str,
        validated_rows: int,
        warnings: list[str] | None = None,
    ) -> tuple[bytes, int]:
        """Convert the workbook into what the warehouse loads.

        Args:
            item: SourceItem being processed.
            data: Raw Excel bytes.
            checksum: SHA-256 of data.
            validated_rows: Row count validate() returned for the
                primary worksheet.
            warnings: Where Parquet conversion reports values loaded
                as NULL (mutated).

        Returns:
            (bytes to load, rows the warehouse should find in them).
            For Parquet that is the primary worksheet's rows; for raw
            JSON it is the rows of every configured worksheet.
        """
        if self.output_format == "raw_json":
            return to_ndjson(self.contract.extract(data), item, checksum)
        return self._to_parquet(data, item.name, warnings), validated_rows

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
        primary = self.contract.worksheets[0]
        sheets = self.contract.extract(data)
        return to_parquet(sheets[primary.name], primary, source_name, warnings)

    def _store_processed(
        self, item: SourceItem, checksum: str, processed: bytes
    ) -> None:
        """Store the converted bytes (Parquet or NDJSON).

        Keyed by destination as well as content: pipelines reading
        different sheets of the same workbook produce different
        bytes, which would otherwise collide under one key. Raw JSON
        copies are keyed by source version too:
        "processed/{warehouse}/{identity}/{checksum}/{version}/
        {name}.ndjson".

        Args:
            item: SourceItem being processed.
            checksum: SHA-256 of the source bytes.
            processed: Converted bytes to store.
        """
        version_key = self._version_key(item, checksum)
        if self.output_format == "raw_json":
            # NDJSON embeds source_generation, so one content under
            # two generations (a revert) is two different files.
            identity, _, name = version_key.rpartition("/")
            key = (
                f"processed/{self.warehouse.identity}/{identity}/"
                f"{item.version}/{name}.ndjson"
            )
        else:
            key = f"processed/{self.warehouse.identity}/{version_key}.parquet"
        self.store.put(key, processed)

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
                )
                return Outcome.SKIPPED
            return Outcome.LOADED
        except IndeterminateCommitError:
            logger.error("Cannot confirm warehouse commit for %s", item.name)
            errors.append("Warehouse commit indeterminate")
            raise
        except ServiceError as e:
            # A service problem, not a file problem: FAILED is
            # retried (Pub/Sub redelivery, next job run), whereas
            # QUARANTINED would be acknowledged and raise the
            # bad-file alert.
            logger.error("Warehouse service error: %s", e)
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
                "Audit write failed for %s: %s", result.item.name, e
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
        audit_key = (
            f"audit/records/{timestamp_key}_{result.item.identity}.json"
        )
        self.store.put(audit_key, audit_json.encode())
