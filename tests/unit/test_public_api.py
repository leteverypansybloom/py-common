"""The supported API is what py_common exports (review L6)."""

import py_common
from py_common import config, errors

EXPECTED = {
    "Pipeline",
    "Contract",
    "Worksheet",
    "Column",
    "Outcome",
    "Result",
    "SourceItem",
    "Source",
    "ObjectStore",
    "Warehouse",
    "IngestionError",
    "SourceError",
    "VersionMismatch",
    "NotFound",
    "ValidationError",
    "ContractError",
    "ServiceError",
    "ObjectStoreConflict",
    "IndeterminateCommitError",
    "RunFailed",
    "ConfigError",
}


def test_all_lists_the_supported_api() -> None:
    assert set(py_common.__all__) == EXPECTED


def test_every_name_in_all_is_importable() -> None:
    for name in py_common.__all__:
        assert getattr(py_common, name) is not None


def test_config_error_lives_in_errors_and_is_reexported() -> None:
    assert config.ConfigError is errors.ConfigError
    assert issubclass(errors.ConfigError, errors.IngestionError)


def test_version_is_0_2_0() -> None:
    assert py_common.__version__ == "0.2.0"
