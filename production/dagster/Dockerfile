# syntax=docker/dockerfile:1.7
FROM python:3.12.10-slim-bookworm AS builder

ARG UV_VERSION=0.10.0
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/auris/venv

WORKDIR /build
RUN python -m pip install "uv==${UV_VERSION}"

COPY production/dagster/pyproject.toml production/dagster/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY production/dagster/src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12.10-slim-bookworm AS runtime

ENV PATH=/opt/auris/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DAGSTER_HOME=/opt/dagster/home

RUN groupadd --gid 10001 auris \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/auris auris \
    && mkdir -p /opt/auris /opt/dagster/app /opt/dagster/home/storage /var/lib/auris-flow-dagster \
    && chown -R 10001:10001 /opt/auris /opt/dagster /var/lib/auris-flow-dagster

COPY --from=builder --chown=10001:10001 /opt/auris/venv /opt/auris/venv
COPY --chown=10001:10001 production/dagster/dagster.yaml /opt/dagster/home/dagster.yaml
COPY --chown=10001:10001 production/dagster/workspace.yaml /opt/dagster/app/workspace.yaml
COPY --chown=10001:10001 production/dagster/dagster-entrypoint.sh /opt/auris/dagster-entrypoint.sh

RUN chmod 0555 /opt/auris/dagster-entrypoint.sh \
    && chmod 0444 /opt/dagster/home/dagster.yaml /opt/dagster/app/workspace.yaml

WORKDIR /opt/dagster/app
USER 10001:10001
EXPOSE 3000 4000

ENTRYPOINT ["/opt/auris/dagster-entrypoint.sh"]
CMD ["webserver"]
