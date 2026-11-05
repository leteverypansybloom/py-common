"""Failure injection with repository stores and warehouse fakes."""

import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from _fake_bigquery import FakeBigQueryClient
from _fake_gcs import FakeBucket
from _memory_store import MemoryStore
from google.api_core.exceptions import BadRequest

from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore
from py_common.adapters.local import LocalObjectStore
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ServiceError
from py_common.model import Outcome, SourceItem
from py_common.pipeline import Pipeline

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows path")


def setup():
    """Build one real pipeline on synthetic bytes and fake services.

    Returns:
        Pipeline and stateful BigQuery fake.
    """
    data = b'[{"id":1}]'
    item = SourceItem("probe.json", "probe", "1", len(data))
    source = Mock()
    source.download.return_value = data
    source.items.return_value = [item]
    client = FakeBigQueryClient()
    with patch(
        "py_common.adapters.bigquery_raw.bigquery.Client", return_value=client
    ):
        warehouse = RawJsonWarehouse("p", "d", {"Sheet": "rows"})
    pipeline = Pipeline(
        source,
        MemoryStore(),
        warehouse,
        Contract(
            [Worksheet("Sheet", [Column("id", "integer")])], file_format="json"
        ),
    )
    return pipeline, client


class TestPipelineProcess:
    """Retry every boundary and check actual fake warehouse row counts."""

    def test_retry_restores_loaded_audit_metadata(self):
        """An audit-write retry restores the loaded contract and warnings."""
        pipeline, client = setup()
        with patch.object(
            pipeline, "_audit", side_effect=ServiceError("audit")
        ):
            assert pipeline.run()[0].outcome == Outcome.FAILED
        assert pipeline.run()[0].outcome == Outcome.SKIPPED
        import json

        records = [
            json.loads(data)
            for key, data in pipeline.store.objects.items()
            if key.startswith("audit/records/")
        ]
        loaded = [
            record for record in records if record["outcome"] == "loaded"
        ]
        assert len(loaded) == 1
        assert loaded[0]["rows_processed"] == 1
        assert (
            loaded[0]["contract_fingerprint"]
            == pipeline.contract.fingerprint()
        )
        assert loaded[0]["processed_key"]
        assert client.rows["p.d.rows"] == 1

    @pytest.mark.parametrize(
        "step",
        [
            "marker_read",
            "download",
            "validate",
            "raw",
            "convert",
            "processed",
            "create_table",
            "load_stage",
            "verify_stage",
            "commit_start",
            "marker_write",
            "audit",
            "indeterminate",
        ],
    )
    def test_retry_is_lossless(self, step):
        """A failed attempt and retry leave exactly one committed row.

        Args:
            step: Boundary that fails once.
        """
        pipeline, client = setup()
        error = ServiceError("synthetic failure")
        context = nullcontext()
        if step == "marker_read":
            context = patch.object(pipeline.store, "get", side_effect=error)
        elif step == "download":
            context = patch.object(
                pipeline.source, "download", side_effect=error
            )
        elif step == "validate":
            context = patch.object(
                pipeline.contract, "read", side_effect=error
            )
        elif step == "convert":
            context = patch("py_common.pipeline.to_ndjson", side_effect=error)
        elif step in {"raw", "processed", "marker_write", "audit"}:
            original = pipeline.store.put
            prefix = {
                "raw": "raw/",
                "processed": "processed/",
                "marker_write": "audit/p.d.rows/",
                "audit": "audit/records/",
            }[step]

            def put(key, data):
                """Fail the selected synthetic write.

                Args:
                    key: Object key.
                    data: Synthetic bytes.
                """
                if key.startswith(prefix):
                    raise error
                original(key, data)

            context = patch.object(pipeline.store, "put", side_effect=put)
        elif step in {"create_table", "load_stage"}:
            method = (
                "create_table"
                if step == "create_table"
                else "load_table_from_file"
            )
            context = patch.object(client, method, side_effect=error)
        elif step == "verify_stage":
            context = patch(
                "py_common.adapters.bigquery_raw.wait_for_load",
                side_effect=error,
            )
        elif step == "commit_start":
            original = client.query

            def query(sql, job_config=None):
                """Fail the selected query.

                Args:
                    sql: SQL text.
                    job_config: Query parameters.

                Returns:
                    Fake completed job.
                """
                commit = "BEGIN TRANSACTION" in sql
                if commit == (step == "commit_start"):
                    raise error
                return original(sql, job_config)

            context = patch.object(client, "query", side_effect=query)
        elif step == "indeterminate":
            client.lose_next_commit_wait = TimeoutError("synthetic timeout")
        with context:
            first = pipeline.run()[0]
        assert first.outcome == Outcome.FAILED, (step, first)
        second = pipeline.run()[0]
        assert second.outcome in {Outcome.LOADED, Outcome.SKIPPED}, second
        assert client.rows["p.d.rows"] == 1, client.rows
        assert len(client.audit) == 1, client.audit
        print(
            step,
            first.outcome.value,
            second.outcome.value,
            "rows=",
            client.rows["p.d.rows"],
            "audit=",
            len(client.audit),
        )

    def test_duplicate_delivery(self):
        """The second delivery leaves a single warehouse row."""
        pipeline, client = setup()
        assert pipeline.run()[0].outcome == Outcome.LOADED
        assert pipeline.run()[0].outcome == Outcome.SKIPPED
        assert client.rows["p.d.rows"] == 1

    def test_commit_failure_rolls_back_inserted_rows(self):
        """A mid-script failure leaves no rows or audit entries."""
        pipeline, client = setup()
        client.fail_after_insert = BadRequest("synthetic commit failure")
        assert pipeline.run()[0].outcome == Outcome.FAILED
        assert client.rows["p.d.rows"] == 0
        assert client.audit == []
        assert pipeline.run()[0].outcome == Outcome.LOADED
        assert client.rows["p.d.rows"] == 1

    def test_query_submission_error_is_private(self, caplog):
        """A rejected job submission cannot disclose source content.

        Args:
            caplog: Captured logs.
        """
        pipeline, client = setup()
        marker = "synthetic.person@example.invalid"
        with patch.object(client, "query", side_effect=BadRequest(marker)):
            result = pipeline.run()[0]
        assert marker not in str(result.errors) + caplog.text


