# Walkthrough: Testing the Ingestion Framework

Plain-language guide to understanding and testing the py-common ingestion library and the HWW integration example.

**Time to complete**: 30 minutes  
**Prerequisites**: Python 3.11+, pip, git

## What You're Testing

You have two components:

1. **py-common** — A reusable library for file ingestion (validation, storage, auditing)
2. **rdd-dst-healthyworkingwales** — An example project showing how to use py-common

The walkthrough takes you through each piece so you understand how they fit together.

---

## Part 1: Understanding the Architecture (5 minutes)

### The Big Picture

```
You have Excel files somewhere (SharePoint, S3, your laptop)
                    ↓
              [Discover files]
                    ↓
              [Download file]
                    ↓
              [Validate structure and data]
                    ↓
              [Store raw copy]
                    ↓
              [Convert to Parquet]
                    ↓
              [Store processed copy]
                    ↓
              [Load to warehouse (BigQuery, database)]
                    ↓
              [Record audit: filename, checksum, row count, errors]
```

### Three Adapters, Pluggable

py-common doesn't care where files come from or where they go:

| Component | Local (testing) | Production |
|-----------|---|---|
| **Source** (discover files) | LocalSource (your laptop) | SharePointSource (SharePoint Online) |
| **Storage** (store files) | LocalObjectStore (folders) | GCSObjectStore (Google Cloud Storage) |
| **Warehouse** (load data) | (testing only, audit logs) | BigQueryWarehouse (BigQuery) |

For testing, you use local adapters. No cloud credentials needed. Same code runs in production with cloud adapters.

---

## Part 2: Install and Setup (5 minutes)

### 1. Navigate to take5

```bash
cd C:\Users\eoinv\Downloads\take5
```

### 2. Install py-common in development mode

```bash
cd py-common
pip install -e .
```

This makes py-common available to other projects locally.

### 3. Install the HWW project

```bash
cd ..\rdd-dst-healthyworkingwales
pip install -e .
```

Now you can import from both libraries.

### 4. Verify installation

```bash
python -c "import py_common; print(f'py-common {py_common.__version__}')"
python -c "import rdd_dst_healthyworkingwales; print('HWW installed')"
```

You should see version numbers and confirmation messages.

---

## Part 3: Generate Test Fixtures (5 minutes)

Test fixtures are small Excel files demonstrating all validation rules. Generate them:

```bash
cd ..\py-common
python -c "
from py_common.fixtures import make_fixtures
from pathlib import Path
make_fixtures(Path('tests/data/fixtures'))
print('Generated 7 test fixtures in tests/data/fixtures/')
"
```

Open `tests/data/fixtures/manifest.json` to see what was created:

```json
{
  "valid": {
    "filename": "events_valid.xlsx",
    "worksheet": "Events",
    "headers": ["event_id", "employer_id", "attendees"],
    "row_count": 2,
    "description": "Passes all validations"
  },
  "missing_sheet": {
    "description": "Expected worksheet 'Events' not found"
  },
  ...
}
```

Each fixture demonstrates one validation rule. This is how you test:

- ✅ Valid file → Loaded
- ❌ Wrong worksheet → Quarantined
- ❌ Missing column → Quarantined
- ❌ Extra column → Quarantined
- ❌ Wrong data type → Quarantined
- ❌ Null in required field → Quarantined
- ❌ Duplicate in unique column → Quarantined

---

## Part 4: Run Unit Tests (5 minutes)

Tests verify that validation logic works correctly:

```bash
cd ..\py-common
pytest tests/ -v
```

You should see:

```
tests/test_contract.py::test_valid_fixture PASSED
tests/test_contract.py::test_missing_worksheet PASSED
tests/test_contract.py::test_missing_column PASSED
tests/test_contract.py::test_extra_column PASSED
tests/test_contract.py::test_invalid_data_type PASSED
tests/test_contract.py::test_missing_required_value PASSED
tests/test_contract.py::test_duplicate_in_unique_column PASSED
tests/test_config.py::test_interpolate_valid_variable PASSED
...

======================== 17 passed in 1.23s ==========================
```

**What this means**: All validation rules work as designed. Each test file demonstrates one rule and verifies it fails correctly.

### View Coverage

```bash
pytest tests/ --cov=src/py_common --cov-report=html
start htmlcov/index.html
```

Opens a browser showing which lines of code are tested.

---

## Part 5: Process Sample Files Locally (5 minutes)

