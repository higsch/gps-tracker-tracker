# syntax=docker/dockerfile:1
#
# Build on the Pi itself (`docker compose build`) and this is a native arm64
# build. DuckDB publishes aarch64 wheels but no armv7 ones, so this needs
# 64-bit Raspberry Pi OS -- `dpkg --print-architecture` must say arm64.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile before the source is copied, so
# editing src/ doesn't re-download duckdb on every rebuild.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable

# README.md is not documentation here -- pyproject points `readme` at it, so
# the wheel build fails without it.
COPY README.md ./
COPY src ./src
# --no-editable is load-bearing: by default uv drops a .pth pointing back at
# /app/src, and the runtime stage below copies only the venv. The package has
# to be built into site-packages, or `gtt` cannot import itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.13-slim-bookworm AS runtime

# Same base as the uv image above, so the copied venv's interpreter path resolves.
ARG PUID=1000
ARG PGID=1000

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    GTT_DB_PATH=/data/tracker.duckdb \
    GTT_CREDENTIALS_PATH=/config/credentials.json

RUN groupadd --gid "$PGID" gtt \
 && useradd --uid "$PUID" --gid "$PGID" --no-create-home gtt \
 && mkdir -p /data /config \
 && chown gtt:gtt /data /config

COPY --from=build --chown=gtt:gtt /app/.venv /app/.venv

USER gtt

# Not /data: the app reads a .env from the working directory, and picking one
# up out of the mounted archive would be a surprise. Configure via compose.
WORKDIR /app

# `gtt watch` traps SIGTERM and finishes the poll in flight before exiting.
STOPSIGNAL SIGTERM

ENTRYPOINT ["gtt"]
CMD ["watch"]
