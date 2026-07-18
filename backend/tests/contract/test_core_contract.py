from __future__ import annotations

import time
from urllib.parse import quote

from sqlalchemy import select

from app.core.auth import sign_auth_token
from app.core.database import SessionLocal
from app.core.rate_limit import InMemoryRateLimiter, RateLimitDecision
from app.main import app, settings
from app.models import (
    AudioRecording,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    KnowledgeEffect,
    KnowledgeIndex,
    KnowledgeQualityGate,
    KnowledgeSource,
    LabelCandidate,
    LabelVersion,
    ListeningAnnotation,
    OutboxEvent,
    Project,
    RunRecord,
    StorageObject,
    Tenant,
    VoiceprintEnrollment,
)
from app.workers.outbox_worker import process_aggregate_events, process_once


def test_readyz_reports_real_dependency_probe_states(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    checks = body["data"]["checks"]
    assert checks["database"] == "ok"
    assert body["data"]["required_checks"] == ["database"]
    assert body["data"]["missing_required"] == {}
    for dependency in ("redis", "object_storage", "qdrant", "dagster"):
        assert checks[dependency] in {"ok", "not_ready", "not_configured"}
        assert checks[dependency] != "configured"


def test_readyz_strict_mode_fails_when_required_dependencies_not_ready(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "ci")
    monkeypatch.setattr(settings, "dependency_check_mode", "strict")
    monkeypatch.setattr(
        settings,
        "required_dependency_checks",
        "database,redis,object_storage,qdrant",
    )
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "object_storage_endpoint", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "qdrant_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "dagster_graphql_url", "")

    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["data"]["status"] == "failed"
    assert body["data"]["checks"]["database"] == "ok"
    assert body["data"]["required_checks"] == [
        "auth",
        "database",
        "object_storage",
        "qdrant",
        "redis",
    ]
    assert body["data"]["missing_required"] == {
        "object_storage": "not_ready",
        "qdrant": "not_ready",
        "redis": "not_ready",
    }


def test_readyz_production_alias_is_strict_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "dependency_check_mode", "local")
    monkeypatch.setattr(settings, "required_dependency_checks", "auto")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "object_storage_endpoint", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "qdrant_url", "http://127.0.0.1:1")

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["data"]["required_checks"] == [
        "auth",
        "database",
        "object_storage",
        "qdrant",
        "redis",
    ]


