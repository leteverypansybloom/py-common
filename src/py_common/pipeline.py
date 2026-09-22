"""Core ingestion pipeline.

Orchestrates:
1. Discover files at source
2. Download and hash each file
3. Validate against contract
4. Store raw file in object store
5. Convert to Parquet
6. Load into warehouse staging
7. Merge into final table
8. Record audit entry
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from time import perf_counter

import pandas as pd
from openpyxl import load_workbook

from py_common.contract import Contract
from py_common.errors import (
    IndeterminateCommitError,
    ServiceError,
    ValidationError,
    VersionMismatch,
)
from py_common.model import AuditRecord, Outcome, Result, SourceItem, digest
from py_common.ports import ObjectStore, Source, Warehouse

logger = logging.getLogger("py_common.pipeline")


@dataclass
class Pipeline:
    """Orchestrate file processing with cloud services.

    Attributes:
        source: Source to discover and download files.
        store: Object store for raw and processed files.
        warehouse: Warehouse for staging and final tables.
        contract: Workbook validation rules.
    """

    source: Source
    store: ObjectStore
    warehouse: Warehouse
    contract: Contract

    def run(self) -> list[Result]:
        """Process all current files at source.

        Iterates discovered files and processes each one. If any
        file is quarantined or fails, processing continues.

        Returns:
            One Result per discovered file.

        Raises:
            RuntimeError: Another process holds the warehouse lock.
            ServiceError: Source listing or audit store failed.
        """
        results: list[Result] = []
        lock_key = f"locks/{digest(self.warehouse.identity.encode())}"

        try:
            with self.store.lock(lock_key):
                for item in self.source.items():
                    result = self.process(item)
                    results.append(result)
                    self._audit(result)
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
                rows_processed = self.contract.validate(data, item.name)
            except ValidationError as e:
                errors = self._quarantine_invalid(item, data, e)
                return self._make_result(
                    item,
                    Outcome.QUARANTINED,
                    checksum,
                    0,
                    errors,
                    start_time,
                )

            self._store_raw(item, data)
            parquet_bytes = self._to_parquet(data)
            self._store_processed(item, parquet_bytes)

            warehouse_key = f"{item.identity}_{checksum[:8]}"
            outcome = self._load_to_warehouse(
                item,
                warehouse_key,
                checksum,
                parquet_bytes,
                rows_processed,
                errors,
            )

            logger.info(
                "Loaded %s (%d rows, %s)",
                item.name,
                rows_processed,
                checksum[:8],
            )

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
        """Check whether this exact content was already processed.

        Args:
            item: SourceItem being processed (for logging).
            checksum: SHA-256 of the downloaded bytes.

        Returns:
            True if an audit record already exists for this checksum.
        """
        existing = self.store.get(f"audit/{checksum}")
        if existing:
            logger.info(
                "Skipping %s: checksum %s already processed",
                item.name,
                checksum[:8],
            )
            return True
        return False

    def _quarantine_invalid(
        self, item: SourceItem, data: bytes, error: ValidationError
    ) -> list[str]:
        """Store raw + quarantine copies after a validation failure.

        Args:
            item: SourceItem that failed validation.
            data: Raw downloaded bytes to preserve for triage.
            error: The validation failure.

        Returns:
            Single-element errors list describing the failure.
        """
        logger.warning("Validation failed for %s: %s", item.name, error)
        self.store.put(f"raw/{item.identity}", data)
        self.store.put(f"quarantine/{item.identity}", data)
        return [str(error)]

    def _store_raw(self, item: SourceItem, data: bytes) -> None:
        """Store the raw downloaded bytes.

        Args:
            item: SourceItem being processed.
            data: Raw file bytes.
        """
        self.store.put(f"raw/{item.identity}", data)

    def _to_parquet(self, data: bytes) -> bytes:
        """Convert the primary worksheet to Parquet bytes.

        Looks up the primary worksheet by the name the contract
        validated, not wb.active: the workbook's active sheet has no
        guaranteed relation to which sheet the contract expects.

        Args:
            data: Raw Excel bytes.

        Returns:
            Parquet-encoded bytes of the primary worksheet.
        """
        wb = load_workbook(BytesIO(data), data_only=True)
        primary_sheet_name = self.contract.worksheets[0].name
        ws = wb[primary_sheet_name]
        headers = [cell.value for cell in ws[1]]
        rows = [
            dict(zip(headers, row))
            for row in ws.iter_rows(min_row=2, values_only=True)
        ]

        df = pd.DataFrame(rows)
        parquet_buffer = BytesIO()
        df.to_parquet(parquet_buffer, index=False)
        return parquet_buffer.getvalue()

    def _store_processed(self, item: SourceItem, parquet_bytes: bytes) -> None:
        """Store the converted Parquet bytes.

        Args:
            item: SourceItem being processed.
            parquet_bytes: Parquet-encoded data to store.
        """
        self.store.put(f"processed/{item.identity}.parquet", parquet_bytes)

    def _load_to_warehouse(
        self,
        item: SourceItem,
        warehouse_key: str,
        checksum: str,
        parquet_bytes: bytes,
        rows_processed: int,
        errors: list[str],
    ) -> Outcome:
        """Load Parquet bytes into the warehouse.

        Args:
            item: SourceItem being processed (for logging).
            warehouse_key: Unique identifier for this load.
            checksum: SHA-256 of the source bytes.
            parquet_bytes: Converted Parquet data to load.
            rows_processed: Expected row count for verification.
            errors: Errors list to append to on failure (mutated).

        Returns:
            Outcome.LOADED on success, Outcome.QUARANTINED if the
            warehouse service failed.

        Raises:
            IndeterminateCommitError: Commit state could not be
                confirmed; caller must treat this as a failure.
        """
        try:
            self.warehouse.load(
                key=warehouse_key,
                checksum=checksum,
                data=parquet_bytes,
                rows=rows_processed,
            )
            return Outcome.LOADED
        except IndeterminateCommitError:
            logger.error("Cannot confirm warehouse commit for %s", item.name)
            errors.append("Warehouse commit indeterminate")
            raise
        except ServiceError as e:
            logger.error("Warehouse service error: %s", e)
            errors.append(f"Warehouse error: {e}")
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
        )

        audit_json = json.dumps(
            asdict(record),
            default=str,
            sort_keys=True,
        )
        audit_key = (
            f"audit/records/{record.timestamp.isoformat()}_"
            f"{result.item.identity}.json"
        )
        self.store.put(audit_key, audit_json.encode())

        # Also store duplicate check record if loaded
        if result.outcome == Outcome.LOADED:
            self.store.put(f"audit/{result.checksum}", b"processed")