Now run the full pipeline with test files:

### 1. Prepare test directory

```bash
cd ..\rdd-dst-healthyworkingwales
mkdir -p test_files output
```

### 2. Copy a valid fixture

```bash
copy ..\py-common\tests\data\fixtures\events_valid.xlsx test_files\
```

### 3. Run the pipeline

```bash
python main.py --config config/config_dev.yaml --env dev --log-level DEBUG
```

You should see:

```
2026-09-18 10:30:45 - root - INFO - Building source adapter...
2026-09-18 10:30:45 - root - INFO - Building storage adapter...
2026-09-18 10:30:45 - root - INFO - Loading contracts...
2026-09-18 10:30:45 - root - INFO - Starting ingestion (source=local, storage=local, contract=events)
2026-09-18 10:30:45 - py_common.pipeline - INFO - Loaded events_valid.xlsx (2 rows, 1a2b3c4d)
2026-09-18 10:30:45 - root - INFO - Processing complete: 1 files
2026-09-18 10:30:45 - root - INFO - events_valid.xlsx: loaded (2 rows, 1a2b3c4d)
```

### 4. Check output directory

```bash
tree output
```

You'll see:

```
output/
├── raw/
│   └── C:\Users\eoinv\Downloads\take5\rdd-dst-healthyworkingwales\test_files\events_valid.xlsx
├── processed/
│   └── C:\Users\eoinv\Downloads\take5\rdd-dst-healthyworkingwales\test_files\events_valid.xlsx_1a2b3c4d.parquet
└── audit/
    └── records/
        └── 2026-09-18T10:30:45.123456+00:00_C:\...\events_valid.xlsx.json
```

**What this shows**:
- ✅ File was discovered and downloaded
- ✅ File was validated (no quarantine)
- ✅ Raw Excel copy was stored
- ✅ Converted to Parquet
- ✅ Audit record was written

### 5. Inspect the audit record

```bash
cat output/audit/records/*.json | jq .
```

Shows:

```json
{
  "checksum": "1a2b3c4d...",
  "filename": "events_valid.xlsx",
  "item_identity": "C:\\...\\test_files\\events_valid.xlsx",
  "outcome": "loaded",
  "rows_processed": 2,
  "timestamp": "2026-09-18T10:30:45.123456+00:00",
  "version": "1695034245"
}
```

This is your audit trail: what ran, when, what data, and the result.

---

## Part 6: Test Invalid Files (3 minutes)

Put a broken file in the test folder and watch it quarantine:

```bash
copy ..\py-common\tests\data\fixtures\events_missing_column.xlsx test_files\
python main.py --config config/config_dev.yaml --env dev
```

Output includes:

```
events_missing_column.xlsx: quarantined (0 rows, xyz...)
  → events_missing_column.xlsx: Missing column 'attendees'
```

And the file appears in:

```bash
ls output/quarantine/
```

