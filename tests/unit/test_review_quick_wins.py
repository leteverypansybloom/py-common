"""Quick wins from REVIEW_.md: M1, M2, M3, M5, M7, M11, L2-L6, L9, L10."""

import io
import logging
import subprocess
import sys
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import requires
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
from _fake_gcs import FakeBucket
from google.api_core.exceptions import BadRequest, Forbidden
from openpyxl import Workbook
from packaging.requirements import Requirement

from py_common.adapters._bq_jobs import (
    JOB_TIMEOUT_SECONDS,
    run_query,
    wait_for_load,
)
from py_common.adapters.bigquery import BigQueryWarehouse
from py_common.adapters.bigquery_raw import RawJsonWarehouse
from py_common.adapters.gcs import GCSObjectStore, GCSSource
from py_common.adapters.local import LocalSource
from py_common.errors import ObjectStoreConflict
from py_common.contract import Column, Contract, Worksheet
from py_common.errors import ConfigError, ContractError, ServiceError
from py_common.logging import JsonFormatter, configure_json_logging
from py_common.model import Outcome
from py_common.pipeline import Pipeline
from py_common.runner import handle_upload

SECRET = "jo.bloggs@example.org"
CONTRACT = Contract(
    worksheets=[Worksheet("Events", [Column("name", "string")])]
)


def _book(rows: list[list[Any]], title: str = "Events") -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = title
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _warehouse() -> Mock:
    warehouse = Mock(identity="p.d.t")
    warehouse.load.return_value = True
    return warehouse


