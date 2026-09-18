"""Integration tests requiring live cloud services.

Tests in this package are marked with @pytest.mark.integration
and are skipped by default. Run them only after cloud infrastructure
is provisioned and credentials are available.

```bash
pytest tests/integration/ -m integration
```
"""
