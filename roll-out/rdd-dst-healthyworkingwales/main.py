#!/usr/bin/env python3
"""Command-line entry point for ingestion pipeline.

Usage:
    python main.py --config config/config_dev.yaml --env dev
    python main.py --config config/config_prod.yaml --env prod
"""

import argparse
import logging
import sys
from pathlib import Path

from rdd_dst_healthyworkingwales.ingest import run_ingestion

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


def main() -> int:
    """Parse arguments and run ingestion pipeline.

    Returns:
        0 on success, 1 on error.
    """
    parser = argparse.ArgumentParser(
        description="Ingest Excel files to BigQuery"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default="config/config_dev.yaml",
        help="Path to configuration file (default: config/config_dev.yaml)",
    )
    parser.add_argument(
        "--env",
        default="dev",
        help="Environment section to use (dev, prod, etc.)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    args = parser.parse_args()

    # Set logging level
    logging.getLogger().setLevel(args.log_level)

    try:
        run_ingestion(args.config, args.env)
        return 0
    except Exception as e:
        logging.exception("Pipeline failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
