# Getting Started with py-common

**Audience:** a developer who has just cloned this repository (or a
project that depends on it) and wants to run something real, end to
end, before reading anything else.

**How this relates to the other docs in this folder:** this is the
only doc you need to *start*. [`USAGE_GUIDE.md`](USAGE_GUIDE.md) tells
you whether `py-common` is the right tool for a new source, and how to
extend it. [`INGESTION_FRAMEWORK_GUIDE.md`](INGESTION_FRAMEWORK_GUIDE.md)
is the deep reference — every ingestion term defined, and a
file-and-line audit of exactly what works today versus what's stubbed.
Read this walkthrough first; go to the other two once you know what
question you're actually asking.

Every command and code sample below was run against this repository on
2026-09-21 before being written down — including the two problems in
[Known issues](#known-issues-youll-hit-immediately), which are real,
reproduced failures, not guesses.

---

## Contents

1. [Set up your environment](#1-set-up-your-environment)
2. [Generate fixtures and run the test suite](#2-generate-fixtures-and-run-the-test-suite)
3. [The four building blocks, in one paragraph each](#3-the-four-building-blocks-in-one-paragraph-each)
4. [Validate one file directly](#4-validate-one-file-directly)
5. [Wire your first pipeline](#5-wire-your-first-pipeline)
6. [Known issues you'll hit immediately](#known-issues-youll-hit-immediately)
7. [What a re-run does](#what-a-re-run-does)
8. [Next steps](#next-steps)

---

## 1. Set up your environment

```shell
python -m venv .venv
# On Mac/Linux: source .venv/bin/activate
.venv\Scripts\activate
pip install -e ".[dev]"
python -m pre_commit install
```

**Note on extras:** the root `README.md` and `CONTRIBUTING.md` show
`pip install -e ".[all,dev]"`. There is no `all` extra defined in
`pyproject.toml` — the real optional groups are `dev`, `gcp` and
`sharepoint` (`pyproject.toml:20-37`). `pip install -e ".[all,dev]"`
will fail with "extra all not found". Use `.[dev]` to work through
this guide; add `.[dev,gcp]` once you're touching `GCSObjectStore` or
`BigQueryWarehouse`, and `.[dev,gcp,sharepoint]` once `SharePointSource`
is implemented (it isn't yet — see [Section 3](#3-the-four-building-blocks-in-one-paragraph-each)).

Expect: a clean install with no errors. `pip install -e ".[all,dev]"`,
if you try it as written elsewhere, fails immediately — that's the
bug above, not something wrong with your machine.

## 2. Generate fixtures and run the test suite

```shell
python -c "
from py_common.fixtures import make_fixtures
from pathlib import Path
make_fixtures(Path('tests/data/fixtures'))
"
pytest tests/ -v
```

Expect: seven `.xlsx` files under `tests/data/fixtures/` (one valid,
six that each break a different validation rule — see
`src/py_common/fixtures.py:1-13` for the full list), and every test
passing except any marked `integration` (those need live SharePoint or
GCP credentials and are skipped by default).

## 3. The four building blocks, in one paragraph each

`py-common` is built around four small `Protocol` interfaces in
`src/py_common/ports.py`, so the core pipeline never imports a cloud
SDK directly. This section is deliberately short — for the full
feature-by-feature status of each one (what's real, what's stubbed,
what's missing), see
[`INGESTION_FRAMEWORK_GUIDE.md`, Section 5](INGESTION_FRAMEWORK_GUIDE.md#5-feature-by-feature-status).

- **`Source`** (`ports.py:14`) discovers what files exist and downloads
  one. `LocalSource` (`adapters/local.py:21`) implements it for a
  folder — this is what you'll use below. `SharePointSource`
  (`adapters/sharepoint.py:21`) exists as a class but every method
  raises `NotImplementedError` on purpose; its docstring points you
  back here.
- **`Contract`** (`contract.py:46`) is a plain Python dataclass
  describing the worksheets, columns, types and uniqueness rules a
  workbook must satisfy. `Contract.validate()` is deterministic: the
  same bytes always produce the same result, pass or fail.
- **`ObjectStore`** (`ports.py:48`) is immutable storage for raw
  files, processed Parquet, and audit records. `LocalObjectStore`
  (`adapters/local.py:87`) implements it against a folder;
  `GCSObjectStore` (`adapters/gcs.py`) implements it against a GCS
  bucket (unit-tested against mocks, not yet run against a real
  bucket in CI).
- **`Warehouse`** (`ports.py:97`) stages, verifies and loads data into
  a destination table, and is expected to record its own audit entry.
  `BigQueryWarehouse` (`adapters/bigquery.py`) implements it — with one
  known gap (it appends instead of merging on `key_columns`; see
  `adapters/bigquery.py:154-156` and the Framework Guide's Section 9,
  item 1). **There is no local, in-memory `Warehouse` implementation
  in this repository today**, even though the module docstring at the
  top of `adapters/local.py:5` mentions one — you'll write a tiny fake
  yourself in [Section 5](#5-wire-your-first-pipeline), which is also
  the easiest way to learn the protocol.

`Pipeline` (`pipeline.py:37`) is the one class that ties all four
together: discover → download → hash → validate → land raw → convert
to Parquet → load → audit, for every file a `Source` reports.

## 4. Validate one file directly

Before touching the pipeline, validate a single workbook to see what a
`Contract` actually checks:

```python
from pathlib import Path
from py_common.contract import Column, Contract, Worksheet

contract = Contract(
    key_columns=["event_id"],
    worksheets=[
        Worksheet(
            name="Events",
            columns=[
                Column("event_id", data_type="string",
                       nullable=False, unique=True),
                Column("employer_id", data_type="string",
                       nullable=False),
                Column("attendees", data_type="integer",
                       nullable=False),
            ],
        )
    ],
)

good = Path("tests/data/fixtures/events_valid.xlsx").read_bytes()
print(contract.validate(good, "events_valid.xlsx"))

bad = Path("tests/data/fixtures/events_missing_column.xlsx").read_bytes()
contract.validate(bad, "events_missing_column.xlsx")
```

Expect:

```
2
```

...then, on the second call:

```
py_common.errors.ContractError: events_missing_column.xlsx: Missing column 'attendees'
```

Try it against each of the other six fixtures — the error message
always names the exact rule that failed (`contract.py:92-185`), which
is what makes a quarantined file something a data owner can act on
without reading Python.

## 5. Wire your first pipeline

This is the part the Quick Start in the root `README.md` doesn't show,
because there's no `Warehouse` you can plug in without cloud
credentials (see [Section 3](#3-the-four-building-blocks-in-one-paragraph-each)).
Writing a five-line fake is the fastest way to see the whole pipeline
run, and it's exactly the pattern `CONTRIBUTING.md`'s "Protocols vs
Implementation" section describes: a class satisfies a `Protocol` by
having the right methods, no inheritance required.

```python
from pathlib import Path
import tempfile

from py_common.adapters import LocalObjectStore, LocalSource
from py_common.contract import Column, Contract, Worksheet
from py_common.fixtures import make_fixtures
from py_common.pipeline import Pipeline

tmp = Path(tempfile.mkdtemp())
fixtures_dir = tmp / "fixtures"
make_fixtures(fixtures_dir)


class FakeWarehouse:
    """In-memory stand-in for a real Warehouse (e.g. BigQuery)."""

    def __init__(self):
        self.loaded = []

    @property
    def identity(self):
        return "fake-warehouse"

    def load(self, key, checksum, data, rows):
        self.loaded.append((key, checksum, rows))


contract = Contract(
    key_columns=["event_id"],
    worksheets=[Worksheet(name="Events", columns=[
        Column("event_id", data_type="string", nullable=False,
               unique=True),
        Column("employer_id", data_type="string", nullable=False),
        Column("attendees", data_type="integer", nullable=False),
    ])],
)

pipeline = Pipeline(
    source=LocalSource(fixtures_dir),
    store=LocalObjectStore(tmp / "store"),
    warehouse=FakeWarehouse(),
    contract=contract,
)
results = pipeline.run()

for r in results:
    print(r.item.name, r.outcome.value, r.rows_processed)
```

**On Windows, run the [Known issues](#known-issues-youll-hit-immediately)
workarounds below first** — as written, this exact snippet currently
crashes with an `OSError` partway through, for reasons that have
nothing to do with your setup.

With the workarounds applied, expect one line per fixture:

```
events_duplicate.xlsx quarantined 0
events_extra_column.xlsx quarantined 0
events_invalid_type.xlsx quarantined 0
events_missing_column.xlsx quarantined 0
events_missing_sheet.xlsx quarantined 0
events_missing_value.xlsx quarantined 0
events_valid.xlsx loaded 2
```

...and, under `tmp / "store"`, a `raw/` copy of every file (even the
quarantined ones), a `quarantine/` copy of the six bad ones, a
`processed/events_valid.xlsx.parquet`, and one JSON audit record per
file under `audit/records/`.

## Known issues you'll hit immediately

Both of these were found while writing this guide, by actually running
the code above on Windows — not by reading it. Neither has a GitHub
issue yet; if you're the one who hits them for real, that's the
moment to raise one.

**1. `LocalSource.identity` breaks any `ObjectStore` key built from
it, on Windows.**
`LocalSource.items()` sets `identity=str(path)`
(`adapters/local.py:51`) — the full absolute path, e.g.
`C:\Users\...\fixtures\events_valid.xlsx`. `pipeline.py` then builds
object-store keys directly from it, e.g.
`f"raw/{item.identity}"` (`pipeline.py:152`). On Windows this produces
a key containing a drive letter and colon, and
`LocalObjectStore.put()` tries to `mkdir` a literal `C:` path segment,
which is invalid — `OSError: [WinError 123]`. Workaround: wrap
`LocalSource` so `identity` is just the filename, and rebuild the full
path in `download()`:

```python
from py_common.model import SourceItem

class RelativeLocalSource(LocalSource):
    def items(self):
        for item in super().items():
            item.identity = Path(item.identity).name
            yield item

    def download(self, item):
        full = SourceItem(
            name=item.name,
            identity=str(self.folder / item.identity),
            version=item.version,
            size_bytes=item.size_bytes,
        )
        return super().download(full)
```

Use `RelativeLocalSource(fixtures_dir)` in place of `LocalSource(...)`
in [Section 5](#5-wire-your-first-pipeline). This also happens to be
closer to how a real adapter should behave — `identity` is meant to be
a stable ID, not a filesystem path (compare `SharePointSource`, where
identity would naturally be a Graph item ID, not a local path at all).

**2. `Pipeline._audit()` builds a filename from an ISO timestamp,
which contains colons.**
`pipeline.py:239-241` builds the audit key as
`f"audit/records/{record.timestamp.isoformat()}_{identity}.json"`.
`datetime.isoformat()` includes colons (`2026-09-21T13:26:52.467023
+00:00`), which are invalid in a Windows filename, so
`LocalObjectStore.put()` raises `OSError: [Errno 22] Invalid argument`
on the very first audit write of every run. This only affects a
*local filesystem* object store — `GCSObjectStore` would accept the
same key without complaint, since GCS blob names don't have this
restriction. Workaround for local Windows development, wrap
`LocalObjectStore` to sanitise the key:

```python
class WindowsSafeLocalObjectStore(LocalObjectStore):
    @staticmethod
    def _safe(key):
        return key.replace(":", "-")

    def get(self, key):
        return super().get(self._safe(key))

    def put(self, key, data):
        return super().put(self._safe(key), data)
```

Use `WindowsSafeLocalObjectStore(tmp / "store")` in place of
`LocalObjectStore(...)`.

With both workarounds in place, the pipeline run in
[Section 5](#5-wire-your-first-pipeline) produces exactly the output
shown there. Neither issue affects `pytest tests/` today only because
**no test in this repository exercises `Pipeline` end to end** — there
is no `test_pipeline.py`; `Pipeline`, `LocalSource` and
`LocalObjectStore` are each unit-tested, but never together. That gap
is worth closing before relying on this walkthrough for anything
beyond onboarding.

## What a re-run does

Run the same `pipeline.run()` again over the same folder. Expect
`events_valid.xlsx` to come back `skipped` — its checksum was recorded
under `audit/{checksum}` the first time it loaded
(`pipeline.py:246-247`), and `Pipeline.process()` checks that marker
before re-validating (`pipeline.py:116-131`). The six quarantined
files, by contrast, come back `quarantined` again, every time — a
checksum marker is only written on a `LOADED` outcome, so a file that
fails validation is re-read and re-validated on every run until it's
fixed or removed from the source. This is deliberate (see
`DECISIONS_en.md`, "Validation errors quarantine, not fail") but worth
knowing before you wonder why a bad file keeps showing up in the logs.

## Next steps

- **Define a contract for your real data**, not the `Events` sample —
  copy the shape in [Section 4](#4-validate-one-file-directly), one
  `Column` per header your source actually produces.
- **Decide if `py-common` is the right fit for your new source at
  all**, and how to add one if it needs an adapter that doesn't exist
  yet (a database, an API, a different file share) — see
  [`USAGE_GUIDE.md`](USAGE_GUIDE.md).
- **Understand exactly what's real versus stubbed** before you build
  on a specific adapter — the feature-by-feature table in
  [`INGESTION_FRAMEWORK_GUIDE.md`, Section 5](INGESTION_FRAMEWORK_GUIDE.md#5-feature-by-feature-status)
  is the source of truth, kept current with file:line citations.
- **Follow the TDD/PR workflow** in `CONTRIBUTING.md` once you're
  ready to add code rather than just run it — write the failing test
  first, then the smallest change that passes it.
