# CDSC Containers

## Overview

cdsc-containers is the central runtime repository for CDSC Cloud Run container images.

The repository provides:

- Shared runtime code used by all CDSC speciality groups
- Automated container image builds
- Speciality group specific dependency management
- Automatic publishing to GCP Artifact Registry

The repository does not contain analytic logic, pipeline code, or PII.

All pipeline code remains within speciality group owned repositories.

# Architecture

```text
1. Pipeline Repository
2. Reusable CDSC CI/CD
3. Build latest.zip
4. Latest code synced to GCS Bucket
5. Cloud Run Job
6. CDSC SG specific container image
7. Run pipeline_runner.py inside container
8. Run pipeline_executor.py inside container
9. Execute pipeline entrypoint script run_scripts.R
10. Output in BigQuery / GCS
```

# Repository Structure

```text
cdsc-containers
│
├── runtime
│   ├── Dockerfile
│   ├── pipeline_runner.py
│   └── pipeline_executor.py
│
├── speciality_groups
│   ├── edge
│   ├── vpdpd
│   ├── genomics
│   └── rsv
│
├── build
│
└── .github
    └── workflows
        └── build-runtime.yml
```

---

# Runtime Components

## runtime/Dockerfile

Responsible for:

- Creating the runtime container
- Installing R
- Installing Python
- Installing Google Cloud SDK
- Installing speciality group specific dependencies

This file is shared by all runtimes.

Changes here cause all speciality group images to rebuild.

Examples:

- Updating Ubuntu version
- Adding system libraries
- Updating container build logic

## runtime/pipeline_runner.py

Runtime entrypoint.

Responsibilities:

- Parse Cloud Run arguments
- Determine execution mode
- Call pipeline executor
- Return appropriate exit code

Cloud Run starts execution here.

## runtime/pipeline_executor.py

Pipeline execution engine.

Responsibilities:

- Download latest pipeline package
- Extract ZIP package
- Locate run_scripts.R
- Execute R script
- Capture logs
- Return success or failure

# Speciality Groups

Each speciality group has its own dependency definition for small independent container image builds and team specific dependencies.

Example:

```text
speciality_groups
└── edge
    ├── r-dependencies.txt
    └── python-dependencies.txt
```

# Dependency Files

## r-dependencies.txt

List of R packages installed into the runtime.

Example: DBI, bigrquery

## python-dependencies.txt

List of Python packages installed into the runtime.

Example: google-cloud-storage

# Container (Docker) Images

The workflow creates one image per speciality group and publishes it to Artifact Registry.

Example: cdsc-edge-img-dev


---

# Build Behaviour

## Dependency Change in One SG

Example: Added package tidyverse in: speciality_groups/edge/r-dependencies.txt

Result: Only cdsc-edge-img-dev rebuilds with tidyverse added

## Runtime Code Change

Example: Code change in runtime/pipeline_executor.py

Result: All runtime images rebuild


Because all speciality groups share the same runtime code.

# Updating Runtime Logic

Changes to runtime logic in pipeline_runner.py / pipeline_executor.py / Dockerfile affect every speciality group.

Any modification to these files causes all speciality group runtime images to rebuild.

# GitHub Configuration

Required GitHub Secret: GCP_SA_KEY, GCP_PROJECT, ARTIFACT_REGISTRY_LOCATION, ARTIFACT_REGISTRY_REPOSITORY


# Cloud Run Usage

Example:

```bash
python pipeline_runner.py --bucket cdsc-code-dev --repo cdsc-edge-sarscov --mode run
```

Arguments:

1. bucket: GCS code bucket
2. repo: Pipeline repository name
3. mode: run or test

The runtime expects run_scripts.R as the standard pipeline entrypoint and run_tests.R for test execution.

---

# Maintenance Guide

# How to add new dependencies for eg: EDGE container?

1. Add the required package in speciality_groups/edge/r-dependencies.txt
2. Commit and merge.
3. GitHub Actions automatically builds, pushes and tags the EDGE container image.

# How to add a new speciality group eg: HARP

1. Create speciality_groups/harp
2. Add speciality_groups/harp/r-dependencies.txt
3. Update workflow matrix: ["edge","vpdp","harp"]
4. Commit and merge.
5. A new container image will be built and pushed cdsc-harp-img-dev


---

# Notes

- All runtime images are built automatically through GitHub Actions.
- Manual docker build and docker push commands are not required.
- All CDSC R pipeline repositories should use run_scripts.R as the primary entrypoint.
- Changes to dependency files rebuild only the affected speciality group image.
- Changes to shared runtime files rebuild all speciality group images.
- Container images are intended to provide a consistent execution environment across CDSC speciality groups while allowing independent dependency management.
