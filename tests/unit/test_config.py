"""Tests for configuration loading and interpolation."""

import os
from pathlib import Path

import pytest

from py_common.config import ConfigError, interpolate, load_config


class TestConfigFile:
    """Config files are UTF-8, and the top level must be a mapping."""

    def test_list_root_is_rejected(self, tmp_path: Path) -> None:
        """A YAML list at the top level is a ConfigError.

        Args:
            tmp_path: Isolated test folder.
        """
        path = tmp_path / "c.yaml"
        path.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="mapping"):
            load_config(path)

    def test_utf8_is_read_on_any_platform(self, tmp_path: Path) -> None:
        """Welsh text survives on Windows, whose default isn't UTF-8.

        Args:
            tmp_path: Isolated test folder.
        """
        path = tmp_path / "c.yaml"
        path.write_bytes("name: Iechyd Cyhoeddus – Cymru\n".encode())
        assert load_config(path) == {"name": "Iechyd Cyhoeddus – Cymru"}


class TestInterpolate:
    """Interpolate configuration behaviour."""

    def test_interpolate_valid_variable(self):
        """Replace ${VARIABLE} with environment value."""
        os.environ["TEST_VAR"] = "hello"

        result = interpolate("The value is ${TEST_VAR}.")

        assert result == "The value is hello."

    def test_interpolate_multiple_variables(self):
        """Replace multiple variables in one string."""
        os.environ["FIRST"] = "alpha"
        os.environ["SECOND"] = "beta"

        result = interpolate("${FIRST} and ${SECOND}")

        assert result == "alpha and beta"

    def test_interpolate_missing_variable(self):
        """Raise ConfigError for missing environment variable."""
        # Ensure variable doesn't exist
        os.environ.pop("NONEXISTENT_VAR", None)

        with pytest.raises(ConfigError, match="Missing environment"):
            interpolate("The value is ${NONEXISTENT_VAR}.")

    def test_interpolate_non_string(self):
        """Return non-string values unchanged."""
        result = interpolate(42)

        assert result == 42


class TestLoadConfig:
    """LoadConfig configuration behaviour."""

    def test_load_config_yaml(self, temp_dir):
        """Load YAML configuration file.

        Args:
            temp_dir: Isolated temporary folder.
        """
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

    def test_load_config_not_found(self):
        """Raise ConfigError if config file doesn't exist."""
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path/config.yaml")

    def test_load_config_malformed_yaml(self, temp_dir):
        """Raise ConfigError if YAML is malformed.

        Args:
            temp_dir: Isolated temporary folder.
        """
        config_path = temp_dir / "bad.yaml"
        config_path.write_text("invalid: yaml: syntax:")

        with pytest.raises(ConfigError, match="YAML parse error"):
            load_config(config_path)

    def test_load_config_empty_file(self, temp_dir):
        """Load empty YAML file as empty dict.

        Args:
            temp_dir: Isolated temporary folder.
        """
        config_path = temp_dir / "empty.yaml"
        config_path.write_text("")

        config = load_config(config_path)

        assert config == {}


class TestRequireKeys:
    """RequireKeys configuration behaviour."""

    def test_require_keys_all_present(self):
        """No error when all required keys are present."""
        config = {"a": 1, "b": 2, "c": 3}

        # Should not raise
        from py_common.config import require_keys

        require_keys(config, ["a", "b"])

    def test_require_keys_missing(self):
        """Raise ConfigError when required key is missing."""
        config = {"a": 1}

        with pytest.raises(ConfigError, match="Missing configuration keys"):
            from py_common.config import require_keys

            require_keys(config, ["a", "b", "c"])
