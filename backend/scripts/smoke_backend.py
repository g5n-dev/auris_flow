from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DB_PATH = Path(gettempdir()) / "auris_flow_smoke.sqlite"

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["APP_ENV"] = "test"
os.environ["ALLOW_DEV_AUTH"] = "true"
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.resource_service import load_seed_file, seed_database  # noqa: E402
from app.workers.outbox_worker import process_once  # noqa: E402


def assert_envelope(body: dict, key: str | None = None) -> None:
    assert "data" in body, body
    assert "meta" in body, body
    assert body["meta"]["trace_id"], body
    if key:
        assert key in body["data"], body


def main() -> None:
    engine.dispose()
    if DB_PATH.exists():
        DB_PATH.unlink()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        seed_database(session, load_seed_file())

    client = TestClient(app)
    headers = {
        "Authorization": "Bearer dev-token",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "smoke-request",
    }

    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] in {"ok", "degraded"}

    ops = client.get("/api/v1/insights/ops-summary?time_range=today", headers=headers)
    assert ops.status_code == 200, ops.text
    assert_envelope(ops.json(), "metrics")

    session = client.get("/api/v1/audio-sessions/S20250526-000128", headers=headers)
    assert session.status_code == 200, session.text
    assert_envelope(session.json(), "audio_session_id")
    assert session.json()["data"]["evidence_packs"]

    asset_key = quote("auris/label/event_tags", safe="")
    asset = client.get(f"/api/v1/data-assets/{asset_key}", headers=headers)
    assert asset.status_code == 200, asset.text
    assert asset.json()["data"]["asset_key"] == "auris/label/event_tags"

    idempotency_headers = {**headers, "Idempotency-Key": "smoke:task-run:001"}
    payload = {
        "task_version_id": "task_version_v3_2_1",
        "trigger_type": "manual",
        "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
        "run_config": {"mode": "smoke"},
    }
    first = client.post("/api/v1/task-runs", headers=idempotency_headers, json=payload)
    replay = client.post("/api/v1/task-runs", headers=idempotency_headers, json=payload)
    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert first.json()["data"]["run_id"] == replay.json()["data"]["run_id"]

    conflict = client.post(
        "/api/v1/task-runs",
        headers=idempotency_headers,
        json={**payload, "partition_key": "changed"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    processed = process_once()
    assert processed >= 1

    run_id = first.json()["data"]["run_id"]
    trace_id = first.json()["meta"]["trace_id"]
    run_detail = client.get(f"/api/v1/task-runs/{run_id}", headers=headers)
    assert run_detail.status_code == 200, run_detail.text
    assert run_detail.json()["data"]["status"] == "submitted"
    assert run_detail.json()["data"]["business_status"] == "awaiting_completion"

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=headers)
    assert trace.status_code == 200, trace.text
    assert trace.json()["data"]["trace_id"] == trace_id
    assert trace.json()["data"]["spans"]

    Base.metadata.drop_all(bind=engine)
    engine.dispose()
    if DB_PATH.exists():
        DB_PATH.unlink()
    print("backend testclient smoke ok")


if __name__ == "__main__":
    main()
