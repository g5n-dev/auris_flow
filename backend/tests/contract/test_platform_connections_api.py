from __future__ import annotations

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import AuditLog, JsonResource, OutboxEvent, PlatformConnection
from app.services import platform_connection_service


def _write_headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _connection_payload() -> dict[str, object]:
    return {
        "name": "测试平台连接",
        "provider_type": "generic_http",
        "auth_mode": "bearer",
        "origin": "https://recordings.example.test",
        "credential_ref": "secret://platform/audio-reader",
        "external_tenant_ref": "tenant-ext-001",
        "store_refs": ["BJ-AURORA-001"],
        "test_path": "/v1/recordings",
    }


def test_platform_connection_is_a_scoped_strong_resource_with_readback(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/api/v1/platform-connections",
        json=_connection_payload(),
        headers=_write_headers(auth_headers, "platform-connection-create"),
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    connection_id = data["platform_connection_id"]
    created_trace_id = created.json()["meta"]["trace_id"]
    assert data == {
        "platform_connection_id": connection_id,
        "name": "测试平台连接",
        "provider_type": "generic_http",
        "auth_mode": "bearer",
        "origin": "https://recordings.example.test",
        "credential_ref": "secret://platform/audio-reader",
        "external_tenant_ref": "tenant-ext-001",
        "store_refs": ["BJ-AURORA-001"],
        "test_path": "/v1/recordings",
        "status": "draft",
        "resource_version": 1,
        "last_test_status": None,
        "last_tested_at": None,
        "root_trace_id": created_trace_id,
        "current_trace_id": created_trace_id,
    }

    replay = client.post(
        "/api/v1/platform-connections",
        json=_connection_payload(),
        headers=_write_headers(auth_headers, "platform-connection-create"),
    )
    assert replay.status_code == 201
    assert replay.json()["data"] == data

    detail = client.get(
        f"/api/v1/platform-connections/{connection_id}",
        headers=auth_headers,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"] == data

    listing = client.get(
        "/api/v1/platform-connections?limit=1",
        headers=auth_headers,
    )
    assert listing.status_code == 200, listing.text
    items = listing.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["platform_connection_id"] == connection_id
    assert listing.json()["meta"]["total"] >= 2
    assert listing.json()["meta"]["next_cursor"]

    with SessionLocal() as session:
        row = session.scalar(
            select(PlatformConnection).where(
                PlatformConnection.platform_connection_id == connection_id,
                PlatformConnection.tenant_id == "aurora_auto",
                PlatformConnection.project_id == "sales_qa",
            )
        )
        assert row is not None
        assert row.credential_ref == "secret://platform/audio-reader"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.object_id == connection_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == connection_id)
            )
            == 1
        )


