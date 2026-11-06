# syntax=docker/dockerfile:1
# One image for all roles (worker, poller, CLI). Dependencies come from uv.lock only.

ARG PYTHON_IMAGE=python:3.12.16-slim-trixie

# --- build stage: resolve the locked environment into /app/.venv -------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Dependencies first (cache-friendly: only re-run when pyproject.toml or uv.lock change).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself, installed non-editable so the runtime stage needs no source tree.
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- runtime stage: slim image, no uv, non-root -------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

RUN groupadd --system --gid 1000 finn \
 && useradd --system --uid 1000 --gid finn --create-home --home-dir /home/finn finn

COPY --from=build --chown=finn:finn /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Working directory = repo root inside the container: runs/, material/, data/ are mounted here.
WORKDIR /app
USER finn

ENTRYPOINT ["finn"]
CMD ["--help"]
