# Contributing to py-common

Thank you for contributing! This guide explains how to develop, test, and submit changes.

## Getting Started

### 1. Clone and Install

```bash
git clone <repository>
cd py-common
python -m venv .venv
# On Mac/Linux: source .venv/bin/activate
.venv\Scripts\activate
pip install -e ".[all,dev]"
python -m pre_commit install
pytest
```

`pytest` should end with every test passing and none skipped except
the ones marked `integration` (those need live SharePoint or GCP and
only run when you set `RUN_INTEGRATION_TESTS=1`).

## Development Workflow

### Write Tests First (TDD)

All new functionality requires tests. Write the test before the code:

```python
# tests/test_myfeature.py
def test_my_feature():
    """Describe what should happen."""
    result = my_function(input_data)
    assert result == expected_output
```

Then implement:

```python
# src/py_common/mymodule.py
def my_function(input_data):
    """One-line description.
    
    Args:
        input_data: What it is.
    
    Returns:
        What it returns.
    """
    # Implementation
    return result
```

### Code Standards

- **PEP 8**: 79-character lines (enforced by `black`)
- **Type hints**: All functions must be annotated
- **Docstrings**: Google style, one per function/class
- **No unused code**: Delete or mark `@deprecated`
- **Comments**: Only on "why", not "what" (code should be obvious)

### Run Tests Locally

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=src/py_common --cov-report=html

# Run only unit tests (skip integration)
pytest tests/ -m "not integration"

# Run specific test
pytest tests/test_contract.py::test_valid_fixture -v
```

All tests must pass before submitting a pull request.

### Format Code

Pre-commit hooks run `black` and `ruff` automatically. To format manually:

```bash
black --line-length=79 src/ tests/
ruff check --fix src/ tests/
```

### Check Types

```bash
mypy src/py_common --strict
```

All type errors must be resolved.

## Commit Guidelines

### Message Format

```
Brief summary (under 50 chars, imperative mood)

Optional detailed explanation if needed.
- Bullet points okay
- Be specific about "why"

Closes #123 (if fixing an issue)
```

Examples:

✅ Good:
```
Add validation for duplicate values in unique columns

Implements check to detect duplicate entries in columns marked
as unique. Raises ValidationError with clear error message listing
which rows have duplicates.
```

❌ Bad:
```
Fixed bug
Updated code
New feature
```

### When to Commit

Commit logically related changes together:
- ✅ One feature per commit
- ❌ Ten unrelated changes in one commit

### Before Pushing

```bash
# Verify tests pass
pytest tests/ -v

# Verify formatting
black --check --line-length=79 src/ tests/

# Verify no secrets
detect-secrets scan

# Verify types
mypy src/py_common --strict
```

## Pull Request Process

1. **Create a branch** from `develop`:
   ```bash
   git checkout develop
   git pull
   git checkout -b feature/my-feature
   ```

2. **Make changes** following the workflow above

3. **Write a clear PR description** explaining:
   - What changed and why
   - How to test it
   - Any breaking changes

4. **Request review** from at least one other developer

5. **Address feedback** with new commits (don't squash)

6. **Merge** once approved (use "Squash and merge" if many small commits)

## Architecture Guidelines

### Protocols vs Implementation

Use Python Protocols for interfaces that adapters implement:

```python
from typing import Protocol

class MyAdapter(Protocol):
    """Interface for adapters."""
    
    def do_thing(self) -> str:
        """Implementations must define this."""
        ...
```

Implementations satisfy the protocol without inheriting:

```python
class LocalAdapter:
    def do_thing(self) -> str:
        return "done"

# LocalAdapter satisfies MyAdapter protocol ✓
```

### Error Handling

Use custom exceptions for expected errors:

```python
from py_common.errors import ValidationError

try:
    validate_data(data)
except ValidationError as e:
    # Handle validation issue
    log_and_quarantine(e)
```

Do NOT catch and swallow errors silently.

### Configuration

Configuration should:
- Be external (YAML, environment variables)
- Not contain secrets (use Secret Manager names instead)
- Be interpolated only when used
- Be documented with examples

## Testing Guidelines

### Unit Tests

Test one function in isolation:

```python
def test_validates_required_column():
    """Test that validation catches missing required column."""
    contract = Contract(columns=[Column("id", nullable=False)])
    with pytest.raises(ValidationError):
        contract.validate(excel_without_id, "test.xlsx")
```

### Integration Tests

Mark tests requiring cloud services:

```python
@pytest.mark.integration
def test_sharepoint_connectivity():
    """Test live SharePoint connection."""
    # Requires SHAREPOINT_TENANT_ID env var
    ...
```

Skip by default:

```bash
# Skip integration tests
pytest tests/ -m "not integration"

# Run only integration tests
pytest tests/ -m "integration"
```

### Fixtures

Use pytest fixtures in `conftest.py`:

```python
@pytest.fixture
def events_contract():
    """Standard contract for testing."""
    return Contract(...)
```

Reuse across tests instead of duplicating setup.

## Documentation

Update documentation when:
- Adding a new public API (docstring + README example)
- Changing configuration format (update config examples)
- Making architectural decisions (add to DECISIONS_en.md)
- Adding a feature (update WALKTHROUGH.md if applicable)

## Common Tasks

### Add a New Adapter

1. Create `src/py_common/adapters/myadapter.py`
2. Implement the protocol (Source, ObjectStore, or Warehouse)
3. Add tests in `tests/test_adapters.py`
4. Add integration tests in `tests/integration/test_myadapter.py`
5. Document in README.md

### Add a New Validation Rule

1. Add test to `tests/test_contract.py`
2. Implement in `Contract.validate()`
3. Create fixture in `scripts/make_fixtures.py` demonstrating the rule
4. Regenerate fixtures
5. Update contract.py docstring

### Deprecate Something

```python
import warnings

def old_function():
    """DEPRECATED: Use new_function instead."""
    warnings.warn(
        "old_function is deprecated; use new_function",
        DeprecationWarning,
        stacklevel=2
    )
    # Implementation
```

## Getting Help

- **Questions?** Ask in an issue or pull request discussion
- **Stuck?** Look at existing tests and examples
- **Design question?** See DECISIONS_en.md for rationale

## Code Review Checklist

When reviewing, check:

- ✅ Tests pass (`pytest tests/ -v`)
- ✅ Code formatted (`black --check`)
- ✅ No linting issues (`ruff check`)
- ✅ Types correct (`mypy --strict`)
- ✅ New tests for new functionality
- ✅ Docstrings are clear and complete
- ✅ No hardcoded values or secrets
- ✅ Commit messages are clear
- ✅ PR description explains "why"

## Standards

This project follows:

- **PEP 8**: Python style guide (79-char lines)
- **RAP**: Reproducible, Auditable, Transparent
- **TDD**: Tests written before code
- **Google Style**: Docstring format

## License

All contributions are licensed under the MIT License. By submitting, you agree to license your work under the same terms.

---

Thank you for contributing to py-common! 🎉
