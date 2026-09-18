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

from py_common.errors import IngestionError


class ConfigError(IngestionError):
    """Configuration file is missing or malformed."""

    pass


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
        Parsed configuration dict.

    Raises:
        ConfigError: File not found or YAML is malformed.
    """
    path = Path(path)

    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with open(path) as f:
            config = yaml.safe_load(f)
        return config or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {path}: {e}") from e
    except IOError as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e


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
        raise ConfigError(
            f"Missing configuration keys: {', '.join(missing)}"
        )
