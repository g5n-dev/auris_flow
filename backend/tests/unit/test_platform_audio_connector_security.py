from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.errors import ApiError
from app.services.connector_import_service import (
    MAX_CONNECTOR_RESPONSE_BYTES,
    build_connector_endpoint,
    fetch_connector_json,
    preview_mapping_status,
    preview_records,
    read_bounded_json_response,
    resolve_credential_headers,
    validate_public_endpoint,
)


class _Response:
    def __init__(self, body: bytes, *, content_type: str = "application/json") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, size: int) -> bytes:
        return self._body[:size]


@pytest.mark.parametrize(
    "url",
    [
        "http://recordings.example.test/v1/recordings",
        "https://user:password@recordings.example.test/v1/recordings",
        "https://localhost/v1/recordings",
        "https://127.0.0.1/v1/recordings",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/v1/recordings",
    ],
)
def test_public_endpoint_validation_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(ApiError) as captured:
        validate_public_endpoint(url)
    assert captured.value.code == "CONNECTOR_ENDPOINT_UNSAFE"


def test_endpoint_builder_rejects_authority_override_and_path_traversal() -> None:
    assert (
        build_connector_endpoint("https://recordings.example.test", "/v1/recordings")
        == "https://recordings.example.test/v1/recordings"
    )
    for path in ("//attacker.example/steal", "/../admin", "/v1/../admin", "\\\\attacker"):
        with pytest.raises(ApiError):
            build_connector_endpoint("https://recordings.example.test", path)


def test_credential_resolver_accepts_only_bounded_safe_headers_and_never_echoes_secret() -> None:
    canary = "credential-secret-canary"
    bindings = json.dumps(
        {
            "secret://platform/audio-reader": {
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "platform_connection_id": "conn_platform_auth",
                "platform_tenant_ref": "tenant-ext-001",
                "base_url": "https://recordings.example.test",
                "headers": {"Authorization": f"Bearer {canary}"},
            }
        }
    )
    expected_binding = {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "platform_connection_id": "conn_platform_auth",
        "platform_tenant_ref": "tenant-ext-001",
        "base_url": "https://recordings.example.test",
    }
    assert resolve_credential_headers(
        "secret://platform/audio-reader",
        serialized_bindings=bindings,
        **expected_binding,
    ) == {"Authorization": f"Bearer {canary}"}

    with pytest.raises(ApiError) as missing:
        resolve_credential_headers(
            "secret://platform/missing",
            serialized_bindings=bindings,
            **expected_binding,
        )
    assert missing.value.code == "CONNECTOR_CREDENTIAL_UNAVAILABLE"
    assert canary not in str(missing.value)

    dangerous = json.dumps(
        {
            "secret://platform/audio-reader": {
                "headers": {"Host": "internal.example", "Authorization": f"Bearer {canary}"}
            }
        }
    )
    with pytest.raises(ApiError) as rejected:
        resolve_credential_headers(
            "secret://platform/audio-reader",
            serialized_bindings=dangerous,
            **expected_binding,
        )
    assert rejected.value.code == "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID"
    assert canary not in str(rejected.value)

    with pytest.raises(ApiError) as cross_scope:
        resolve_credential_headers(
            "secret://platform/audio-reader",
            serialized_bindings=bindings,
            **{**expected_binding, "project_id": "other_project"},
        )
    assert cross_scope.value.code == "CONNECTOR_CREDENTIAL_SCOPE_MISMATCH"
    assert canary not in str(cross_scope.value)

    binding_with_unknown_key = json.loads(bindings)
    binding_with_unknown_key["secret://platform/audio-reader"]["legacy_scope"] = "unsafe"
    with pytest.raises(ApiError) as unknown_key:
        resolve_credential_headers(
            "secret://platform/audio-reader",
            serialized_bindings=json.dumps(binding_with_unknown_key),
            **expected_binding,
        )
    assert unknown_key.value.code == "CONNECTOR_CREDENTIAL_CONFIGURATION_INVALID"
    assert canary not in str(unknown_key.value)


def test_bounded_json_reader_rejects_oversized_or_non_json_responses() -> None:
    oversized = _Response(b"{" + b"x" * MAX_CONNECTOR_RESPONSE_BYTES + b"}")
    with pytest.raises(ApiError) as too_large:
        read_bounded_json_response(oversized)
    assert too_large.value.code == "CONNECTOR_RESPONSE_TOO_LARGE"

    html = _Response(b"<html>login</html>", content_type="text/html")
    with pytest.raises(ApiError) as wrong_type:
        read_bounded_json_response(html)
    assert wrong_type.value.code == "CONNECTOR_RESPONSE_NOT_JSON"