def test_ops_summary_contract(client, auth_headers):
    response = client.get(
        "/api/v1/insights/ops-summary",
        headers={**auth_headers, "X-Business-Date": "2025-05-26"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "data" in body
    assert "meta" in body
    assert body["meta"]["trace_id"]
    data = body["data"]
    metrics = {item["metric_key"]: item for item in data["metrics"]}
    assert metrics["projects"]["value"] == 1
    assert metrics["today_audio"]["value"] == 2
    assert metrics["auto_pass_rate"]["value"] == 50.0
    assert metrics["human_review"]["value"] == 3
    assert metrics["asset_risk"]["value"] == 3
    assert data["audio_count"] == 2
    assert data["pending_count"] == 3
    assert data["anomaly_count"] == 3
    assert data["context"]["business_date"] == "2025-05-26"


def test_ops_summary_is_derived_from_scoped_resources(client, auth_headers):
    with SessionLocal.begin() as session:
        session.add_all(
            [
                JsonResource(
                    collection="audio_sessions",
                    resource_key="S20250526-DYNAMIC",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id="trace_ops_dynamic",
                    data={
                        "audio_session_id": "S20250526-DYNAMIC",
                        "started_at": "2025-05-26T15:00:00+08:00",
                        "status": "success",
                    },
                ),
                JsonResource(
                    collection="audio_sessions",
                    resource_key="S20250525-OUTSIDE",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id="trace_ops_outside",
                    data={
                        "audio_session_id": "S20250525-OUTSIDE",
                        "started_at": "2025-05-25T15:00:00+08:00",
                        "status": "success",
                    },
                ),
                JsonResource(
                    collection="human_review_tasks",
                    resource_key="hrt_ops_dynamic",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="pending",
                    trace_id="trace_ops_dynamic",
                    data={
                        "id": "hrt_ops_dynamic",
                        "queue": "amount_conflict",
                        "priority": "high",
                        "status": "pending",
                    },
                ),
                JsonResource(
                    collection="data_assets",
                    resource_key="auris/ops/dynamic-risk",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="warning",
                    trace_id="trace_ops_dynamic",
                    data={
                        "asset_key": "auris/ops/dynamic-risk",
                        "status": "warning",
                    },
                ),
            ]
        )

    response = client.get(
        "/api/v1/insights/ops-summary",
        headers={**auth_headers, "X-Business-Date": "2025-05-26"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    metrics = {item["metric_key"]: item["value"] for item in data["metrics"]}
    assert metrics["today_audio"] == 3
    assert metrics["auto_pass_rate"] == 66.7
    assert metrics["human_review"] == 4
    assert metrics["asset_risk"] == 4
    assert data["audio_count"] == 3
    assert all(item.get("started_at", "").startswith("2025-05-26") for item in data["sessions"])


def test_ops_summary_rejects_invalid_business_date_context(client, auth_headers):
    response = client.get(
        "/api/v1/insights/ops-summary",
        headers={**auth_headers, "X-Business-Date": "2025-02-31"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONTEXT_INVALID_BUSINESS_DATE"


def test_task_run_requires_idempotency(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
        },
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_task_run_idempotency_replay_and_conflict(client, auth_headers):
    headers = {**auth_headers, "Idempotency-Key": "pytest-task-run"}
    payload = {
        "task_version_id": "task_version_v3_2_1",
        "trigger_type": "manual",
        "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
    }
    first = client.post("/api/v1/task-runs", json=payload, headers=headers)
    second = client.post("/api/v1/task-runs", json=payload, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]

    conflict = client.post(
        "/api/v1/task-runs",
        json={**payload, "partition_key": "changed"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_task_run_rejects_missing_or_draft_production_version_without_side_effects(
    client, auth_headers
):
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="task_versions",
                resource_key="task_version_draft_guard",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="draft",
                trace_id="trace_task_version_draft_guard",
                data={
                    "task_version_id": "task_version_draft_guard",
                    "task_type_id": "task_sales_quality",
                    "version": "draft-guard",
                    "status": "draft",
                },
            )
        )
        session.commit()
        run_count = session.query(RunRecord).count()
        outbox_count = session.query(OutboxEvent).count()

    missing = client.post(
        "/api/v1/task-runs",
        json={"task_version_id": "task_version_missing", "execution_mode": "production"},
        headers={**auth_headers, "Idempotency-Key": "task-version-missing-run"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_VERSION_NOT_FOUND"

    draft = client.post(
        "/api/v1/task-runs",
        json={"task_version_id": "task_version_draft_guard", "execution_mode": "production"},
        headers={**auth_headers, "Idempotency-Key": "task-version-draft-production-run"},
    )
    assert draft.status_code == 409
    assert draft.json()["error"]["code"] == "TASK_VERSION_NOT_PUBLISHED"

    with SessionLocal() as session:
        assert session.query(RunRecord).count() == run_count
        assert session.query(OutboxEvent).count() == outbox_count


def test_task_run_allows_draft_diagnostic_mode_with_immutable_version_snapshot(
    client, auth_headers
):
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="task_versions",
                resource_key="task_version_diagnostic_guard",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="draft",
                trace_id="trace_task_version_diagnostic_guard",
                data={
                    "task_version_id": "task_version_diagnostic_guard",
                    "task_type_id": "task_sales_quality",
                    "version": "diagnostic-guard",
                    "canvas_variant": "candidate-v4",
                    "label_version": "label_v1_8_4",
                    "model_version": "asr_v2.3.1",
                    "status": "draft",
                },
            )
        )
        session.commit()

    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_diagnostic_guard",
            "execution_mode": "diagnostic",
            "external_outputs_enabled": True,
            "writeback_mode": "enabled",
            "business_context": {
                "provider": "crm-master-data",
                "language": "zh-CN",
            },
        },
        headers={**auth_headers, "Idempotency-Key": "task-version-diagnostic-run"},
    )

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["execution_mode"] == "diagnostic"
    assert data["external_outputs_enabled"] is False
    assert data["writeback_mode"] == "disabled"
    assert data["callback_mode"] == "disabled"
    assert data["business_context"] == {
        "provider": "crm-master-data",
        "language": "zh-CN",
    }
    snapshot = data["task_version_snapshot"]
    assert snapshot["task_version_id"] == "task_version_diagnostic_guard"
    assert snapshot["status"] == "draft"
    assert snapshot["version"] == "diagnostic-guard"
    assert len(snapshot["sha256"]) == 64

    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=data["run_id"]).one()
        assert event.payload["external_outputs_enabled"] is False
        assert event.payload["task_version_snapshot"] == snapshot


def test_idempotency_key_cannot_replay_across_resource_paths(client, auth_headers):
    first_version_id = "task_version_idempotency_path_a"
    second_version_id = "task_version_idempotency_path_b"
    for index, version_id in enumerate((first_version_id, second_version_id), start=1):
        created = client.post(
            "/api/v1/task-versions",
            json={
                "task_version_id": version_id,
                "task_type_id": "task_sales_quality",
                "version": f"idempotency-path-{index}",
                "canvas_variant": "stable-v3",
                "label_version": "label_v1_8_4",
            },
            headers={
                **auth_headers,
                "Idempotency-Key": f"create-{version_id}",
            },
        )
        assert created.status_code == 201, created.text

    publish_headers = {**auth_headers, "Idempotency-Key": "same-key-different-publish-path"}
    publish_payload = {"decision": "publish", "gate": "compatibility"}
    first_publish = client.post(
        f"/api/v1/task-versions/{first_version_id}/publish",
        json=publish_payload,
        headers=publish_headers,
    )
    assert first_publish.status_code == 202
    second_publish = client.post(
        f"/api/v1/task-versions/{second_version_id}/publish",
        json=publish_payload,
        headers=publish_headers,
    )
    assert second_publish.status_code == 409
    assert second_publish.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    asset_headers = {**auth_headers, "Idempotency-Key": "same-key-different-asset-path"}
    asset_payload = {"reason": "same body different asset"}
    first_asset = client.post(
        f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/backfills",
        json=asset_payload,
        headers=asset_headers,
    )
    assert first_asset.status_code == 202
    second_asset = client.post(
        f"/api/v1/data-assets/{quote('auris/audio/raw_recordings', safe='')}/backfills",
        json=asset_payload,
        headers=asset_headers,
    )
    assert second_asset.status_code == 409
    assert second_asset.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_task_run_cursor_pagination_is_stable(client, auth_headers):
    for index in range(3):
        response = client.post(
            "/api/v1/task-runs",
            json={
                "task_version_id": "task_version_v3_2_1",
                "trigger_type": "manual",
                "partition_key": f"aurora_auto/BJ-AURORA-001/2025-05-26/{index}",
            },
            headers={**auth_headers, "Idempotency-Key": f"run-cursor-{index}"},
        )
        assert response.status_code == 202

    first = client.get("/api/v1/task-runs?limit=1", headers=auth_headers)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["meta"]["limit"] == 1
    assert first_body["meta"]["total"] >= 3
    assert first_body["meta"]["next_cursor"]

    second = client.get(
        f"/api/v1/task-runs?limit=1&cursor={first_body['meta']['next_cursor']}",
        headers=auth_headers,
    )
    assert second.status_code == 200
    assert first_body["data"]["items"][0]["run_id"] != second.json()["data"]["items"][0]["run_id"]

    invalid = client.get("/api/v1/task-runs?cursor=bad-run-cursor", headers=auth_headers)
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"


def test_idempotency_key_scope_does_not_change_with_user(client, auth_headers):
    payload = {
        "task_version_id": "task_version_v3_2_1",
        "trigger_type": "manual",
        "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
    }
    shared_key = "pytest-user-scoped-key"
    admin = client.post(
        "/api/v1/task-runs",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": shared_key},
    )
    model_user = client.post(
        "/api/v1/task-runs",
        json=payload,
        headers={
            **auth_headers,
            "Authorization": "Bearer model-token",
            "Idempotency-Key": shared_key,
        },
    )
    assert admin.status_code == 202
    assert model_user.status_code == 202
    assert admin.json()["data"]["run_id"] == model_user.json()["data"]["run_id"]


def test_rbac_blocks_low_privilege_write(client, auth_headers):
    response = client.post(
        "/api/v1/task-versions",
        json={"name": "低权限创建任务版本"},
        headers={
            **auth_headers,
            "Authorization": "Bearer annotator-token",
            "Idempotency-Key": "pytest-rbac-blocked",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_task_version_create_cannot_overwrite_existing_published_version(client, auth_headers):
    before = client.get(
        "/api/v1/task-versions/task_version_v3_2_1",
        headers=auth_headers,
    )
    assert before.status_code == 200
    assert before.json()["data"]["status"] == "published"

    takeover = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": "task_version_v3_2_1",
            "status": "draft",
            "name": "attempted takeover",
        },
        headers={
            **auth_headers,
            "Authorization": "Bearer model-token",
            "Idempotency-Key": "task-version-create-takeover",
        },
    )
    assert takeover.status_code == 409
    assert takeover.json()["error"]["code"] == "RESOURCE_ALREADY_EXISTS"

    after = client.get(
        "/api/v1/task-versions/task_version_v3_2_1",
        headers=auth_headers,
    )
    assert after.status_code == 200
    assert after.json()["data"] == before.json()["data"]


def test_task_version_patch_preserves_server_owned_root_trace(client, auth_headers):
    task_version_id = "task_version_root_trace_guard"
    created = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": task_version_id,
            "task_type_id": "task_sales_quality",
            "version": "root-trace-v1",
        },
        headers={**auth_headers, "Idempotency-Key": "task-root-trace-create"},
    )
    assert created.status_code == 201, created.text
    original_root_trace_id = created.json()["data"]["root_trace_id"]

    patched = client.patch(
        f"/api/v1/task-versions/{task_version_id}",
        json={"version": "root-trace-v2"},
        headers={**auth_headers, "Idempotency-Key": "task-root-trace-patch"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["version"] == "root-trace-v2"
    assert patched.json()["data"]["root_trace_id"] == original_root_trace_id
    assert patched.json()["data"]["trace_id"] != original_root_trace_id


def test_data_asset_key_decoding(client, auth_headers):
    asset_key = quote("auris/label/event_tags", safe="")
    response = client.get(f"/api/v1/data-assets/{asset_key}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["asset_key"] == "auris/label/event_tags"
    assert body["data"]["trace_id"]


def test_collection_cursor_pagination_is_stable(client, auth_headers):
    for index in range(3):
        response = client.post(
            "/api/v1/connectors",
            json={
                "connector_id": f"cursor_connector_{index}",
                "name": f"分页连接器 {index}",
            },
            headers={**auth_headers, "Idempotency-Key": f"cursor-connector-{index}"},
        )
        assert response.status_code == 201

    first = client.get("/api/v1/connectors?limit=2", headers=auth_headers)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["meta"]["limit"] == 2
    assert first_body["meta"]["total"] >= 3
    assert first_body["meta"]["next_cursor"]
    assert len(first_body["data"]["items"]) <= 2

    second = client.get(
        f"/api/v1/connectors?limit=2&cursor={first_body['meta']['next_cursor']}",
        headers=auth_headers,
    )
    assert second.status_code == 200
    first_ids = {item.get("id") or item.get("connector_id") for item in first_body["data"]["items"]}
    second_ids = {
        item.get("id") or item.get("connector_id") for item in second.json()["data"]["items"]
    }
    assert first_ids.isdisjoint(second_ids)

    invalid = client.get("/api/v1/connectors?cursor=not-a-valid-cursor", headers=auth_headers)
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"


def test_api_rate_limit_returns_429_with_headers(client, auth_headers):
    old_limit = settings.rate_limit_per_minute
    old_limiter = app.state.rate_limiter
    settings.rate_limit_per_minute = 2
    app.state.rate_limiter = InMemoryRateLimiter()
    try:
        first = client.get("/api/v1/insights/ops-summary", headers=auth_headers)
        second = client.get("/api/v1/insights/ops-summary", headers=auth_headers)
        third = client.get("/api/v1/insights/ops-summary", headers=auth_headers)
    finally:
        settings.rate_limit_per_minute = old_limit
        app.state.rate_limiter = old_limiter

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert third.headers["Retry-After"]
    assert third.headers["X-RateLimit-Limit"] == "2"


def test_api_rate_limit_returns_503_when_production_redis_is_unavailable(client, auth_headers):
    class UnavailableRateLimiter:
        def allow(self, _key: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
            return RateLimitDecision(
                allowed=False,
                limit=limit,
                remaining=0,
                reset_after_seconds=window_seconds,
                backend="redis-unavailable",
            )

    old_limit = settings.rate_limit_per_minute
    old_limiter = app.state.rate_limiter
    settings.rate_limit_per_minute = 2
    app.state.rate_limiter = UnavailableRateLimiter()
    try:
        response = client.get("/api/v1/insights/ops-summary", headers=auth_headers)
    finally:
        settings.rate_limit_per_minute = old_limit
        app.state.rate_limiter = old_limiter

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RATE_LIMIT_BACKEND_UNAVAILABLE"
    assert response.json()["error"]["details"][0]["backend"] == "redis-unavailable"
    assert response.headers["Retry-After"] == "60"


def test_api_rate_limit_bucket_cannot_be_bypassed_by_rotating_bearer_tokens(client, auth_headers):
    old_limit = settings.rate_limit_per_minute
    old_limiter = app.state.rate_limiter
    settings.rate_limit_per_minute = 2
    app.state.rate_limiter = InMemoryRateLimiter()
    try:
        responses = [
            client.get(
                "/api/v1/insights/ops-summary",
                headers={**auth_headers, "Authorization": f"Bearer invalid-{index}"},
            )
            for index in range(3)
        ]
    finally:
        settings.rate_limit_per_minute = old_limit
        app.state.rate_limiter = old_limiter

    assert [response.status_code for response in responses] == [401, 401, 429]


def test_api_responses_include_security_headers(client, auth_headers):
    response = client.get("/api/v1/insights/ops-summary", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_production_alias_responses_include_hsts(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"] == ("max-age=31536000; includeSubDomains")


def test_audio_session_detail_projection(client, auth_headers):
    response = client.get("/api/v1/audio-sessions/S20250526-000128", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["audio_session_id"] == "S20250526-000128"
    assert data["recording"]["file_name"] == "A-1001_20250526_122300.wav"
    assert data["evidence_packs"]
    assert data["asr_segments"]


def _issue_audio_playback_url(client, auth_headers, *, key: str) -> str:
    response = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["playback_url"])


def test_audio_session_recording_supports_http_range(client, auth_headers):
    playback_url = _issue_audio_playback_url(
        client,
        auth_headers,
        key="grant-audio-range-contract",
    )
    grant = playback_url.partition("grant=")[2]
    legacy_url = f"/api/v1/audio-sessions/S20250526-000128/recording?grant={grant}"

    whole = client.get(playback_url)
    assert whole.status_code == 200
    assert whole.headers["Accept-Ranges"] == "bytes"
    assert whole.headers["Content-Type"].startswith("audio/wav")
    assert whole.content.startswith(b"RIFF")

    partial = client.get(
        legacy_url,
        headers={"Range": "bytes=0-15"},
    )
    assert partial.status_code == 206
    assert partial.headers["Accept-Ranges"] == "bytes"
    assert partial.headers["Content-Range"] == f"bytes 0-15/{len(whole.content)}"
    assert partial.headers["Content-Length"] == "16"
    assert partial.content == whole.content[:16]

    suffix = client.get(
        playback_url,
        headers={"Range": "bytes=-8"},
    )
    assert suffix.status_code == 206
    assert suffix.content == whole.content[-8:]

    open_ended = client.get(playback_url, headers={"Range": "bytes=16-"})
    assert open_ended.status_code == 206
    assert open_ended.content == whole.content[16:]

    multiple = client.get(playback_url, headers={"Range": "bytes=0-1,4-5"})
    assert multiple.status_code == 416
    assert multiple.headers["Content-Range"] == f"bytes */{len(whole.content)}"

    invalid = client.get(
        playback_url,
        headers={"Range": "bytes=999999-1000000"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["Content-Range"] == f"bytes */{len(whole.content)}"


def test_audio_recording_if_range_mismatch_returns_full_representation(client, auth_headers):
    registration = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json={
            "storage_object_id": "sto_if_range_contract",
            "provider": "minio",
            "bucket": "auris-flow-local",
            "object_key": (
                "tenants/aurora_auto/projects/sales_qa/audio/raw/2025-05-26/if-range.wav"
            ),
            "content_type": "audio/wav",
            "content_length": 68,
            "checksum_sha256": "c" * 64,
            "etag": "if-range-etag",
        },
        headers={**auth_headers, "Idempotency-Key": "register-if-range-contract"},
    )
    assert registration.status_code == 200

    playback_url = _issue_audio_playback_url(
        client,
        auth_headers,
        key="grant-if-range-contract",
    )
    grant = playback_url.partition("grant=")[2]
    legacy_url = f"/api/v1/audio-sessions/S20250526-000128/recording?grant={grant}"

    mismatch = client.get(
        legacy_url,
        headers={"Range": "bytes=0-15", "If-Range": '"stale-etag"'},
    )
    matched = client.get(
        legacy_url,
        headers={
            "Range": "bytes=0-15",
            "If-Range": '"if-range-etag"',
        },
    )

    assert mismatch.status_code == 200
    assert len(mismatch.content) > 16
    assert matched.status_code == 206
    assert matched.content == mismatch.content[:16]


def test_audio_playback_grant_allows_native_media_range_without_custom_headers(
    client, auth_headers
):
    grant = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={**auth_headers, "Idempotency-Key": "grant-native-audio-playback"},
    )
    assert grant.status_code == 201
    grant_data = grant.json()["data"]
    assert grant_data["audio_session_id"] == "S20250526-000128"
    assert grant_data["playback_url"].startswith("/api/v1/audio-playback?grant=")
    assert grant_data["expires_at"]

    partial = client.get(grant_data["playback_url"], headers={"Range": "bytes=0-15"})
    assert partial.status_code == 206
    assert partial.headers["Accept-Ranges"] == "bytes"
    assert partial.headers["Content-Range"].startswith("bytes 0-15/")
    assert partial.headers["Cache-Control"] == "private, no-store"
    assert partial.headers["Referrer-Policy"] == "no-referrer"
    assert partial.content.startswith(b"RIFF")

    tampered = f"{grant_data['playback_url']}x"
    denied = client.get(tampered, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_INVALID"


def test_audio_playback_grant_rechecks_project_membership(client, auth_headers):
    grant = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={**auth_headers, "Idempotency-Key": "grant-membership-recheck"},
    )
    assert grant.status_code == 201

    with SessionLocal() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        project.data = {
            **project.data,
            "member_user_ids": ["u_model_001"],
            "members": [{"user_id": "u_model_001"}],
        }
        session.commit()

    denied = client.get(grant.json()["data"]["playback_url"])
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PROJECT_MEMBERSHIP_REQUIRED"


def test_audio_playback_grant_is_bound_to_registered_storage_version(client, auth_headers):
    grant = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={**auth_headers, "Idempotency-Key": "grant-storage-version-binding"},
    )
    assert grant.status_code == 201

    with SessionLocal() as session:
        storage_object = session.get(StorageObject, "sto_rec_A_1001_20250526_122300")
        assert storage_object is not None
        storage_object.etag = "replacement-etag"
        session.commit()

    stale = client.get(grant.json()["data"]["playback_url"], headers={"Range": "bytes=0-15"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_STALE"


def test_audio_recording_object_registration_is_scoped_idempotent_and_traceable(
    client, auth_headers
):
    headers = {
        **auth_headers,
        "Idempotency-Key": "register-audio-object-rec-a-1001",
        "X-Trace-Id": "trace_audio_object_registration",
    }
    payload = {
        "storage_object_id": "sto_rec_A_1001_20250526_122300",
        "provider": "minio",
        "bucket": "auris-flow-local",
        "object_key": (
            "tenants/aurora_auto/projects/sales_qa/audio/raw/2025-05-26/A-1001_20250526_122300.wav"
        ),
        "content_type": "audio/wav",
        "content_length": 68,
        "checksum_sha256": "a" * 64,
        "etag": "fixture-etag",
    }

    created = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json=payload,
        headers=headers,
    )
    replay = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json=payload,
        headers=headers,
    )

    assert created.status_code == 200
    assert replay.status_code == 200
    assert created.json() == replay.json()
    data = created.json()["data"]
    assert data["recording_id"] == "rec_A_1001_20250526_122300"
    assert data["storage_object"]["storage_object_id"] == payload["storage_object_id"]
    assert data["storage_object"]["object_key"] == payload["object_key"]
    root_trace_id = created.json()["meta"]["trace_id"]
    assert root_trace_id != "trace_audio_object_registration"
    assert created.headers["X-Trace-Id"] == root_trace_id

    with SessionLocal() as session:
        recording = session.get(AudioRecording, "rec_A_1001_20250526_122300")
        storage_object = session.get(StorageObject, payload["storage_object_id"])
        assert recording is not None
        assert recording.tenant_id == "aurora_auto"
        assert recording.project_id == "sales_qa"
        assert recording.payload["storage_object_id"] == payload["storage_object_id"]
        assert storage_object is not None
        assert storage_object.source_type == "audio_recording"
        assert storage_object.source_id == recording.recording_id
        assert storage_object.content_sha256 == payload["checksum_sha256"]
        assert storage_object.size_bytes == payload["content_length"]
        assert storage_object.trace_id == root_trace_id

    detail = client.get("/api/v1/audio-sessions/S20250526-000128", headers=auth_headers)
    assert detail.status_code == 200
    assert (
        detail.json()["data"]["recording"]["storage_object"]["storage_object_id"]
        == payload["storage_object_id"]
    )

    playback_url = _issue_audio_playback_url(
        client,
        auth_headers,
        key="grant-registered-audio-object",
    )
    partial = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert partial.status_code == 206
    assert partial.headers["X-Storage-Object-Id"] == payload["storage_object_id"]
    assert partial.headers["X-Storage-Provider"] == "minio"

    conflict = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json={**payload, "etag": "changed-etag"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_audio_recording_object_registration_rejects_unsafe_object_key(client, auth_headers):
    response = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json={
            "storage_object_id": "sto_unsafe_audio",
            "provider": "oss",
            "bucket": "auris-flow-local",
            "object_key": "tenants/aurora_auto/../other-tenant/private.wav",
            "content_type": "audio/wav",
            "content_length": 68,
            "checksum_sha256": "b" * 64,
        },
        headers={**auth_headers, "Idempotency-Key": "reject-unsafe-audio-object"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_page_read_models_cover_assets_reviews_and_labels(client, auth_headers):
    asset_key = quote("auris/label/event_tags", safe="")
    recent_assets = client.get("/api/v1/data-assets/recent?limit=2", headers=auth_headers)
    assert recent_assets.status_code == 200
    assert recent_assets.json()["meta"]["status_counts"]

    filtered_assets = client.get(f"/api/v1/data-assets?asset_key={asset_key}", headers=auth_headers)
    assert filtered_assets.status_code == 200
    asset_items = filtered_assets.json()["data"]["items"]
    assert len(asset_items) == 1
    assert asset_items[0]["asset_key"] == "auris/label/event_tags"

    partitions = client.get(f"/api/v1/data-assets/{asset_key}/partitions", headers=auth_headers)
    materializations = client.get(
        f"/api/v1/data-assets/{asset_key}/materializations", headers=auth_headers
    )
    lineage = client.get(f"/api/v1/data-assets/{asset_key}/lineage", headers=auth_headers)
    assert partitions.status_code == 200
    assert partitions.json()["meta"]["status_counts"]["success"] == 1
    assert materializations.status_code == 200
    assert materializations.json()["data"]["items"][0]["asset_key"] == "auris/label/event_tags"
    assert lineage.status_code == 200
    assert lineage.json()["data"]["nodes"]

    review_tasks = client.get(
        "/api/v1/human-review-tasks?queue=amount_conflict", headers=auth_headers
    )
    assert review_tasks.status_code == 200
    assert review_tasks.json()["meta"]["status_counts"]

    review_detail = client.get("/api/v1/human-review-tasks/hrt_amount_001", headers=auth_headers)
    assert review_detail.status_code == 200
    assert review_detail.json()["data"]["evidence_pack"]

    labels = client.get("/api/v1/labels", headers=auth_headers)
    label_versions = client.get("/api/v1/label-versions?status=published", headers=auth_headers)
    assert labels.status_code == 200
    assert labels.json()["data"]["items"][0]["labels"]
    assert label_versions.status_code == 200
    assert label_versions.json()["data"]["items"][0]["status"] == "published"
    with SessionLocal() as session:
        assert session.get(LabelVersion, "label_v1_8_4") is not None
        assert session.get(LabelCandidate, "cand_af128_amount_conflict") is not None
        assert session.get(HumanReviewTask, "hrt_amount_001") is not None


def test_audio_review_supporting_endpoints_are_interactive(client, auth_headers):
    sessions = client.get("/api/v1/audio-sessions", headers=auth_headers)
    assert sessions.status_code == 200
    assert sessions.json()["meta"]["status_counts"]

    missing_idempotency = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={"capabilities": ["vad", "asr"]},
        headers=auth_headers,
    )
    assert missing_idempotency.status_code == 400
    assert missing_idempotency.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    intelligence_run = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["vad", "asr", "diarization", "voiceprint", "quality"],
            "model_version": "audio-v2.3.1",
        },
        headers={**auth_headers, "Idempotency-Key": "audio-intelligence-contract"},
    )
    assert intelligence_run.status_code == 202
    intelligence_data = intelligence_run.json()["data"]
    assert intelligence_data["run_type"] == "audio_intelligence"
    assert intelligence_data["status"] == "pending"
    assert intelligence_data["audio_session_id"] == "S20250526-000128"
    assert intelligence_data["job_name"] == "audio_intelligence_pipeline"
    assert {item["capability"] for item in intelligence_data["output_assets"]} == {
        "vad",
        "asr",
        "diarization",
        "voiceprint",
        "quality",
    }

    aggregations = client.get("/api/v1/audio-sessions/aggregations", headers=auth_headers)
    assert aggregations.status_code == 200
    assert aggregations.json()["data"]["items"][0]["children"]

    voiceprints = client.get("/api/v1/voiceprints", headers=auth_headers)
    assert voiceprints.status_code == 200
    assert voiceprints.json()["data"]["items"][0]["status"] == "enrollable"

    voiceprint_payload = {
        "enrollment_id": "vp_a1001_enrollment_contract",
        "voiceprint_id": "VP-A1001",
        "employee_ref": "销售A / A-1001",
        "speaker_id": "spk_emp_a1001_v3",
        "audio_session_id": "S20250526-000128",
        "recording_id": "A-1001_20250526_122300",
        "wav_file": "A-1001_20250526_122300.wav",
        "asset_key": "auris/audio/raw_recordings",
        "voice_asset_key": "auris/voiceprint/enrollment_templates",
        "quality": {"overall": 88, "duration": 92, "snr": 84, "purity": 90, "stability": 86},
        "consistency": {"ab": 0.91, "ac": 0.88, "bc": 0.9},
        "min_consistency": 0.88,
        "samples": [
            {"sample_id": "A", "type": "固定文本", "window": "12:23:42-12:24:12"},
            {"sample_id": "B", "type": "随机短句", "window": "12:24:48-12:25:18"},
            {"sample_id": "C", "type": "业务自然语音", "window": "12:26:06-12:26:54"},
        ],
        "embedding_ref": {
            "collection": "voiceprint_embeddings",
            "vector_dim": 512,
            "status": "reference_only",
        },
        "decision": "submit_enrollment",
        "source": "contract_test",
    }
    missing_voiceprint_key = client.post(
        "/api/v1/voiceprint-enrollments",
        json=voiceprint_payload,
        headers=auth_headers,
    )
    assert missing_voiceprint_key.status_code == 400
    assert missing_voiceprint_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    voiceprint_enrollment = client.post(
        "/api/v1/voiceprint-enrollments",
        json=voiceprint_payload,
        headers={**auth_headers, "Idempotency-Key": "voiceprint-enrollment-contract"},
    )
    assert voiceprint_enrollment.status_code == 201
    voiceprint_enrollment_data = voiceprint_enrollment.json()["data"]
    assert voiceprint_enrollment_data["id"] == "vp_a1001_enrollment_contract"
    assert voiceprint_enrollment_data["voiceprint_id"] == "VP-A1001"
    assert voiceprint_enrollment_data["status"] == "pending_review"
    assert voiceprint_enrollment_data["confirm_state"] == "pending_review"
    assert voiceprint_enrollment_data["quality_gate"]["passed"] is True
    assert (
        voiceprint_enrollment_data["trace_id"] == voiceprint_enrollment.json()["meta"]["trace_id"]
    )
    assert {"type": "voiceprint", "id": "VP-A1001"} in voiceprint_enrollment_data[
        "affected_objects"
    ]
    assert {"type": "audio_session", "id": "S20250526-000128"} in voiceprint_enrollment_data[
        "affected_objects"
    ]
    assert {"type": "voiceprint_sample", "id": "A"} in voiceprint_enrollment_data[
        "affected_objects"
    ]
    with SessionLocal() as session:
        voiceprint_projection = session.get(VoiceprintEnrollment, "vp_a1001_enrollment_contract")
        assert voiceprint_projection is not None
        assert voiceprint_projection.voiceprint_id == "VP-A1001"
        assert voiceprint_projection.status == "pending_review"
        assert voiceprint_projection.trace_id == voiceprint_enrollment.json()["meta"]["trace_id"]
        assert (
            voiceprint_projection.payload["qdrant_payload"]["collection"] == "voiceprint_embeddings"
        )

    voiceprint_replay = client.post(
        "/api/v1/voiceprint-enrollments",
        json=voiceprint_payload,
        headers={**auth_headers, "Idempotency-Key": "voiceprint-enrollment-contract"},
    )
    assert voiceprint_replay.status_code == 201
    assert voiceprint_replay.json()["data"]["id"] == voiceprint_enrollment_data["id"]

    voiceprint_conflict = client.post(
        "/api/v1/voiceprint-enrollments",
        json={**voiceprint_payload, "voiceprint_id": "VP-OTHER"},
        headers={**auth_headers, "Idempotency-Key": "voiceprint-enrollment-contract"},
    )
    assert voiceprint_conflict.status_code == 409
    assert voiceprint_conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    voiceprint_detail = client.get(
        "/api/v1/voiceprint-enrollments/vp_a1001_enrollment_contract",
        headers=auth_headers,
    )
    assert voiceprint_detail.status_code == 200
    assert voiceprint_detail.json()["data"]["voiceprint_id"] == "VP-A1001"
    voiceprint_list = client.get(
        "/api/v1/voiceprint-enrollments?voiceprint_id=VP-A1001",
        headers=auth_headers,
    )
    assert voiceprint_list.status_code == 200
    assert any(
        item["id"] == "vp_a1001_enrollment_contract"
        for item in voiceprint_list.json()["data"]["items"]
    )
    voiceprint_trace = client.get(
        f"/api/v1/traces/{voiceprint_enrollment.json()['meta']['trace_id']}",
        headers=auth_headers,
    )
    assert voiceprint_trace.status_code == 200
    voiceprint_spans = voiceprint_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "resource"
        and span.get("collection") == "voiceprint_enrollments"
        and span.get("id") == "vp_a1001_enrollment_contract"
        for span in voiceprint_spans
    )
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == "vp_a1001_enrollment_contract"
        for span in voiceprint_spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "voiceprint_enrollments.upserted"
        for span in voiceprint_spans
    )
    assert any(
        span.get("kind") == "voiceprint_enrollment"
        and span.get("voiceprint_id") == "VP-A1001"
        and span.get("status") == "pending_review"
        for span in voiceprint_spans
    )

    arbitrator_headers = {
        **auth_headers,
        "Authorization": "Bearer annotator-token",
        "Idempotency-Key": "voiceprint-enrollment-arbitrated-contract",
    }
    arbitrated = client.post(
        "/api/v1/voiceprint-enrollments",
        json={**voiceprint_payload, "enrollment_id": "vp_a1001_enrollment_arbitrated"},
        headers=arbitrator_headers,
    )
    assert arbitrated.status_code == 201
    assert arbitrated.json()["data"]["status"] == "enrolled"
    assert arbitrated.json()["data"]["quality_gate"]["reviewer_role_present"] is True
    assert arbitrated.json()["data"]["embedding_ref"]["status"] == "pending_qdrant_upsert"
    with SessionLocal() as session:
        arbitrated_projection = session.get(VoiceprintEnrollment, "vp_a1001_enrollment_arbitrated")
        assert arbitrated_projection is not None
        assert arbitrated_projection.status == "enrolled"
        assert arbitrated_projection.payload["qdrant_payload"]["voiceprint_id"] == "VP-A1001"

    event_links = client.get("/api/v1/event-links", headers=auth_headers)
    assert event_links.status_code == 200
    assert event_links.json()["data"]["items"]

    existing_patch = client.patch(
        "/api/v1/event-links/event_quote_122718",
        json={
            "audio_session_id": "S20250526-000128",
            "source_event_id": "quote_amount_mismatch",
            "event_ref": "quote_amount_mismatch",
            "target_doc_id": "#BJ-041",
            "document_ref": "#BJ-041",
            "relation_type": "auris/relation/reception_session_link",
            "join_keys": ["tenant_id=aurora_auto", "store_id=BJ-AURORA-001"],
            "confidence": 0.92,
            "status": "success",
            "relation_state": "confirmed",
            "evidence_window": "12:27:12 - 12:28:48",
        },
        headers={**auth_headers, "Idempotency-Key": "event-link-existing-patch-contract"},
    )
    assert existing_patch.status_code == 200
    existing_detail = client.get("/api/v1/event-links/event_quote_122718", headers=auth_headers)
    existing_data = existing_detail.json()["data"]
    assert existing_data["status"] == "success"
    assert existing_data["relation_state"] == "confirmed"
    assert existing_data["event_ref"] == "quote_amount_mismatch"
    assert existing_data["trace_id"] == existing_patch.json()["meta"]["trace_id"]

    created = client.post(
        "/api/v1/event-links",
        json={
            "id": "event_contract_link",
            "audio_session_id": "S20250526-000128",
            "document_id": "BJ-041",
        },
        headers={**auth_headers, "Idempotency-Key": "event-link-contract"},
    )
    assert created.status_code == 201
    patched = client.patch(
        "/api/v1/event-links/event_contract_link",
        json={"status": "success", "review_note": "confirmed"},
        headers={**auth_headers, "Idempotency-Key": "event-link-patch-contract"},
    )
    assert patched.status_code == 200
    detail = client.get("/api/v1/event-links/event_contract_link", headers=auth_headers)
    assert detail.json()["data"]["review_note"] == "confirmed"

    missing_annotation_key = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            "annotation_id": "contract_annotation_amount",
            "track": "qa",
            "label": "金额冲突",
        },
        headers=auth_headers,
    )
    assert missing_annotation_key.status_code == 400
    assert missing_annotation_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    annotation = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            "annotation_id": "contract_annotation_amount",
            "audio_session_id": "S20250526-000128",
            "track": "qa",
            "track_label": "质检标签",
            "label": "金额冲突",
            "left": 43,
            "width": 8,
            "start_time": "12:27:12",
            "end_time": "12:28:48",
            "field_key": "qa.amount_conflict",
            "value": "需要人工复核",
            "confidence": 82,
            "review_state": "待人工复核",
            "evidence_ref": "W2 / ASR-qa-1",
            "write_target": "auris/label/segment_annotations",
        },
        headers={**auth_headers, "Idempotency-Key": "listening-annotation-contract"},
    )
    assert annotation.status_code == 201
    annotation_data = annotation.json()["data"]
    assert annotation_data["id"] == "contract_annotation_amount"
    assert annotation_data["audio_session_id"] == "S20250526-000128"
    assert annotation_data["status"] == "draft"
    assert annotation_data["trace_id"] == annotation.json()["meta"]["trace_id"]
    assert {"type": "audio_session", "id": "S20250526-000128"} in annotation_data[
        "affected_objects"
    ]
    with SessionLocal() as session:
        annotation_projection = session.get(ListeningAnnotation, "contract_annotation_amount")
        assert annotation_projection is not None
        assert annotation_projection.audio_session_id == "S20250526-000128"
        assert annotation_projection.status == "draft"
        assert annotation_projection.trace_id == annotation.json()["meta"]["trace_id"]

    annotation_replay = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            "annotation_id": "contract_annotation_amount",
            "audio_session_id": "S20250526-000128",
            "track": "qa",
            "track_label": "质检标签",
            "label": "金额冲突",
            "left": 43,
            "width": 8,
            "start_time": "12:27:12",
            "end_time": "12:28:48",
            "field_key": "qa.amount_conflict",
            "value": "需要人工复核",
            "confidence": 82,
            "review_state": "待人工复核",
            "evidence_ref": "W2 / ASR-qa-1",
            "write_target": "auris/label/segment_annotations",
        },
        headers={**auth_headers, "Idempotency-Key": "listening-annotation-contract"},
    )
    assert annotation_replay.status_code == 201
    assert annotation_replay.json()["data"]["id"] == "contract_annotation_amount"

    annotations = client.get(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        headers=auth_headers,
    )
    assert annotations.status_code == 200
    assert any(
        item["id"] == "contract_annotation_amount" for item in annotations.json()["data"]["items"]
    )

    annotation_trace = client.get(
        f"/api/v1/traces/{annotation.json()['meta']['trace_id']}",
        headers=auth_headers,
    )
    assert annotation_trace.status_code == 200
    annotation_spans = annotation_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == "contract_annotation_amount"
        for span in annotation_spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "listening_annotations.upserted"
        for span in annotation_spans
    )
    assert any(
        span.get("kind") == "listening_annotation"
        and span.get("annotation_id") == "contract_annotation_amount"
        and span.get("audio_session_id") == "S20250526-000128"
        for span in annotation_spans
    )

    boundary = client.patch(
        "/api/v1/conversation-boundaries/boundary_s128_v1",
        json={
            "audio_session_id": "S20250526-000128",
            "start_ms": 30_000,
            "end_ms": 300_000,
            "decision": "manual_confirmed",
            "merged_slice_ids": ["W1", "W2"],
            "split_slice_ids": [],
            "extension_ids": ["prev_1"],
        },
        headers={**auth_headers, "Idempotency-Key": "conversation-boundary-sync-contract"},
    )
    assert boundary.status_code == 200
    boundary_data = boundary.json()["data"]
    assert boundary_data["status"] == "pending_sync"
    assert boundary_data["message"] == "边界已保存，已创建下游同步运行"
    assert boundary_data["boundary"]["status"] == "pending_sync"
    assert boundary_data["boundary"]["audio_session_id"] == "S20250526-000128"
    assert boundary_data["run_type"] == "boundary_sync"
    assert boundary_data["run_id"].startswith("boundary_sync_")
    assert {"type": "conversation_boundary", "id": "boundary_s128_v1"} in boundary_data[
        "affected_objects"
    ]
    assert {"type": "data_asset", "id": "auris/audio/voice_segments"} in boundary_data[
        "affected_objects"
    ]
    with SessionLocal() as session:
        boundary_run = session.get(RunRecord, boundary_data["run_id"])
        assert boundary_run is not None
        assert boundary_run.run_type == "boundary_sync"
        assert boundary_run.status == "pending"
        assert boundary_run.payload["boundary_id"] == "boundary_s128_v1"
        assert boundary_run.payload["job_name"] == "conversation_boundary_sync_pipeline"
        assert boundary_run.payload["partition_key"] == "aurora_auto/sales_qa/S20250526-000128"
    boundary_trace = client.get(
        f"/api/v1/traces/{boundary.json()['meta']['trace_id']}",
        headers=auth_headers,
    )
    assert boundary_trace.status_code == 200
    boundary_spans = boundary_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "conversation_boundaries.patched"
        for span in boundary_spans
    )
    assert any(
        span.get("kind") == "outbox"
        and span.get("event_type") == "conversation_boundary.sync_requested"
        for span in boundary_spans
    )


