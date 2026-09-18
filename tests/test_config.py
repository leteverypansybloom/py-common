"""Tests for configuration loading and interpolation."""

import os

import pytest

from py_common.config import ConfigError, interpolate, load_config


def test_interpolate_valid_variable():
    """Replace ${VARIABLE} with environment value."""
    os.environ["TEST_VAR"] = "hello"

    result = interpolate("The value is ${TEST_VAR}.")

    assert result == "The value is hello."


def test_interpolate_multiple_variables():
    """Replace multiple variables in one string."""
    os.environ["FIRST"] = "alpha"
    os.environ["SECOND"] = "beta"

    result = interpolate("${FIRST} and ${SECOND}")

    assert result == "alpha and beta"


def test_interpolate_missing_variable():
    """Raise ConfigError for missing environment variable."""
    # Ensure variable doesn't exist
    os.environ.pop("NONEXISTENT_VAR", None)

    with pytest.raises(ConfigError, match="Missing environment"):
        interpolate("The value is ${NONEXISTENT_VAR}.")


def test_interpolate_non_string():
    """Return non-string values unchanged."""
    result = interpolate(42)

    assert result == 42


def test_load_config_yaml(temp_dir):
    """Load YAML configuration file."""
    config_path = temp_dir / "config.yaml"
    config_path.write_text("""
dev:
  project: my-dev-project
  bucket: dev-bucket
prod:
  project: my-prod-project
  bucket: prod-bucket
""")

    config = load_config(config_path)

    assert config["dev"]["project"] == "my-dev-project"
    assert config["prod"]["bucket"] == "prod-bucket"


def test_load_config_not_found():
    """Raise ConfigError if config file doesn't exist."""
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/path/config.yaml")


def test_load_config_malformed_yaml(temp_dir):
    """Raise ConfigError if YAML is malformed."""
    config_path = temp_dir / "bad.yaml"
    config_path.write_text("invalid: yaml: syntax:")

    with pytest.raises(ConfigError, match="YAML parse error"):
        load_config(config_path)


def test_load_config_empty_file(temp_dir):
    """Load empty YAML file as empty dict."""
    config_path = temp_dir / "empty.yaml"
    config_path.write_text("")

    config = load_config(config_path)

    assert config == {}


def test_require_keys_all_present():
    """No error when all required keys are present."""
    config = {"a": 1, "b": 2, "c": 3}

    # Should not raise
    from py_common.config import require_keys
    require_keys(config, ["a", "b"])


def test_require_keys_missing():
    """Raise ConfigError when required key is missing."""
    config = {"a": 1}

    with pytest.raises(ConfigError, match="Missing configuration keys"):
        from py_common.config import require_keys
        require_keys(config, ["a", "b", "c"])