def test_redirect_errors_are_reported_without_location_or_credentials() -> None:
    redirect = HTTPError(
        "https://recordings.example.test/v1/recordings",
        302,
        "Found",
        {"Location": "https://attacker.example/steal?token=must-not-leak"},
        None,
    )
    with pytest.raises(ApiError) as captured:
        read_bounded_json_response(redirect)
    assert captured.value.code == "CONNECTOR_REDIRECT_FORBIDDEN"
    assert "must-not-leak" not in str(captured.value)


def test_connector_probe_uses_the_same_initial_cursor_window_as_dagster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_urls: list[str] = []

    class ProbeResponse(_Response):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_open(request, _timeout):
        requested_urls.append(request.full_url)
        return ProbeResponse(b'{"records":[],"next_cursor":null}')

    monkeypatch.setattr(
        "app.services.connector_import_service.validate_public_endpoint",
        lambda _url: None,
    )
    monkeypatch.setattr(
        "app.services.connector_import_service.resolve_credential_headers",
        lambda _reference, **_expected: {"Authorization": "Bearer opaque"},
    )
    monkeypatch.setattr(
        "app.services.connector_import_service.open_url_no_redirect",
        fake_open,
    )

    status, payload = fetch_connector_json(
        {
            "base_url": "https://recordings.example.test",
            "request_path": "/v1/recordings",
            "credential_ref": "secret://platform/audio-reader",
            "pagination": {
                "cursor_param": "cursor",
            },
            "cursor_policy": {
                "initial_window_start": "2026-07-01T00:00:00+00:00",
            },
        },
        limit=3,
        tenant_id="aurora_auto",
        project_id="sales_qa",
    )

    assert status == 200
    assert payload["records"] == []
    assert len(requested_urls) == 1
    assert parse_qs(urlsplit(requested_urls[0]).query) == {
        "cursor": ["2026-07-01T00:00:00+00:00"],
        "limit": ["3"],
    }


def test_preview_requires_a_strict_durable_incremental_cursor() -> None:
    connector = {
        "field_mapping": {
            "external_record_id": "recording_id",
            "audio_url": "audio_url",
            "started_at": "started_at",
        },
        "cursor_policy": {
            "field": "updated_at",
            "initial_window_start": "2026-07-27T09:59:00+08:00",
        },
        "pagination": {
            "next_cursor_path": "paging.next_cursor",
        },
        "platform_scope": {"store_refs": []},
    }
    records = [
        {
            "recording_id": "recording-1",
            "audio_url": "https://media.example.test/1.wav",
            "started_at": "2026-07-27T10:00:00+08:00",
            "updated_at": "2026-07-27T10:00:00+08:00",
        },
        {
            "recording_id": "recording-2",
            "audio_url": "https://media.example.test/2.wav",
            "started_at": "2026-07-27T10:01:00+08:00",
            "updated_at": "2026-07-27T10:01:00+08:00",
        },
    ]
    valid, errors = preview_mapping_status(
        connector,
        {
            "records": records,
            "paging": {"next_cursor": "2026-07-27T10:01:00+08:00"},
        },
        limit=3,
    )
    assert valid is True
    assert errors == []

    repeated = [
        records[0],
        {**records[1], "updated_at": "2026-07-27T10:00:00+08:00"},
    ]
    valid, errors = preview_mapping_status(
        connector,
        {
            "records": repeated,
            "paging": {"next_cursor": "2026-07-27T10:00:00+08:00"},
        },
        limit=3,
    )
    assert valid is False
    assert "cursor_policy.field" in errors

    valid, errors = preview_mapping_status(
        connector,
        {"records": records, "paging": {"next_cursor": "opaque-page-token"}},
        limit=3,
    )
    assert valid is False
    assert "pagination.next_cursor_path" in errors


def test_record_preview_rejects_records_outside_the_frozen_store_scope() -> None:
    connector = {
        "field_mapping": {
            "external_record_id": "recording_id",
            "audio_url": "download_url",
            "started_at": "started_at",
            "store_ref": "store_id",
        },
        "platform_scope": {
            "tenant_ref": "tenant-ext-001",
            "store_refs": ["BJ-AURORA-001"],
        },
    }
    payload = {
        "records": [
            {
                "recording_id": "recording-outside-scope",
                "download_url": "https://media.example.test/audio.wav",
                "started_at": "2026-07-27T10:00:00+08:00",
                "store_id": "SH-OTHER-001",
            }
        ]
    }

    with pytest.raises(ApiError) as rejected:
        preview_records(connector, payload, limit=3)

    assert rejected.value.code == "CONNECTOR_RECORD_SCOPE_MISMATCH"