class MemoryStore:
    """Dict-backed ObjectStore with the real put() conflict rule.

    Used instead of LocalObjectStore, whose deep keys can pass
    Windows' 260-character path limit under a long temp directory.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, data: bytes) -> None:
        if key in self.objects and self.objects[key] != data:
            raise ObjectStoreConflict(key)
        self.objects[key] = data

    @contextmanager
    def lock(self, key: str) -> Iterator[None]:
        yield


# ------------------------------------------------------------------ M1


class TestLockReleaseNeverRaises:
    """A failed lock release is logged; it never hides the run's result."""

    @pytest.fixture
    def store(self) -> tuple[GCSObjectStore, FakeBucket]:
        bucket = FakeBucket()
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "store")
        store.bucket = bucket
        real_blob = bucket.blob

        def blob(key: str) -> Any:
            found = real_blob(key)
            if key.startswith("locks/"):

                def refuse(if_generation_match: Any = None) -> None:
                    raise Forbidden("no storage.objects.delete on locks/")

                found.delete = refuse  # type: ignore[method-assign]
            return found

        bucket.blob = blob  # type: ignore[method-assign]
        return store, bucket

    def test_release_failure_is_logged_not_raised(
        self,
        store: tuple[GCSObjectStore, FakeBucket],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        gcs, bucket = store

        with caplog.at_level(logging.ERROR, logger="py_common.adapters.gcs"):
            with gcs.lock("locks/k"):
                pass

        assert "locks/k" in bucket.objects  # left to expire
        assert any(
            "Could not release lock locks/k" in r.getMessage()
            for r in caplog.records
        )

    def test_run_still_returns_its_results(
        self, store: tuple[GCSObjectStore, FakeBucket], tmp_path: Path
    ) -> None:
        gcs, _ = store
        (tmp_path / "a.xlsx").write_bytes(_book([["name"], ["x"]]))

        results = Pipeline(
            LocalSource(tmp_path), gcs, _warehouse(), CONTRACT
        ).run()

        assert [r.outcome for r in results] == [Outcome.LOADED]


# ------------------------------------------------------------------ M11


class TestMarkerBeforeDownload:
    """An upload already loaded here isn't downloaded again."""

    @pytest.fixture
    def setup(self, tmp_path: Path) -> tuple[Pipeline, list[str]]:
        folder = tmp_path / "in"
        folder.mkdir()
        (folder / "a.xlsx").write_bytes(_book([["name"], ["x"]]))
        source = LocalSource(folder)
        downloads: list[str] = []
        real = source.download

        def download(item: Any) -> bytes:
            downloads.append(item.name)
            return real(item)

        source.download = download  # type: ignore[method-assign]
        pipeline = Pipeline(source, MemoryStore(), _warehouse(), CONTRACT)
        return pipeline, downloads

    def test_second_run_skips_without_downloading(
        self, setup: tuple[Pipeline, list[str]]
    ) -> None:
        pipeline, downloads = setup

        first = pipeline.run()
        second = pipeline.run()

        assert [r.outcome for r in first] == [Outcome.LOADED]
        assert [r.outcome for r in second] == [Outcome.SKIPPED]
        assert downloads == ["a.xlsx"]

    def test_unreachable_but_loaded_file_is_not_failed(
        self, setup: tuple[Pipeline, list[str]]
    ) -> None:
        """A loaded file that can't be downloaded now isn't an alert."""
        pipeline, _ = setup
        pipeline.run()

        def refuse(item: Any) -> bytes:
            raise ServiceError("503 from the source")

        pipeline.source.download = refuse  # type: ignore[method-assign]

        assert [r.outcome for r in pipeline.run()] == [Outcome.SKIPPED]


# ------------------------------------------------------------------ M2


def _contract(**limits: int) -> Contract:
    return Contract(
        worksheets=[Worksheet("Events", [Column("name", "string")])],
        **limits,
    )


class TestWorkbookLimits:
    """Oversized uploads are refused before they're parsed."""

    def test_file_over_max_file_bytes_is_quarantined(self) -> None:
        data = _book([["name"], ["x"]])

        with pytest.raises(ContractError, match="max_file_bytes"):
            _contract(max_file_bytes=len(data) - 1).read(data, "f.xlsx")

    def test_uncompressed_size_over_limit_is_quarantined(self) -> None:
        """Guards against zip bombs: checked from the zip directory."""
        data = _book([["name"]] + [["x" * 50]] * 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unpacked = sum(i.file_size for i in archive.infolist())

        with pytest.raises(ContractError, match="max_uncompressed_bytes"):
            _contract(max_uncompressed_bytes=unpacked - 1).read(data, "f.xlsx")

    def test_defaults_accept_ordinary_workbooks(self) -> None:
        assert _contract().validate(_book([["name"], ["x"]]), "f.xlsx") == 1

    @pytest.mark.parametrize(
        "limits", [{"max_file_bytes": 0}, {"max_uncompressed_bytes": -1}]
    )
    def test_limits_must_be_positive(self, limits: dict[str, int]) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            _contract(**limits)

    def test_pipeline_quarantines_and_keeps_the_original(
        self, tmp_path: Path
    ) -> None:
        folder = tmp_path / "in"
        folder.mkdir()
        (folder / "big.xlsx").write_bytes(_book([["name"], ["x"]]))
        store = MemoryStore()

        [result] = Pipeline(
            LocalSource(folder),
            store,
            _warehouse(),
            _contract(max_file_bytes=100),
        ).run()

        assert result.outcome == Outcome.QUARANTINED
        assert any(
            k.startswith("quarantine/") and k.endswith("/big.xlsx")
            for k in store.objects
        )


# ------------------------------------------------------------------ M2/M3


def _requirement(name: str, extra: str | None = None) -> Requirement:
    for raw in requires("py-common") or []:
        req = Requirement(raw)
        in_extra = req.marker is not None and "extra" in str(req.marker)
        wanted = extra is not None and f'extra == "{extra}"' in str(req.marker)
        if req.name.lower() == name and (
            wanted or (extra is None and not in_extra)
        ):
            return req
    raise AssertionError(f"py-common doesn't require {name} ({extra})")


class TestDependencyFloors:
    """The package can't install XML-unsafe or known-vulnerable deps."""

    def test_defusedxml_is_a_runtime_dependency(self) -> None:
        """openpyxl only guards against XML entity attacks with it."""
        assert _requirement("defusedxml")

    def test_pyarrow_floor_excludes_advisories(self) -> None:
        spec = _requirement("pyarrow").specifier
        assert not spec.contains("23.0.0")
        assert spec.contains("23.0.1")

    @pytest.mark.parametrize("extra", ["sharepoint", "dev"])
    def test_requests_floor_excludes_advisories(self, extra: str) -> None:
        spec = _requirement("requests", extra).specifier
        assert not spec.contains("2.32.4")
        assert spec.contains("2.33.0")


# ------------------------------------------------------------------ M5


def _with_cell_text(text: str) -> bytes:
    good = _book([["name", "n"], ["a", 1]])
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(good)) as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            body = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                body = body.replace(b"<v>1</v>", f"<v>{text}</v>".encode())
            dst.writestr(info, body)
    return out.getvalue()


class TestNoCellTextInErrors:
    """Parser and BigQuery error text can quote data; it's left out."""

    def test_unreadable_sheet_reason_has_no_cell_text(self) -> None:
        contract = Contract(
            worksheets=[
                Worksheet(
                    "Events",
                    [Column("name", "string"), Column("n", "integer")],
                )
            ]
        )

        with pytest.raises(ContractError) as info:
            contract.read(_with_cell_text(SECRET), "f.xlsx")

        assert SECRET not in str(info.value)
        assert "ValueError" in str(info.value)
        assert info.value.__cause__ is None  # not in tracebacks either

    def test_load_job_error_message_is_not_repeated(self) -> None:
        job = Mock(job_id="load-9")
        job.result.side_effect = BadRequest(
            f"Could not parse '{SECRET}' as INT64 for field n"
        )

        with pytest.raises(ServiceError, match="load-9") as info:
            wait_for_load(job, "k")

        assert SECRET not in str(info.value)
        assert "bq show -j load-9" in str(info.value)
        assert info.value.__cause__ is None

    def test_failed_job_error_result_message_is_not_repeated(self) -> None:
        client = MagicMock()
        job = Mock(job_id="q-1", state="DONE")
        job.result.side_effect = BadRequest("job failed")
        job.error_result = {
            "reason": "invalidQuery",
            "location": "query",
            "message": f"Bad int64 value: {SECRET}",
        }
        client.query.return_value = job

        with pytest.raises(ServiceError, match="invalidQuery") as info:
            run_query(client, "SELECT 1", MagicMock(), "k")

        assert SECRET not in str(info.value)
        assert "q-1" in str(info.value)


# ------------------------------------------------------------------ M7/L2


def _typed(keys: list[str]) -> BigQueryWarehouse:
    with patch("py_common.adapters.bigquery.bigquery.Client"):
        return BigQueryWarehouse("p", "d", "t", key_columns=keys)


def _raw(tables: dict[str, str]) -> RawJsonWarehouse:
    with patch("py_common.adapters.bigquery_raw.bigquery.Client"):
        return RawJsonWarehouse("p", "d", tables)


KEYED = Contract(
    key_columns=["id"],
    worksheets=[Worksheet("Events", [Column("id", "integer")])],
)
TWO_SHEETS = Contract(
    worksheets=[
        Worksheet("Events", [Column("id", "integer")]),
        Worksheet("Other", [Column("x", "integer")]),
    ]
)


class TestPipelineConfigChecks:
    """Mismatched keys or sheet mappings are refused when built."""

    def test_warehouse_without_the_contract_keys_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="key_columns"):
            Pipeline(Mock(), Mock(), _typed([]), KEYED)

    def test_warehouse_with_other_keys_is_refused(self) -> None:
        contract = Contract(
            key_columns=["id"],
            worksheets=[
                Worksheet(
                    "Events", [Column("id", "integer"), Column("n", "integer")]
                )
            ],
        )
        with pytest.raises(ConfigError, match="'n'"):
            Pipeline(Mock(), Mock(), _typed(["n"]), contract)

    def test_matching_keys_are_accepted(self) -> None:
        assert Pipeline(Mock(), Mock(), _typed(["id"]), KEYED)

    def test_raw_sheet_without_a_table_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="Other"):
            Pipeline(
                Mock(), Mock(), _raw({"Events": "t1"}), TWO_SHEETS, "raw_json"
            )

    def test_raw_table_without_a_sheet_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="Extra"):
            Pipeline(
                Mock(),
                Mock(),
                _raw({"Events": "t1", "Other": "t2", "Extra": "t3"}),
                TWO_SHEETS,
                "raw_json",
            )

    def test_matching_mapping_is_accepted(self) -> None:
        assert Pipeline(
            Mock(),
            Mock(),
            _raw({"Events": "t1", "Other": "t2"}),
            TWO_SHEETS,
            "raw_json",
        )


