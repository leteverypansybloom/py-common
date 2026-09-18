"""Wire py-common's adapters to this project's configuration.

This module handles all the wiring: load configuration, instantiate
adapters, define contracts, and run the pipeline.

Three switches in config.yaml decide what runs:
- source: 'local' (a folder) or 'sharepoint'
- storage: 'local' (folders) or 'gcp' (Cloud Storage, BigQuery)
- secret_store: 'env' (environment variables) or 'secret_manager'

Each build_* function reads only the section it needs and resolves
its ${ENV_VAR} placeholders at that moment, so a SharePoint check
never needs GCP variables.

Example config.yaml:

    dev:
      source:
        type: local
        folder: /tmp/test_files
      storage:
        type: local
        folder: /tmp/output
      contracts:
        - name: events
          worksheet: Events
          key_columns: [event_id]
    prod:
      source:
        type: sharepoint
        tenant_id: ${SHAREPOINT_TENANT_ID}
        client_id: ${SHAREPOINT_CLIENT_ID}
        secret_name: sharepoint-credential
      storage:
        type: gcp
        project: ${GCP_PROJECT}
        bucket_raw: ${GCS_BUCKET_RAW}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from py_common.adapters import LocalObjectStore, LocalSource
from py_common.config import ConfigError, interpolate, require_keys
from py_common.contract import Column, Contract, Worksheet
from py_common.pipeline import Pipeline
from py_common.ports import ObjectStore, Source

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


@dataclass
class SourceConfig:
    """Configuration for a file source."""

    type: str
    folder: str | None = None
    tenant_id: str | None = None
    client_id: str | None = None
    secret_name: str | None = None
    hostname: str | None = None
    site_path: str | None = None
    library: str | None = None


@dataclass
class StorageConfig:
    """Configuration for object storage."""

    type: str
    folder: str | None = None
    project: str | None = None
    bucket_raw: str | None = None
    bucket_processed: str | None = None


def load_config(path: Path | str) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        path: Path to config.yaml.

    Returns:
        Configuration dict.

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


def build_source(config: dict[str, Any]) -> Source:
    """Instantiate source adapter from configuration.

    Args:
        config: Source configuration dict.

    Returns:
        Source implementation (LocalSource, SharePointSource, etc.).

    Raises:
        ConfigError: Required keys missing or unsupported type.
    """
    source_type = config.get("type")

    if source_type == "local":
        require_keys(config, ["folder"])
        folder = interpolate(str(config["folder"]))
        return LocalSource(folder)

    elif source_type == "sharepoint":
        require_keys(
            config,
            ["tenant_id", "client_id", "secret_name", "hostname",
             "site_path", "library"],
        )
        logger.error(
            "SharePoint source not yet implemented. "
            "Use local source for testing."
        )
        raise NotImplementedError(
            "SharePoint adapter coming in phase 2. "
            "Use 'local' source type for now."
        )

    else:
        raise ConfigError(
            f"Unknown source type: {source_type}. "
            "Supported: 'local', 'sharepoint'"
        )


def build_storage(config: dict[str, Any]) -> ObjectStore:
    """Instantiate storage adapter from configuration.

    Args:
        config: Storage configuration dict.

    Returns:
        ObjectStore implementation (LocalObjectStore, GCSObjectStore).

    Raises:
        ConfigError: Required keys missing or unsupported type.
    """
    storage_type = config.get("type")

    if storage_type == "local":
        require_keys(config, ["folder"])
        folder = interpolate(str(config["folder"]))
        return LocalObjectStore(folder)

    elif storage_type == "gcp":
        require_keys(
            config,
            ["project", "bucket_raw", "bucket_processed"],
        )
        logger.error(
            "GCS storage not yet implemented. "
            "Use local storage for testing."
        )
        raise NotImplementedError(
            "GCS adapter coming in phase 2. "
            "Use 'local' storage type for now."
        )

    else:
        raise ConfigError(
            f"Unknown storage type: {storage_type}. "
            "Supported: 'local', 'gcp'"
        )


def load_contracts(config: dict[str, Any]) -> dict[str, Contract]:
    """Load contract definitions from configuration.

    Args:
        config: Configuration dict with contracts section.

    Returns:
        Dict of contract name -> Contract object.

    Raises:
        ConfigError: Contract definition is invalid.
    """
    contracts_config = config.get("contracts", [])
    contracts = {}

    for contract_cfg in contracts_config:
        name = contract_cfg.get("name")
        if not name:
            raise ConfigError("Contract missing 'name'")

        key_columns = contract_cfg.get("key_columns", [])
        worksheet_name = contract_cfg.get("worksheet")
        columns_cfg = contract_cfg.get("columns", [])

        if not worksheet_name:
            raise ConfigError(
                f"Contract '{name}' missing 'worksheet'"
            )

        columns = [
            Column(
                name=col.get("name"),
                data_type=col.get("data_type", "string"),
                nullable=col.get("nullable", False),
                unique=col.get("unique", False),
            )
            for col in columns_cfg
        ]

        contract = Contract(
            key_columns=key_columns,
            worksheets=[
                Worksheet(
                    name=worksheet_name,
                    columns=columns,
                )
            ],
        )
        contracts[name] = contract

    return contracts


def run_ingestion(
    config_path: Path | str,
    environment: str = "dev",
) -> None:
    """Run the ingestion pipeline.

    Args:
        config_path: Path to config.yaml.
        environment: Environment section to use (dev, prod).

    Raises:
        ConfigError: Configuration is invalid.
        RuntimeError: Pipeline execution failed.
    """
    # Load configuration
    config = load_config(config_path)

    if environment not in config:
        raise ConfigError(
            f"Environment '{environment}' not found in config. "
            f"Available: {', '.join(config.keys())}"
        )

    env_config = config[environment]

    # Build adapters
    logger.info("Building source adapter...")
    source = build_source(env_config.get("source", {}))

    logger.info("Building storage adapter...")
    storage = build_storage(env_config.get("storage", {}))

    # Load contracts
    logger.info("Loading contracts...")
    contracts = load_contracts(env_config)

    if not contracts:
        logger.warning("No contracts defined; cannot validate files")
        return

    # For now, process with first contract
    contract_name = list(contracts.keys())[0]
    contract = contracts[contract_name]

    # Warehouse is not yet implemented; use None for now
    warehouse = None

    logger.info(
        "Starting ingestion (source=%s, storage=%s, contract=%s)",
        env_config.get("source", {}).get("type"),
        env_config.get("storage", {}).get("type"),
        contract_name,
    )

    # Run pipeline
    pipeline = Pipeline(
        source=source,
        store=storage,
        warehouse=warehouse,
        contract=contract,
    )

    results = pipeline.run()

    # Report results
    logger.info("Processing complete: %d files", len(results))
    for result in results:
        logger.info(
            "%s: %s (%d rows, %s)",
            result.item.name,
            result.outcome.value,
            result.rows_processed,
            result.checksum[:8] if result.checksum else "n/a",
        )
        if result.errors:
            for error in result.errors:
                logger.error("  → %s", error)
