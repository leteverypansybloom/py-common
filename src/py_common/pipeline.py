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
            # Download file
            try:
                data = self.source.download(item)
            except VersionMismatch:
                logger.info(
                    "Skipping %s: version changed during download",
                    item.name,
                )
                return Result(
                    item=item,
                    outcome=Outcome.SKIPPED,
                    checksum="",
                    rows_processed=0,
                    errors=[],
                    processing_time_seconds=perf_counter() - start_time,
                )

            # Hash and check for duplicates
            checksum = digest(data)
            existing = self.store.get(f"audit/{checksum}")
            if existing:
                logger.info(
                    "Skipping %s: checksum %s already processed",
                    item.name,
                    checksum[:8],
                )
                return Result(
                    item=item,
                    outcome=Outcome.SKIPPED,
                    checksum=checksum,
                    rows_processed=0,
                    errors=[],
                    processing_time_seconds=perf_counter() - start_time,
                )

            # Validate contract
            try:
                rows_processed = self.contract.validate(data, item.name)
            except ValidationError as e:
                logger.warning("Validation failed for %s: %s", item.name, e)
                errors.append(str(e))
                outcome = Outcome.QUARANTINED
                self.store.put(f"raw/{item.identity}", data)
                self.store.put(f"quarantine/{item.identity}", data)
                return Result(
                    item=item,
                    outcome=outcome,
                    checksum=checksum,
                    rows_processed=0,
                    errors=errors,
                    processing_time_seconds=perf_counter() - start_time,
                )

            # Store raw file
            self.store.put(f"raw/{item.identity}", data)

            # Convert to Parquet. Look up the primary worksheet by
            # the name the contract validated, not wb.active: the
            # workbook's active sheet has no guaranteed relation to
            # which sheet the contract expects.
            wb = load_workbook(BytesIO(data), data_only=True)
            primary_sheet_name = self.contract.worksheets[0].name
            ws = wb[primary_sheet_name]
            headers = [cell.value for cell in ws[1]]
            rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                rows.append(dict(zip(headers, row)))

            df = pd.DataFrame(rows)
            df["_loaded_at"] = datetime.now(timezone.utc)
            
            parquet_buffer = BytesIO()
            df.to_parquet(parquet_buffer, index=False)
            parquet_bytes = parquet_buffer.getvalue()

            # Store processed file
            self.store.put(f"processed/{item.identity}.parquet", parquet_bytes)

            # Load to warehouse
            warehouse_key = f"{item.identity}_{checksum[:8]}"
            try:
                self.warehouse.load(
                    key=warehouse_key,
                    checksum=checksum,
                    data=parquet_bytes,
                    rows=rows_processed,
                )
            except IndeterminateCommitError:
                logger.error(
                    "Cannot confirm warehouse commit for %s", item.name
                )
                outcome = Outcome.FAILED
                errors.append("Warehouse commit indeterminate")
                raise
            except ServiceError as e:
                logger.error("Warehouse service error: %s", e)
                outcome = Outcome.QUARANTINED
                errors.append(f"Warehouse error: {e}")

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