def test_human_review_decision_is_idempotent_and_updates_queue(client, auth_headers):
    missing = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted"},
        headers=auth_headers,
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {**auth_headers, "Idempotency-Key": "human-review-decision-contract"}
    payload = {"decision": "accepted", "note": "金额与单据一致，接受候选"}
    first = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json=payload,
        headers=headers,
    )
    replay = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200
    assert replay.status_code == 200
    first_data = first.json()["data"]
    assert replay.json()["data"] == first_data
    assert first_data["status"] == "success"
    assert first_data["decision"] == "accepted"
    assert first_data["decision_id"].startswith("hrd_")
    assert {
        ("human_review_task", "hrt_amount_001"),
        ("human_review_decision", first_data["decision_id"]),
        ("evidence_pack", "AF-128"),
        ("label_candidate", "cand_af128_amount_conflict"),
        ("event_link", "event_quote_122718"),
    } <= {(item["type"], item["id"]) for item in first_data["affected_objects"]}

    detail = client.get("/api/v1/human-review-tasks/hrt_amount_001", headers=auth_headers)
    assert detail.json()["data"]["decision"] == "accepted"
    assert detail.json()["data"]["decision_id"] == first_data["decision_id"]

    evidence = client.get("/api/v1/evidence-packs/AF-128", headers=auth_headers)
    assert evidence.status_code == 200
    assert evidence.json()["data"]["review_decision_id"] == first_data["decision_id"]
    candidate = next(
        item
        for item in evidence.json()["data"]["label_candidates"]
        if item["candidate_id"] == "cand_af128_amount_conflict"
    )
    assert candidate["human_state"] == "accepted"
    assert candidate["review_decision_id"] == first_data["decision_id"]

    event_link = client.get("/api/v1/event-links/event_quote_122718", headers=auth_headers)
    assert event_link.status_code == 200
    assert event_link.json()["data"]["relation_state"] == "confirmed"
    assert event_link.json()["data"]["review_decision_id"] == first_data["decision_id"]

    trace = client.get(f"/api/v1/traces/{first.json()['meta']['trace_id']}", headers=auth_headers)
    spans = trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "run" and span.get("run_type") == "human_review_decision"
        for span in spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "human_review.decision.created"
        for span in spans
    )
    assert any(
        span.get("kind") == "resource" and span.get("collection") == "human_review_decisions"
        for span in spans
    )
    assert any(
        span.get("kind") == "resource" and span.get("object_id") == "cand_af128_amount_conflict"
        for span in spans
    )
    assert any(
        span.get("kind") == "human_review_task"
        and span.get("review_task_id") == "hrt_amount_001"
        and span.get("status") == "success"
        for span in spans
    )
    assert any(
        span.get("kind") == "label_candidate"
        and span.get("candidate_id") == "cand_af128_amount_conflict"
        and span.get("status") == "success"
        for span in spans
    )
    assert any(
        span.get("kind") == "human_review_decision"
        and span.get("decision_id") == first_data["decision_id"]
        for span in spans
    )
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == "hrt_amount_001" for span in spans
    )

    decision_id = first_data["decision_id"]
    assert process_aggregate_events([decision_id]) == 1
    with SessionLocal() as session:
        decision_run = session.get(RunRecord, decision_id)
        decision_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "human_review.decision.created",
                OutboxEvent.aggregate_id == decision_id,
            )
        )
        assert decision_run is not None
        assert decision_run.status == "success"
        assert decision_event is not None
        assert decision_event.status == "processed"
        assert decision_event.delivery_state == "confirmed"


