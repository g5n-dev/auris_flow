from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest

from app.api.routers import generic
from app.core.database import SessionLocal
from app.models import RunRecord

BODY = b'{"artifact_state":"materialized","rows":2}\n'


class _VersionLockedObjectClient:
    def __init__(self) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.head_calls: list[dict[str, Any]] = []

    def allows_bucket(self, bucket: str) -> bool:
        return bucket == "auris-flow-local"

    def head_object(self, bucket: str, object_key: str, *, if_match: str) -> dict[str, Any]:
        self.head_calls.append({"bucket": bucket, "object_key": object_key, "if_match": if_match})
        return {
            "status": 200,
            "etag": "export-etag-v1",
            "content_length": str(len(BODY)),
            "content_type": "application/jsonl",
        }

    def open_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None,
        if_match: str,
    ) -> dict[str, Any]:
        self.open_calls.append(
            {
                "bucket": bucket,
                "object_key": object_key,
                "byte_range": byte_range,
                "if_match": if_match,
            }
        )
        if byte_range == "bytes=0-7":
            body = BODY[:8]
            status = 206
            content_range = f"bytes 0-7/{len(BODY)}"
        else:
            body = BODY
            status = 200
            content_range = None
        return {
            "status": status,
            "etag": "export-etag-v1",
            "content_length": str(len(body)),
            "content_range": content_range,
            "content_type": "application/jsonl",
            "stream": BytesIO(body),
        }


class _InvalidMetadataObjectClient(_VersionLockedObjectClient):
    def __init__(self, mutation: dict[str, Any]) -> None:
        super().__init__()
        self.mutation = mutation

    def open_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None,
        if_match: str,
    ) -> dict[str, Any]:
        result = super().open_object(
            bucket,
            object_key,
            byte_range=byte_range,
            if_match=if_match,
        )
        result.update(self.mutation)
        return result


def _seed_export(*, run_id: str, status: str = "success", project_id: str = "sales_qa") -> None:
    with SessionLocal.begin() as session:
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id=project_id,
                run_type="export",
                status=status,
                trace_id=f"trace_{run_id}",
                payload={
                    "format": "jsonl",
                    "target": "evidence_pack",
                    "object_id": "AF-EXPORT",
                    "content_type": "application/jsonl",
                    "dispatch": {
                        "adapter": "object_storage",
                        "details": {
                            "provider": "minio",
                            "bucket": "auris-flow-local",
                            "object_key": (
                                f"tenants/aurora_auto/projects/{project_id}/exports/{run_id}.jsonl"
                            ),
                            "object_uri": "s3://must-not-cross-public-boundary/export.jsonl",
                            "storage_object_id": "obj_must_not_cross_public_boundary",
                            "etag": "export-etag-v1",
                            "content_length": len(BODY),
                            "content_type": "application/jsonl",
                        },
                    },
                },
            )
        )


