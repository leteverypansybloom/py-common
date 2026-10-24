# Contributing

How to set up, check and change py-common.

## Set up

Clone the repository, then follow
[`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) for the virtual
environment and the test fixtures. Turn on the pre-commit hooks from
the activated venv:

```bash
pre-commit install
```

Before each commit, git then runs Black, Ruff, mypy, a secrets scan
(`detect-secrets`, against `.secrets.baseline`) and the YAML,
large-file, whitespace and merge-conflict checks. Run them all by hand
with `pre-commit run --all-files`.

If detect-secrets flags something that is not a secret, update the
baseline and commit it with your change:

```bash
detect-secrets scan --baseline .secrets.baseline
```

## Checks

Run these before every commit. CI runs the same checks.

```bash
black --check .
ruff check .
mypy src/py_common --strict
pytest -m "not integration"
```

On every push and pull request, GitHub Actions
(`.github/workflows/tests.yml`) runs Ruff, Black, mypy and
`pip-audit` first, as they fail fast, then pytest with coverage, on
Python 3.11 and 3.12.

## Code standards

- PEP 8 with 79-character lines. Black and Ruff must pass (Ruff rules
  E, W, F, I, N, D with the Google docstring convention, UP and B).
- mypy in strict mode for `src/py_common`.
- Type hints on every function.
- Google-style docstrings on modules, classes, functions, fixtures and
  test methods, with `Args:`, `Returns:`, `Yields:` and `Raises:`
  sections where they apply.
- Comments say why, not what.
- No new blanket suppressions: no bare `# noqa`, no `# type: ignore`
  without an error code, no file-wide ignores.

## Tests

- Write the test first. A bug fix comes with a test that fails
  without the fix.
- Unit tests (`tests/unit/`) mock every cloud service. The stateful
  fakes in `tests/unit/_fake_bigquery.py` and `tests/unit/_fake_gcs.py`
  are preferred to a `Mock` where the outcome depends on what was
  stored.
- Tests that need a cloud service are marked
  `@pytest.mark.integration` and skip unless their environment
  variables are set.

Useful variations:

```bash
pytest tests/ --cov=src/py_common --cov-report=html
pytest tests/unit/test_pipeline.py::TestPipelineAuditKey -v
pytest -m integration
```

### BigQuery SQL dry run

`tests/integration/test_bq_dry_run.py` generates every SQL statement
`RawJsonWarehouse` sends (the commit script with its "already
loaded?" check and the inserts) and sends each to BigQuery with
`dry_run=True`: parsed and checked against real
tables, never run, no cost. Run it after any change to adapter SQL,
against a sandbox project:

```bash
gcloud auth application-default login
GCP_PROJECT_ID=my-sandbox pytest -m integration tests/integration/test_bq_dry_run.py
```

It creates a scratch dataset (`py_common_dry_run_<random>`, tables
expiring after a day) and deletes it afterwards, so your account
needs `roles/bigquery.dataEditor` and `roles/bigquery.jobUser` in the
project.

### Live BigQuery tests

`tests/integration/test_bigquery_live.py` runs `RawJsonWarehouse`
in real BigQuery, in an existing test dataset, and checks what lands
(each upload once, reverts, rollback, the "latest upload" query). GitHub Actions runs it nightly and on demand
(`.github/workflows/bigquery-live.yml`); the one-off set-up is in
[`docs/CI_SETUP.md`](docs/CI_SETUP.md). To run it yourself:

```bash
GCP_PROJECT_ID=my-project BQ_TEST_DATASET=py_common_ci \
  pytest -m integration tests/integration/test_bigquery_live.py
```

## Docker image

The `Dockerfile` builds py-common into a base image for projects that
deploy to Cloud Run. It installs py-common with the `gcp` extra (GCS
and BigQuery); a child image adds its own entry point and any further
extras (such as `sharepoint`).

```bash
docker build -t py-common:latest .
```

## Common changes

### A new adapter

1. Create `src/py_common/adapters/<name>.py`, implementing `Source`,
   `ObjectStore` or `Warehouse` from `ports.py`. Adapters satisfy the
   protocol without inheriting from it.
2. Add unit tests in `tests/unit/test_<name>_adapter.py`, with the
   service mocked, and integration tests in `tests/integration/`.
3. Map the service's errors to `py_common.errors` (see `ports.py` for
   which outcome each one gives).
4. Add it to the adapter table in `README.md`.

### A new validation rule

1. Add the test to `tests/unit/test_contract.py`.
2. Implement it in `Contract._validate_worksheet()` (`contract.py`).
3. Add a fixture case to `make_fixtures()` in
   `src/py_common/fixtures.py`, and regenerate the fixtures
   (`docs/GETTING_STARTED.md`, section 2).

### Deprecating something

Keep it working for one minor release and warn:

```python
warnings.warn(
    "old_function is deprecated; use new_function",
    DeprecationWarning,
    stacklevel=2,
)
```

## Decisions and docs

- Record each design decision in `DECISIONS_en.md`, in its format.
- Keep the docs in step with the code: a new public API gets a
  docstring and a `README.md` example; a change to what works updates
  the status tables in `README.md` and
  `docs/INGESTION_FRAMEWORK_GUIDE.md` (section 5); a change to
  anything `docs/GETTING_STARTED.md` runs means re-running its
  snippets.
- Note user-visible changes in `CHANGELOG.md`.

## Commits and reviews

- One commit per logical change. The summary is under 50 characters,
  in the imperative ("Add ...", "Fix ..."); the body says why.
- Every change reaches `main` through a pull request reviewed by
  someone other than its author. The description says what changed,
  why, how to test it, and any breaking change.

## Licence

Contributions are licensed under the MIT License, as the project is.