def test_human_review_terminal_decision_rejects_new_key_without_duplicate_ledger(
    client,
    auth_headers,
):
    first = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "首次终态决策"},
        headers={**auth_headers, "Idempotency-Key": "human-review-terminal-first"},
    )
    assert first.status_code == 200, first.text
    decision_id = first.json()["data"]["decision_id"]

    same_decision = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "首次终态决策"},
        headers={**auth_headers, "Idempotency-Key": "human-review-terminal-second-key"},
    )
    opposite_decision = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "rejected", "note": "尝试覆盖终态"},
        headers={**auth_headers, "Idempotency-Key": "human-review-terminal-overwrite"},
    )
    for response in (same_decision, opposite_decision):
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "HUMAN_REVIEW_TASK_ALREADY_DECIDED"

    with SessionLocal() as session:
        decisions = (
            session.query(HumanReviewDecision)
            .filter(HumanReviewDecision.review_task_id == "hrt_amount_001")
            .all()
        )
        decision_runs = (
            session.query(RunRecord)
            .filter(
                RunRecord.run_type == "human_review_decision",
                RunRecord.run_id == decision_id,
            )
            .all()
        )
        decision_events = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.event_type == "human_review.decision.created",
                OutboxEvent.aggregate_id == decision_id,
            )
            .all()
        )
        task = session.get(HumanReviewTask, "hrt_amount_001")
        assert task is not None
        assert len(decisions) == 1
        assert decisions[0].terminal_review_task_id == "hrt_amount_001"
        assert len(decision_runs) == 1
        assert len(decision_events) == 1
        assert len(task.payload["decision_history"]) == 1


