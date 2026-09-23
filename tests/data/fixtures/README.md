# Test Fixtures

7 small Excel files for validating ingestion rules.

## Fixtures

| File | Scenario | Expected Outcome |
|------|----------|------------------|
| events_valid.xlsx | Passes all rules | Loaded |
| events_missing_sheet.xlsx | Wrong worksheet name | Quarantined |
| events_missing_column.xlsx | Missing 'attendees' column | Quarantined |
| events_extra_column.xlsx | Extra unexpected column | Quarantined |
| events_invalid_type.xlsx | 'attendees' is string not int | Quarantined |
| events_missing_value.xlsx | Required 'attendees' is null | Quarantined |
| events_duplicate.xlsx | Duplicate in unique 'event_id' | Quarantined |

## Usage

Copy these files to your test SharePoint folder or local test directory.
Each fixture is small (~1 KB). `manifest.json` records each file's
worksheet, headers and row count.

## Regenerating Fixtures

This file is written by `make_fixtures()` in
`src/py_common/fixtures.py`; edit it there, not here, then regenerate:

```python
from py_common.fixtures import make_fixtures
from pathlib import Path

make_fixtures(Path("tests/data/fixtures"))
```
