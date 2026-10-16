"""Unit tests for py_common.runner.

Pipelines and the BigQuery client are mocks: these tests cover how
results, events and freshness rows are turned into exit codes,
exceptions and log lines, not the pipeline itself (test_pipeline.py).
"""

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from py_common.errors import RunFailed, ServiceError
from py_common.model import Outcome, Result, SourceItem
from py_common.runner import (
    Upload,
    check_freshness,
    handle_upload,
    parse_upload,
    run_job,
    run_or_raise,
)


def _result(name: str, outcome: Outcome) -> Result:
    """Result for one file with the given outcome.

    Args:
        name: File name.
        outcome: Outcome to report.

    Returns:
        Result.
    """
    return Result(
        item=SourceItem(name, f"was/{name}", "1", 10),
        outcome=outcome,
        checksum="abc",
        rows_processed=0,
        errors=[],
        processing_time_seconds=0.0,
    )


def _pipeline(*outcomes: Outcome, identity: str = "p.raw.was") -> Mock:
    """Mock pipeline whose run() returns one result per outcome.

    Args:
        *outcomes: Outcome of each file, in order.
        identity: Warehouse identity, used in log lines.

    Returns:
        Mock pipeline.
    """
    pipeline = Mock()
    pipeline.warehouse.identity = identity
    pipeline.run.return_value = [
        _result(f"file{number}.xlsx", outcome)
        for number, outcome in enumerate(outcomes, start=1)
    ]
    return pipeline


def _event(
    name: str,
    event_type: str | None = "OBJECT_FINALIZE",
    bucket: str = "hww-landing",
) -> dict[str, Any]:
    """CloudEvent data for a GCS upload, as Eventarc delivers it.

    Args:
        name: Object name.
        event_type: eventType attribute; None leaves attributes out.
        bucket: Bucket name.

    Returns:
        Pub/Sub push body with the notification in message.data.
    """
    notification = {"bucket": bucket, "name": name, "generation": "17"}
    message: dict[str, Any] = {
        "data": base64.b64encode(json.dumps(notification).encode()).decode()
    }
    if event_type is not None:
        message["attributes"] = {"eventType": event_type}
    return {"message": message, "subscription": "projects/p/subs/s"}


def _client(*rows: dict[str, Any]) -> Mock:
    """Mock BigQuery client whose query returns the given rows.

    Args:
        *rows: Rows of the freshness view that are overdue.

    Returns:
        Mock client.
    """
    client = Mock()
    client.query.return_value.result.return_value = list(rows)
    return client


class TestRunOrRaise:
    """run_or_raise() raises only when a file FAILED."""

    def test_clean_run_returns_results(self) -> None:
        """Loaded and skipped files come back as results."""
        pipeline = _pipeline(Outcome.LOADED, Outcome.SKIPPED)

        results = run_or_raise(pipeline)

        assert [r.outcome for r in results] == [
            Outcome.LOADED,
            Outcome.SKIPPED,
        ]

    def test_quarantined_file_does_not_raise(self) -> None:
        """A bad file is not retried: retrying won't fix it."""
        pipeline = _pipeline(Outcome.QUARANTINED)

        results = run_or_raise(pipeline)

        assert results[0].outcome == Outcome.QUARANTINED

    def test_failed_file_raises_run_failed(self) -> None:
        """A FAILED file raises, naming it, after the run finishes."""
        pipeline = _pipeline(Outcome.LOADED, Outcome.FAILED)

        with pytest.raises(RunFailed, match="1 file\\(s\\) failed") as e:
            run_or_raise(pipeline)

        assert [r.item.name for r in e.value.results] == ["file2.xlsx"]
        assert "file2.xlsx" in str(e.value)

    def test_lock_error_propagates(self) -> None:
        """A run refused by the lock raises, so the event is retried."""
        pipeline = _pipeline()
        pipeline.run.side_effect = RuntimeError("lock held")

        with pytest.raises(RuntimeError, match="lock held"):
            run_or_raise(pipeline)

    def test_summary_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """One INFO line counts each outcome for the pipeline."""
        pipeline = _pipeline(Outcome.LOADED, Outcome.QUARANTINED)

        with caplog.at_level(logging.INFO, logger="py_common.runner"):
            run_or_raise(pipeline)

        assert (
            "Run for p.raw.was: 1 loaded, 0 skipped, 1 quarantined, "
            "0 failed" in caplog.text
        )


