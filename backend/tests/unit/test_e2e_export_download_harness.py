from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_harness() -> ModuleType:
    path = ROOT / "scripts" / "verify_e2e_outbox_dispatch.py"
    spec = importlib.util.spec_from_file_location("auris_e2e_outbox_dispatch_export", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXPORT_BODY = b'{"row":1}\n{"row":2}\n'


def _real_details(**overrides: object) -> dict[str, object]:
    details: dict[str, object] = {
        "mode": "real",
        "provider": "minio",
        "storage_object_id": "sto_export_001",
        "bucket": "auris-flow-local",
        "object_key": "tenants/aurora_auto/projects/sales_qa/exports/export_001.jsonl",
        "object_uri": (
            "s3://auris-flow-local/tenants/aurora_auto/projects/sales_qa/exports/export_001.jsonl"
        ),
        "etag": "export-etag-v1",
        "content_type": "application/x-ndjson",
        "content_length": len(EXPORT_BODY),
        "content_sha256": hashlib.sha256(EXPORT_BODY).hexdigest(),
    }
    details.update(overrides)
    return details


def _expected(harness: ModuleType):
    return harness.ExpectedRun(
        label="export",
        run_id="export_001",
        trace_id="trace-export-001",
        adapter="object_storage",
    )


class _BoundStorageClient:
    def __init__(self) -> None:
        self.head_calls: list[tuple[object, ...]] = []
        self.get_calls: list[tuple[object, ...]] = []

    def allows_bucket(self, bucket: str) -> bool:
        return bucket == "auris-flow-local"

    def head_object(
        self,
        bucket: str,
        key: str,
        *,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, object]:
        self.head_calls.append((bucket, key, if_match, version_id))
        return {
            "status": 200,
            "etag": "export-etag-v1",
            "content_length": len(EXPORT_BODY),
            "content_type": "application/x-ndjson",
            "version_id": version_id,
            "body": b"",
        }

    def get_object(
        self,
        bucket: str,
        key: str,
        *,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, object]:
        self.get_calls.append((bucket, key, if_match, version_id))
        return {
            "status": 200,
            "etag": "export-etag-v1",
            "content_length": len(EXPORT_BODY),
            "content_type": "application/x-ndjson",
            "version_id": version_id,
            "body": EXPORT_BODY,
        }


@pytest.mark.parametrize("missing_field", ["provider", "etag"])
def test_real_object_receipt_requires_provider_and_etag(monkeypatch, missing_field: str) -> None:
    harness = _load_harness()
    details = _real_details()
    details.pop(missing_field)
    storage_client = _BoundStorageClient()
    monkeypatch.setattr(harness, "RealObjectStorageClient", lambda: storage_client)
    monkeypatch.setattr(
        harness,
        "object_storage_client_for_provider",
        lambda _provider: storage_client,
        raising=False,
    )

    with pytest.raises(SystemExit):
        harness.validate_real_object_storage_object(_expected(harness), details)


def test_real_object_receipt_binds_provider_etag_and_version(monkeypatch) -> None:
    harness = _load_harness()
    details = _real_details(version_id="version-7")
    storage_client = _BoundStorageClient()
    providers: list[str] = []

    def client_for_provider(provider: str):
        providers.append(provider)
        return storage_client

    monkeypatch.setattr(harness, "RealObjectStorageClient", lambda: storage_client)
    monkeypatch.setattr(
        harness,
        "object_storage_client_for_provider",
        client_for_provider,
        raising=False,
    )

    harness.validate_real_object_storage_object(_expected(harness), details)

    assert providers == ["minio"]
    assert storage_client.head_calls == [
        (
            "auris-flow-local",
            details["object_key"],
            '"export-etag-v1"',
            "version-7",
        )
    ]
    assert storage_client.get_calls == storage_client.head_calls


def test_real_export_cannot_complete_with_unavailable_download_ref(monkeypatch) -> None:
    harness = _load_harness()
    details = _real_details(provider="")
    monkeypatch.setattr(
        harness,
        "bff_json_request",
        lambda *_args, **_kwargs: {
            "data": {
                "status": "success",
                "download_ref": {
                    "kind": "bff_download",
                    "status": "unavailable",
                    "href": None,
                    "content_type": details["content_type"],
                },
            }
        },
    )
    monkeypatch.setattr(
        harness,
        "bff_binary_request",
        lambda *_args, **_kwargs: pytest.fail("unavailable real export must fail first"),
    )

    with pytest.raises(SystemExit):
        harness.verify_export_ready("export_001", details, "trace-export-001")


def test_real_export_download_route_is_verified_with_head_and_range_get(
    monkeypatch,
) -> None:
    harness = _load_harness()
    details = _real_details()
    href = "/api/v1/exports/export_001/download"
    monkeypatch.setattr(
        harness,
        "bff_json_request",
        lambda *_args, **_kwargs: {
            "data": {
                "status": "success",
                "download_ref": {
                    "kind": "bff_download",
                    "status": "ready",
                    "href": href,
                    "content_type": details["content_type"],
                },
            }
        },
    )
    calls: list[tuple[str, str, dict[str, str]]] = []

    def binary_request(
        method: str,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
        include_default_context: bool = True,
    ) -> dict[str, object]:
        assert include_default_context is True
        calls.append((method, path, extra_headers or {}))
        common_headers = {
            "accept-ranges": "bytes",
            "content-type": str(details["content_type"]),
            "etag": '"export-etag-v1"',
            "content-length": str(len(EXPORT_BODY)),
        }
        if method == "HEAD":
            return {"status": 200, "headers": common_headers, "body": b""}
        assert method == "GET"
        return {
            "status": 206,
            "headers": {
                **common_headers,
                "content-range": f"bytes 0-{len(EXPORT_BODY) - 1}/{len(EXPORT_BODY)}",
            },
            "body": EXPORT_BODY,
        }

    monkeypatch.setattr(harness, "bff_binary_request", binary_request)

    harness.verify_export_ready("export_001", details, "trace-export-001")

    assert calls == [
        ("HEAD", href, {"X-Trace-Id": "trace-export-001"}),
        (
            "GET",
            href,
            {
                "X-Trace-Id": "trace-export-001",
                "Range": f"bytes=0-{len(EXPORT_BODY) - 1}",
            },
        ),
    ]


def test_real_export_range_download_rejects_wrong_bytes(monkeypatch) -> None:
    harness = _load_harness()
    details = _real_details()
    href = "/api/v1/exports/export_001/download"
    monkeypatch.setattr(
        harness,
        "bff_json_request",
        lambda *_args, **_kwargs: {
            "data": {
                "status": "success",
                "download_ref": {
                    "kind": "bff_download",
                    "status": "ready",
                    "href": href,
                    "content_type": details["content_type"],
                },
            }
        },
    )

    def binary_request(method: str, *_args, **_kwargs) -> dict[str, object]:
        headers = {
            "accept-ranges": "bytes",
            "content-type": str(details["content_type"]),
            "etag": '"export-etag-v1"',
            "content-length": str(len(EXPORT_BODY)),
        }
        if method == "HEAD":
            return {"status": 200, "headers": headers, "body": b""}
        return {
            "status": 206,
            "headers": {
                **headers,
                "content-range": f"bytes 0-{len(EXPORT_BODY) - 1}/{len(EXPORT_BODY)}",
            },
            "body": b"x" * len(EXPORT_BODY),
        }

    monkeypatch.setattr(harness, "bff_binary_request", binary_request)

    with pytest.raises(SystemExit):
        harness.verify_export_ready("export_001", details, "trace-export-001")


def test_local_export_keeps_unavailable_download_ref_compatibility(monkeypatch) -> None:
    harness = _load_harness()
    details = {
        "mode": "local",
        "content_type": "application/x-ndjson",
        "object_uri": "mock://object-storage/export_001",
    }
    monkeypatch.setattr(
        harness,
        "bff_json_request",
        lambda *_args, **_kwargs: {
            "data": {
                "status": "success",
                "download_ref": {
                    "kind": "bff_download",
                    "status": "unavailable",
                    "href": None,
                    "content_type": details["content_type"],
                },
            }
        },
    )
    monkeypatch.setattr(
        harness,
        "bff_binary_request",
        lambda *_args, **_kwargs: pytest.fail(
            "local unavailable export must not call the binary route"
        ),
    )

    harness.verify_export_ready("export_001", details, "trace-export-001")