def test_platform_connection_connection_test_is_real_and_persisted(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    created = client.post(
        "/api/v1/platform-connections",
        json=_connection_payload(),
        headers=_write_headers(auth_headers, "platform-connection-probe-create"),
    )
    assert created.status_code == 201, created.text
    connection_id = created.json()["data"]["platform_connection_id"]

    observed: dict[str, object] = {}

    def fake_probe(connection: PlatformConnection) -> dict[str, object]:
        observed.update(
            {
                "connection_id": connection.platform_connection_id,
                "origin": connection.origin,
                "credential_ref": connection.credential_ref,
            }
        )
        return {"response_status": 200}

    monkeypatch.setattr(
        platform_connection_service,
        "probe_platform_connection",
        fake_probe,
    )
    tested = client.post(
        f"/api/v1/platform-connections/{connection_id}/connection-tests",
        json={},
        headers=_write_headers(auth_headers, "platform-connection-probe-test"),
    )
    assert tested.status_code == 200, tested.text
    assert observed == {
        "connection_id": connection_id,
        "origin": "https://recordings.example.test",
        "credential_ref": "secret://platform/audio-reader",
    }
    tested_data = tested.json()["data"]
    assert tested_data["status"] == "success"
    assert tested_data["response_status"] == 200
    assert tested_data["readback_url"].endswith(f"/api/v1/platform-connections/{connection_id}")
    assert tested_data["root_trace_id"] == created.json()["data"]["root_trace_id"]
    assert tested_data["current_trace_id"] == tested.json()["meta"]["trace_id"]

    readback = client.get(
        f"/api/v1/platform-connections/{connection_id}",
        headers=auth_headers,
    )
    assert readback.status_code == 200
    assert readback.json()["data"]["status"] == "active"
    assert readback.json()["data"]["last_test_status"] == "success"
    assert readback.json()["data"]["last_tested_at"]


def test_all_connector_writes_recursively_reject_plaintext_credentials(
    client,
    auth_headers,
) -> None:
    plaintext = "Bearer must-never-be-persisted"
    response = client.post(
        "/api/v1/connectors",
        json={
            "connector_id": "connector_nested_plaintext",
            "name": "非法连接器",
            "type": "generic_http",
            "configuration": {
                "headers": {
                    "authorization": plaintext,
                }
            },
        },
        headers=_write_headers(auth_headers, "connector-nested-plaintext"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PLAINTEXT_CREDENTIAL_FORBIDDEN"
    assert plaintext not in response.text

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(JsonResource).where(
                    JsonResource.collection == "connectors",
                    JsonResource.resource_key == "connector_nested_plaintext",
                )
            )
            is None
        )

    for index, field in enumerate(("accessToken", "clientSecret", "refreshToken")):
        response = client.post(
            "/api/v1/connectors",
            json={
                "connector_id": f"connector_camelcase_plaintext_{index}",
                "name": "非法连接器",
                "type": "generic_http",
                "configuration": {"credentials": {field: "must-never-persist"}},
            },
            headers=_write_headers(
                auth_headers,
                f"connector-camelcase-plaintext-{index}",
            ),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "PLAINTEXT_CREDENTIAL_FORBIDDEN"

    for index, credential_ref in enumerate(("actual-secret", "Bearer live-token")):
        response = client.post(
            "/api/v1/connectors",
            json={
                "connector_id": f"connector_invalid_credential_ref_{index}",
                "name": "非法连接器",
                "type": "generic_http",
                "configuration": {"credential_ref": credential_ref},
            },
            headers=_write_headers(
                auth_headers,
                f"connector-invalid-credential-ref-{index}",
            ),
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "CREDENTIAL_REFERENCE_INVALID"


def test_platform_connection_rejects_plaintext_and_legacy_session_endpoint(
    client,
    auth_headers,
) -> None:
    plaintext = "RawPlatformPassword123"
    payload = _connection_payload()
    payload["credential_ref"] = plaintext
    rejected = client.post(
        "/api/v1/platform-connections",
        json=payload,
        headers=_write_headers(auth_headers, "platform-connection-plaintext"),
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "CREDENTIAL_REFERENCE_INVALID"
    assert plaintext not in rejected.text

    deprecated = client.post(
        "/api/v1/platform-connections/platformAuth/session",
        json={"scope": "current_project"},
        headers=_write_headers(auth_headers, "platform-session-deprecated"),
    )
    assert deprecated.status_code == 410
    assert deprecated.json()["error"]["code"] == "PLATFORM_SESSION_ENDPOINT_DEPRECATED"


def test_platform_connection_id_is_server_generated_and_client_namespace_is_rejected(
    client,
    auth_headers,
) -> None:
    created = client.post(
        "/api/v1/platform-connections",
        json=_connection_payload(),
        headers=_write_headers(auth_headers, "platform-connection-server-id"),
    )
    assert created.status_code == 201, created.text
    generated_id = created.json()["data"]["platform_connection_id"]
    assert generated_id.startswith("platform_connection_")

    for index, supplied_id in enumerate((generated_id, "shared-prod")):
        client_named = client.post(
            "/api/v1/platform-connections",
            json={
                **_connection_payload(),
                "platform_connection_id": supplied_id,
            },
            headers=_write_headers(
                auth_headers,
                f"platform-connection-client-id-{index}",
            ),
        )
        assert client_named.status_code == 422
        assert client_named.json()["error"]["code"] == "PLATFORM_CONNECTION_INVALID"


def test_platform_connection_runtime_openapi_documents_strong_commands(client):
    paths = client.get("/openapi.json").json()["paths"]
    create = paths["/api/v1/platform-connections"]["post"]
    assert create["requestBody"]["required"] is True
    create_schema = create["requestBody"]["content"]["application/json"]["schema"]
    assert set(create_schema["required"]) >= {"origin", "credential_ref"}
    assert "platform_connection_id" not in create_schema["properties"]
    assert create_schema["additionalProperties"] is False

    connection_test = paths[
        "/api/v1/platform-connections/{platform_connection_id}/connection-tests"
    ]["post"]
    assert connection_test["requestBody"]["required"] is True
    test_schema = connection_test["requestBody"]["content"]["application/json"]["schema"]
    assert test_schema["additionalProperties"] is False

    deprecated = paths["/api/v1/platform-connections/{platform_connection_id}/session"]["post"]
    assert "410" in deprecated["responses"]
    assert "200" not in deprecated["responses"]
