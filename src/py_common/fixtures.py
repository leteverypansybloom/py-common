"""Generate small synthetic Excel fixtures for testing.

Seven fixtures cover the contract validation scenarios:
1. Valid workbook
2. Missing worksheet
3. Missing column
4. Extra column
5. Invalid data type
6. Missing required value
7. Duplicate in unique column

Re-run behaviour (unchanged and changed files) needs no extra
fixtures; tests/unit/test_pipeline.py covers it.
"""

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook


def make_fixtures(output_dir: Path) -> None:
    """Generate the test Excel files, a manifest and a README.

    Args:
        output_dir: Directory to write .xlsx files and manifest.json.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Standard fixture data
    valid_headers = ["event_id", "employer_id", "attendees"]
    valid_data = [
        ["EVT001", "EMP001", 12],
        ["EVT002", "EMP002", 8],
    ]

    cases: dict[str, tuple[str, list[str], list[list[Any]]]] = {
        "valid": ("Events", valid_headers, valid_data),
        "missing_sheet": ("Wrong", valid_headers, valid_data),
        "missing_column": (
            "Events",
            valid_headers[:2],
            [row[:2] for row in valid_data],
        ),
        "extra_column": (
            "Events",
            valid_headers + ["extra"],
            [row + ["x"] for row in valid_data],
        ),
        "invalid_type": (
            "Events",
            valid_headers,
            [["EVT001", "EMP001", "twelve"]],
        ),
        "missing_value": (
            "Events",
            valid_headers,
            [["EVT001", "EMP001", None]],
        ),
        "duplicate": (
            "Events",
            # Just the three expected columns
            valid_headers,
            [
                ["EVT001", "EMP001", 12],
                # Duplicate event_id (should fail)
                ["EVT001", "EMP002", 8],
            ],
        ),
    }

    manifest: dict[str, dict[str, Any]] = {}

    for case_name, (ws_name, headers, rows) in cases.items():
        filename = f"events_{case_name}.xlsx"
        filepath = output_dir / filename

        wb = Workbook()
        ws = wb.active
        # A freshly created Workbook() always has exactly one active
        # sheet; .active is only Optional for a workbook that's had
        # all its sheets removed, which never happens here.
        assert ws is not None
        ws.title = ws_name

        # Write headers
        for col_idx, header in enumerate(headers, start=1):
            ws.cell(row=1, column=col_idx, value=header)

        # Write data rows
        for row_idx, row in enumerate(rows, start=2):
            for col_idx, value in enumerate(row, start=1):
                ws.cell(row=row_idx, column=col_idx, value=value)

        wb.save(filepath)

        # Record in manifest
        manifest[case_name] = {
            "filename": filename,
            "worksheet": ws_name,
            "headers": headers,
            "row_count": len(rows),
            "description": {
                "valid": "Passes all validations",
                "missing_sheet": "Expected worksheet 'Events' not found",
                "missing_column": "Missing required column 'attendees'",
                "extra_column": "Unexpected column 'extra'",
                "invalid_type": "Column 'attendees' has string instead of int",
                "missing_value": "Required column 'attendees' is null",
                "duplicate": "Duplicate value in unique column 'event_id'",
            }.get(case_name, ""),
        }

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, default=str)

    # Write README for testers
    readme_path = output_dir / "README.md"
    with open(readme_path, "w") as f:
        f.write(f"""# Test Fixtures

{len(cases)} small Excel files for validating ingestion rules.

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
""")
