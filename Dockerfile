# syntax=docker/dockerfile:1

# Base images pinned by digest (multi-arch index, so this works on amd64 and arm64).
# To update: docker buildx imagetools inspect python:3.12-slim-bookworm
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1

FROM ${UV_IMAGE} AS uv

# ---- build: resolve locked dependencies into /app/.venv ------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# ---- test: `make test` builds this stage; lint + tests must pass ---------------
FROM build AS test
RUN uv sync --frozen --no-editable
COPY tests ./tests
RUN uv run --frozen ruff check . \
 && uv run --frozen ruff format --check . \
 && uv run --frozen pytest

# ---- audit: `make audit` builds this stage; fails on known vulnerabilities -----
FROM build AS audit
RUN uv sync --frozen --no-editable \
 && uv export --frozen --format requirements-txt --no-emit-project -q > /tmp/requirements.txt \
 && uv run --frozen pip-audit --disable-pip --require-hashes -r /tmp/requirements.txt

# ---- runtime: minimal, non-root --------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 newsroom \
 && useradd --system --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin newsroom
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NEWSROOM_DATA_DIR=/data
WORKDIR /app
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "newsroom.web.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--no-server-header", "--no-access-log"]