def test_human_review_escalation_remains_open_until_one_terminal_decision(
    client,
    auth_headers,
):
    escalated = client.post(
        "/api/v1/human-review-tasks/hrt_crosstalk_001/decisions",
        json={"decision": "escalated", "note": "升级给仲裁员"},
        headers={**auth_headers, "Idempotency-Key": "human-review-escalate"},
    )
    assert escalated.status_code == 200, escalated.text
    assert escalated.json()["data"]["status"] == "escalated"

    terminal = client.post(
        "/api/v1/human-review-tasks/hrt_crosstalk_001/decisions",
        json={"decision": "accepted", "note": "仲裁完成"},
        headers={**auth_headers, "Idempotency-Key": "human-review-after-escalation"},
    )
    assert terminal.status_code == 200, terminal.text

    duplicate = client.post(
        "/api/v1/human-review-tasks/hrt_crosstalk_001/decisions",
        json={"decision": "accepted", "note": "重复终态"},
        headers={**auth_headers, "Idempotency-Key": "human-review-after-terminal"},
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "HUMAN_REVIEW_TASK_ALREADY_DECIDED"

    with SessionLocal() as session:
        decisions = (
            session.query(HumanReviewDecision)
            .filter(HumanReviewDecision.review_task_id == "hrt_crosstalk_001")
            .order_by(HumanReviewDecision.created_at)
            .all()
        )
        assert len(decisions) == 2
        assert [item.terminal_review_task_id for item in decisions].count("hrt_crosstalk_001") == 1


def test_human_review_targets_are_server_derived_and_existing_tasks_cannot_be_overwritten(
    client,
    auth_headers,
):
    injected = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={
            "decision": "accepted",
            "evidence_pack_id": "AF-INJECTED",
            "affected_objects": [{"type": "label_candidate", "id": "candidate-from-another-task"}],
        },
        headers={**auth_headers, "Idempotency-Key": "human-review-target-injection"},
    )
    assert injected.status_code == 422
    assert injected.json()["error"]["code"] == "VALIDATION_ERROR"

    duplicate = client.post(
        "/api/v1/human-review-tasks",
        json={"id": "hrt_amount_001", "queue": "overwritten_queue"},
        headers={**auth_headers, "Idempotency-Key": "human-review-task-takeover"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "RESOURCE_ALREADY_EXISTS"


def test_modified_human_review_applies_only_task_bound_changes(client, auth_headers):
    modified = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={
            "decision": "modified",
            "note": "按报价单修正金额",
            "changes": [
                {
                    "target_type": "label_candidate",
                    "target_id": "cand_af128_amount_conflict",
                    "fields": {"value": "28.19 万", "confidence": 0.99},
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "human-review-modified-bound"},
    )
    assert modified.status_code == 200
    assert modified.json()["data"]["decision"] == "modified"

    evidence = client.get("/api/v1/evidence-packs/AF-128", headers=auth_headers)
    candidate = next(
        item
        for item in evidence.json()["data"]["label_candidates"]
        if item["candidate_id"] == "cand_af128_amount_conflict"
    )
    assert candidate["value"] == "28.19 万"
    assert candidate["confidence"] == 0.99
    assert candidate["human_state"] == "modified"


def test_modified_human_review_rejects_unbound_or_server_managed_changes(
    client,
    auth_headers,
):
    unbound = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={
            "decision": "modified",
            "changes": [
                {
                    "target_type": "label_candidate",
                    "target_id": "candidate-from-another-task",
                    "fields": {"value": "forged"},
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "human-review-modified-unbound"},
    )
    assert unbound.status_code == 422
    assert unbound.json()["error"]["code"] == "REVIEW_TARGET_NOT_BOUND_TO_TASK"

    protected = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={
            "decision": "modified",
            "changes": [
                {
                    "target_type": "label_candidate",
                    "target_id": "cand_af128_amount_conflict",
                    "fields": {"status": "published"},
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "human-review-modified-protected"},
    )
    assert protected.status_code == 422
    assert protected.json()["error"]["code"] == "VALIDATION_ERROR"


def test_platform_connection_session_is_persisted_and_requires_idempotency(client, auth_headers):
    missing = client.post(
        "/api/v1/platform-connections/platformAuth/session",
        json={"scope": "current_project"},
        headers=auth_headers,
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {**auth_headers, "Idempotency-Key": "platform-session-contract"}
    session = client.post(
        "/api/v1/platform-connections/platformAuth/session",
        json={"scope": "current_project"},
        headers=headers,
    )
    replay = client.post(
        "/api/v1/platform-connections/platformAuth/session",
        json={"scope": "current_project"},
        headers=headers,
    )
    assert session.status_code == 201
    assert replay.status_code == 201
    assert session.json()["data"]["session_ref"] == replay.json()["data"]["session_ref"]
    trace = client.get(f"/api/v1/traces/{session.json()['meta']['trace_id']}", headers=auth_headers)
    assert any(
        span.get("kind") == "resource" and span.get("collection") == "platform_sessions"
        for span in trace.json()["data"]["spans"]
    )


def test_structured_validation_blocks_invalid_eval_and_callback_payloads(client, auth_headers):
    eval_run = client.post(
        "/api/v1/eval-runs",
        json={"model_version": "candidate"},
        headers={**auth_headers, "Idempotency-Key": "invalid-eval-run"},
    )
    assert eval_run.status_code == 422
    assert eval_run.json()["error"]["code"] == "VALIDATION_ERROR"

    callback = client.post(
        "/api/v1/output-sinks/platform-callbacks",
        json={"target": " "},
        headers={**auth_headers, "Idempotency-Key": "invalid-callback"},
    )
    assert callback.status_code == 422
    assert callback.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_object_uses_error_envelope(client, auth_headers):
    response = client.get("/api/v1/audio-sessions/missing", headers=auth_headers)
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["trace_id"]


def test_api_requires_known_bearer_token(client):
    context_headers = {
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-auth",
    }
    missing = client.get("/api/v1/insights/ops-summary", headers=context_headers)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "UNAUTHORIZED"

    invalid = client.get(
        "/api/v1/insights/ops-summary",
        headers={**context_headers, "Authorization": "Bearer unknown-token"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "UNAUTHORIZED"


def test_signed_auth_token_allows_scoped_api_access_and_rejects_dev_token(client, monkeypatch):
    token_key = "contract-signing-key-provider-tests-32"
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "auth_provider", "signed")
    monkeypatch.setattr(settings, "auth_token_secret", token_key)
    monkeypatch.setattr(settings, "allow_dev_auth", False)
    token = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin", "asset_manager"),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) + 300,
    )
    context_headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-signed-auth",
    }

    response = client.get("/api/v1/insights/ops-summary", headers=context_headers)
    assert response.status_code == 200, response.text
    assert response.json()["meta"]["trace_id"]

    scoped_out = client.get(
        "/api/v1/insights/ops-summary",
        headers={**context_headers, "X-Project-Id": "same_tenant_unassigned"},
    )
    assert scoped_out.status_code == 403
    assert scoped_out.json()["error"]["code"] == "TOKEN_SCOPE_MISMATCH"

    dev_token = client.get(
        "/api/v1/insights/ops-summary",
        headers={**context_headers, "Authorization": "Bearer dev-token"},
    )
    assert dev_token.status_code == 401
    assert dev_token.json()["error"]["code"] == "UNAUTHORIZED"


def test_production_rejects_unsigned_manual_completion_receipts(client, monkeypatch):
    token_key = "contract-manual-completion-guard-32"
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "auth_provider", "signed")
    monkeypatch.setattr(settings, "auth_token_secret", token_key)
    monkeypatch.setattr(settings, "allow_dev_auth", False)
    token = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin",),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) + 300,
    )
    response = client.post(
        "/api/v1/runs/any-run/completion-receipts",
        json={"status": "success", "adapter": "dagster"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
            "X-Request-Id": "pytest-manual-completion-guard",
            "Idempotency-Key": "manual-completion-production-denied",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MANUAL_COMPLETION_RECEIPT_DISABLED"


def test_runtime_failure_injection_fields_are_rejected_outside_test_env(client, monkeypatch):
    token_key = "contract-signing-key-runtime-guard-32"
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "auth_provider", "signed")
    monkeypatch.setattr(settings, "auth_token_secret", token_key)
    monkeypatch.setattr(settings, "allow_dev_auth", False)
    token = sign_auth_token(
        secret=token_key,
        user_id="u_admin_001",
        roles=("project_admin", "asset_manager"),
        tenant_ids=("aurora_auto",),
        project_ids=("sales_qa",),
        expires_at=int(time.time()) + 300,
    )
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "simulate_adapter_failure": True,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
            "X-Request-Id": "pytest-runtime-guard",
            "Idempotency-Key": "runtime-guard-prod",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RUNTIME_FAILURE_INJECTION_NOT_ALLOWED"
    assert response.json()["error"]["details"][0]["fields"] == ["simulate_adapter_failure"]


def test_api_requires_tenant_and_project_context(client):
    base_headers = {
        "Authorization": "Bearer dev-token",
        "X-Request-Id": "pytest-context-required",
    }
    missing_tenant = client.get(
        "/api/v1/insights/ops-summary",
        headers={**base_headers, "X-Project-Id": "sales_qa"},
    )
    assert missing_tenant.status_code == 400
    assert missing_tenant.json()["error"]["code"] == "CONTEXT_MISSING_TENANT"

    missing_project = client.get(
        "/api/v1/insights/ops-summary",
        headers={**base_headers, "X-Tenant-Id": "aurora_auto"},
    )
    assert missing_project.status_code == 400
    assert missing_project.json()["error"]["code"] == "CONTEXT_MISSING_PROJECT"


def test_run_style_writes_require_idempotency(client, auth_headers):
    asset_key = quote("auris/label/event_tags", safe="")
    cases = [
        ("/api/v1/eval-runs", {"dataset_id": "eval_quote_guard_v12"}),
        ("/api/v1/exports", {"target": "evidence_pack", "object_id": "AF-128"}),
        (f"/api/v1/data-assets/{asset_key}/backfills", {"reason": "manual"}),
        ("/api/v1/settings/provider-tests", {"provider": "self_hosted"}),
        ("/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs", {"reason": "manual"}),
    ]
    for path, payload in cases:
        response = client.post(path, json=payload, headers=auth_headers)
        assert response.status_code == 400, path
        assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_knowledge_settings_and_task_version_backend_loop(client, auth_headers):
    sources = client.get("/api/v1/knowledge-sources", headers=auth_headers)
    assert sources.status_code == 200
    source_items = sources.json()["data"]["items"]
    assert source_items[0]["knowledge_source_id"] == "ks_sales_policy"
    assert sources.json()["meta"]["status_counts"]["success"] >= 1

    source_detail = client.get("/api/v1/knowledge-sources/ks_sales_policy", headers=auth_headers)
    assert source_detail.status_code == 200
    assert source_detail.json()["data"]["chunk_count"] == 5620

    sync_run = client.post(
        "/api/v1/knowledge-sources/ks_sales_policy/sync-runs",
        json={"reason": "contract_sync"},
        headers={**auth_headers, "Idempotency-Key": "knowledge-source-sync"},
    )
    assert sync_run.status_code == 202
    assert sync_run.json()["data"]["run_type"] == "knowledge_sync"
    assert {"type": "knowledge_source", "id": "ks_sales_policy"} in sync_run.json()["data"][
        "affected_objects"
    ]

    indexes = client.get("/api/v1/knowledge-indexes", headers=auth_headers)
    assert indexes.status_code == 200
    assert indexes.json()["data"]["items"][0]["vector_collection"] == "knowledge_chunks"

    index_detail = client.get("/api/v1/knowledge-indexes/ki_sales_policy_v1", headers=auth_headers)
    assert index_detail.status_code == 200
    assert index_detail.json()["data"]["quality_gates"]
    assert index_detail.json()["data"]["effect"]["funnel"][-1]["stage"] == "writeback"

    gates = client.get(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/quality-gates", headers=auth_headers
    )
    effects = client.get(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/effects", headers=auth_headers
    )
    assert gates.status_code == 200
    assert gates.json()["meta"]["status_counts"]["success"] == 2
    assert effects.status_code == 200
    assert effects.json()["data"]["hit_rate"] == 0.846
    with SessionLocal() as session:
        assert session.get(KnowledgeSource, "ks_sales_policy") is not None
        assert session.get(KnowledgeIndex, "ki_sales_policy_v1") is not None
        assert session.get(KnowledgeQualityGate, "kg_recall") is not None
        assert session.get(KnowledgeEffect, "ke_sales_policy_v1") is not None
    source_trace = client.get(
        "/api/v1/traces/trace_knowledge_source_sales_policy", headers=auth_headers
    )
    assert any(
        span.get("kind") == "knowledge_source"
        and span.get("knowledge_source_id") == "ks_sales_policy"
        for span in source_trace.json()["data"]["spans"]
    )
    index_trace = client.get(
        "/api/v1/traces/trace_knowledge_index_sales_policy_v1", headers=auth_headers
    )
    assert any(
        span.get("kind") == "knowledge_index"
        and span.get("knowledge_index_id") == "ki_sales_policy_v1"
        for span in index_trace.json()["data"]["spans"]
    )
    effect_trace = client.get(
        "/api/v1/traces/trace_knowledge_effect_sales_policy_v1", headers=auth_headers
    )
    assert any(
        span.get("kind") == "knowledge_effect" and span.get("effect_id") == "ke_sales_policy_v1"
        for span in effect_trace.json()["data"]["spans"]
    )

    build_run = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "contract_build"},
        headers={**auth_headers, "Idempotency-Key": "knowledge-index-build"},
    )
    assert build_run.status_code == 202
    assert build_run.json()["data"]["run_type"] == "knowledge_build"
    assert build_run.json()["data"]["vector_collection"] == "knowledge_chunks"

    invalid_recall = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "   "},
        headers=auth_headers,
    )
    assert invalid_recall.status_code == 422
    assert invalid_recall.json()["error"]["code"] == "VALIDATION_ERROR"

    forbidden_recall = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "scope": {"project_id": "other_project"}},
        headers=auth_headers,
    )
    assert forbidden_recall.status_code == 403
    assert forbidden_recall.json()["error"]["code"] == "KNOWLEDGE_RECALL_SCOPE_FORBIDDEN"

    settings = client.get("/api/v1/settings", headers=auth_headers)
    assert settings.status_code == 200
    assert {item["setting_id"] for item in settings.json()["data"]["items"]} >= {
        "model-chain",
        "policy-guard",
    }

    missing_patch_key = client.patch(
        "/api/v1/settings/model-chain",
        json={"provider": "qwen_asr_shadow"},
        headers=auth_headers,
    )
    assert missing_patch_key.status_code == 400
    assert missing_patch_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    patch_setting = client.patch(
        "/api/v1/settings/model-chain",
        json={"provider": "qwen_asr_shadow", "change_reason": "contract"},
        headers={**auth_headers, "Idempotency-Key": "settings-patch-model-chain"},
    )
    assert patch_setting.status_code == 200
    assert patch_setting.json()["data"]["status"] == "draft"
    assert patch_setting.json()["data"]["provider"] == "qwen_asr_shadow"
    replay_setting = client.patch(
        "/api/v1/settings/model-chain",
        json={"provider": "qwen_asr_shadow", "change_reason": "contract"},
        headers={**auth_headers, "Idempotency-Key": "settings-patch-model-chain"},
    )
    assert replay_setting.status_code == 200
    assert replay_setting.json()["data"]["provider"] == "qwen_asr_shadow"
    conflict_setting = client.patch(
        "/api/v1/settings/model-chain",
        json={"provider": "another_provider", "change_reason": "contract"},
        headers={**auth_headers, "Idempotency-Key": "settings-patch-model-chain"},
    )
    assert conflict_setting.status_code == 409
    assert conflict_setting.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    setting_detail = client.get("/api/v1/settings/model-chain", headers=auth_headers)
    assert setting_detail.json()["data"]["provider"] == "qwen_asr_shadow"

    task_payload = {
        "task_version_id": "task_version_contract_draft",
        "task_type_id": "task_sales_quality",
        "version": "contract-draft",
        "canvas_variant": "stable-v3",
        "label_version": "label_v1_8_4",
    }
    task_version = client.post(
        "/api/v1/task-versions",
        json=task_payload,
        headers={**auth_headers, "Idempotency-Key": "task-version-contract"},
    )
    assert task_version.status_code == 201
    assert task_version.json()["data"]["status"] == "draft"

    drafts = client.get("/api/v1/task-versions?status=draft", headers=auth_headers)
    assert any(
        item["task_version_id"] == "task_version_contract_draft"
        for item in drafts.json()["data"]["items"]
    )

    publish = client.post(
        "/api/v1/task-versions/task_version_contract_draft/publish",
        json={"decision": "publish", "gate": "compatibility"},
        headers={**auth_headers, "Idempotency-Key": "task-version-publish-contract"},
    )
    assert publish.status_code == 202
    assert publish.json()["data"]["run_type"] == "task_version_publish"
    assert publish.json()["data"]["status"] == "blocked"


