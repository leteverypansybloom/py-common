"""
CDSC Pipeline Executor

Responsible for:
- Downloading pipeline package
- Extracting package
- Running R scripts
- Returning execution status
"""

import os
import shutil
import subprocess
import tempfile
import zipfile


def download_package(
    storage_client,
    bucket_name,
    repo_name
):

    temp_dir = tempfile.mkdtemp()

    print(f"Created working directory: {temp_dir}")

    local_zip = os.path.join(
        temp_dir,
        "latest.zip"
    )

    blob_path = f"{repo_name}/latest.zip"

    print(f"Downloading package: gs://{bucket_name}/{blob_path}")

    bucket = storage_client.bucket(bucket_name)

    blob = bucket.blob(blob_path)

    blob.download_to_filename(local_zip)

    print("Package download completed")
    print(f"Downloaded package to: {local_zip}")

    return temp_dir, local_zip


def extract_package(
    zip_file,
    temp_dir
):

    print("Extracting package")

    project_dir = os.path.join(
        temp_dir,
        "project"
    )

    with zipfile.ZipFile(zip_file, "r") as archive:
        archive.extractall(project_dir)

    print(f"Package extracted to: {project_dir}")

    return project_dir


def run_r_script(
    storage_client,
    bucket_name,
    repo_name,
    script_name
):

    temp_dir = None

    try:

        temp_dir, zip_file = download_package(
            storage_client,
            bucket_name,
            repo_name
        )

        project_dir = extract_package(
            zip_file,
            temp_dir
        )

        script_path = os.path.join(
            project_dir,
            script_name
        )

        if not os.path.exists(script_path):
            raise FileNotFoundError(
                f"{script_name} not found in package"
            )

        print(f"Found entrypoint: {script_name}")
        print(f"Working directory: {project_dir}")
        print(f"Executing script: {script_name}")

        result = subprocess.run(
            ["Rscript", script_path],
            cwd=project_dir,
            capture_output=True,
            text=True
        )

        print("----- STDOUT -----")
        print(result.stdout)

        print("----- STDERR -----")
        print(result.stderr)

        result.check_returncode()

        print("Pipeline completed successfully")

        return {
            "code": 0
        }

    except Exception as exc:

        print("Pipeline execution failed")
        print(str(exc))

        return {
            "code": 1,
            "error": str(exc)
        }

    finally:

        print("Cleaning up temporary files")

        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)