class TestRunJob:
    """run_job() runs every pipeline and returns the exit code."""

    def test_clean_runs_exit_zero(self) -> None:
        """No FAILED file in any pipeline: exit code 0."""
        pipelines = [_pipeline(Outcome.LOADED), _pipeline(Outcome.SKIPPED)]

        assert run_job(pipelines) == 0

    def test_quarantine_exits_zero(self) -> None:
        """A quarantine has its own alert; the job still succeeds."""
        assert run_job([_pipeline(Outcome.QUARANTINED)]) == 0

    def test_failed_file_exits_one(self) -> None:
        """A FAILED file marks the job execution failed."""
        assert run_job([_pipeline(Outcome.FAILED)]) == 1

    def test_later_pipelines_run_after_a_failure(self) -> None:
        """One source failing doesn't stop the next being loaded."""
        first = _pipeline(identity="p.raw.was")
        first.run.side_effect = ServiceError("listing failed")
        second = _pipeline(Outcome.LOADED, identity="p.raw.survey")

        code = run_job([first, second])

        assert code == 1
        second.run.assert_called_once()

    def test_failure_names_the_pipeline(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The error line says which pipeline didn't finish."""
        pipeline = _pipeline(identity="p.raw.was")
        pipeline.run.side_effect = RuntimeError("lock held")

        with caplog.at_level(logging.ERROR, logger="py_common.runner"):
            run_job([pipeline])

        assert "Run for p.raw.was did not finish cleanly" in caplog.text

    def test_no_pipelines_exit_zero(self) -> None:
        """Nothing to run is not a failure."""
        assert run_job([]) == 0


class TestParseUpload:
    """parse_upload() reads the GCS notification from the event."""

    def test_reads_object_and_event_type(self) -> None:
        """Bucket, name, generation and event type are read."""
        upload = parse_upload(_event("was/was_database.xlsx"))

        assert upload == Upload(
            bucket="hww-landing",
            name="was/was_database.xlsx",
            generation="17",
            event_type="OBJECT_FINALIZE",
        )

    def test_missing_attributes_give_empty_event_type(self) -> None:
        """A message without attributes still parses."""
        upload = parse_upload(_event("was/x.xlsx", event_type=None))

        assert upload.event_type == ""

    def test_missing_message_is_rejected(self) -> None:
        """A body with no message is not a notification."""
        with pytest.raises(ValueError, match="Not a GCS upload"):
            parse_upload({"subscription": "s"})

    def test_data_that_is_not_json_is_rejected(self) -> None:
        """message.data must decode to JSON."""
        data = {"message": {"data": base64.b64encode(b"nope").decode()}}

        with pytest.raises(ValueError, match="Not a GCS upload"):
            parse_upload(data)

    def test_notification_without_name_is_rejected(self) -> None:
        """The notification must name the object."""
        encoded = base64.b64encode(b'{"bucket": "b"}').decode()

        with pytest.raises(ValueError, match="Not a GCS upload"):
            parse_upload({"message": {"data": encoded}})


class TestUploadFolder:
    """Upload.folder matches GCSSource's prefix format."""

    def test_folder_ends_in_slash(self) -> None:
        """An object in a folder gives that folder, with "/"."""
        upload = Upload("b", "was/was_database.xlsx", "1", "")

        assert upload.folder == "was/"

    def test_nested_folder_is_kept_whole(self) -> None:
        """Every level of the path is part of the folder."""
        upload = Upload("b", "landing/was/x.xlsx", "1", "")

        assert upload.folder == "landing/was/"

    def test_root_object_has_empty_folder(self) -> None:
        """An object at the bucket root is in folder ""."""
        assert Upload("b", "x.xlsx", "1", "").folder == ""


class TestHandleUpload:
    """handle_upload() runs the pipeline for the upload's folder."""

    def test_runs_the_pipeline_for_the_folder(self) -> None:
        """Only the pipeline reading that folder runs."""
        was = _pipeline(Outcome.LOADED)
        survey = _pipeline()

        results = handle_upload(
            _event("was/was_database.xlsx"),
            {"was/": was, "survey/": survey},
        )

        assert len(results) == 1
        was.run.assert_called_once()
        survey.run.assert_not_called()

    def test_folder_keys_are_normalised(self) -> None:
        """Key "/was" matches an upload to was/, as GCSSource reads."""
        was = _pipeline(Outcome.LOADED)

        handle_upload(_event("was/x.xlsx"), {"/was": was})

        was.run.assert_called_once()

    def test_root_folder_key_matches_root_upload(self) -> None:
        """Key "" reads the bucket root."""
        root = _pipeline(Outcome.LOADED)

        handle_upload(_event("x.xlsx"), {"": root})

        root.run.assert_called_once()

    def test_unknown_folder_is_ignored(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No pipeline for the folder: warn and acknowledge."""
        was = _pipeline()

        with caplog.at_level(logging.WARNING, logger="py_common.runner"):
            results = handle_upload(_event("peers/x.xlsx"), {"was/": was})

        assert results == []
        was.run.assert_not_called()
        assert "no pipeline reads folder 'peers/'" in caplog.text

    def test_sub_folder_is_not_its_parent(self) -> None:
        """GCSSource doesn't read sub-folders, so they don't match."""
        was = _pipeline()

        results = handle_upload(_event("was/old/x.xlsx"), {"was/": was})

        assert results == []
        was.run.assert_not_called()

    def test_non_finalize_event_is_ignored(self) -> None:
        """A delete or metadata change is not a new file."""
        was = _pipeline()

        results = handle_upload(
            _event("was/x.xlsx", event_type="OBJECT_DELETE"), {"was/": was}
        )

        assert results == []
        was.run.assert_not_called()

    def test_event_without_type_is_ignored(self) -> None:
        """Only OBJECT_FINALIZE runs; GCS notifications always say so.

        A message with no eventType attribute isn't a GCS upload
        notification (perhaps mis-routed from another topic), so it
        is acknowledged without a run (review L8).
        """
        was = _pipeline(Outcome.LOADED)

        results = handle_upload(
            _event("was/x.xlsx", event_type=None), {"was/": was}
        )

        assert results == []
        was.run.assert_not_called()

    def test_failed_file_raises_for_redelivery(self) -> None:
        """A FAILED file raises, so Pub/Sub delivers the event again."""
        was = _pipeline(Outcome.FAILED)

        with pytest.raises(RunFailed):
            handle_upload(_event("was/x.xlsx"), {"was/": was})

    def test_lock_error_raises_for_redelivery(self) -> None:
        """A run refused by the lock is retried later."""
        was = _pipeline()
        was.run.side_effect = RuntimeError("lock held")

        with pytest.raises(RuntimeError):
            handle_upload(_event("was/x.xlsx"), {"was/": was})

    def test_unreadable_event_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Retrying a malformed event wouldn't help: log and ack."""
        was = _pipeline()

        with caplog.at_level(logging.ERROR, logger="py_common.runner"):
            results = handle_upload({"bad": "event"}, {"was/": was})

        assert results == []
        was.run.assert_not_called()
        assert "Ignoring event: Not a GCS upload" in caplog.text


class TestCheckFreshness:
    """check_freshness() logs each overdue source for the alert."""

    def test_overdue_source_is_logged_and_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The alert's text names the source and its last load."""
        last = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
        client = _client({"source": "was", "last_loaded_at": last})

        with caplog.at_level(logging.ERROR, logger="py_common.runner"):
            overdue = check_freshness(client, "p.raw_hww.source_freshness")

        assert overdue == ["was"]
        assert (
            "Missing monthly file: was (last loaded 2026-09-04)" in caplog.text
        )

    def test_never_loaded_source_says_never(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A source with no load yet is reported as never loaded."""
        client = _client({"source": "badges", "last_loaded_at": None})

        with caplog.at_level(logging.ERROR, logger="py_common.runner"):
            check_freshness(client, "p.raw_hww.source_freshness")

        assert "Missing monthly file: badges (last loaded never)" in (
            caplog.text
        )

    def test_nothing_overdue_logs_no_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No overdue rows: no ERROR line, so no alert."""
        client = _client()

        with caplog.at_level(logging.ERROR, logger="py_common.runner"):
            overdue = check_freshness(client, "p.raw_hww.source_freshness")

        assert overdue == []
        assert caplog.records == []

    def test_query_reads_overdue_rows_of_the_view(self) -> None:
        """The SQL selects only overdue rows from the named view."""
        client = _client()

        check_freshness(client, "p.raw_hww.source_freshness")

        sql = client.query.call_args.args[0]
        assert "FROM `p.raw_hww.source_freshness`" in sql
        assert "WHERE overdue" in sql

    @pytest.mark.parametrize(
        "view",
        [
            "p.d.v` OR TRUE --",
            "p.d",
            "p.d.v.x",
            "p.d.v WHERE 1=1",
            "p.d.v;DROP",
            "",
        ],
    )
    def test_view_must_be_project_dataset_view(self, view: str) -> None:
        """Only project.dataset.view, of word characters and hyphens."""
        client = _client()

        with pytest.raises(ValueError, match="project.dataset.view"):
            check_freshness(client, view)

        client.query.assert_not_called()

    def test_hyphenated_project_is_accepted(self) -> None:
        client = _client()

        check_freshness(client, "phw-prod-1.raw_hww.source_freshness")

        client.query.assert_called_once()

    def test_query_error_propagates(self) -> None:
        """A check that couldn't run isn't reported as nothing due."""
        client = Mock()
        client.query.side_effect = RuntimeError("access denied")

        with pytest.raises(RuntimeError, match="access denied"):
            check_freshness(client, "p.raw_hww.source_freshness")
