# Quick Reference Card

Print this or share with your team.

---

## What Is py-common?

A Python library for Excel file ingestion: discover, validate, store, audit.

**Use when:**
- ✅ Reading Excel files (local, SharePoint, S3)
- ✅ Validating structure (worksheets, columns, types)
- ✅ Needing audit records

**Don't use when:**
- ❌ Reading databases directly
- ❌ Real-time streaming
- ❌ ML pipelines

---

## 60-Second Architecture

```
Excel Files → Discover → Validate → Store → Audit
                (Source)  (Contract)  (Store)
```

Three adapters:
- **Source** (discover files): local folder, SharePoint, S3, ...
- **Store** (keep files): local folder, Cloud Storage, S3, ...
- **Warehouse** (load data): BigQuery, Snowflake, PostgreSQL, ...

---

## Install (5 minutes)

```bash
cd py-common && pip install -e .
cd ../rdd-dst-healthyworkingwales && pip install -e .
```

## Run Tests (2 minutes)

```bash
cd py-common
pytest tests/ -v
# Should see: 17 passed
```

## Process Files (5 minutes)

```bash
cd ../rdd-dst-healthyworkingwales
mkdir -p test_files output
# Add Excel files to test_files/
python main.py --config config/config_dev.yaml --env dev
# Check output/ directory
```

---

## Configuration

### Local (Development)

```yaml
dev:
  source:
    type: local
    folder: ./test_files
  storage:
    type: local
    folder: ./output
  contracts:
    - name: events
      worksheet: Events
      key_columns: [event_id]
      columns:
        - name: event_id
          data_type: string
          nullable: false
```

### Cloud (Production)

```yaml
prod:
  source:
    type: sharepoint
    tenant_id: ${SHAREPOINT_TENANT_ID}
    client_id: ${SHAREPOINT_CLIENT_ID}
  storage:
    type: gcp
    project: ${GCP_PROJECT}
    bucket_raw: ${GCS_BUCKET_RAW}
  contracts:
    - [same as dev]
```

Environment variables are set separately (no secrets in config).

---

## Validation Rules

Contracts check:

1. ✅ Worksheet exists
2. ✅ Required columns exist
3. ✅ No unexpected columns
4. ✅ Data types match (string, integer, float, date)
5. ✅ Required fields not null
6. ✅ Unique columns have no duplicates

Invalid files → quarantined (not loaded, error recorded).

---

## Output Structure

```
output/
├── raw/                  # Original Excel files
├── processed/            # Converted to Parquet
├── quarantine/           # Files that failed validation
└── audit/
    ├── records/          # JSON audit log
    └── checksums/        # Deduplication cache
```

Each audit record includes: filename, version, checksum, row count, errors.

---

## Command Line

```bash
python main.py \
  --config config/config_dev.yaml \
  --env dev \
  --log-level INFO
```

Options:
- `--config` — Path to config file
- `--env` — Environment section (dev, prod, etc.)
- `--log-level` — DEBUG, INFO, WARNING, ERROR

---

## Common Tasks

### Generate Test Fixtures

```bash
python -c "
from py_common.fixtures import make_fixtures
from pathlib import Path
make_fixtures(Path('py-common/tests/data/fixtures'))
"
```

Creates 9 Excel files testing all validation scenarios.

### View Audit Records

```bash
cat output/audit/records/*.json | jq .
```

Shows: filename, outcome (loaded/quarantined), errors.

### Run with Debug Logging

```bash
python main.py --log-level DEBUG
```

See every step of processing.

### Check Test Coverage

```bash
pytest py-common/tests/ --cov=src/py_common --cov-report=html
start htmlcov/index.html
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Module not found" | `pip install -e py-common` |
| Tests fail | Generate fixtures: see "Generate Test Fixtures" above |
| Pipeline doesn't process files | Check `ls test_files/` and `cat config/config_dev.yaml` |
| SharePoint adapter error | It's phase 2; use `source.type: local` for now |
| Audit records empty | Check `ls output/audit/records/` |

---

## Key Concepts

**SourceItem** — A file discovered at the source (name, identity, version).

**Result** — Outcome of processing (loaded, skipped, quarantined, failed).

**Contract** — Validation rules (worksheets, columns, types, nullability).

**Audit** — Record of every attempt (checksum, row count, errors, timestamp).

**Checksum** — SHA-256 hash of file bytes (detects duplicates).

---

## Standards

- **PEP 8**: 79-character lines, strict Python formatting
- **Type hints**: All functions annotated
- **Docstrings**: Google style
- **TDD**: Tests written first; 100% coverage
- **RAP**: Reproducible, auditable, transparent

---

## Documentation

| Document | Read When |
|----------|-----------|
| README.md | You want an overview |
| WALKTHROUGH.md | You want hands-on (30 min) |
| DECISIONS_en.md | You want to understand why |
| SHARING_PLAN.md | You're rolling out to the team |

---

## What's Next

**Phase 1 (now)**: ✅ Local testing, validation, audit  
**Phase 2 (planned)**: 🔄 SharePoint, Cloud Storage, BigQuery adapters  
**Phase 3 (future)**: 🔄 Real-time streams, ML pipelines  

---

## FAQ

**Q: Can I modify it?**  
Yes. Fork it, extend it, add adapters.

**Q: Is it production-ready?**  
Phase 1 (local) is ready. Phase 2 (cloud) is planned.

**Q: How do I add a new adapter?**  
Implement the Source, ObjectStore, or Warehouse protocol. See ports.py.

**Q: Can I use this without YAML?**  
Yes, use the Python API directly. See README.md.

**Q: Where are my files?**  
All in `output/` directory: raw/, processed/, quarantine/, audit/.

---

## One-Page Cheat Sheet

```python
# Load configuration
from py_common.config import load_config
config = load_config("config/config_dev.yaml")

# Define validation rules
from py_common.contract import Column, Contract, Worksheet
contract = Contract(
    key_columns=["id"],
    worksheets=[Worksheet("Data", columns=[
        Column("id", data_type="string", nullable=False, unique=True),
    ])]
)

# Set up adapters
from py_common.adapters import LocalSource, LocalObjectStore
source = LocalSource("test_files")
store = LocalObjectStore("output")

# Run pipeline
from py_common.pipeline import Pipeline
pipeline = Pipeline(source, store, None, contract)
results = pipeline.run()

# Check results
for result in results:
    print(f"{result.item.name}: {result.outcome.value}")
```

---

## Contact

Questions? Start with WALKTHROUGH.md or ask the data team.

---

**Version**: 0.1.0  
**Date**: 2026-09-18  
**License**: MIT
