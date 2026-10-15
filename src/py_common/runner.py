"""Run pipelines from a scheduled job or an upload event.

Pipeline.run() records a file that fails as a FAILED result and
carries on with the next one, so a run that returns normally can
still have failed files. These helpers turn that into the signal the
platform acts on:

- run_job(): for a Cloud Run job. Runs every pipeline, even after one
  fails, and returns the exit code: 1 if any file FAILED or any run
  could not finish.
- handle_upload(): for a Cloud Run service behind an Eventarc trigger.
  Runs the pipeline for the landing folder that received the upload,
  and raises if a file FAILED, so Pub/Sub delivers the event again.
- check_freshness(): logs each source that a freshness view says is
  overdue, for a log-based alert to pick up.

QUARANTINED is not a failure here: the file itself is wrong, and
retrying won't fix it. Pipeline.process() logs the quarantine, which
has its own alert.

An upload event runs Pipeline.run(), not Pipeline.process(). run()
takes the warehouse lock and writes the audit record and the "already
loaded" marker, so an event delivered twice loads the file once.
process() does neither, and calling it alone would load the file
again on every delivery.

No cloud SDK is imported here (see DECISIONS_en.md, "No cloud SDK
imports in core pipeline"): the event arrives as a plain mapping, and
check_freshness() takes any client with a BigQuery-style query().
"""

import base64
import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from py_common.errors import RunFailed
from py_common.model import Outcome, Result
from py_common.pipeline import Pipeline

logger = logging.getLogger("py_common.runner")

#: GCS notification event types that mean "a new object version".
#: An empty type is accepted too, for messages without attributes.
_NEW_OBJECT_EVENTS = ("", "OBJECT_FINALIZE")


class QueryClient(Protocol):
    """A client that runs SQL; google.cloud.bigquery.Client fits."""

    def query(self, query: str) -> Any:
        """Start a query.

        Args:
            query: SQL text.

        Returns:
            A job whose result() iterates rows by column name.
        """
        ...


@dataclass(frozen=True)
class Upload:
    """One object written to a landing bucket.

    Attributes:
        bucket: Bucket name, without gs://.
        name: Object name, e.g. "was/was_database.xlsx".
        generation: Object generation, as text.
        event_type: GCS notification event type, e.g.
            "OBJECT_FINALIZE"; empty if the message didn't say.
    """

    bucket: str
    name: str
    generation: str
    event_type: str

    @property
    def folder(self) -> str:
        """Folder the object sits directly in, as GCSSource names it.

        Returns:
            "was/" for "was/was_database.xlsx"; "" at the bucket root.
        """
        head, sep, _ = self.name.rpartition("/")
        return f"{head}/" if sep else ""


def run_or_raise(pipeline: Pipeline) -> list[Result]:
    """Run one pipeline and raise if any file FAILED.

    Args:
        pipeline: Pipeline to run.

    Returns:
        One Result per file at the source.

    Raises:
        RunFailed: One or more files FAILED. Every other file was
            still processed.
        RuntimeError: Another run holds the warehouse lock.
        ServiceError: Listing the source or writing audit failed.
    """
    results = pipeline.run()
    counts = Counter(result.outcome for result in results)
    logger.info(
        "Run for %s: %d loaded, %d skipped, %d quarantined, %d failed",
        pipeline.warehouse.identity,
        counts[Outcome.LOADED],
        counts[Outcome.SKIPPED],
        counts[Outcome.QUARANTINED],
        counts[Outcome.FAILED],
    )
    failed = [r for r in results if r.outcome == Outcome.FAILED]
    if failed:
        raise RunFailed(failed)
    return results


def run_job(pipelines: Iterable[Pipeline]) -> int:
    """Run every pipeline once, for a scheduled Cloud Run job.

    A pipeline that fails, or can't start because another run holds
    its lock, doesn't stop the others: each source is independent.

    Args:
        pipelines: Pipelines to run, in order.

    Returns:
        Exit code: 0 if every run finished with no FAILED file,
        otherwise 1, so the job execution shows as failed.
    """
    ok = True
    for pipeline in pipelines:
        try:
            run_or_raise(pipeline)
        except Exception as e:
            # Pipeline.run() has already logged the detail ("Pipeline
            # run failed", or "Processing error for" each file); this
            # line names the pipeline it happened in.
            logger.error(
                "Run for %s did not finish cleanly: %s",
                pipeline.warehouse.identity,
                e,
            )
            ok = False
    return 0 if ok else 1


