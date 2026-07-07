# syntax=docker/dockerfile:1.7

# ─── Stage 1: builder ──────────────────────────────────────────────
# Installs ALL dependencies from pyproject.toml into site-packages.
# We don't strip build tools here because some packages (onnxruntime,
# opencv, rapidocr) may need to compile against system libraries.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System deps for building C extensions (psycopg2, cryptography, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer if pyproject.toml unchanged)
COPY pyproject.toml ./
COPY app/__init__.py app/__init__.py
RUN pip install --upgrade pip setuptools wheel

# Copy the full source and install the package with ALL dependencies
COPY . /build
RUN pip install .

# ─── Stage 2: runtime ──────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    PORT=8000

# Runtime system deps: libpq5 for psycopg2, curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 autopay \
    && useradd  --system --uid 1001 --gid autopay --create-home --shell /bin/bash autopay

WORKDIR /code

# Copy installed packages + entrypoints from the builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code + entrypoint (chmod BEFORE USER switch)
COPY --chown=autopay:autopay . /code
RUN chmod +x /code/scripts/entrypoint.sh

USER autopay

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT:-8000}/healthz || exit 1

ENTRYPOINT ["/code/scripts/entrypoint.sh"]

# Use sh -c so ${PORT} is expanded at runtime (Railway sets PORT)
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --workers 1"]