def test_high_risk_writes_reject_annotator_role(client):
    annotator_headers = {
        "Authorization": "Bearer annotator-token",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-rbac-annotator",
    }
    attempts = [
        (
            "/api/v1/label-versions/label_version_candidate/publish",
            {"decision": "publish"},
            "rbac-label-publish",
        ),
        (
            "/api/v1/task-versions/task_version_v3_2_1/publish",
            {"decision": "publish"},
            "rbac-task-publish",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/backfills",
            {"reason": "manual"},
            "rbac-asset-backfill",
        ),
        (
            "/api/v1/settings/publish-requests",
            {"settings_id": "model-chain"},
            "rbac-settings-publish",
        ),
        (
            "/api/v1/output-sinks/platform-callbacks",
            {"target": "crm_reception_order"},
            "rbac-platform-callback",
        ),
        (
            "/api/v1/exports",
            {"target": "evidence_pack", "object_id": "AF-128"},
            "rbac-export",
        ),
        (
            "/api/v1/insights/reports",
            {"report_type": "management_summary"},
            "rbac-insight-report",
        ),
    ]

    for path, payload, key in attempts:
        response = client.post(
            path,
            json=payload,
            headers={**annotator_headers, "Idempotency-Key": key},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "FORBIDDEN"
        assert body["error"]["trace_id"]


def test_json_resources_are_scoped_by_tenant_and_project(client, auth_headers):
    project = client.post(
        "/api/v1/projects",
        json={"project_id": "project_scope_contract", "name": "隔离项目"},
        headers={**auth_headers, "Idempotency-Key": "connector-scope-project-create"},
    )
    assert project.status_code == 201
    project_headers = {
        **auth_headers,
        "X-Project-Id": "project_scope_contract",
        "X-Request-Id": "pytest-project-scope",
    }
    other_headers = {
        "Authorization": "Bearer dev-token",
        "X-Tenant-Id": "other_tenant",
        "X-Project-Id": "other_project",
        "X-Request-Id": "pytest-other-scope",
    }
    connector_id = "shared_connector"

    aurora = client.post(
        "/api/v1/connectors",
        json={"connector_id": connector_id, "name": "同名连接器", "tenant_marker": "aurora"},
        headers={**auth_headers, "Idempotency-Key": "connector-scope-aurora"},
    )
    other = client.post(
        "/api/v1/connectors",
        json={"connector_id": connector_id, "name": "同名连接器", "tenant_marker": "project"},
        headers={**project_headers, "Idempotency-Key": "connector-scope-project"},
    )
    assert aurora.status_code == 201
    assert other.status_code == 201

    aurora_items = client.get("/api/v1/connectors", headers=auth_headers).json()["data"]["items"]
    project_items = client.get("/api/v1/connectors", headers=project_headers).json()["data"][
        "items"
    ]
    assert any(item.get("tenant_marker") == "aurora" for item in aurora_items)
    assert not any(item.get("tenant_marker") == "project" for item in aurora_items)
    assert any(item.get("tenant_marker") == "project" for item in project_items)
    assert not any(item.get("tenant_marker") == "aurora" for item in project_items)

    denied = client.get("/api/v1/connectors", headers=other_headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "TENANT_NOT_FOUND"

    with SessionLocal() as session:
        session.merge(
            Project(
                project_id="same_tenant_unassigned",
                tenant_id="aurora_auto",
                name="同租户未授权项目",
                status="active",
                data={
                    "project_id": "same_tenant_unassigned",
                    "tenant_id": "aurora_auto",
                    "name": "同租户未授权项目",
                    "status": "active",
                    "member_user_ids": ["u_model_001"],
                    "members": [{"user_id": "u_model_001", "roles": ["model_engineer"]}],
                },
            )
        )
        session.commit()

    same_tenant_denied = client.get(
        "/api/v1/connectors",
        headers={**auth_headers, "X-Project-Id": "same_tenant_unassigned"},
    )
    assert same_tenant_denied.status_code == 403
    assert same_tenant_denied.json()["error"]["code"] == "PROJECT_MEMBERSHIP_REQUIRED"

    visible_projects = client.get("/api/v1/projects", headers=auth_headers)
    assert visible_projects.status_code == 200
    assert not any(
        item.get("project_id") == "same_tenant_unassigned"
        for item in visible_projects.json()["data"]["items"]
    )


def test_project_detail_and_patch_are_bound_to_header_context(client, auth_headers):
    with SessionLocal() as session:
        session.add(
            Project(
                project_id="same_tenant_assigned",
                tenant_id="aurora_auto",
                name="同租户已授权项目",
                status="active",
                data={
                    "project_id": "same_tenant_assigned",
                    "tenant_id": "aurora_auto",
                    "name": "同租户已授权项目",
                    "status": "active",
                    "member_user_ids": ["u_admin_001"],
                    "members": [{"user_id": "u_admin_001", "roles": ["project_admin"]}],
                },
            )
        )
        session.commit()

    detail = client.get("/api/v1/projects/same_tenant_assigned", headers=auth_headers)
    assert detail.status_code == 403
    assert detail.json()["error"]["code"] == "PROJECT_CONTEXT_MISMATCH"

    patch = client.patch(
        "/api/v1/projects/same_tenant_assigned",
        json={"name": "越权修改"},
        headers={**auth_headers, "Idempotency-Key": "cross-project-patch"},
    )
    assert patch.status_code == 403
    assert patch.json()["error"]["code"] == "PROJECT_CONTEXT_MISMATCH"

    duplicate = client.post(
        "/api/v1/projects",
        json={"project_id": "same_tenant_assigned", "name": "覆盖已有项目"},
        headers={**auth_headers, "Idempotency-Key": "cross-project-create-takeover"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PROJECT_ALREADY_EXISTS"

    switched_headers = {**auth_headers, "X-Project-Id": "same_tenant_assigned"}
    switched = client.get(
        "/api/v1/projects/same_tenant_assigned",
        headers=switched_headers,
    )
    assert switched.status_code == 200
    assert switched.json()["data"]["project_id"] == "same_tenant_assigned"


def test_work_items_require_idempotency_and_replay(client, auth_headers):
    payload = {
        "module": "home",
        "action": "create_backfill",
        "title": "首页失败分区处理",
        "target": {"module": "assets", "asset_key": "auris/label/event_tags"},
    }
    missing = client.post("/api/v1/work-items", json=payload, headers=auth_headers)
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {**auth_headers, "Idempotency-Key": "pytest-work-item"}
    first = client.post("/api/v1/work-items", json=payload, headers=headers)
    second = client.post("/api/v1/work-items", json=payload, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert first.json()["meta"]["trace_id"]

    conflict = client.post(
        "/api/v1/work-items",
        json={**payload, "action": "changed_action"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_admin_writes_emit_idempotency_audit_and_outbox(client, auth_headers):
    system_headers = {
        **auth_headers,
        "Authorization": "Bearer system-token",
    }
    tenant_missing_key = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "contract_tenant", "name": "契约租户"},
        headers=system_headers,
    )
    assert tenant_missing_key.status_code == 400
    assert tenant_missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    tenant_headers = {**system_headers, "Idempotency-Key": "contract-tenant-create"}
    tenant = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "contract_tenant", "tenant_code": "contract", "name": "契约租户"},
        headers=tenant_headers,
    )
    tenant_replay = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "contract_tenant", "tenant_code": "contract", "name": "契约租户"},
        headers=tenant_headers,
    )
    assert tenant.status_code == 201
    assert tenant_replay.status_code == 201
    assert tenant.json()["data"]["tenant_id"] == tenant_replay.json()["data"]["tenant_id"]
    tenant_trace = client.get(
        f"/api/v1/traces/{tenant.json()['meta']['trace_id']}", headers=system_headers
    )
    tenant_spans = tenant_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == "contract_tenant"
        for span in tenant_spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "tenant.created"
        for span in tenant_spans
    )

    project_headers = {**auth_headers, "Idempotency-Key": "contract-project-create"}
    project_payload = {
        "project_id": "contract_project",
        "name": "契约项目",
        "owner_name": "契约负责人",
        "scene": "销售话术质检",
        "data_mode": "连接器画布导入",
        "label_version": "v1.8.4",
        "quality_target": "88%",
        "member_user_ids": ["u_admin_001"],
        "members": [{"user_id": "u_admin_001", "name": "契约负责人", "roles": ["project_admin"]}],
    }
    project = client.post(
        "/api/v1/projects",
        json=project_payload,
        headers=project_headers,
    )
    project_replay = client.post(
        "/api/v1/projects",
        json=project_payload,
        headers=project_headers,
    )
    assert project.status_code == 201
    assert project_replay.status_code == 201
    assert project.json()["data"]["project_id"] == project_replay.json()["data"]["project_id"]
    assert project.json()["data"]["tenant_id"] == "aurora_auto"
    assert project.json()["data"]["member_user_ids"] == ["u_admin_001"]
    assert project.json()["data"]["members"][0]["user_id"] == "u_admin_001"
    assert project.json()["data"]["trace_id"] == project.json()["meta"]["trace_id"]
    project_trace = client.get(
        f"/api/v1/traces/{project.json()['meta']['trace_id']}", headers=auth_headers
    )
    project_spans = project_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == "contract_project"
        for span in project_spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "project.created"
        for span in project_spans
    )

    connector_headers = {**auth_headers, "Idempotency-Key": "contract-connector-create"}
    connector_payload = {
        "connector_id": "contract_data_connector",
        "name": "契约数据连接器",
        "source_type": "authenticated_events_api",
        "target_asset_key": "auris/events/document_links",
        "source": "contract_test",
    }
    connector = client.post(
        "/api/v1/connectors",
        json=connector_payload,
        headers=connector_headers,
    )
    connector_replay = client.post(
        "/api/v1/connectors",
        json=connector_payload,
        headers=connector_headers,
    )
    assert connector.status_code == 201
    assert connector_replay.status_code == 201
    assert connector.json()["data"]["id"] == "contract_data_connector"
    assert (
        connector.json()["data"]["connector_id"] == connector_replay.json()["data"]["connector_id"]
    )
    assert connector.json()["data"]["target_asset_key"] == "auris/events/document_links"
    assert connector.json()["data"]["trace_id"] == connector.json()["meta"]["trace_id"]
    connector_trace = client.get(
        f"/api/v1/traces/{connector.json()['meta']['trace_id']}", headers=auth_headers
    )
    connector_spans = connector_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == "contract_data_connector"
        for span in connector_spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "connectors.created"
        for span in connector_spans
    )