class TestPipelineRun:
    """An expired live worker and its replacement use warehouse dedup."""

    def test_expired_worker_resumes_after_takeover(self):
        """Serial commits after lease takeover retain one row."""
        pipeline, client = setup()
        bucket = FakeBucket()
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "store", lease_seconds=1)
        store.bucket = bucket
        pipeline.store = store
        first_download = pipeline.source.download.return_value
        time = [datetime(2026, 1, 1, tzinfo=UTC)]

        def download(item):
            """Let a second worker run while the first download waits.

            Args:
                item: Source item.

            Returns:
                Synthetic source bytes.
            """
            time[0] += timedelta(seconds=2)
            pipeline.source.download.side_effect = None
            assert pipeline.run()[0].outcome == Outcome.LOADED
            return first_download

        pipeline.source.download.side_effect = download
        with patch("py_common.adapters.gcs._utcnow", lambda: time[0]):
            result = pipeline.run()[0]
        assert result.outcome == Outcome.SKIPPED
        assert client.rows["p.d.rows"] == 1


class TestSecurityBoundaries:
    """Object keys and source metadata cannot become filesystem or SQL code."""

    @pytest.mark.parametrize(
        "option", ["project_id", "dataset_id", "tables", "audit_table_id"]
    )
    def test_warehouse_identifiers_are_validated(self, option):
        """SQL identifier text is refused before a client is constructed.

        Args:
            option: Identifier configuration field under test.
        """
        settings = {
            "project_id": "p",
            "dataset_id": "d",
            "tables": {"Sheet": "rows"},
            "audit_table_id": "audit",
        }
        bad = "rows` CROSS JOIN `unexpected"
        settings[option] = {"Sheet": bad} if option == "tables" else bad
        with patch(
            "py_common.adapters.bigquery_raw.bigquery.Client"
        ) as client:
            with pytest.raises(ValueError, match="invalid characters"):
                RawJsonWarehouse(**settings)
        client.assert_not_called()

    @pytest.mark.parametrize(
        "key",
        [
            "../outside",
            "../../outside",
            pytest.param("C:/outside", marks=WINDOWS_ONLY),
            pytest.param("..\\outside", marks=WINDOWS_ONLY),
        ],
    )
    def test_object_store_traversal_is_refused(self, tmp_path, key):
        """An escaping object key cannot read or write outside the store.

        Args:
            tmp_path: Synthetic object-store folder.
            key: Escaping object key.
        """
        store = LocalObjectStore(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            store.put(key, b"synthetic")

    def test_source_sql_text_is_parameterised(self):
        """Source identity and version stay out of the commit SQL."""
        pipeline, client = setup()
        marker = "'; DROP TABLE secret; --"
        item = SourceItem("probe.json", marker, marker, 10)
        pipeline.source.items.return_value = [item]
        assert pipeline.run()[0].outcome == Outcome.LOADED
        assert all(marker not in sql for sql in client.scripts)
        assert client.rows["p.d.rows"] == 1
