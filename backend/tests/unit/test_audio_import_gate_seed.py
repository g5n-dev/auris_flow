from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import PlatformConnection

ROOT = Path(__file__).resolve().parents[3]


def _load_gate_seed() -> ModuleType:
    path = ROOT / "production" / "tests" / "audio_import_gate_seed.py"
    spec = importlib.util.spec_from_file_location("audio_import_gate_seed_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audio_import_gate_seed_binds_the_strong_platform_contract() -> None:
    module = _load_gate_seed()

    with SessionLocal() as session, session.begin():
        connection = module.bind_gate_platform_connection(session)
        assert connection.platform_connection_id == module.PLATFORM_CONNECTION_ID

    with SessionLocal() as session:
        connection = session.scalar(
            select(PlatformConnection).where(
                PlatformConnection.platform_connection_id == module.PLATFORM_CONNECTION_ID
            )
        )
        assert connection is not None
        assert connection.tenant_id == module.TENANT_ID
        assert connection.project_id == module.PROJECT_ID
        assert connection.external_tenant_ref == module.PLATFORM_TENANT_REF
        assert connection.origin == module.PLATFORM_ORIGIN
        assert connection.credential_ref == module.PLATFORM_CREDENTIAL_REF
        assert connection.store_refs == module.PLATFORM_STORE_REFS
        assert connection.test_path == module.PLATFORM_TEST_PATH
        assert connection.status == "active"
        assert connection.last_test_status == "success"
        assert connection.last_tested_at is not None
        assert connection.root_trace_id == module.TRACE_ID
