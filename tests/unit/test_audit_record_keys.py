"""Audit records never share a key, however coarse the clock.

Each processing attempt writes its own audit record, keyed by the
time and the file. Python before 3.13 reads the Windows clock only
about every 15 ms, so two runs of one file in quick succession got
the same key; the store, which is write-once, refused the second
record, and that run FAILED. Keys now have a random part too.
"""

import io
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from _memory_store import MemoryStore
from openpyxl import Workbook

from py_common.contract import Column, Contract, Worksheet
from py_common.model import Outcome, SourceItem
from py_common.pipeline import Pipeline

TICK = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Events"
    ws.append(["name"])
    ws.append(["x"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _pipeline(store: MemoryStore) -> Pipeline:
    source = Mock()
    source.items.return_value = [
        SourceItem("events.xlsx", "landing/events.xlsx", "1", 1)
    ]
    source.download.return_value = _workbook()
    warehouse = Mock(identity="p.d.t")
    warehouse.load.return_value = True
    contract = Contract(
        worksheets=[Worksheet("Events", [Column("name", "string")])]
    )
    return Pipeline(source, store, warehouse, contract)


def test_two_records_in_one_clock_tick_both_land() -> None:
    store = MemoryStore()
    pipeline = _pipeline(store)

    with patch("py_common.pipeline.datetime") as clock:
        clock.now.return_value = TICK
        outcomes = [r.outcome for r in pipeline.run() + pipeline.run()]

    assert outcomes == [Outcome.LOADED, Outcome.SKIPPED]
    records = sorted(k for k in store.objects if k.startswith("audit/rec"))
    assert len(records) == 2
    for key in records:
        assert key.startswith("audit/records/2026-10-16T12-00-00+00-00_")
        assert key.endswith("_landing/events.xlsx.json")
