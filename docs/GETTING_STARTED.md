# Getting Started with py-common

**Audience:** a developer who has just cloned this repository (or a
project that depends on it) and wants to run something real, end to
end, before reading anything else.

**How this relates to the other docs in this folder:** this is the
only doc you need to *start*. [`USAGE_GUIDE.md`](USAGE_GUIDE.md) tells
you whether `py-common` is the right tool for a new source, and how to
extend it. [`INGESTION_FRAMEWORK_GUIDE.md`](INGESTION_FRAMEWORK_GUIDE.md)
is the deep reference — every ingestion term defined, and an audit of
exactly what works today versus what's stubbed. Read this walkthrough
first; go to the other two once you know what question you're
actually asking.

Every command and code sample below was run against this repository on
Windows before being written down, and the output shown is the output
it produced.

---

## Contents

1. [Set up your environment](#1-set-up-your-environment)
2. [Generate fixtures and run the test suite](#2-generate-fixtures-and-run-the-test-suite)
3. [The four building blocks, in one paragraph each](#3-the-four-building-blocks-in-one-paragraph-each)
4. [Validate one file directly](#4-validate-one-file-directly)
5. [Wire your first pipeline](#5-wire-your-first-pipeline)
6. [What a re-run does](#6-what-a-re-run-does)
7. [Configuration](#7-configuration)
8. [Next steps](#8-next-steps)

---

## 1. Set up your environment

```shell
python -m venv .venv
# On Mac/Linux: source .venv/bin/activate
.venv\Scripts\activate
pip install -e ".[dev,gcp]"
python -m pre_commit install
```

**Why `gcp` is included:** the unit tests for `GCSObjectStore` and
`gcp_auth` import `google.cloud` directly (mocking the calls, not the
import), so with `.[dev]` alone `pytest` fails to collect them. No GCP
credentials are needed — only the libraries. The optional groups in
`pyproject.toml` are `dev`, `gcp` and `sharepoint`; there is no `all`
extra. Add `sharepoint` once `SharePointSource` is implemented (it
isn't yet — see [Section 3](#3-the-four-building-blocks-in-one-paragraph-each)).

Expect: a clean install with no errors.

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
[`tests/data/fixtures/README.md`](../tests/data/fixtures/README.md)
for what each one tests), plus a `manifest.json` and that `README.md`.

Every test should pass. The tests under `tests/integration/` need live
GCP or SharePoint access; each one skips itself when credentials
aren't available, so they show as `SKIPPED`, not failed. To leave them
out entirely, run `pytest tests/ -m "not integration"`.

## 3. The four building blocks, in one paragraph each

`py-common` is built around small `Protocol` interfaces in
`src/py_common/ports.py`, so the core pipeline never imports a cloud
SDK directly. This section is deliberately short — for the full
feature-by-feature status of each one (what's real, what's stubbed,
what's missing), see
[`INGESTION_FRAMEWORK_GUIDE.md`, Section 5](INGESTION_FRAMEWORK_GUIDE.md#5-feature-by-feature-status).

- **`Source`** (`ports.py`) discovers what files exist and downloads
  one. `LocalSource` (`adapters/local.py`) implements it for a
  folder — this is what you'll use below. `SharePointSource`
  (`adapters/sharepoint.py`) exists as a class but every method
  raises `NotImplementedError` on purpose.
- **`Contract`** (`contract.py`) is a plain Python dataclass
  describing the worksheets, columns, types and uniqueness rules a
  workbook must satisfy. `Contract.validate()` is deterministic: the
  same bytes always produce the same result, pass or fail.
- **`ObjectStore`** (`ports.py`) is immutable storage for raw
  files, processed Parquet, and audit records. `LocalObjectStore`
  (`adapters/local.py`) implements it against a folder;
  `GCSObjectStore` (`adapters/gcs.py`) implements it against a GCS
  bucket. Both accept a repeated `put()` of identical bytes and raise
  `ObjectStoreConflict` if a key already holds *different* bytes.
- **`Warehouse`** (`ports.py`) stages, verifies and loads data into
  a destination table, and records its own audit entry.
  `BigQueryWarehouse` (`adapters/bigquery.py`) implements it: it loads
  into a staging table, checks the row count, then `MERGE`s into the
  final table on the `key_columns` you pass it (normally
  `contract.key_columns`), or appends if you pass none. **There is no
  local, in-memory `Warehouse` in this repository** — you'll write a
  tiny fake yourself in [Section 5](#5-wire-your-first-pipeline),
  which is also the easiest way to learn the protocol.

`Pipeline` (`pipeline.py`) is the one class that ties them together:
discover → download → hash → validate → land raw → convert to
Parquet → load → audit, for every file a `Source` reports.

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
py_common.errors.ContractError: events_missing_column.xlsx [Events]: Missing column 'attendees'
```

Try it against each of the other five fixtures — the error message
always names the exact rule that failed, which is what makes a
quarantined file something a data owner can act on without reading
Python.

## 5. Wire your first pipeline

There's no `Warehouse` you can plug in without cloud credentials (see
[Section 3](#3-the-four-building-blocks-in-one-paragraph-each)), so
write a five-line fake. It's exactly the pattern `CONTRIBUTING.md`'s
"Protocols vs Implementation" section describes: a class satisfies a
`Protocol` by having the right methods, no inheritance required.

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

Expect a logged `Validation failed for ...` warning for each bad
fixture, then one line per fixture:

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

The same flow is covered by the unit tests in
`tests/unit/test_pipeline.py`, one test per outcome.

## 6. What a re-run does

Run the same `pipeline.run()` again over the same folder. Expect
`events_valid.xlsx` to come back `skipped` — its checksum was recorded
under `audit/{checksum}` the first time it loaded (`Pipeline._audit()`),
and `Pipeline.process()` checks that marker before re-validating
(`Pipeline._is_duplicate()`). The six quarantined files, by contrast,
come back `quarantined` again, every time — a checksum marker is only
written on a `LOADED` outcome, so a file that fails validation is
re-read and re-validated on every run until it's fixed or removed from
the source. This is deliberate (see `DECISIONS_en.md`, "Validation
errors quarantine, not fail") but worth knowing before you wonder why a
bad file keeps showing up in the logs.

## 7. Configuration

Configuration is YAML with environment variable interpolation:

```yaml
# config.yaml
dev:
  tenant_id: ${SHAREPOINT_TENANT_ID}
  client_id: ${SHAREPOINT_CLIENT_ID}
  project: dev-project
  bucket: dev-bucket

prod:
  tenant_id: ${SHAREPOINT_TENANT_ID}
  client_id: ${SHAREPOINT_CLIENT_ID}
  project: prod-project
  bucket: prod-bucket
```

Load and use:

```python
from py_common.config import load_config, interpolate, require_keys

config = load_config("config.yaml")
dev_cfg = config["dev"]

# Interpolate environment variables only when section is used
dev_cfg = {k: interpolate(v) if isinstance(v, str) else v
           for k, v in dev_cfg.items()}

require_keys(dev_cfg, ["tenant_id", "client_id", "project"])
```

Expect: `load_config` returns the `${...}` placeholders untouched;
after `interpolate`, `dev_cfg["tenant_id"]` holds the value of
`SHAREPOINT_TENANT_ID` from your environment.

## 8. Next steps

- **Define a contract for your real data**, not the `Events` sample —
  copy the shape in [Section 4](#4-validate-one-file-directly), one
  `Column` per header your source actually produces.
- **Try it against real GCS and BigQuery** — `scripts/demo.py` runs
  the fixtures through `GCSObjectStore` and `BigQueryWarehouse` in a
  development project; read its header for the environment variables
  it needs first. It writes to real cloud resources.
- **Decide if `py-common` is the right fit for your new source at
  all**, and how to add one if it needs an adapter that doesn't exist
  yet (a database, an API, a different file share) — see
  [`USAGE_GUIDE.md`](USAGE_GUIDE.md).
- **Understand exactly what's real versus stubbed** before you build
  on a specific adapter — the feature-by-feature table in
  [`INGESTION_FRAMEWORK_GUIDE.md`, Section 5](INGESTION_FRAMEWORK_GUIDE.md#5-feature-by-feature-status)
  is the source of truth.
- **Follow the TDD/PR workflow** in `CONTRIBUTING.md` once you're
  ready to add code rather than just run it — write the failing test
  first, then the smallest change that passes it.
