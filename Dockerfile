# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------- #
# Build stage - compile wheels once so the runtime image needs no toolchain.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install ".[postgres,redis]"

# --------------------------------------------------------------------------- #
# Runtime stage - no compiler, no package manager cache, no root.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PATH="/opt/venv/bin:$PATH" \
    JOBINTEL_STORAGE__DATA_DIR=/data \
    JOBINTEL_STORAGE__RAW_DIR=/data/raw \
    JOBINTEL_STORAGE__PROCESSED_DIR=/data/processed \
    JOBINTEL_STORAGE__ANALYTICS_DIR=/data/analytics

RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 jobintel \
 && useradd --system --uid 10001 --gid jobintel --home /app --shell /usr/sbin/nologin jobintel

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=jobintel:jobintel app ./app
COPY --chown=jobintel:jobintel configs ./configs
COPY --chown=jobintel:jobintel dashboard ./dashboard
COPY --chown=jobintel:jobintel migrations ./migrations
COPY --chown=jobintel:jobintel alembic.ini pyproject.toml README.md ./

RUN mkdir -p /data/raw /data/processed /data/analytics && chown -R jobintel:jobintel /data

USER jobintel
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/live || exit 1

# The API is the default; the worker container overrides the command.
CMD ["uvicorn", "app.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--log-config", "/dev/null"]
