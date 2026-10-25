# Base image for Cloud Run deployments that use py-common.
#
# Installs py-common with the [gcp] extra (GCS and BigQuery
# adapters), pinned by constraints.txt, and runs as a non-root user.
# A solution's image builds FROM this one and adds its own code (and,
# for the upload-triggered service, functions-framework); see
# docs/GCP_SETUP.md, section 4.
#
# The base image is pinned by digest so builds are reproducible.
# Update it deliberately, for Python security fixes:
#   docker pull python:3.11-slim
#   docker inspect --format '{{index .RepoDigests 0}}' python:3.11-slim

ARG PYTHON_IMAGE=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

FROM ${PYTHON_IMAGE} AS builder

# Compilers, in case a pinned dependency has no wheel for this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Build wheels for py-common[gcp] and every dependency, at the
# versions in constraints.txt.
WORKDIR /build
COPY . .
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels \
        --constraint constraints.txt ".[gcp]"

# Runtime stage
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# OpenMP runtime, kept from the previous image for compiled
# dependencies that use it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install only the wheels built above: nothing is resolved from PyPI.
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels \
        "py-common[gcp]" && \
    rm -rf /wheels

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app
USER app
WORKDIR /home/app

# Verify installation
RUN python -c "import py_common, py_common.adapters.bigquery_raw; print(f'py-common {py_common.__version__} installed')"

# Default command (override in child images)
CMD ["python", "-c", "import py_common; print('py-common library ready')"]