def test_tenant_management_is_system_scoped(client, auth_headers):
    denied_create = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "tenant_b", "tenant_code": "tenant-b", "name": "租户 B"},
        headers={**auth_headers, "Idempotency-Key": "tenant-b-project-admin-create"},
    )
    assert denied_create.status_code == 403
    assert denied_create.json()["error"]["code"] == "FORBIDDEN"

    system_headers = {
        **auth_headers,
        "Authorization": "Bearer system-token",
        "Idempotency-Key": "tenant-b-system-create",
    }
    created = client.post(
        "/api/v1/tenants",
        json={"tenant_id": "tenant_b", "tenant_code": "tenant-b", "name": "租户 B"},
        headers=system_headers,
    )
    assert created.status_code == 201

    tenant_list = client.get("/api/v1/tenants", headers=auth_headers)
    assert tenant_list.status_code == 200
    visible_tenants = [item["tenant_id"] for item in tenant_list.json()["data"]["items"]]
    assert visible_tenants == ["aurora_auto"]

    denied_detail = client.get("/api/v1/tenants/tenant_b", headers=auth_headers)
    assert denied_detail.status_code == 403
    assert denied_detail.json()["error"]["code"] == "FORBIDDEN"

    denied_patch = client.patch(
        "/api/v1/tenants/tenant_b",
        json={"status": "suspended"},
        headers={**auth_headers, "Idempotency-Key": "tenant-b-project-admin-patch"},
    )
    assert denied_patch.status_code == 403
    assert denied_patch.json()["error"]["code"] == "FORBIDDEN"

    system_list = client.get(
        "/api/v1/tenants",
        headers={**auth_headers, "Authorization": "Bearer system-token"},
    )
    assert system_list.status_code == 200
    assert {item["tenant_id"] for item in system_list.json()["data"]["items"]} >= {
        "aurora_auto",
        "tenant_b",
    }


def test_project_detail_create_and_patch_reject_cross_tenant_scope(client, auth_headers):
    with SessionLocal() as session:
        session.merge(
            Tenant(
                tenant_id="tenant_b",
                tenant_code="tenant-b",
                name="租户 B",
                status="active",
                data={
                    "tenant_id": "tenant_b",
                    "tenant_code": "tenant-b",
                    "name": "租户 B",
                    "status": "active",
                },
            )
        )
        session.merge(
            Project(
                project_id="foreign_project",
                tenant_id="tenant_b",
                name="外部项目",
                status="active",
                data={
                    "project_id": "foreign_project",
                    "tenant_id": "tenant_b",
                    "name": "外部项目",
                    "status": "active",
                },
            )
        )
        session.commit()

    denied_detail = client.get("/api/v1/projects/foreign_project", headers=auth_headers)
    assert denied_detail.status_code == 403
    assert denied_detail.json()["error"]["code"] == "PROJECT_CONTEXT_MISMATCH"

    denied_patch = client.patch(
        "/api/v1/projects/foreign_project",
        json={"status": "paused"},
        headers={**auth_headers, "Idempotency-Key": "foreign-project-patch"},
    )
    assert denied_patch.status_code == 403
    assert denied_patch.json()["error"]["code"] == "PROJECT_CONTEXT_MISMATCH"

    denied_create_overwrite = client.post(
        "/api/v1/projects",
        json={"project_id": "foreign_project", "name": "覆盖外部项目"},
        headers={**auth_headers, "Idempotency-Key": "foreign-project-create"},
    )
    assert denied_create_overwrite.status_code == 403
    assert denied_create_overwrite.json()["error"]["code"] == "FORBIDDEN"


def test_data_aggregation_view_patch_is_persisted_and_replayable(client, auth_headers):
    missing_key = client.patch(
        "/api/v1/data-aggregation-views/view_audio_event_space_person",
        json={"priority": ["time", "space", "event", "person"]},
        headers=auth_headers,
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {**auth_headers, "Idempotency-Key": "contract-aggregation-view"}
    payload = {"priority": ["time", "space", "event", "person"], "reason": "contract"}
    first = client.patch(
        "/api/v1/data-aggregation-views/view_audio_event_space_person",
        json=payload,
        headers=headers,
    )
    replay = client.patch(
        "/api/v1/data-aggregation-views/view_audio_event_space_person",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["data"]["id"] == replay.json()["data"]["id"]
    assert first.json()["data"]["priority"] == ["time", "space", "event", "person"]

    trace = client.get(f"/api/v1/traces/{first.json()['meta']['trace_id']}", headers=auth_headers)
    spans = trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "resource"
        and span.get("collection") == "data_aggregation_views"
        and span.get("id") == "view_audio_event_space_person"
        for span in spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "data_aggregation_views.upserted"
        for span in spans
    )


def test_trace_lookup_is_scoped_by_tenant_and_project(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
        },
        headers={**auth_headers, "Idempotency-Key": "trace-scope-run"},
    )
    assert response.status_code == 202
    trace_id = response.json()["meta"]["trace_id"]

    same_scope = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert same_scope.status_code == 200
    assert same_scope.json()["data"]["spans"]

    other_scope = client.get(
        f"/api/v1/traces/{trace_id}",
        headers={
            "Authorization": "Bearer dev-token",
            "X-Tenant-Id": "other_tenant",
            "X-Project-Id": "other_project",
            "X-Request-Id": "pytest-trace-other",
        },
    )
    assert other_scope.status_code == 403
    assert other_scope.json()["error"]["code"] == "TENANT_NOT_FOUND"


