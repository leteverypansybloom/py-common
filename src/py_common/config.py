"""Configuration loading with environment variable interpolation.

YAML files may contain ${VARIABLE_NAME} placeholders that are
replaced at load time. Variables are resolved only when a section
is used, so missing GCP variables don't block SharePoint checks.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml

# ConfigError lives in py_common.errors with the other exceptions;
# it is re-exported here so `from py_common.config import ConfigError`
# keeps working.
from py_common.errors import ConfigError

__all__ = ["ConfigError", "interpolate", "load_config", "require_keys"]


def interpolate(text: str) -> str:
    """Replace ${VAR_NAME} placeholders with environment values.

    Args:
        text: String possibly containing ${VAR_NAME} patterns.

    Returns:
        String with all variables replaced.

    Raises:
        ConfigError: A required variable is not set.
    """
    if not isinstance(text, str):
        return text

    pattern = r"\$\{([A-Z_][A-Z0-9_]*)\}"
    missing = []

    def replace_var(match: re.Match[str]) -> str:
        """Swap one ${VAR} for its value, noting it if it isn't set.

        Args:
            match: The ${VAR} found in the text.

        Returns:
            The variable's value, or the ${VAR} unchanged if it isn't
            set.
        """
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            missing.append(var_name)
            return match.group(0)
        return value

    result = re.sub(pattern, replace_var, text)

    if missing:
        raise ConfigError(
            f"Missing environment variables: {', '.join(sorted(set(missing)))}"
        )

    return result


def load_config(path: Path | str) -> dict[str, Any]:
    """Load YAML configuration file.

    Args:
        path: Path to .yaml or .yml file.

    Returns:
        Parsed configuration dict ({} for an empty file).

    Raises:
        ConfigError: File not found, YAML is malformed, or its top
            level isn't a mapping.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        # UTF-8 always: the platform default (cp1252 on Windows)
        # would misread non-ASCII names such as Welsh ones.
        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {path}: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ConfigError(
            f"{path}: top level must be a mapping, got "
            f"{type(config).__name__}"
        )
    return config


def require_keys(config: dict[str, Any], keys: list[str]) -> None:
    """Verify config dict has all required keys.

    Args:
        config: Configuration dict.
        keys: Required keys.

    Raises:
        ConfigError: A required key is missing.
    """
    missing = [k for k in keys if k not in config]
    if missing:
        raise ConfigError(f"Missing configuration keys: {', '.join(missing)}")
