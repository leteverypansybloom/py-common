"""The supported API is what py_common exports."""

import py_common
from py_common import config, errors

EXPECTED = {
    "Pipeline",
    "Contract",
    "Worksheet",
    "Column",
    "SheetRows",
    "GraphJsonLinesContract",
    "ROW_FIELDS",
    "digest",
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


class TestPublicApi:
    """Supported package exports."""

    def test_all_lists_the_supported_api(self) -> None:
        """__all__ names exactly the supported API."""
        assert set(py_common.__all__) == EXPECTED

    def test_every_name_in_all_is_importable(self) -> None:
        """Every name in __all__ exists on the package."""
        for name in py_common.__all__:
            assert getattr(py_common, name) is not None

    def test_config_error_lives_in_errors_and_is_reexported(self) -> None:
        """config.ConfigError is the errors module's, in the hierarchy."""
        assert config.ConfigError is errors.ConfigError
        assert issubclass(errors.ConfigError, errors.IngestionError)

    def test_version_matches_package(self) -> None:
        """The package reports the version in pyproject.toml."""
        assert py_common.__version__ == "0.5.0"
