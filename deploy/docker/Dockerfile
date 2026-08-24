# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Common Application Base -- multi-stage image.
#   builder : resolve + install dependencies into a self-contained /app/.venv
#   runtime : copy that venv onto a slim base, drop privileges, run uvicorn
# ---------------------------------------------------------------------------

ARG PYTHON_VERSION=3.12

# --------------------------------- builder ---------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first: this layer is cached until pyproject/uv.lock change.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then the source, so code edits never invalidate the dependency layer.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --------------------------------- runtime ---------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# curl is used by the container HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --create-home app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8000/health/live || exit 1

# --no-server-header: do not advertise the stack.
# --proxy-headers:    trust X-Forwarded-Proto from the ingress, so the app
#                     knows the request arrived over HTTPS and emits HSTS.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