def test_labeling_evaluation_insight_closed_loop_contract(client, auth_headers):
    def dispatch_run(run_id: str) -> tuple[str, str, dict]:
        external_id_keys = {
            "dagster": "external_run_id",
            "object_storage": "storage_object_id",
            "external_callback": "callback_receipt_id",
        }
        for _attempt in range(30):
            with SessionLocal() as session:
                run = session.get(RunRecord, run_id)
                assert run is not None
                if run.status == "submitted":
                    dispatch = run.payload["dispatch"]
                    adapter = str(dispatch["adapter"])
                    details = dict(dispatch["details"])
                    external_id = details[external_id_keys[adapter]]
                    return adapter, str(external_id), details
                assert run.status == "pending", run.payload
            assert process_once() >= 1
        raise AssertionError(f"运行未被调度：{run_id}")

    def trace_spans(trace_id: str) -> list[dict]:
        trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
        assert trace.status_code == 200
        spans = trace.json()["data"]["spans"]
        assert spans
        return spans

    def assert_trace_has(trace_id: str, *, kind: str, **fields: str) -> None:
        spans = trace_spans(trace_id)
        assert any(
            span.get("kind") == kind
            and all(str(span.get(key)) == str(value) for key, value in fields.items())
            for span in spans
        ), {"trace_id": trace_id, "kind": kind, "fields": fields, "spans": spans}

    label_payload = {
        "base_version": "v1.8.4",
        "source": "contract_test",
        "changeset": [
            {
                "task": "报价金额",
                "rule": "金额字段必须绑定 ASR 证据窗口、报价单和人工确认状态",
                "evidence_ref": "AF-128",
            }
        ],
    }
    label = client.post(
        "/api/v1/label-versions",
        json=label_payload,
        headers={**auth_headers, "Idempotency-Key": "closed-loop-label"},
    )
    replay_label = client.post(
        "/api/v1/label-versions",
        json=label_payload,
        headers={**auth_headers, "Idempotency-Key": "closed-loop-label"},
    )
    assert label.status_code == 201
    assert replay_label.status_code == 201
    assert label.json()["data"]["id"] == replay_label.json()["data"]["id"]
    assert label.json()["data"]["status"] == "draft"
    label_id = label.json()["data"]["id"]
    label_trace_id = label.json()["meta"]["trace_id"]
    label_detail = client.get(f"/api/v1/label-versions/{label_id}", headers=auth_headers)
    assert label_detail.status_code == 200
    assert label_detail.json()["data"]["id"] == label_id
    assert label_detail.json()["data"]["base_version"] == "v1.8.4"
    label_versions = client.get("/api/v1/label-versions?status=draft", headers=auth_headers).json()[
        "data"
    ]["items"]
    assert any(item["id"] == label_id for item in label_versions)
    assert_trace_has(label_trace_id, kind="resource", collection="label_versions", id=label_id)
    assert_trace_has(label_trace_id, kind="audit", object_id=label_id)

    eval_payload = {
        "dataset_id": "eval_quote_guard_v12",
        "model_version": "prod-v5",
        "label_version": "v1.9.0-rc2",
        "source": "contract_test",
        "label_version_id": label_id,
    }
    eval_run = client.post(
        "/api/v1/eval-runs",
        json=eval_payload,
        headers={**auth_headers, "Idempotency-Key": "closed-loop-eval"},
    )
    assert eval_run.status_code == 202
    eval_run_body = eval_run.json()
    eval_run_id = eval_run_body["data"]["run_id"]
    assert eval_run_body["data"]["run_type"] == "eval_run"
    assert eval_run_body["data"]["status"] == "pending"
    eval_trace_id = eval_run_body["meta"]["trace_id"]
    eval_detail = client.get(f"/api/v1/eval-runs/{eval_run_id}", headers=auth_headers)
    assert eval_detail.status_code == 200
    assert eval_detail.json()["data"]["run_id"] == eval_run_id
    eval_runs = client.get("/api/v1/eval-runs", headers=auth_headers).json()["data"]["items"]
    assert any(item["run_id"] == eval_run_id for item in eval_runs)
    assert_trace_has(eval_trace_id, kind="run", run_type="eval_run", id=eval_run_id)
    assert_trace_has(eval_trace_id, kind="outbox", event_type="eval_run.requested")

    missing_feedback = client.post(
        "/api/v1/eval-runs/missing-run/feedback-tasks",
        json={"badcase_refs": ["B-2031"], "target": "Prompt 优化"},
        headers={**auth_headers, "Idempotency-Key": "closed-loop-feedback-missing"},
    )
    assert missing_feedback.status_code == 404
    assert missing_feedback.json()["error"]["code"] == "NOT_FOUND"

    empty_feedback = client.post(
        f"/api/v1/eval-runs/{eval_run_id}/feedback-tasks",
        json={"badcase_refs": [], "target": "Prompt 优化"},
        headers={**auth_headers, "Idempotency-Key": "closed-loop-feedback-empty"},
    )
    assert empty_feedback.status_code == 400
    assert empty_feedback.json()["error"]["code"] == "BADCASE_REFS_REQUIRED"

    task_run = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/13",
        },
        headers={**auth_headers, "Idempotency-Key": "closed-loop-non-eval-run"},
    )
    assert task_run.status_code == 202
    invalid_feedback = client.post(
        f"/api/v1/eval-runs/{task_run.json()['data']['run_id']}/feedback-tasks",
        json={"badcase_refs": ["B-2031"], "target": "Prompt 优化"},
        headers={**auth_headers, "Idempotency-Key": "closed-loop-feedback-invalid-type"},
    )
    assert invalid_feedback.status_code == 409
    assert invalid_feedback.json()["error"]["code"] == "INVALID_EVAL_RUN"

    feedback = client.post(
        f"/api/v1/eval-runs/{eval_run_id}/feedback-tasks",
        json={
            "badcase_refs": ["B-2031", "LC-quote-002"],
            "target": "标签规则 / Prompt 优化 / 打标黄金集",
            "source": "contract_test",
        },
        headers={**auth_headers, "Idempotency-Key": "closed-loop-feedback"},
    )
    assert feedback.status_code == 202
    feedback_data = feedback.json()["data"]
    assert feedback_data["eval_run_id"] == eval_run_id
    assert feedback_data["run_type"] == "eval_feedback"
    assert feedback_data["feedback_task_id"].startswith(f"feedback_{eval_run_id}_")
    assert feedback_data["eval_run_trace_id"] == eval_trace_id
    assert {"type": "feedback_task", "id": feedback_data["feedback_task_id"]} in feedback_data[
        "affected_objects"
    ]
    assert {"type": "badcase", "id": "B-2031"} in feedback_data["affected_objects"]

    metric_keys = ["reception_conversion_quality", "quote_consistency"]
    metric_scope = {
        "time_range": "2025-05-01/2025-05-31",
        "store_ids": ["BJ-AURORA-001"],
        "model_version": "prod-v5",
        "label_version": "v1.9.0-rc2",
    }
    insight_metric_run = client.post(
        "/api/v1/insights/metric-runs",
        json={
            "metric_keys": metric_keys,
            **metric_scope,
            "source": "contract_test",
        },
        headers={**auth_headers, "Idempotency-Key": "closed-loop-insight-metrics"},
    )
    assert insight_metric_run.status_code == 202, insight_metric_run.text
    metric_run_body = insight_metric_run.json()
    metric_run_id = metric_run_body["data"]["run_id"]
    assert metric_run_body["data"]["run_type"] == "insight_metric_aggregation"
    assert metric_run_body["data"]["status"] == "pending"
    assert metric_run_body["data"]["metric_keys"] == metric_keys
    assert_trace_has(
        metric_run_body["meta"]["trace_id"],
        kind="outbox",
        event_type="insight_metric_aggregation.requested",
    )

    metric_adapter, metric_external_id, _metric_dispatch = dispatch_run(metric_run_id)
    assert metric_adapter == "dagster"
    metric_completion = client.post(
        f"/api/v1/runs/{metric_run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "closed-loop-insight-metrics-completed",
            "external_id": metric_external_id,
            "result_ref": {
                "metric_results": [
                    {
                        "metric_key": "reception_conversion_quality",
                        "value": 82.4,
                        "unit": "score",
                        "sample_size": 1204,
                    },
                    {
                        "metric_key": "quote_consistency",
                        "value": 74.2,
                        "unit": "percent",
                        "sample_size": 842,
                    },
                ]
            },
            "metrics": {"materialized_count": 2},
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "closed-loop-insight-metrics-receipt",
        },
    )
    assert metric_completion.status_code == 200, metric_completion.text
    metric_completion_data = metric_completion.json()["data"]
    assert metric_completion_data["status"] == "success"
    materialization = metric_completion_data["insight_completion"]
    assert materialization["status"] == "materialized"
    assert materialization["source_run_id"] == metric_run_id
    metric_result_ids = materialization["metric_result_ids"]
    assert len(metric_result_ids) == len(metric_keys)

    insight_report = client.post(
        "/api/v1/insights/reports",
        json={
            "report_type": "management_summary",
            **metric_scope,
            "metric_result_ids": metric_result_ids,
            "evidence_refs": ["AF-128", "BJ-041"],
            "report_sections": ["north_star", "risk_root_cause", "next_actions"],
            "source": "contract_test",
        },
        headers={**auth_headers, "Idempotency-Key": "closed-loop-insight-report"},
    )
    assert insight_report.status_code == 202
    assert insight_report.json()["data"]["run_type"] == "insight_report"
    assert insight_report.json()["data"]["status"] == "pending"
    report_body = insight_report.json()
    report_id = report_body["data"]["report_id"]
    assert report_body["data"]["metric_result_ids"] == metric_result_ids
    assert report_body["data"]["metric_refs"] == metric_keys
    reports = client.get("/api/v1/insights/reports", headers=auth_headers)
    assert reports.status_code == 200
    report_items = reports.json()["data"]["items"]
    assert any(
        item.get("id") == report_id and item.get("run_id") == report_body["data"]["run_id"]
        for item in report_items
    )
    assert_trace_has(
        report_body["meta"]["trace_id"],
        kind="resource",
        collection="insight_reports",
        id=report_id,
    )
    assert_trace_has(
        report_body["meta"]["trace_id"],
        kind="outbox",
        event_type="export.requested",
    )

    report_adapter, report_external_id, report_dispatch = dispatch_run(
        report_body["data"]["run_id"]
    )
    assert report_adapter == "object_storage"
    report_completion = client.post(
        f"/api/v1/runs/{report_body['data']['run_id']}/completion-receipts",
        json={
            "adapter": "object_storage",
            "status": "success",
            "completion_receipt_id": "closed-loop-insight-report-completed",
            "external_id": report_external_id,
            "result_ref": {
                "storage_object_id": report_external_id,
                "object_uri": report_dispatch["object_uri"],
                "content_sha256": report_dispatch["content_sha256"],
                "content_type": report_dispatch["content_type"],
            },
            "metrics": {"section_count": 3, "evidence_count": 2},
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "closed-loop-insight-report-receipt",
        },
    )
    assert report_completion.status_code == 200, report_completion.text
    assert report_completion.json()["data"]["insight_completion"] == {
        "report_id": report_id,
        "status": "generated",
        "invalidated_actions": 0,
        "invalidated_experiments": 0,
        "cancelled_runs": 0,
    }
    report_detail = client.get(f"/api/v1/insights/reports/{report_id}", headers=auth_headers)
    assert report_detail.status_code == 200
    assert report_detail.json()["data"]["status"] == "generated"
    assert report_detail.json()["data"]["metric_result_ids"] == metric_result_ids

    insight_action = client.post(
        "/api/v1/insights/actions",
        json={
            "report_id": report_id,
            "metric_result_id": metric_result_ids[0],
            "metric_key": "reception_conversion_quality",
            "action_type": "create_training_action",
            "owner": "业务运营",
            "evidence_refs": ["AF-128", "BJ-041"],
            "source": "contract_test",
        },
        headers={**auth_headers, "Idempotency-Key": "closed-loop-insight-action"},
    )
    assert insight_action.status_code == 201
    insight_action_body = insight_action.json()
    action_id = insight_action_body["data"]["id"]
    assert action_id.startswith("insight_action_")
    assert insight_action.json()["data"]["status"] == "experiment_ready"
    action_detail = client.get(f"/api/v1/insights/actions/{action_id}", headers=auth_headers)
    assert action_detail.status_code == 200
    assert action_detail.json()["data"]["report_id"] == report_id
    assert_trace_has(
        insight_action_body["meta"]["trace_id"],
        kind="resource",
        collection="work_items",
        id=action_id,
    )

    for response in [
        label,
        eval_run,
        feedback,
        insight_metric_run,
        metric_completion,
        insight_report,
        report_completion,
        insight_action,
    ]:
        trace_id = response.json()["meta"]["trace_id"]
        assert trace_spans(trace_id)
