# Multi-stage build for py-common library
# Used by projects that depend on py-common for Cloud Run deployment
# Dependencies are defined in pyproject.toml with optional extras:
#   - Base: pandas, openpyxl, pyyaml, pyarrow
#   - [dev]: pytest, black, ruff, mypy, pre-commit, detect-secrets
#   - [gcp]: google-cloud-storage, google-cloud-bigquery
#   - [sharepoint]: microsoft-graph-core, azure-identity

FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create wheel from pyproject.toml
WORKDIR /build
COPY . .
RUN pip install --upgrade pip setuptools wheel && \
    pip wheel --no-cache-dir --wheel-dir /wheels .

# Runtime stage
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder
COPY --from=builder /wheels /wheels

# Install py-common from wheel
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --no-index --find-links /wheels py-common && \
    rm -rf /wheels

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import py_common; print(py_common.__version__)" || exit 1

# Verify installation
RUN python -c "import py_common; print(f'py-common {py_common.__version__} installed')"

# Default command (override in child images)
CMD ["python", "-c", "import py_common; print('py-common library ready')"]