def test_export_download_is_scoped_version_locked_bff_stream(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id = "export_bff_download_ready"
    _seed_export(run_id=run_id)
    object_client = _VersionLockedObjectClient()
    monkeypatch.setattr(
        generic,
        "object_storage_client_for_provider",
        lambda provider: object_client,
    )

    detail = client.get(f"/api/v1/exports/{run_id}", headers=auth_headers)
    full = client.get(f"/api/v1/exports/{run_id}/download", headers=auth_headers)
    partial = client.get(
        f"/api/v1/exports/{run_id}/download",
        headers={**auth_headers, "Range": "bytes=0-7"},
    )
    head = client.head(
        f"/api/v1/exports/{run_id}/download",
        headers={**auth_headers, "Range": "bytes=0-7"},
    )

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["download_ref"] == {
        "kind": "bff_download",
        "status": "ready",
        "href": f"/api/v1/exports/{run_id}/download",
        "content_type": "application/jsonl",
        "expires_at": None,
    }
    assert "bucket" not in detail.text
    assert "object_key" not in detail.text
    assert "object_uri" not in detail.text
    assert "storage_object_id" not in detail.text

    assert full.status_code == 200
    assert full.content == BODY
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["etag"] == '"export-etag-v1"'
    assert full.headers["content-disposition"].startswith("attachment;")
    assert partial.status_code == 206
    assert partial.content == BODY[:8]
    assert partial.headers["content-range"] == f"bytes 0-7/{len(BODY)}"
    assert head.status_code == 206
    assert head.content == b""
    assert head.headers["content-range"] == f"bytes 0-7/{len(BODY)}"
    assert head.headers["content-length"] == "8"
    assert all(call["if_match"] == '"export-etag-v1"' for call in object_client.open_calls)
    assert object_client.head_calls == [
        {
            "bucket": "auris-flow-local",
            "object_key": (
                "tenants/aurora_auto/projects/sales_qa/exports/export_bff_download_ready.jsonl"
            ),
            "if_match": '"export-etag-v1"',
        }
    ]


def test_export_download_fails_closed_before_success_and_across_scope(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    _seed_export(run_id="export_bff_not_ready", status="submitted")
    _seed_export(run_id="export_bff_other_project", project_id="other_project")
    object_client = _VersionLockedObjectClient()
    monkeypatch.setattr(
        generic,
        "object_storage_client_for_provider",
        lambda provider: object_client,
    )

    pending = client.get("/api/v1/exports/export_bff_not_ready/download", headers=auth_headers)
    hidden = client.get("/api/v1/exports/export_bff_other_project/download", headers=auth_headers)

    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "EXPORT_DOWNLOAD_NOT_READY"
    assert hidden.status_code == 404
    assert object_client.open_calls == []
    assert object_client.head_calls == []


def test_export_detail_head_and_download_require_project_admin_even_with_known_run_id(
    client,
    monkeypatch,
) -> None:
    run_id = "export_bff_sensitive_known_id"
    _seed_export(run_id=run_id)
    object_client = _VersionLockedObjectClient()
    monkeypatch.setattr(
        generic,
        "object_storage_client_for_provider",
        lambda provider: object_client,
    )
    annotator_headers = {
        "Authorization": "Bearer annotator-token",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-export-read-role",
    }

    responses = (
        client.get(f"/api/v1/exports/{run_id}", headers=annotator_headers),
        client.get(f"/api/v1/exports/{run_id}/download", headers=annotator_headers),
        client.head(f"/api/v1/exports/{run_id}/download", headers=annotator_headers),
    )

    assert [response.status_code for response in responses] == [403, 403, 403]
    assert responses[0].json()["error"]["code"] == "FORBIDDEN"
    assert responses[1].json()["error"]["code"] == "FORBIDDEN"
    assert object_client.open_calls == []
    assert object_client.head_calls == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ({"status": "not-an-http-status"}, "EXPORT_OBJECT_INVALID"),
        ({"content_type": "text/html"}, "EXPORT_OBJECT_CONTENT_TYPE_MISMATCH"),
        (
            {"content_range": f"bytes 0-{len(BODY) - 1}/{len(BODY)}"},
            "EXPORT_OBJECT_RANGE_INVALID",
        ),
    ),
)
def test_export_download_rejects_untrusted_upstream_metadata(
    client,
    auth_headers,
    monkeypatch,
    mutation: dict[str, Any],
    expected_code: str,
) -> None:
    run_id = f"export_bff_bad_metadata_{expected_code.lower()}"
    _seed_export(run_id=run_id)
    object_client = _InvalidMetadataObjectClient(mutation)
    monkeypatch.setattr(
        generic,
        "object_storage_client_for_provider",
        lambda provider: object_client,
    )

    response = client.get(f"/api/v1/exports/{run_id}/download", headers=auth_headers)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == expected_code


def test_export_download_rejects_unsafe_persisted_content_type(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id = "export_bff_unsafe_content_type"
    _seed_export(run_id=run_id)
    with SessionLocal.begin() as session:
        record = session.get(RunRecord, run_id)
        assert record is not None
        payload = dict(record.payload)
        dispatch = dict(payload["dispatch"])
        details = dict(dispatch["details"])
        details["content_type"] = "application/jsonl\r\nX-Injected: yes"
        dispatch["details"] = details
        payload["dispatch"] = dispatch
        record.payload = payload

    object_client = _VersionLockedObjectClient()
    monkeypatch.setattr(
        generic,
        "object_storage_client_for_provider",
        lambda provider: object_client,
    )

    response = client.get(f"/api/v1/exports/{run_id}/download", headers=auth_headers)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EXPORT_DOWNLOAD_NOT_READY"
    assert object_client.open_calls == []


def test_export_download_filename_cannot_inject_response_headers() -> None:
    record = RunRecord(
        run_id='export_ready\r\nX-Injected: yes"',
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type="export",
        status="success",
        trace_id="trace_export_header",
        payload={"format": "jsonl"},
    )

    headers = generic._export_response_headers(  # noqa: SLF001
        record,
        {"content_type": "application/jsonl", "etag": "export-etag-v1"},
        content_length=1,
    )

    disposition = headers["Content-Disposition"]
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert ":" not in disposition
    assert disposition == 'attachment; filename="export-export_ready-X-Injected-yes.jsonl"'
