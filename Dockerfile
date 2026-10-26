FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

# Pinned third-party dependencies first for better layer caching.
COPY requirements.lock ./
RUN pip install -r requirements.lock

# Optional warehouse drivers: docker build --build-arg EXTRAS="snowflake,bigquery,databricks,mysql" .
ARG EXTRAS=""
COPY pyproject.toml README.md ./
COPY semanticone ./semanticone
RUN if [ -n "$EXTRAS" ]; then pip install ".[${EXTRAS}]"; else pip install --no-deps .; fi

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"

# Migrations run on startup; `semanticone migrate` is also available as a release command.
CMD ["semanticone", "serve"]