def parse_upload(data: Mapping[str, Any]) -> Upload:
    """Read the GCS notification inside an Eventarc Pub/Sub event.

    Args:
        data: The CloudEvent's data: a Pub/Sub push body whose
            message.data is the GCS notification as base64 JSON
            (bucket notifications with --payload-format=json).

    Returns:
        The uploaded object.

    Raises:
        ValueError: data is not a GCS notification.
    """
    try:
        message = data["message"]
        notification = json.loads(base64.b64decode(message["data"]))
        attributes = message.get("attributes") or {}
        return Upload(
            bucket=notification["bucket"],
            name=notification["name"],
            generation=str(notification.get("generation", "")),
            event_type=attributes.get("eventType", ""),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Not a GCS upload notification: {e!r}") from e


def handle_upload(
    data: Mapping[str, Any], pipelines: Mapping[str, Pipeline]
) -> list[Result]:
    """Run the pipeline for the landing folder an upload went to.

    For a Cloud Run service behind an Eventarc Pub/Sub trigger; pass
    the CloudEvent's data. The run lists the whole folder, so files
    that land together are all loaded by whichever event runs first,
    and the later events find them already loaded.

    An event that can't be acted on is logged and returns normally,
    so Pub/Sub doesn't deliver it again: retrying wouldn't change it.

    Args:
        data: The CloudEvent's data (see parse_upload()).
        pipelines: Pipeline per landing folder, keyed by the folder
            its GCSSource reads, e.g. "was/" ("" for the bucket
            root). Leading and trailing slashes are optional.

    Returns:
        One Result per file in the folder; empty if the event was
        ignored (unreadable, not a new object, or a folder with no
        pipeline).

    Raises:
        RunFailed: A file FAILED; the event should be retried.
        RuntimeError: Another run holds the warehouse lock.
        ServiceError: Listing the folder or writing audit failed.
    """
    try:
        upload = parse_upload(data)
    except ValueError as e:
        logger.error("Ignoring event: %s", e)
        return []

    where = f"gs://{upload.bucket}/{upload.name}"
    if upload.event_type not in _NEW_OBJECT_EVENTS:
        logger.info("Ignoring %s event for %s", upload.event_type, where)
        return []

    by_folder = {_folder(key): value for key, value in pipelines.items()}
    pipeline = by_folder.get(upload.folder)
    if pipeline is None:
        logger.warning(
            "Ignoring upload %s: no pipeline reads folder %r",
            where,
            upload.folder,
        )
        return []

    logger.info(
        "Upload %s (generation %s): running %s",
        where,
        upload.generation,
        pipeline.warehouse.identity,
    )
    return run_or_raise(pipeline)


def check_freshness(client: QueryClient, view: str) -> list[str]:
    """Log every source that the freshness view says is overdue.

    Each overdue source is logged at ERROR as "Missing monthly file:
    <source> (last loaded <date>)", for a log-based alert to match.
    A run of the daily job therefore repeats the alert every day
    until the file arrives.

    Args:
        client: BigQuery client.
        view: Fully qualified view, "project.dataset.view", with
            columns source (STRING), last_loaded_at (TIMESTAMP, NULL
            if never loaded) and overdue (BOOL).

    Returns:
        Names of the overdue sources, sorted.

    Raises:
        ValueError: view contains a backtick.
        Exception: Whatever the client raises; a check that couldn't
            run is not reported as "nothing missing".
    """
    if "`" in view:
        raise ValueError(f"View name must not contain '`': {view!r}")
    rows = client.query(
        f"SELECT source, last_loaded_at FROM `{view}` "
        "WHERE overdue ORDER BY source"
    ).result()
    overdue: list[str] = []
    for row in rows:
        last = row["last_loaded_at"]
        logger.error(
            "Missing monthly file: %s (last loaded %s)",
            row["source"],
            last.date().isoformat() if last else "never",
        )
        overdue.append(row["source"])
    return overdue


def _folder(prefix: str) -> str:
    """Normalise a landing folder the way GCSSource does.

    Args:
        prefix: Folder, with or without leading/trailing slashes.

    Returns:
        "was/" for "was", "/was" or "was/"; "" for the bucket root.
    """
    folder = prefix.strip("/")
    return f"{folder}/" if folder else ""
