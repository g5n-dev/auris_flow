# syntax=docker/dockerfile:1.7
FROM python:3.12.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /build

RUN python -m pip install --no-cache-dir uv==0.10.0
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app

RUN groupadd --gid 10001 auris \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin auris
COPY --from=builder --chown=10001:10001 /build/.venv /app/.venv
COPY --chown=10001:10001 backend/app /app/app
COPY --chown=10001:10001 backend/migrations /app/migrations
COPY --chown=10001:10001 backend/scripts /app/scripts
COPY --chown=10001:10001 backend/alembic.ini /app/alembic.ini

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
