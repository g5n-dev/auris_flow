from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

TEST_DB = Path(gettempdir()) / f"auris_flow_pytest_{os.getpid()}.sqlite"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["APP_ENV"] = "test"
os.environ["ALLOW_DEV_AUTH"] = "true"
os.environ["COMPLETION_RECEIPT_SECRET"] = "auris-test-completion-secret-32chars-minimum"
os.environ["COMPLETION_RECEIPT_SIGNATURE_ID"] = "auris-test-completion"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.secrets import SecretFileSettingsSource  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.resource_service import load_seed_file, seed_database  # noqa: E402


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_database_file():
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()
    yield
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture(autouse=True)
def reset_database():
    # A hotword candidate points at the published baseline through a
    # self-referential FK. SQLite implements ``DROP TABLE`` as an implicit
    # row delete, so a populated self-reference can otherwise make fixture
    # teardown fail before SQLAlchemy can rebuild the schema.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(bind=connection)
        Base.metadata.create_all(bind=connection)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with SessionLocal() as session:
        seed_database(session, load_seed_file())
    yield
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(bind=connection)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer dev-token",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-request",
    }


@pytest.fixture
def allow_inline_production_settings_for_policy_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate cross-field model checks from the separately tested source policy."""

    original = SecretFileSettingsSource._has_nonempty_inline_value

    def ignore_initializer_values(
        source: SecretFileSettingsSource,
        field_name: str,
    ) -> bool:
        init_values = source.settings_sources_data.get("InitSettingsSource", {})
        if field_name in init_values:
            return False
        return original(source, field_name)

    monkeypatch.setattr(
        SecretFileSettingsSource,
        "_has_nonempty_inline_value",
        ignore_initializer_values,
    )