# ------------------------------------------------------------------ L3


class TestNaiveLeaseTimes:
    """A lock with a time-zone-less time is never taken over."""

    @pytest.mark.parametrize(
        "holder",
        [
            '{"run_id": "x", "expires_at": "2026-01-01T00:00:00"}',
            '{"run_id": "x", "acquired_at": "2026-01-01T00:00:00"}',
        ],
    )
    def test_refused_not_type_error(self, holder: str) -> None:
        bucket = FakeBucket()
        with patch("py_common.adapters.gcs.storage.Client"):
            store = GCSObjectStore("p", "store")
        store.bucket = bucket
        bucket.blob("locks/k").upload_from_string(
            holder, if_generation_match=0
        )

        with pytest.raises(RuntimeError, match="held by another run"):
            with store.lock("locks/k"):
                pass

        assert bucket.objects["locks/k"][0].decode() == holder


# ------------------------------------------------------------------ L4


class TestJsonLoggingReplacesHandlers:
    """Each line is written once, as JSON, unless asked otherwise."""

    @pytest.fixture(autouse=True)
    def restore_root(self) -> Any:
        root = logging.getLogger()
        saved = list(root.handlers), root.level
        yield
        root.handlers[:], root.level = saved

    @pytest.fixture
    def plain(self) -> logging.Handler:
        """A plain-text handler, as logging.basicConfig() would add.

        Added directly: under pytest the root logger already has
        pytest's capture handlers, so basicConfig() would do nothing.
        """
        handler = logging.StreamHandler(io.StringIO())
        logging.getLogger().addHandler(handler)
        return handler

    def test_existing_handlers_are_replaced(
        self, plain: logging.Handler
    ) -> None:
        configure_json_logging(stream=io.StringIO())

        handlers = logging.getLogger().handlers
        assert plain not in handlers
        assert [type(h.formatter) for h in handlers] == [JsonFormatter]

    def test_replace_false_keeps_them(self, plain: logging.Handler) -> None:
        configure_json_logging(stream=io.StringIO(), replace=False)
        configure_json_logging(stream=io.StringIO(), replace=False)

        handlers = logging.getLogger().handlers
        assert plain in handlers
        assert [
            h for h in handlers if isinstance(h.formatter, JsonFormatter)
        ] == [handlers[-1]]


