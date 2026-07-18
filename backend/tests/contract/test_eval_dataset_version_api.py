from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import EvalDatasetVersion, StorageObject
from app.services import eval_dataset_service
from app.services.eval_dataset_service import locked_eval_dataset_snapshot


def _headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _storage_object(
    storage_object_id: str,
    *,
    eval_dataset_id: str,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
    sha256: str = "f" * 64,
    provider: str = "minio",
    bucket: str = "auris-flow-local",
    object_key: str | None = None,
    source_type: str = "eval_dataset_manifest",
    source_id: str | None = None,
    content_type: str = "application/x-ndjson",
    size_bytes: int = 1024,
    etag: str | None = None,
) -> None:
    resolved_object_key = object_key or (
        f"tenants/{tenant_id}/projects/{project_id}/eval-datasets/{eval_dataset_id}/manifest.jsonl"
    )
    with SessionLocal() as session:
        session.add(
            StorageObject(
                storage_object_id=storage_object_id,
                tenant_id=tenant_id,
                project_id=project_id,
                provider=provider,
                bucket=bucket,
                object_key=resolved_object_key,
                object_key_sha256=hashlib.sha256(resolved_object_key.encode()).hexdigest(),
                source_type=source_type,
                source_id=source_id or eval_dataset_id,
                content_type=content_type,
                size_bytes=size_bytes,
                content_sha256=sha256,
                etag=etag if etag is not None else f'"{sha256[:16]}"',
                status="verified",
                trace_id=f"trace-{storage_object_id}",
                payload={"verified": True},
            )
        )
        session.commit()


def _dataset_body(
    eval_dataset_id: str,
    storage_object_id: str,
    *,
    sha256: str = "f" * 64,
) -> dict[str, Any]:
    return {
        "eval_dataset_id": eval_dataset_id,
        "name": f"固定评测集 {eval_dataset_id}",
        "capability": "asr_hotword",
        "dataset_version": "v1",
        "manifest_storage_object_id": storage_object_id,
        "manifest_sha256": sha256,
        "sample_count": 40,
        "source": "contract-test",
    }


def _ctx(trace_id: str) -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id=f"req-{trace_id}",
        trace_id=trace_id,
    )


def _public_provenance(manifest_sha256: str) -> dict[str, Any]:
    return {
        "source_type": "public_dataset",
        "registry_dataset_id": "alimeeting-slr119",
        "registry_schema_version": "auris.public-audio-datasets.v1",
        "upstream_id": "SLR119",
        "upstream_revision": "SLR119-2022-release",
        "upstream_split": "eval",
        "upstream_url": (
            "https://speech-lab-share-data.oss-cn-shanghai.aliyuncs.com/"
            "AliMeeting/openlr/Eval_Ali.tar.gz"
        ),
        "license_spdx": "CC-BY-SA-4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "license_accepted": True,
        "repository_distribution": "metadata-only",
        "evaluation_only": True,
        "archive_sha256": "d" * 64,
        "prepared_manifest_sha256": manifest_sha256,
        "citation": "Yu et al., M2MeT, ICASSP 2022.",
    }


class _FakeObjectStorageClient:
    def __init__(self, *, content_length: int | None, etag: str | None) -> None:
        self.content_length = content_length
        self.etag = etag
        self.calls: list[tuple[str, str]] = []

    def allows_bucket(self, bucket: str) -> bool:
        return bucket == "auris-flow-local"

    def head_object(self, bucket: str, object_key: str) -> dict[str, object | None]:
        self.calls.append((bucket, object_key))
        return {
            "status": 200,
            "content_length": (
                str(self.content_length) if self.content_length is not None else None
            ),
            "content_type": "application/x-ndjson",
            "etag": self.etag,
        }