**What this shows**:
- ✅ Validation catches schema problems
- ✅ File is quarantined, not loaded
- ✅ Error details are recorded
- ✅ Pipeline continues (doesn't fail)

---

## Part 7: Configuration Deep Dive (3 minutes)

### Local Development Config

Open `config/config_dev.yaml`:

```yaml
dev:
  source:
    type: local
    folder: ./test_files        # Where to find Excel files
  storage:
    type: local
    folder: ./output            # Where to store everything
  contracts:
    - name: events
      worksheet: Events         # Required worksheet name
      key_columns: [event_id]   # Used for deduplication
      columns:                  # Required columns
        - name: event_id
          data_type: string
          nullable: false
          unique: true
```

This says:
- "Look for Excel files in `./test_files`"
- "Store copies in `./output`"
- "Validate against Events worksheet with these rules"

No cloud credentials. No fancy environment setup. Just folders.

### Production Config

Open `config/config_prod.yaml`:

```yaml
prod:
  source:
    type: sharepoint
    tenant_id: ${SHAREPOINT_TENANT_ID}      # Loaded from environment
    client_id: ${SHAREPOINT_CLIENT_ID}      # at runtime
  storage:
    type: gcp
    project: ${GCP_PROJECT}
    bucket_raw: ${GCS_BUCKET_RAW}
```

This says:
- "Find SharePoint connection details in environment variables"
- "Same for GCP"
- "Same validation rules as dev"

**Key insight**: Same validation, same business logic. Only the *source* and *destination* change.

---

## Part 8: Understanding Three Switches (2 minutes)

Configuration has three independent switches:

```yaml
source:                    # Where files come from
  type: local             # ← Change this independently
storage:                  # Where files go
  type: local             # ← Change this independently
contracts:                # How to validate
  - name: events          # ← Same rules everywhere
```

This lets a developer:
- ✅ Test SharePoint connectivity without GCP variables
- ✅ Test BigQuery loading with local files
- ✅ Swap from local to cloud one piece at a time
- ✅ Never needs "if running in prod" logic

---

## Part 9: Look at the Code (5 minutes)

### Core Pipeline Logic

Open `py-common/src/py_common/pipeline.py`:

```python
def run(self) -> list[Result]:
    """Process all current files at source."""
    results: list[Result] = []
    for item in self.source.items():          # Discover files
        result = self.process(item)           # Process each one
        results.append(result)
        self._audit(result)                   # Record audit
    return results

def process(self, item: SourceItem) -> Result:
    data = self.source.download(item)         # Download
    checksum = digest(data)                   # Hash
    self.contract.validate(data, item.name)   # Validate
    self.store.put(f"raw/{item.identity}", data)  # Store raw
    df = convert_to_parquet(data)             # Convert
    self.warehouse.load(...)                  # Load
```

This is readable: each step is clear and in order.

### Contract Validation

Open `py-common/src/py_common/contract.py`:

It checks:
1. ✅ All required worksheets exist
2. ✅ All required columns exist
3. ✅ No unexpected columns
4. ✅ Data types match (string, integer, float, date)
5. ✅ Required fields are not null
6. ✅ Unique columns have no duplicates

Each check is in a separate function with clear error messages.

### Configuration + Wiring

Open `rdd-dst-healthyworkingwales/src/rdd_dst_healthyworkingwales/ingest.py`:

It shows:
- `build_source()` — instantiate the right adapter based on config
- `build_storage()` — same for storage
- `load_contracts()` — load validation rules from YAML

Example:

```python
def build_source(config: dict[str, Any]) -> Source:
    if config.get("type") == "local":
        return LocalSource(config["folder"])
    elif config.get("type") == "sharepoint":
        return SharePointSource(config["tenant_id"], ...)
```

Simple: read config, pick adapter, instantiate. No magic.

---

## Part 10: Next Steps

### For the Team

1. **Start here** → Run through this walkthrough
2. **Read the code** → Open py-common/README.md
3. **Understand decisions** → Read py-common/DECISIONS_en.md
4. **Run tests** → `pytest tests/ -v`
5. **Try your own files** → Put them in test_files/ and see what breaks

### For Development

- Phase 1 (now): Local testing with fixtures ✅
- Phase 2 (next): SharePoint adapter for real data
- Phase 3: BigQuery adapter for cloud loading
- Phase 4: Integrate with Cloud Run scheduler

Each phase adds one adapter. Core logic doesn't change.

### For Production

Once adapters exist:

```bash
export SHAREPOINT_TENANT_ID=...
export GCP_PROJECT=...
python main.py --config config/config_prod.yaml --env prod
```

Same code. Different adapters. Different config.

---

## Troubleshooting

### "Module not found: py_common"

You haven't installed py-common:

```bash
cd py-common
pip install -e .
```

### "Configuration file not found"

The config path is wrong. Check:

```bash
ls config/config_dev.yaml
```

If it doesn't exist, create it or use the example.

### Tests fail

Make sure test fixtures were generated:

```bash
python -c "from py_common.fixtures import make_fixtures; make_fixtures('tests/data/fixtures')"
pytest tests/ -v
```

### "NotImplementedError: SharePoint adapter"

SharePoint isn't implemented yet (phase 2). Use `source.type: local` for now.

### Audit records are empty

Check the audit directory:

```bash
find output/audit -name "*.json" -ls
cat output/audit/records/*.json
```

If nothing there, the pipeline ran but wrote nothing. Check log level:

```bash
python main.py --log-level DEBUG
```

---

## Summary

You now understand:

✅ **Architecture**: Source → Validate → Store → Warehouse → Audit  
✅ **Adapters**: Pluggable implementations (local for testing, cloud for production)  
✅ **Configuration**: YAML with environment variables, three independent switches  
✅ **Validation**: Contract-based rules for worksheets, columns, data types, uniqueness  
✅ **Testing**: Fixtures and unit tests covering all scenarios  
✅ **Code**: Clear, readable pipeline with no magic  

The framework is ready for your data engineering team to start using and extending.
