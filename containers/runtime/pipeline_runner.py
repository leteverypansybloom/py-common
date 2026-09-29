"""
CDSC Runtime Entrypoint

Responsible for:
- Reading Cloud Run arguments
- Determining execution mode
- Calling the pipeline executor
"""

import argparse
import sys

from google.cloud import storage

import pipeline_executor

storage_client = storage.Client()


def parse_args():

    parser = argparse.ArgumentParser(
        description="CDSC Runtime"
    )

    parser.add_argument(
        "--bucket",
        required=True,
        help="GCS code bucket"
    )

    parser.add_argument(
        "--repo",
        required=True,
        help="Pipeline repository name"
    )

    parser.add_argument(
        "--mode",
        choices=["run", "test"],
        default="run",
        help="Execution mode"
    )

    return parser.parse_args()


def main():

    args = parse_args()

    print("Starting pipeline execution")
    print(f"Repository : {args.repo}")
    print(f"Bucket     : {args.bucket}")
    print(f"Mode       : {args.mode}")

    script_name = (
        "run_scripts.R"
        if args.mode == "run"
        else "run_tests.R"
    )

    result = pipeline_executor.run_r_script(
        storage_client=storage_client,
        bucket_name=args.bucket,
        repo_name=args.repo,
        script_name=script_name
    )

    print("Execution result:")
    print(result)

    sys.exit(result["code"])


if __name__ == "__main__":
    main()