def _enable_fake_remote_head(monkeypatch, fake: _FakeObjectStorageClient) -> None:
    monkeypatch.setattr(
        eval_dataset_service,
        "_remote_manifest_head_enabled",
        lambda _provider: True,
        raising=False,
    )
    monkeypatch.setattr(
        eval_dataset_service,
        "object_storage_client_for_provider",
        lambda _provider: fake,
        raising=False,
    )


def test_eval_dataset_create_lock_and_replay_are_auditable(client, auth_headers) -> None:
    storage_object_id = "sto-eval-dataset-contract"
    eval_dataset_id = "evalset-contract-v1"
    _storage_object(storage_object_id, eval_dataset_id=eval_dataset_id)
    body = _dataset_body(eval_dataset_id, storage_object_id)
    body["name"] = "ASR 热词固定回归集"
    created = client.post(
        "/api/v1/eval-datasets",
        json=body,
        headers=_headers(auth_headers, "eval-dataset-create"),
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["status"] == "draft"
    assert created.json()["data"]["snapshot_sha256"]

    replay = client.post(
        "/api/v1/eval-datasets",
        json=body,
        headers=_headers(auth_headers, "eval-dataset-create"),
    )
    assert replay.status_code == 201
    assert replay.json() == created.json()

    locked = client.post(
        "/api/v1/eval-datasets/evalset-contract-v1/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, "eval-dataset-lock"),
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["data"]["status"] == "locked"
    assert locked.json()["data"]["locked"] is True
    assert locked.json()["data"]["resource_version"] == 2
    assert locked.json()["data"]["manifest_provider"] == "minio"
    assert locked.json()["data"]["manifest_bucket"] == "auris-flow-local"
    assert locked.json()["data"]["manifest_object_key"].endswith(
        "/evalset-contract-v1/manifest.jsonl"
    )
    assert locked.json()["data"]["manifest_size_bytes"] == 1024
    assert locked.json()["data"]["manifest_etag"] == "f" * 16
    with SessionLocal() as session:
        stored = session.get(EvalDatasetVersion, eval_dataset_id)
        assert stored is not None
        assert stored.manifest_provider == "minio"
        assert stored.manifest_bucket == "auris-flow-local"
        assert stored.manifest_object_key.endswith(f"/{eval_dataset_id}/manifest.jsonl")
        assert stored.manifest_size_bytes == 1024
        assert stored.manifest_etag == "f" * 16

    detail = client.get("/api/v1/eval-datasets/evalset-contract-v1", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["manifest_sha256"] == "f" * 64


def test_public_eval_dataset_provenance_is_hashed_and_locked(client, auth_headers) -> None:
    eval_dataset_id = "evalset-alimeeting-contract"
    storage_object_id = "sto-eval-alimeeting-contract"
    manifest_sha256 = "d" * 64
    _storage_object(
        storage_object_id,
        eval_dataset_id=eval_dataset_id,
        sha256=manifest_sha256,
    )
    body = {
        **_dataset_body(eval_dataset_id, storage_object_id, sha256=manifest_sha256),
        "name": "AliMeeting Eval contract",
        "capability": "speaker_diarization",
        "source": "public_dataset",
        "sample_count": 8,
        "provenance": _public_provenance(manifest_sha256),
    }

    created = client.post(
        "/api/v1/eval-datasets",
        json=body,
        headers=_headers(auth_headers, "eval-dataset-alimeeting-create"),
    )
    assert created.status_code == 201, created.text
    created_data = created.json()["data"]
    assert created_data["source"] == "public_dataset"
    assert created_data["provenance"]["upstream_split"] == "eval"
    assert len(created_data["provenance_sha256"]) == 64

    locked = client.post(
        f"/api/v1/eval-datasets/{eval_dataset_id}/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, "eval-dataset-alimeeting-lock"),
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["data"]["provenance_sha256"] == created_data["provenance_sha256"]


def test_public_eval_dataset_lock_rejects_provenance_drift(client, auth_headers) -> None:
    eval_dataset_id = "evalset-alimeeting-drift"
    storage_object_id = "sto-eval-alimeeting-drift"
    manifest_sha256 = "e" * 64
    _storage_object(
        storage_object_id,
        eval_dataset_id=eval_dataset_id,
        sha256=manifest_sha256,
    )
    body = {
        **_dataset_body(eval_dataset_id, storage_object_id, sha256=manifest_sha256),
        "name": "AliMeeting Eval drift",
        "capability": "speaker_diarization",
        "source": "public_dataset",
        "sample_count": 8,
        "provenance": _public_provenance(manifest_sha256),
    }
    created = client.post(
        "/api/v1/eval-datasets",
        json=body,
        headers=_headers(auth_headers, "eval-dataset-alimeeting-drift-create"),
    )
    assert created.status_code == 201, created.text

    with SessionLocal() as session:
        dataset = session.get(EvalDatasetVersion, eval_dataset_id)
        assert dataset is not None
        payload = dict(dataset.payload)
        provenance = dict(payload["provenance"])
        provenance["upstream_split"] = "train"
        payload["provenance"] = provenance
        dataset.payload = payload
        session.commit()

    locked = client.post(
        f"/api/v1/eval-datasets/{eval_dataset_id}/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, "eval-dataset-alimeeting-drift-lock"),
    )
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "EVAL_DATASET_PROVENANCE_DRIFT"


def test_eval_dataset_rejects_hash_scope_and_unknown_snapshot(client, auth_headers) -> None:
    _storage_object(
        "sto-eval-hash-contract",
        eval_dataset_id="evalset-hash-mismatch",
        sha256="a" * 64,
    )
    mismatch = client.post(
        "/api/v1/eval-datasets",
        json={
            "eval_dataset_id": "evalset-hash-mismatch",
            "name": "错误哈希评测集",
            "capability": "asr_hotword",
            "dataset_version": "v1",
            "manifest_storage_object_id": "sto-eval-hash-contract",
            "manifest_sha256": "b" * 64,
            "sample_count": 10,
        },
        headers=_headers(auth_headers, "eval-dataset-hash-mismatch"),
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "EVAL_DATASET_MANIFEST_HASH_MISMATCH"

    _storage_object(
        "sto-eval-cross-project",
        eval_dataset_id="evalset-cross-project",
        project_id="other_project",
        sha256="c" * 64,
    )
    forbidden = client.post(
        "/api/v1/eval-datasets",
        json={
            "eval_dataset_id": "evalset-cross-project",
            "name": "跨项目评测集",
            "capability": "asr_hotword",
            "dataset_version": "v1",
            "manifest_storage_object_id": "sto-eval-cross-project",
            "manifest_sha256": "c" * 64,
            "sample_count": 10,
        },
        headers=_headers(auth_headers, "eval-dataset-cross-project"),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "EVAL_DATASET_MANIFEST_SCOPE_FORBIDDEN"

    missing = client.get("/api/v1/eval-datasets/evalset-missing", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "EVAL_DATASET_NOT_FOUND"


def test_eval_dataset_rejects_missing_descriptor_etag(client, auth_headers) -> None:
    eval_dataset_id = "evalset-missing-etag"
    storage_object_id = "sto-eval-missing-etag"
    _storage_object(
        storage_object_id,
        eval_dataset_id=eval_dataset_id,
        etag="",
    )

    response = client.post(
        "/api/v1/eval-datasets",
        json=_dataset_body(eval_dataset_id, storage_object_id),
        headers=_headers(auth_headers, "eval-dataset-missing-etag"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVAL_DATASET_MANIFEST_ETAG_REQUIRED"


@pytest.mark.parametrize(
    ("storage_overrides", "expected_code"),
    [
        (
            {"source_type": "hotword_build"},
            "EVAL_DATASET_MANIFEST_SOURCE_TYPE_INVALID",
        ),
        (
            {"content_type": "audio/wav"},
            "EVAL_DATASET_MANIFEST_CONTENT_TYPE_INVALID",
        ),
        (
            {
                "object_key": (
                    "tenants/aurora_auto/projects/other_project/eval-datasets/"
                    "evalset-invalid-manifest/manifest.jsonl"
                )
            },
            "EVAL_DATASET_MANIFEST_KEY_FORBIDDEN",
        ),
        (
            {"provider": "unknown-provider"},
            "EVAL_DATASET_MANIFEST_PROVIDER_INVALID",
        ),
        (
            {"etag": 'W/"weak-etag"'},
            "EVAL_DATASET_MANIFEST_ETAG_WEAK",
        ),
    ],
)
def test_eval_dataset_rejects_invalid_manifest_identity(
    client,
    auth_headers,
    storage_overrides: dict[str, object],
    expected_code: str,
) -> None:
    eval_dataset_id = "evalset-invalid-manifest"
    storage_object_id = f"sto-{expected_code.lower()}"
    _storage_object(
        storage_object_id,
        eval_dataset_id=eval_dataset_id,
        **storage_overrides,
    )

    response = client.post(
        "/api/v1/eval-datasets",
        json=_dataset_body(eval_dataset_id, storage_object_id),
        headers=_headers(auth_headers, f"invalid-{expected_code.lower()}"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code


def test_locked_dataset_snapshot_drift_is_detectable(client, auth_headers) -> None:
    storage_object_id = "sto-eval-drift-contract"
    _storage_object(
        storage_object_id,
        eval_dataset_id="evalset-drift-v1",
        sha256="d" * 64,
    )
    created = client.post(
        "/api/v1/eval-datasets",
        json={
            "eval_dataset_id": "evalset-drift-v1",
            "name": "漂移检测评测集",
            "capability": "asr_hotword",
            "dataset_version": "v1",
            "manifest_storage_object_id": storage_object_id,
            "manifest_sha256": "d" * 64,
            "sample_count": 10,
        },
        headers=_headers(auth_headers, "eval-dataset-drift-create"),
    )
    assert created.status_code == 201, created.text
    locked = client.post(
        "/api/v1/eval-datasets/evalset-drift-v1/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, "eval-dataset-drift-lock"),
    )
    assert locked.status_code == 200
    with SessionLocal() as session:
        dataset = session.get(EvalDatasetVersion, "evalset-drift-v1")
        assert dataset is not None
        dataset.sample_count = 11
        session.commit()

    with SessionLocal() as session:
        try:
            locked_eval_dataset_snapshot(
                session,
                _ctx("trace-eval-drift"),
                "evalset-drift-v1",
                required_capability="asr_hotword",
            )
        except ApiError as exc:
            assert exc.code == "EVAL_DATASET_SNAPSHOT_DRIFT"
        else:
            raise AssertionError("locked dataset drift must be rejected")


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("size_bytes", 2048, "EVAL_DATASET_MANIFEST_SIZE_DRIFT"),
        ("etag", '"replacement-etag"', "EVAL_DATASET_MANIFEST_ETAG_DRIFT"),
        ("bucket", "replacement-bucket", "EVAL_DATASET_MANIFEST_LOCATOR_DRIFT"),
    ],
)
def test_locked_dataset_rejects_storage_descriptor_drift(
    client,
    auth_headers,
    field: str,
    value: object,
    expected_code: str,
) -> None:
    eval_dataset_id = f"evalset-descriptor-{field}"
    storage_object_id = f"sto-eval-descriptor-{field}"
    _storage_object(storage_object_id, eval_dataset_id=eval_dataset_id)
    created = client.post(
        "/api/v1/eval-datasets",
        json=_dataset_body(eval_dataset_id, storage_object_id),
        headers=_headers(auth_headers, f"descriptor-{field}-create"),
    )
    assert created.status_code == 201, created.text
    locked = client.post(
        f"/api/v1/eval-datasets/{eval_dataset_id}/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, f"descriptor-{field}-lock"),
    )
    assert locked.status_code == 200, locked.text

    with SessionLocal() as session:
        storage_object = session.get(StorageObject, storage_object_id)
        assert storage_object is not None
        setattr(storage_object, field, value)
        session.commit()

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        locked_eval_dataset_snapshot(
            session,
            _ctx(f"trace-descriptor-{field}"),
            eval_dataset_id,
            required_capability="asr_hotword",
        )
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("remote_field", "remote_value", "expected_code"),
    [
        ("content_length", 2048, "EVAL_DATASET_MANIFEST_SIZE_DRIFT"),
        (
            "content_length",
            None,
            "EVAL_DATASET_MANIFEST_REMOTE_SIZE_REQUIRED",
        ),
        ("etag", "remote-etag-v2", "EVAL_DATASET_MANIFEST_ETAG_DRIFT"),
        ("etag", None, "EVAL_DATASET_MANIFEST_REMOTE_ETAG_REQUIRED"),
        ("etag", 'W/"weak-etag"', "EVAL_DATASET_MANIFEST_REMOTE_ETAG_WEAK"),
    ],
)
def test_locked_dataset_rejects_remote_object_drift_with_controlled_fake(
    client,
    auth_headers,
    monkeypatch,
    remote_field: str,
    remote_value: object,
    expected_code: str,
) -> None:
    eval_dataset_id = f"evalset-remote-{expected_code.lower()}"
    storage_object_id = f"sto-remote-{expected_code.lower()}"
    _storage_object(
        storage_object_id,
        eval_dataset_id=eval_dataset_id,
        etag='"remote-etag-v1"',
    )
    fake = _FakeObjectStorageClient(content_length=1024, etag="remote-etag-v1")
    _enable_fake_remote_head(monkeypatch, fake)
    created = client.post(
        "/api/v1/eval-datasets",
        json=_dataset_body(eval_dataset_id, storage_object_id),
        headers=_headers(auth_headers, f"remote-{expected_code.lower()}-create"),
    )
    assert created.status_code == 201, created.text
    locked = client.post(
        f"/api/v1/eval-datasets/{eval_dataset_id}/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, f"remote-{expected_code.lower()}-lock"),
    )
    assert locked.status_code == 200, locked.text

    with SessionLocal() as session:
        run_snapshot = locked_eval_dataset_snapshot(
            session,
            _ctx(f"trace-run-{expected_code.lower()}"),
            eval_dataset_id,
            required_capability="asr_hotword",
        )
    assert run_snapshot["manifest_etag"] == "remote-etag-v1"

    setattr(fake, remote_field, remote_value)
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        locked_eval_dataset_snapshot(
            session,
            _ctx(f"trace-remote-{expected_code.lower()}"),
            eval_dataset_id,
            required_capability="asr_hotword",
        )
    assert exc_info.value.code == expected_code
    assert fake.calls[-1][1].endswith(f"/{eval_dataset_id}/manifest.jsonl")


def test_mock_provider_never_performs_remote_head(client, auth_headers, monkeypatch) -> None:
    eval_dataset_id = "evalset-mock-provider"
    storage_object_id = "sto-eval-mock-provider"
    _storage_object(
        storage_object_id,
        eval_dataset_id=eval_dataset_id,
        provider="mock",
        bucket="eval-fixtures",
    )

    def unexpected_client(_provider: str):
        raise AssertionError("mock provider must not create a remote object-storage client")

    monkeypatch.setattr(
        eval_dataset_service,
        "object_storage_client_for_provider",
        unexpected_client,
        raising=False,
    )
    response = client.post(
        "/api/v1/eval-datasets",
        json=_dataset_body(eval_dataset_id, storage_object_id),
        headers=_headers(auth_headers, "eval-dataset-mock-provider"),
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["manifest_provider"] == "mock"