# ------------------------------------------------------------------ L5


def _event(bucket: str, name: str) -> dict[str, Any]:
    import base64
    import json

    data = json.dumps({"bucket": bucket, "name": name, "generation": "7"})
    return {
        "message": {
            "data": base64.b64encode(data.encode()).decode(),
            "attributes": {"eventType": "OBJECT_FINALIZE"},
        }
    }


class TestUploadBucketCheck:
    """Only uploads to the bucket a pipeline reads start it."""

    @pytest.fixture
    def pipeline(self) -> MagicMock:
        with patch("py_common.adapters.gcs.storage.Client"):
            source = GCSSource("p", "hww-landing", "was")
        pipeline = MagicMock()
        pipeline.source = source
        pipeline.run.return_value = []
        return pipeline

    def test_other_bucket_is_ignored(self, pipeline: MagicMock) -> None:
        assert (
            handle_upload(
                _event("elsewhere", "was/x.xlsx"), {"was/": pipeline}
            )
            == []
        )
        pipeline.run.assert_not_called()

    def test_own_bucket_runs(self, pipeline: MagicMock) -> None:
        handle_upload(_event("hww-landing", "was/x.xlsx"), {"was/": pipeline})

        pipeline.run.assert_called_once()


# ------------------------------------------------------------------ L6


def test_live_tests_are_all_marked_integration() -> None:
    """The unit selection never collects a test that calls GCP."""
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            "not integration",
            "tests/integration",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 5, done.stdout + done.stderr  # none collected


# ------------------------------------------------------------------ L9


def test_load_wait_is_bounded_and_cancelled_on_timeout() -> None:
    job = Mock(job_id="load-1")
    job.result.side_effect = TimeoutError("still running")

    with pytest.raises(ServiceError, match="load-1"):
        wait_for_load(job, "k")

    assert job.result.call_args.kwargs["timeout"] == JOB_TIMEOUT_SECONDS
    job.cancel.assert_called_once()


# ------------------------------------------------------------------ L10


class TestOnlyParserErrorsQuarantine:
    """A sheet the parser rejects is the file's fault; nothing else is."""

    def test_memory_error_is_not_blamed_on_the_file(self) -> None:
        with patch("py_common.contract.read_sheet", side_effect=MemoryError):
            with pytest.raises(MemoryError):
                CONTRACT.read(_book([["name"], ["x"]]), "f.xlsx")

    def test_parser_error_quarantines(self) -> None:
        with patch(
            "py_common.contract.read_sheet", side_effect=ValueError("bad")
        ):
            with pytest.raises(ContractError, match="Cannot read worksheet"):
                CONTRACT.read(_book([["name"], ["x"]]), "f.xlsx")
