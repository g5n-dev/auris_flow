#!/usr/bin/env python3
"""Bind the isolated real-stack gate to its pre-validated platform fixture."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import PlatformConnection

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
PLATFORM_CONNECTION_ID = "conn_platform_auth"
PLATFORM_ORIGIN = "https://recordings.audio-import-gate.test:8443"
PLATFORM_CREDENTIAL_REF = "secret://platform/audio-import-gate"
PLATFORM_TENANT_REF = "audio-import-gate-tenant"
PLATFORM_STORE_REFS = ["BJ-AURORA-001"]
PLATFORM_TEST_PATH = "/healthz"
TRACE_ID = "trace_audio_import_gate_platform_connection"


def bind_gate_platform_connection(session: Session) -> PlatformConnection:
    connection = session.scalar(
        select(PlatformConnection)
        .where(
            PlatformConnection.platform_connection_id == PLATFORM_CONNECTION_ID,
            PlatformConnection.tenant_id == TENANT_ID,
            PlatformConnection.project_id == PROJECT_ID,
        )
        .with_for_update()
    )
    if connection is None:
        raise RuntimeError("audio import gate platform connection seed is missing")

    connection.external_tenant_ref = PLATFORM_TENANT_REF
    connection.name = "真实音频导入门禁平台"
    connection.provider_type = "generic_http"
    connection.auth_mode = "bearer_token"
    connection.origin = PLATFORM_ORIGIN
    connection.credential_ref = PLATFORM_CREDENTIAL_REF
    connection.store_refs = list(PLATFORM_STORE_REFS)
    connection.test_path = PLATFORM_TEST_PATH
    connection.status = "active"
    connection.resource_version = int(connection.resource_version or 0) + 1
    connection.last_test_status = "success"
    connection.last_tested_at = datetime.now(UTC)
    connection.root_trace_id = TRACE_ID
    connection.current_trace_id = TRACE_ID
    session.flush()
    return connection


def main() -> int:
    database_url_path = Path(os.environ["DATABASE_URL_FILE"])
    database_url = database_url_path.read_text(encoding="utf-8").strip()
    if not database_url:
        raise RuntimeError("audio import gate database URL is empty")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session, session.begin():
            bind_gate_platform_connection(session)
    finally:
        engine.dispose()

    print("audio import gate platform connection bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
