from __future__ import annotations

import hashlib
from urllib.error import HTTPError

import pytest
from sqlalchemy import select

from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AssetMaterialization,
    AuditLog,
    Badcase,
    HotwordMetricSnapshot,
    HotwordPack,
    HotwordPackVersion,
    HotwordVersionItem,
    JsonResource,
    OutboxEvent,
    Project,
    RunCompletionReceipt,
    RunRecord,
    StorageObject,
    User,
)
from app.services import audio_intelligence_service
from app.services.hotword_service import (
    _verify_eval_run,
    materialize_hotword_analysis_completion,
)
from app.workers.outbox_worker import process_aggregate_events


def _headers(auth_headers, *, key: str, token: str = "dev-token"):
    return {**auth_headers, "Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _release_second_admin_token() -> str:
    user_id = "u_annotator_001"
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        project = session.get(Project, "sales_qa")
        assert user is not None and project is not None
        user.roles = list(dict.fromkeys([*(user.roles or []), "project_admin"]))
        project.data = {
            **project.data,
            "members": [
                {
                    **member,
                    "roles": list(dict.fromkeys([*member.get("roles", []), "project_admin"])),
                }
                if member.get("user_id") == user_id
                else member
                for member in project.data.get("members", [])
            ],
        }
    profile = DevAuthProfile(
        email="hotword-task-release-reviewer@auris.local",
        user_id=user_id,
        name="任务发布复核管理员",
        role_label="项目管理员",
        initials="复",
        roles=("annotator", "review_arbitrator", "project_admin"),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


def _register_storage_object(
    storage_object_id: str,
    *,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
    status: str = "registered",
    content_sha256: str | None = None,
    size_bytes: int | None = 256,
    source_type: str = "test_artifact",
    source_id: str | None = None,
    trace_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    object_key = f"tenants/{tenant_id}/projects/{project_id}/tests/{storage_object_id}.json"
    with SessionLocal() as session:
        session.add(
            StorageObject(
                storage_object_id=storage_object_id,
                tenant_id=tenant_id,
                project_id=project_id,
                provider="minio",
                bucket="auris-flow-local",
                object_key=object_key,
                object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
                source_type=source_type,
                source_id=source_id or storage_object_id,
                content_type="application/json",
                size_bytes=size_bytes,
                content_sha256=content_sha256,
                etag=f"etag-{storage_object_id}",
                status=status,
                trace_id=trace_id or f"trace-{storage_object_id}",
                payload=payload or {"status": status},
            )
        )
        session.commit()


def _run_storage_descriptor(
    run_id: str,
    storage_object_id: str,
    *,
    role: str,
    content_sha256: str,
    content_type: str = "application/json",
    object_key: str | None = None,
) -> dict:
    return {
        "storage_object_id": storage_object_id,
        "role": role,
        "provider": "minio",
        "bucket": "auris-flow-local",
        "object_key": object_key
        or (f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/{storage_object_id}.json"),
        "content_type": content_type,
        "size_bytes": 256,
        "content_sha256": content_sha256,
        "etag": f"etag-{storage_object_id}",
    }


def _create_validating_hotword_version(client, auth_headers, *, key: str):
    pack = client.post(
        "/api/v1/hotword-packs",
        json={"name": f"对象校验词包-{key}", "language": "zh-CN", "domain": key},
        headers=_headers(auth_headers, key=f"{key}-pack"),
    )
    assert pack.status_code == 201, pack.text
    pack_data = pack.json()["data"]
    version = client.post(
        f"/api/v1/hotword-packs/{pack_data['pack_id']}/versions",
        json={"version": "v1", "task_type_id": "task_sales_quality"},
        headers=_headers(auth_headers, key=f"{key}-version"),
    )
    assert version.status_code == 201, version.text
    version_data = version.json()["data"]
    item = client.post(
        f"/api/v1/hotword-pack-versions/{version_data['version_id']}/items",
        json={
            "canonical_term": "银河E8",
            "aliases": [],
            "category": "vehicle-model",
            "weight": 80,
        },
        headers=_headers(auth_headers, key=f"{key}-item"),
    )
    assert item.status_code == 201, item.text
    validating = client.patch(
        f"/api/v1/hotword-pack-versions/{version_data['version_id']}",
        json={
            "expected_resource_version": 2,
            "status": "validating",
            "provider": "auris-audio-stack",
        },
        headers=_headers(auth_headers, key=f"{key}-validating"),
    )
    assert validating.status_code == 200, validating.text
    return pack_data, version_data, validating


def _complete_dagster_run(client, auth_headers, run_id: str, result_ref: dict, *, key: str):
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        run_status = run.status
    if run_status == "pending":
        assert process_aggregate_events([run_id]) == 1
    else:
        assert run_status == "submitted"
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None and run.status == "submitted"
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]
    return client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": f"receipt-{run_id}",
            "external_id": external_run_id,
            "result_ref": result_ref,
        },
        headers=_headers(auth_headers, key=key),
    )


@pytest.mark.parametrize(
    "failure,expected_status,expected_code",
    [
        ("missing", 404, "STORAGE_OBJECT_NOT_FOUND"),
        ("cross_tenant", 403, "STORAGE_OBJECT_SCOPE_FORBIDDEN"),
        ("cross_project", 403, "STORAGE_OBJECT_SCOPE_FORBIDDEN"),
        ("uploading", 409, "STORAGE_OBJECT_NOT_READY"),
        ("incomplete", 409, "STORAGE_OBJECT_METADATA_INCOMPLETE"),
        ("hash_mismatch", 409, "STORAGE_OBJECT_CONTENT_HASH_MISMATCH"),
    ],
)
def test_hotword_build_rejects_untrusted_manifest_storage_reference(
    client,
    auth_headers,
    failure,
    expected_status,
    expected_code,
):
    _, version, validating = _create_validating_hotword_version(
        client,
        auth_headers,
        key=f"manifest-{failure}",
    )
    validating_data = validating.json()["data"]
    manifest_id = f"sto-hotword-manifest-{failure}"
    if failure != "missing":
        _register_storage_object(
            manifest_id,
            tenant_id="other_tenant" if failure == "cross_tenant" else "aurora_auto",
            project_id="other_project" if failure == "cross_project" else "sales_qa",
            status="uploading" if failure == "uploading" else "registered",
            content_sha256=(
                None
                if failure == "incomplete"
                else ("f" * 64 if failure == "hash_mismatch" else "a" * 64)
            ),
        )
    completed = _complete_dagster_run(
        client,
        auth_headers,
        validating_data["build_run_id"],
        {
            "hotword_pack_version_id": version["version_id"],
            "content_sha256": validating_data["content_sha256"],
            "provider": "auris-audio-stack",
            "manifest_storage_object_id": manifest_id,
            "provider_artifact_ref": "storage://compiled/invalid-manifest.json",
            "artifact_sha256": "a" * 64,
        },
        key=f"manifest-{failure}-completion",
    )
    assert completed.status_code == expected_status, completed.text
    assert completed.json()["error"]["code"] == expected_code
    with SessionLocal() as session:
        run = session.get(RunRecord, validating_data["build_run_id"])
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert run is not None and run.status == "submitted"
        assert "completion_receipt" not in run.payload
        assert stored_version is not None and stored_version.status == "validating"
        assert stored_version.manifest_storage_object_id is None


def test_hotword_completion_real_head_failure_rolls_back_storage_registration(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, version, validating = _create_validating_hotword_version(
        client,
        auth_headers,
        key="real-head-rollback",
    )
    validating_data = validating.json()["data"]
    build_run_id = validating_data["build_run_id"]
    manifest_id = "sto-real-head-missing-manifest"
    artifact_id = "sto-real-head-missing-artifact"

    class MissingHeadClient:
        def allows_bucket(self, _bucket: str) -> bool:
            return True

        def head_object(self, _bucket: str, object_key: str) -> dict[str, object]:
            raise HTTPError(object_key, 404, "not found", hdrs=None, fp=None)

    monkeypatch.setattr(audio_intelligence_service, "_real_object_storage_enabled", lambda: True)
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda _provider: MissingHeadClient(),
    )
    completed = _complete_dagster_run(
        client,
        auth_headers,
        build_run_id,
        {
            "hotword_pack_version_id": version["version_id"],
            "content_sha256": validating_data["content_sha256"],
            "provider": "auris-audio-stack",
            "manifest_storage_object_id": manifest_id,
            "provider_artifact_ref": artifact_id,
            "artifact_sha256": "e" * 64,
            "storage_objects": [
                _run_storage_descriptor(
                    build_run_id,
                    manifest_id,
                    role="manifest",
                    content_sha256=validating_data["content_sha256"],
                ),
                _run_storage_descriptor(
                    build_run_id,
                    artifact_id,
                    role="provider_artifact",
                    content_sha256="e" * 64,
                ),
            ],
        },
        key="real-head-rollback-completion",
    )

    assert completed.status_code == 404, completed.text
    assert completed.json()["error"]["code"] == "STORAGE_OBJECT_REMOTE_NOT_FOUND"
    with SessionLocal() as session:
        run = session.get(RunRecord, build_run_id)
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert run is not None and run.status == "submitted"
        assert stored_version is not None and stored_version.status == "validating"
        assert session.get(StorageObject, manifest_id) is None
        assert session.get(StorageObject, artifact_id) is None


@pytest.mark.parametrize(
    "reference_kind,failure,expected_status,expected_code",
    [
        ("word_timestamps", "missing", 404, "STORAGE_OBJECT_NOT_FOUND"),
        ("diagnostics", "missing", 404, "STORAGE_OBJECT_NOT_FOUND"),
        ("word_timestamps", "cross_tenant", 403, "STORAGE_OBJECT_SCOPE_FORBIDDEN"),
        ("diagnostics", "cross_project", 403, "STORAGE_OBJECT_SCOPE_FORBIDDEN"),
        ("word_timestamps", "uploading", 409, "STORAGE_OBJECT_NOT_READY"),
        ("diagnostics", "incomplete", 409, "STORAGE_OBJECT_METADATA_INCOMPLETE"),
    ],
)
def test_audio_completion_rejects_untrusted_artifact_storage_reference(
    client,
    auth_headers,
    reference_kind,
    failure,
    expected_status,
    expected_code,
):
    requested = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["asr"],
            "task_version_id": "task_version_v3_2_1",
            "provider": "auris-audio-stack",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
            "return_word_timestamps": True,
        },
        headers=_headers(
            auth_headers,
            key=f"audio-storage-{reference_kind}-{failure}-request",
        ),
    )
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    word_timestamps_id = "sto-word-timestamps-contract"
    diagnostics_id = "sto-hotword-diagnostics-contract"
    target_id = word_timestamps_id if reference_kind == "word_timestamps" else diagnostics_id
    counterpart_id = diagnostics_id if reference_kind == "word_timestamps" else word_timestamps_id
    _register_storage_object(counterpart_id, content_sha256="b" * 64)
    if failure != "missing":
        _register_storage_object(
            target_id,
            tenant_id="other_tenant" if failure == "cross_tenant" else "aurora_auto",
            project_id="other_project" if failure == "cross_project" else "sales_qa",
            status="uploading" if failure == "uploading" else "registered",
            content_sha256=None if failure == "incomplete" else "c" * 64,
        )
    completed = _complete_dagster_run(
        client,
        auth_headers,
        run_id,
        {
            "audio_session_id": "S20250526-000128",
            "recording_id": "A-1001_20250526_122300",
            "capability_statuses": {"asr": {"status": "success"}},
            "asr_segments": [
                {
                    "start_ms": 100,
                    "end_ms": 900,
                    "speaker": "销售A",
                    "text": "银河E8",
                    "confidence": 0.91,
                }
            ],
            "word_timestamps_storage_object_id": word_timestamps_id,
            "hotword_diagnostics": {
                "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
                "matched_terms": ["银河E8"],
                "missed_terms": [],
                "false_boosted_terms": [],
                "diagnostics_storage_object_id": diagnostics_id,
            },
        },
        key=f"audio-storage-{reference_kind}-{failure}-completion",
    )
    assert completed.status_code == expected_status, completed.text
    assert completed.json()["error"]["code"] == expected_code
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None and run.status == "submitted"
        assert "completion_receipt" not in run.payload


def test_hotword_analysis_run_separates_request_trace_from_domain_root_trace(
    client,
    auth_headers,
):
    requested = client.post(
        "/api/v1/hotword-analysis-runs",
        json={
            "date_from": "2026-07-01",
            "date_to": "2026-07-01",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(auth_headers, key="analysis-trace-separation"),
    )
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    request_trace_id = requested.json()["meta"]["trace_id"]
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        version = session.get(HotwordPackVersion, "hwpv-auto-sales-v1-8")
        assert run is not None and version is not None
        assert run.trace_id == request_trace_id
        assert run.payload["root_trace_id"] == version.root_trace_id
        assert run.trace_id != run.payload["root_trace_id"]


def test_hotword_completion_registers_only_run_scoped_storage_descriptors(
    client,
    auth_headers,
):
    pack, version, validating = _create_validating_hotword_version(
        client,
        auth_headers,
        key="completion-storage-registration",
    )
    validating_data = validating.json()["data"]
    build_run_id = validating_data["build_run_id"]
    manifest_id = "sto-hotword-completion-manifest"
    artifact_id = "sto-hotword-completion-artifact"
    artifact_sha256 = "7" * 64
    manifest_descriptor = _run_storage_descriptor(
        build_run_id,
        manifest_id,
        role="manifest",
        content_sha256=validating_data["content_sha256"],
    )
    manifest_descriptor.update(
        {
            "tenant_id": "attacker-tenant",
            "project_id": "attacker-project",
            "source_type": "attacker-source",
            "source_id": "attacker-id",
            "status": "uploading",
            "trace_id": "attacker-trace",
            "object_key_sha256": "0" * 64,
        }
    )
    result_ref = {
        "hotword_pack_version_id": version["version_id"],
        "content_sha256": validating_data["content_sha256"],
        "provider": "auris-audio-stack",
        "manifest_storage_object_id": manifest_id,
        "provider_artifact_ref": artifact_id,
        "artifact_sha256": artifact_sha256,
        "storage_objects": [
            manifest_descriptor,
            _run_storage_descriptor(
                build_run_id,
                artifact_id,
                role="provider_artifact",
                content_sha256=artifact_sha256,
                content_type="application/octet-stream",
            ),
        ],
    }
    completed = _complete_dagster_run(
        client,
        auth_headers,
        build_run_id,
        result_ref,
        key="completion-storage-registration-receipt",
    )
    assert completed.status_code == 200, completed.text
    registered = completed.json()["data"]["registered_storage_objects"]
    # Completion responses expose only domain registration status. Immutable
    # storage identifiers and locators remain in the scoped internal ledger.
    assert all("storage_object_id" not in item for item in registered)
    assert len(registered) == 2
    assert all(item["source_type"] == "hotword_build" for item in registered)
    assert all(item["source_id"] == build_run_id for item in registered)
    assert all(item["status"] == "verified" for item in registered)
    assert all(item["trace_id"] == pack["root_trace_id"] for item in registered)

    with SessionLocal() as session:
        stored_manifest = session.get(StorageObject, manifest_id)
        stored_artifact = session.get(StorageObject, artifact_id)
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert stored_manifest is not None and stored_artifact is not None
        assert stored_manifest.tenant_id == "aurora_auto"
        assert stored_manifest.project_id == "sales_qa"
        assert stored_manifest.source_type == "hotword_build"
        assert stored_manifest.source_id == build_run_id
        assert stored_manifest.status == "verified"
        assert stored_manifest.trace_id == pack["root_trace_id"]
        assert stored_manifest.payload["registration_mode"] == "trusted_run_completion"
        assert stored_artifact.content_sha256 == artifact_sha256
        assert stored_version is not None and stored_version.status == "ready_for_eval"
        registered_audits = (
            session.query(AuditLog).filter(AuditLog.action == "storage_object.registered").all()
        )
        assert {audit.object_id for audit in registered_audits} == {
            manifest_id,
            artifact_id,
        }
        assert {audit.trace_id for audit in registered_audits} == {pack["root_trace_id"]}
        registered_events = (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "storage_object.registered")
            .all()
        )
        assert {event.aggregate_id for event in registered_events} == {
            manifest_id,
            artifact_id,
        }
        assert {event.payload["trace_id"] for event in registered_events} == {pack["root_trace_id"]}
        assert {event.payload["correlation_id"] for event in registered_events} == {
            pack["root_trace_id"]
        }

        run = session.get(RunRecord, build_run_id)
        assert run is not None
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]

    replayed = client.post(
        f"/api/v1/runs/{build_run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": f"receipt-{build_run_id}",
            "external_id": external_run_id,
            "result_ref": result_ref,
        },
        headers=_headers(
            auth_headers,
            key="completion-storage-registration-receipt",
        ),
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["data"]["registered_storage_objects"] == registered


def test_hotword_completion_rejects_storage_id_collision(client, auth_headers):
    _, version, validating = _create_validating_hotword_version(
        client,
        auth_headers,
        key="completion-storage-collision",
    )
    validating_data = validating.json()["data"]
    build_run_id = validating_data["build_run_id"]
    manifest_id = "sto-hotword-collision-manifest"
    artifact_id = "sto-hotword-collision-artifact"
    _register_storage_object(
        manifest_id,
        content_sha256=validating_data["content_sha256"],
    )
    rejected = _complete_dagster_run(
        client,
        auth_headers,
        build_run_id,
        {
            "hotword_pack_version_id": version["version_id"],
            "content_sha256": validating_data["content_sha256"],
            "provider": "auris-audio-stack",
            "manifest_storage_object_id": manifest_id,
            "provider_artifact_ref": artifact_id,
            "artifact_sha256": "6" * 64,
            "storage_objects": [
                _run_storage_descriptor(
                    build_run_id,
                    manifest_id,
                    role="manifest",
                    content_sha256=validating_data["content_sha256"],
                ),
                _run_storage_descriptor(
                    build_run_id,
                    artifact_id,
                    role="provider_artifact",
                    content_sha256="6" * 64,
                ),
            ],
        },
        key="completion-storage-collision-receipt",
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "RUN_COMPLETION_STORAGE_COLLISION"
    with SessionLocal() as session:
        run = session.get(RunRecord, build_run_id)
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        collision = session.get(StorageObject, manifest_id)
        assert run is not None and run.status == "submitted"
        assert stored_version is not None and stored_version.status == "validating"
        assert collision is not None and collision.source_type == "test_artifact"
        assert session.get(StorageObject, artifact_id) is None


def test_canvas_task_bound_to_seeded_hotword_version_releases_after_gate(
    client,
    auth_headers,
):
    """Keep the ordinary Canvas publish path compatible with the v1.8 seed binding."""

    badcases = client.get(
        "/api/v1/badcases?capability=asr-hotword&limit=20",
        headers=auth_headers,
    )
    assert badcases.status_code == 200, badcases.text
    seeded_badcase = next(
        item for item in badcases.json()["data"]["items"] if item["badcase_id"] == "A-4107"
    )
    assert seeded_badcase["evidence_storage_object_id"] == ("storage_badcase_a_4107_evidence")
    assert seeded_badcase["evidence_ref"] == ("storage-object:storage_badcase_a_4107_evidence")

    created = client.post(
        "/api/v1/task-versions",
        json={
            "task_type_id": "evidence-dataflow",
            "version": "v3.3.0-rc1",
            "canvas_variant": "stable-v3",
            "label_version": "label_v1_8_4",
            "source": "canvas_module",
            "status": "draft",
            "audio_intelligence": {
                "execution_mode": "production",
                "language": "zh-CN",
                "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
                "return_word_timestamps": True,
            },
        },
        headers=_headers(auth_headers, key="canvas-seeded-hotword-draft"),
    )
    assert created.status_code == 201, created.text
    task_version_id = created.json()["data"]["id"]

    requested = client.post(
        f"/api/v1/task-versions/{task_version_id}/publish",
        json={"reason": "验证现有 Canvas 发布链路与种子热词版本兼容"},
        headers=_headers(auth_headers, key="canvas-seeded-hotword-publish"),
    )
    assert requested.status_code == 202, requested.text
    publish_run_id = requested.json()["data"]["run_id"]
    assert requested.json()["data"]["status"] == "blocked"
    assert process_aggregate_events([publish_run_id]) == 0

    second_admin_token = _release_second_admin_token()
    approved = client.post(
        f"/api/v1/runs/{publish_run_id}/decisions",
        json={"decision": "approved", "reason": "发布门禁已人工确认"},
        headers=_headers(
            auth_headers,
            key="canvas-seeded-hotword-approve",
            token=second_admin_token,
        ),
    )
    assert approved.status_code == 200, approved.text
    assert process_aggregate_events([publish_run_id]) == 1

    task_version = client.get(
        f"/api/v1/task-versions/{task_version_id}",
        headers=auth_headers,
    )
    assert task_version.status_code == 200, task_version.text
    assert task_version.json()["data"]["status"] == "published"
    assert task_version.json()["data"]["hotword_pack_version_id"] == "hwpv-auto-sales-v1-8"


def test_hotword_eval_authorization_hides_foreign_or_wrong_type_runs_and_rejects_bad_payload():
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="hotword-eval-authorization",
        trace_id="trace-hotword-eval-authorization",
    )
    with SessionLocal() as session:
        version = session.get(HotwordPackVersion, "hwpv-auto-sales-v1-8")
        assert version is not None
        session.add_all(
            [
                RunRecord(
                    run_id="hweval-foreign-tenant",
                    tenant_id="foreign-tenant",
                    project_id=ctx.project_id,
                    run_type="hotword_eval",
                    status="success",
                    trace_id="foreign-tenant-canary",
                    payload={"canary": "foreign-tenant-canary"},
                ),
                RunRecord(
                    run_id="hweval-foreign-project",
                    tenant_id=ctx.tenant_id,
                    project_id="foreign-project",
                    run_type="hotword_eval",
                    status="success",
                    trace_id="foreign-project-canary",
                    payload={"canary": "foreign-project-canary"},
                ),
                RunRecord(
                    run_id="hweval-wrong-type",
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    run_type="export",
                    status="success",
                    trace_id="wrong-type-canary",
                    payload={
                        "run_type": "hotword_eval",
                        "status": "success",
                        "canary": "wrong-type-canary",
                    },
                ),
                RunRecord(
                    run_id="hweval-invalid-payload",
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    run_type="hotword_eval",
                    status="success",
                    trace_id="invalid-payload-canary",
                    payload=[],
                ),
            ]
        )
        session.flush()

        for run_id, canary in (
            ("hweval-foreign-tenant", "foreign-tenant-canary"),
            ("hweval-foreign-project", "foreign-project-canary"),
            ("hweval-wrong-type", "wrong-type-canary"),
        ):
            with pytest.raises(ApiError) as hidden:
                _verify_eval_run(session, ctx, version, run_id)
            assert hidden.value.status_code == 404
            assert hidden.value.code == "HOTWORD_EVAL_RUN_NOT_FOUND"
            assert canary not in hidden.value.message

        with pytest.raises(ApiError) as invalid_payload:
            _verify_eval_run(session, ctx, version, "hweval-invalid-payload")
        assert invalid_payload.value.status_code == 409
        assert invalid_payload.value.code == "HOTWORD_EVAL_GATE_NOT_PASSED"


@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        ("cross_run_locator", "RUN_COMPLETION_STORAGE_LOCATOR_INVALID"),
        ("frozen_binding", "RUN_COMPLETION_STORAGE_BINDING_MISMATCH"),
        ("hash_mismatch", "RUN_COMPLETION_STORAGE_HASH_MISMATCH"),
    ],
)
def test_hotword_completion_rejects_unbound_storage_descriptors(
    client,
    auth_headers,
    mutation,
    expected_code,
):
    _, version, validating = _create_validating_hotword_version(
        client,
        auth_headers,
        key=f"completion-storage-{mutation}",
    )
    validating_data = validating.json()["data"]
    build_run_id = validating_data["build_run_id"]
    manifest_id = f"sto-hotword-{mutation}-manifest"
    artifact_id = f"sto-hotword-{mutation}-artifact"
    artifact_sha256 = "8" * 64
    result_ref = {
        "hotword_pack_version_id": version["version_id"],
        "content_sha256": validating_data["content_sha256"],
        "provider": "auris-audio-stack",
        "manifest_storage_object_id": manifest_id,
        "provider_artifact_ref": artifact_id,
        "artifact_sha256": artifact_sha256,
        "storage_objects": [
            _run_storage_descriptor(
                build_run_id,
                manifest_id,
                role="manifest",
                content_sha256=(
                    "9" * 64 if mutation == "hash_mismatch" else validating_data["content_sha256"]
                ),
                object_key=(
                    f"tenants/aurora_auto/projects/sales_qa/runs/other-run/{manifest_id}.json"
                    if mutation == "cross_run_locator"
                    else None
                ),
            ),
            _run_storage_descriptor(
                build_run_id,
                artifact_id,
                role="provider_artifact",
                content_sha256=artifact_sha256,
            ),
        ],
    }
    if mutation == "frozen_binding":
        result_ref["hotword_pack_version_id"] = "hwpv-other"
    rejected = _complete_dagster_run(
        client,
        auth_headers,
        build_run_id,
        result_ref,
        key=f"completion-storage-{mutation}-receipt",
    )
    assert rejected.status_code in {409, 422}, rejected.text
    assert rejected.json()["error"]["code"] == expected_code
    with SessionLocal() as session:
        run = session.get(RunRecord, build_run_id)
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert run is not None and run.status == "submitted"
        assert stored_version is not None and stored_version.status == "validating"
        assert session.get(StorageObject, manifest_id) is None
        assert session.get(StorageObject, artifact_id) is None
        assert (
            session.query(AuditLog).filter(AuditLog.action == "storage_object.registered").count()
            == 0
        )
        assert (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "storage_object.registered")
            .count()
            == 0
        )


def test_eval_approval_publish_creates_task_draft_and_preserves_root_trace(client, auth_headers):
    with SessionLocal() as session:
        seeded_pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        seeded_badcase = session.get(Badcase, "A-4107")
        assert seeded_pack is not None and seeded_badcase is not None
        seeded_evidence = session.get(
            StorageObject,
            seeded_badcase.evidence_storage_object_id,
        )
        assert seeded_evidence is not None
        assert seeded_evidence.source_id == "A-4107"
        assert seeded_badcase.evidence_ref == (
            f"storage-object:{seeded_evidence.storage_object_id}"
        )
        pack = {
            "pack_id": seeded_pack.pack_id,
            "root_trace_id": seeded_pack.root_trace_id,
            "current_version_id": seeded_pack.current_version_id,
            "source_badcase_root_trace_id": seeded_badcase.root_trace_id,
        }
    version_response = client.post(
        f"/api/v1/hotword-packs/{pack['pack_id']}/versions",
        json={"version": "v1.9"},
        headers=_headers(auth_headers, key="loop-version"),
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()["data"]
    assert version["baseline_version_id"] == pack["current_version_id"]
    assert version["inherited_item_count"] == 3
    assert {item["canonical_term"] for item in version["items"]} == {
        "星越L",
        "银河E8",
        "领克08",
    }
    inherited_xingyue = next(item for item in version["items"] if item["canonical_term"] == "星越L")
    patched_item = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items/"
        f"{inherited_xingyue['item_id']}",
        json={
            "expected_resource_version": inherited_xingyue["resource_version"],
            "aliases": ["星越 L", "星月L"],
            "weight": 91,
        },
        headers=_headers(auth_headers, key="loop-item-patch"),
    )
    assert patched_item.status_code == 200, patched_item.text
    assert patched_item.json()["data"]["source_badcase_id"] == "A-4107"
    with SessionLocal() as session:
        baseline_item = session.get(HotwordVersionItem, "hotword_item_xingyue_l")
        assert baseline_item is not None
        assert baseline_item.aliases == ["星越 L"]
        assert baseline_item.weight == 80

    validating = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 2,
            "status": "validating",
            "provider": "auris-audio-stack",
        },
        headers=_headers(auth_headers, key="loop-validating"),
    )
    assert validating.status_code == 200, validating.text
    validating_data = validating.json()["data"]
    build_request_trace_id = validating.json()["meta"]["trace_id"]
    build_run_id = validating_data["build_run_id"]
    build_result = {
        "hotword_pack_version_id": version["version_id"],
        "content_sha256": validating_data["content_sha256"],
        "provider": "auris-audio-stack",
        "manifest_storage_object_id": "sto-hotword-manifest-v1-9",
        "provider_artifact_ref": "sto-hotword-provider-v1-9",
        "artifact_sha256": "a" * 64,
        "storage_objects": [
            _run_storage_descriptor(
                build_run_id,
                "sto-hotword-manifest-v1-9",
                role="manifest",
                content_sha256=validating_data["content_sha256"],
            ),
            _run_storage_descriptor(
                build_run_id,
                "sto-hotword-provider-v1-9",
                role="provider_artifact",
                content_sha256="a" * 64,
                content_type="application/octet-stream",
            ),
        ],
    }
    built = _complete_dagster_run(
        client,
        auth_headers,
        build_run_id,
        build_result,
        key="loop-build-completion",
    )
    assert built.status_code == 200, built.text

    evaluated = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/eval-runs",
        json={
            "eval_dataset_id": "evalset-asr-hotword-v1",
            "provider": "auris-audio-stack",
            "expected_resource_version": 4,
        },
        headers=_headers(auth_headers, key="loop-eval", token="model-token"),
    )
    assert evaluated.status_code == 202, evaluated.text
    eval_data = evaluated.json()["data"]
    eval_request_trace_id = evaluated.json()["meta"]["trace_id"]
    assert eval_data["status"] == "pending"
    assert eval_data["baseline_mode"] == "published_version"
    assert eval_data["baseline_ref"] == pack["current_version_id"]
    assert eval_data["evaluated_terms"] == ["星越L"]
    assert len(eval_data["evaluated_term_ids"]) == 1
    eval_result = {
        "hotword_pack_version_id": version["version_id"],
        "baseline_version_id": pack["current_version_id"],
        "baseline_mode": "published_version",
        "baseline_ref": pack["current_version_id"],
        "evaluated_term_ids": eval_data["evaluated_term_ids"],
        "evaluated_terms": eval_data["evaluated_terms"],
        "eval_dataset_id": "evalset-asr-hotword-v1",
        "content_sha256": validating_data["content_sha256"],
        "manifest_storage_object_id": "sto-hotword-manifest-v1-9",
        "provider": "auris-audio-stack",
        "provider_artifact_ref": "sto-hotword-provider-v1-9",
        "artifact_sha256": "a" * 64,
        "baseline_metrics": {
            "trusted_occurrences": 40,
            "unique_terms": 3,
            "error_rate": 0.30,
            "recall_rate": 0.60,
            "false_boost_rate": 0.01,
            "cer": 0.10,
            "wer": 0.20,
            "downstream_f1": 0.80,
            "p95_latency_ms": 1000,
            "cost_per_minute": 0.10,
        },
        "candidate_metrics": {
            "trusted_occurrences": 40,
            "unique_terms": 3,
            "error_rate": 0.20,
            "recall_rate": 0.70,
            "false_boost_rate": 0.014,
            "cer": 0.101,
            "wer": 0.201,
            "downstream_f1": 0.798,
            "p95_latency_ms": 1040,
            "cost_per_minute": 0.104,
        },
        "per_term_trusted_occurrences": {"星越L": 14},
        "locked": True,
        "result_storage_object_ids": ["sto-hotword-eval-v1-9"],
        "storage_objects": [
            _run_storage_descriptor(
                eval_data["run_id"],
                "sto-hotword-eval-v1-9",
                role="eval_result",
                content_sha256="b" * 64,
            )
        ],
    }
    eval_completed = _complete_dagster_run(
        client,
        auth_headers,
        eval_data["run_id"],
        eval_result,
        key="loop-eval-completion",
    )
    assert eval_completed.status_code == 200, eval_completed.text
    assert eval_completed.json()["data"]["gate"]["passed"] is True

    approved = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 6,
            "status": "approved",
            "eval_run_id": eval_data["run_id"],
        },
        headers=_headers(auth_headers, key="loop-approve", token="model-token"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["model_approved_by"] == "u_model_001"

    published = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/publish",
        json={
            "expected_resource_version": 7,
            "eval_run_id": eval_data["run_id"],
            "confirmation": "publish",
        },
        headers=_headers(auth_headers, key="loop-publish"),
    )
    assert published.status_code == 202, published.text
    published_data = published.json()["data"]
    publish_request_trace_id = published.json()["meta"]["trace_id"]
    assert published_data["status"] == "pending"
    assert published_data["root_trace_id"] == pack["root_trace_id"]
    duplicate_publish = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/publish",
        json={
            "expected_resource_version": 8,
            "eval_run_id": eval_data["run_id"],
            "confirmation": "publish",
        },
        headers=_headers(auth_headers, key="loop-publish-duplicate"),
    )
    assert duplicate_publish.status_code == 409
    assert duplicate_publish.json()["error"]["code"] == "HOTWORD_PUBLISH_ALREADY_PENDING"
    with SessionLocal() as session:
        publish_run = session.get(RunRecord, published_data["run_id"])
        assert publish_run is not None
        publish_result = {
            "version_id": version["version_id"],
            "pack_id": pack["pack_id"],
            "eval_run_id": eval_data["run_id"],
            "content_sha256": publish_run.payload["content_sha256"],
            "manifest_storage_object_id": publish_run.payload["manifest_storage_object_id"],
            "compiled_provider": publish_run.payload["compiled_provider"],
            "provider_artifact_ref": publish_run.payload["provider_artifact_ref"],
            "artifact_sha256": publish_run.payload["artifact_sha256"],
        }
        preclaimed_task_version_id = (
            f"task_hotword_{version['version_id']}_{published_data['run_id'][-12:]}"
        )
        session.add(
            JsonResource(
                collection="task_versions",
                resource_key=preclaimed_task_version_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="draft",
                trace_id=pack["root_trace_id"],
                data={
                    "id": preclaimed_task_version_id,
                    "task_version_id": preclaimed_task_version_id,
                    "status": "draft",
                    "hotword_pack_version_id": version["version_id"],
                    "provider": publish_run.payload["compiled_provider"],
                    "language": "zh-CN",
                    "root_trace_id": pack["root_trace_id"],
                    "source_publish_run_id": published_data["run_id"],
                    "source": "client_preclaim",
                },
            )
        )
        session.commit()
    preclaimed_publish = _complete_dagster_run(
        client,
        auth_headers,
        published_data["run_id"],
        publish_result,
        key="loop-publish-preclaimed-task",
    )
    assert preclaimed_publish.status_code == 409, preclaimed_publish.text
    assert preclaimed_publish.json()["error"]["code"] == "HOTWORD_TASK_VERSION_ID_CONFLICT"
    with SessionLocal() as session:
        preclaimed_task = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "task_versions",
                JsonResource.resource_key == preclaimed_task_version_id,
            )
        )
        assert preclaimed_task is not None and preclaimed_task.data["source"] == "client_preclaim"
        session.delete(preclaimed_task)
        session.commit()
    publish_completed = _complete_dagster_run(
        client,
        auth_headers,
        published_data["run_id"],
        publish_result,
        key="loop-publish-completion",
    )
    assert publish_completed.status_code == 200, publish_completed.text
    assert publish_completed.json()["data"]["hotword_publish"]["version_status"] == "published"
    published_task_version_id = publish_completed.json()["data"]["hotword_publish"][
        "task_version_id"
    ]

    with SessionLocal() as session:
        pack_record = session.get(HotwordPack, pack["pack_id"])
        version_record = session.get(HotwordPackVersion, version["version_id"])
        build_record = session.get(RunRecord, build_run_id)
        eval_record = session.get(RunRecord, eval_data["run_id"])
        publish_record = session.get(RunRecord, published_data["run_id"])
        task_draft = (
            session.query(JsonResource)
            .filter(JsonResource.collection == "task_versions")
            .filter(
                JsonResource.data["hotword_pack_version_id"].as_string() == version["version_id"]
            )
            .one()
        )
        assert pack_record is not None and pack_record.current_version_id == version["version_id"]
        assert pack_record.production_version_id == "hwpv-auto-sales-v1-8"
        assert (
            version_record is not None
            and version_record.project_admin_confirmed_by == "u_admin_001"
        )
        assert version_record.payload["task_version_id"] == published_task_version_id
        assert version_record.payload["production_active"] is False
        assert version_record.payload["production_task_version_id"] is None
        assert build_record is not None
        assert build_record.trace_id == build_request_trace_id
        assert build_record.payload["root_trace_id"] == pack["root_trace_id"]
        assert eval_record is not None and eval_record.trace_id == eval_request_trace_id
        assert eval_record.payload["root_trace_id"] == pack["root_trace_id"]
        assert publish_record is not None
        assert publish_record.trace_id == publish_request_trace_id
        assert publish_record.payload["root_trace_id"] == pack["root_trace_id"]
        assert task_draft.trace_id == pack["root_trace_id"]
        assert task_draft.resource_key == published_task_version_id
        assert task_draft.data["source_publish_run_id"] == published_data["run_id"]
        assert (
            session.query(AuditLog).filter(AuditLog.action == "hotword_version.published").count()
            == 1
        )
        events = {event.event_type for event in session.query(OutboxEvent).all()}
        assert "hotword_pack_version.eval-requested" in events
        assert "hotword_pack_version.publish-requested" in events
        assert "hotword_pack_version.published" in events

    immutable = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 9,
            "eval_run_id": "hweval-mutated",
        },
        headers=_headers(auth_headers, key="loop-published-immutable"),
    )
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "HOTWORD_VERSION_IMMUTABLE"

    legitimate_task_patch = client.patch(
        f"/api/v1/task-versions/{published_task_version_id}",
        json={"version": "hotword-release-v1"},
        headers=_headers(auth_headers, key="loop-task-legitimate-patch"),
    )
    assert legitimate_task_patch.status_code == 200, legitimate_task_patch.text
    assert legitimate_task_patch.json()["data"]["root_trace_id"] == pack["root_trace_id"]
    assert legitimate_task_patch.json()["data"]["source_publish_run_id"] == published_data["run_id"]

    tampered_task_lineage = client.patch(
        f"/api/v1/task-versions/{published_task_version_id}",
        json={
            "source_publish_run_id": "hotword_publish_tampered",
            "root_trace_id": "trace_tampered",
        },
        headers=_headers(auth_headers, key="loop-task-lineage-tamper"),
    )
    assert tampered_task_lineage.status_code == 409
    assert tampered_task_lineage.json()["error"]["code"] == "HOTWORD_TASK_VERSION_LINEAGE_IMMUTABLE"

    before_task_publish = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["asr"],
            "task_version_id": published_task_version_id,
        },
        headers=_headers(auth_headers, key="loop-audio-before-task-publish"),
    )
    assert before_task_publish.status_code == 409, before_task_publish.text
    assert before_task_publish.json()["error"]["code"] == "TASK_VERSION_NOT_PUBLISHED"

    task_publish = client.post(
        f"/api/v1/task-versions/{published_task_version_id}/publish",
        json={"reason": "热词包已通过锁定评测与双人审批"},
        headers=_headers(auth_headers, key="loop-task-publish"),
    )
    assert task_publish.status_code == 202, task_publish.text
    task_publish_run_id = task_publish.json()["data"]["run_id"]
    assert task_publish.json()["data"]["status"] == "blocked"
    assert process_aggregate_events([task_publish_run_id]) == 0
    second_admin_token = _release_second_admin_token()
    task_publish_approval = client.post(
        f"/api/v1/runs/{task_publish_run_id}/decisions",
        json={"decision": "approved", "reason": "任务版本生产发布门禁已确认"},
        headers=_headers(
            auth_headers,
            key="loop-task-publish-approve",
            token=second_admin_token,
        ),
    )
    assert task_publish_approval.status_code == 200, task_publish_approval.text
    assert process_aggregate_events([task_publish_run_id]) == 1
    task_detail = client.get(
        f"/api/v1/task-versions/{published_task_version_id}",
        headers=auth_headers,
    )
    assert task_detail.status_code == 200, task_detail.text
    assert task_detail.json()["data"]["status"] == "published"
    assert task_detail.json()["data"]["task_type_id"] == "task_sales_quality"
    assert task_detail.json()["data"]["task_type_binding"]["source"] == ("baseline_task_version")
    assert task_detail.json()["data"]["model_version"] == "asr_v2.3.1"
    assert task_detail.json()["data"]["scene_profile_id"] == "scene_auto_sales_quality"
    assert task_detail.json()["data"]["scene_profile_version_id"] == "scenev_auto_sales_quality_v1"
    assert len(task_detail.json()["data"]["scene_profile_snapshot_sha256"]) == 64
    with SessionLocal() as session:
        activated_pack = session.get(HotwordPack, pack["pack_id"])
        activated_version = session.get(HotwordPackVersion, version["version_id"])
        assert activated_pack is not None
        assert activated_pack.production_version_id == version["version_id"]
        assert activated_version is not None
        assert activated_version.payload["production_active"] is True
        assert activated_version.payload["production_task_version_id"] == published_task_version_id
        assert (
            session.query(AuditLog)
            .filter(AuditLog.action == "hotword_version.production_activated")
            .count()
            == 1
        )
        assert (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "hotword_pack_version.production-activated")
            .count()
            == 1
        )

    production_audio = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["asr"],
            "task_version_id": published_task_version_id,
        },
        headers=_headers(auth_headers, key="loop-audio-after-task-publish"),
    )
    assert production_audio.status_code == 202, production_audio.text
    assert production_audio.json()["data"]["hotword_pack_version_id"] == version["version_id"]
    assert production_audio.json()["data"]["task_version_id"] == published_task_version_id

    superseded_audio = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["asr"],
            "task_version_id": "task_version_v3_2_1",
        },
        headers=_headers(auth_headers, key="loop-audio-superseded-task-version"),
    )
    assert superseded_audio.status_code == 409, superseded_audio.text
    assert superseded_audio.json()["error"]["code"] == "TASK_VERSION_NOT_PUBLISHED"
    superseded_task = client.get("/api/v1/task-versions/task_version_v3_2_1", headers=auth_headers)
    assert superseded_task.status_code == 200, superseded_task.text
    assert superseded_task.json()["data"]["status"] == "deprecated"

    backfill_payload = {
        "reason": "使用已发布 ASR 热词包生成新转写资产",
        "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/hotword-v1.9",
        "impact_scope": {
            "hotword_pack_version_id": version["version_id"],
            "eval_run_id": eval_data["run_id"],
            "task_version_id": published_task_version_id,
            "materialization_id": "mat_asr_20250526_122300",
            "overwrite_history": False,
        },
    }
    backfill = client.post(
        "/api/v1/data-assets/auris/model/asr_transcripts/backfills",
        json=backfill_payload,
        headers=_headers(auth_headers, key="loop-controlled-backfill"),
    )
    assert backfill.status_code == 202, backfill.text
    backfill_data = backfill.json()["data"]
    assert backfill_data["root_trace_id"] == pack["root_trace_id"]
    assert backfill_data["impact_scope"]["overwrite_history"] is False
    assert {item["type"] for item in backfill_data["affected_objects"]} == {
        "data_asset",
        "asset_materialization",
        "hotword_pack_version",
        "eval_run",
        "task_version",
    }
    backfill_run_id = backfill_data["run_id"]
    assert process_aggregate_events([backfill_run_id]) == 1
    with SessionLocal() as session:
        backfill_run = session.get(RunRecord, backfill_run_id)
        assert backfill_run is not None and backfill_run.status == "submitted"
        assert backfill_run.trace_id == pack["root_trace_id"]
        external_run_id = backfill_run.payload["dispatch"]["details"]["external_run_id"]
    backfill_storage_object_id = f"sto-{backfill_run_id}-materialization"
    backfill_content_sha256 = hashlib.sha256(b"hotword-v1.9-backfill").hexdigest()
    backfill_completed = client.post(
        f"/api/v1/runs/{backfill_run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "receipt-hotword-controlled-backfill",
            "external_id": external_run_id,
            "result_ref": {
                "asset_key": "auris/model/asr_transcripts",
                "partition_key": backfill_payload["partition_key"],
                "storage_object_id": backfill_storage_object_id,
                "storage_objects": [
                    _run_storage_descriptor(
                        backfill_run_id,
                        backfill_storage_object_id,
                        role="asset_materialization",
                        content_sha256=backfill_content_sha256,
                        content_type="application/x-ndjson",
                    )
                ],
                "upstream_asset_keys": ["auris/audio/raw_recordings"],
                "downstream_asset_keys": ["auris/label/event_tags"],
                "record_count": 128,
                "error_count": 0,
                "checks": [{"name": "schema", "status": "passed"}],
            },
            "metrics": {"record_count": 128, "error_count": 0},
        },
        headers=_headers(auth_headers, key="loop-controlled-backfill-completion"),
    )
    assert backfill_completed.status_code == 200, backfill_completed.text
    registered_backfill_objects = backfill_completed.json()["data"]["registered_storage_objects"]
    assert len(registered_backfill_objects) == 1
    assert "storage_object_id" not in registered_backfill_objects[0]
    assert registered_backfill_objects[0]["source_id"] == backfill_run_id
    assert registered_backfill_objects[0]["status"] == "verified"
    backfill_materialization_id = backfill_completed.json()["data"]["materialized_assets"][0][
        "materialization_id"
    ]
    with SessionLocal() as session:
        original = session.get(AssetMaterialization, "mat_asr_20250526_122300")
        replacement = session.get(AssetMaterialization, backfill_materialization_id)
        assert original is not None and original.status == "success"
        assert replacement is not None and replacement.status == "success"
        assert replacement.trace_id == pack["root_trace_id"]
        assert replacement.payload["source_materialization_id"] == original.materialization_id
        assert replacement.payload["hotword_pack_version_id"] == version["version_id"]
        assert replacement.payload["eval_run_id"] == eval_data["run_id"]
        assert replacement.payload["task_version_id"] == published_task_version_id
        assert replacement.payload["root_trace_id"] == pack["root_trace_id"]
        assert replacement.payload["overwrite_history"] is False
        assert replacement.payload["storage_refs"][0]["storage_object_id"] == (
            backfill_storage_object_id
        )

    lineage = client.get(
        "/api/v1/data-assets/auris/model/asr_transcripts/lineage",
        headers=auth_headers,
    )
    assert lineage.status_code == 200, lineage.text
    lineage_data = lineage.json()["data"]
    governed_nodes = {node["asset_key"]: node for node in lineage_data["nodes"]}
    assert {
        "mat_asr_20250526_122300",
        "storage_badcase_a_4107_evidence",
        "A-4107",
        version["version_id"],
        eval_data["run_id"],
        published_task_version_id,
        backfill_run_id,
    } <= set(governed_nodes)
    assert (
        governed_nodes["storage_badcase_a_4107_evidence"]["trace_id"]
        == pack["source_badcase_root_trace_id"]
    )
    assert governed_nodes["A-4107"]["trace_id"] == pack["source_badcase_root_trace_id"]
    assert {
        governed_nodes[node_id]["trace_id"]
        for node_id in (
            version["version_id"],
            eval_data["run_id"],
            published_task_version_id,
            backfill_run_id,
        )
    } == {pack["root_trace_id"]}
    governed_relations = {
        edge.get("relation")
        for edge in lineage_data["edges"]
        if edge.get("lineage_source") == "hotword_governance"
    }
    assert {
        "supports",
        "fixed-by",
        "evaluated-by",
        "bound-to",
        "executed-by",
        "materialized-as",
        "reprocessed-by",
    } <= governed_relations

    with SessionLocal() as session:
        active_version = session.get(HotwordPackVersion, version["version_id"])
        assert active_version is not None
        active_resource_version = active_version.resource_version
    current_deprecation = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": active_resource_version,
            "status": "deprecated",
        },
        headers=_headers(auth_headers, key="loop-current-deprecation"),
    )
    assert current_deprecation.status_code == 409
    assert (
        current_deprecation.json()["error"]["code"]
        == "HOTWORD_CURRENT_VERSION_DEPRECATION_FORBIDDEN"
    )
    with SessionLocal() as session:
        historical_version = session.get(HotwordPackVersion, pack["current_version_id"])
        assert historical_version is not None and historical_version.status == "published"
        historical_resource_version = historical_version.resource_version
    historical_deprecation = client.patch(
        f"/api/v1/hotword-pack-versions/{pack['current_version_id']}",
        json={
            "expected_resource_version": historical_resource_version,
            "status": "deprecated",
        },
        headers=_headers(auth_headers, key="loop-historical-deprecation"),
    )
    assert historical_deprecation.status_code == 200, historical_deprecation.text
    assert historical_deprecation.json()["data"]["status"] == "deprecated"


def test_first_hotword_version_uses_no_hotword_baseline_and_cannot_bypass_gate(
    client,
    auth_headers,
):
    pack_response = client.post(
        "/api/v1/hotword-packs",
        json={"name": "首版安全引导词包", "language": "zh-CN", "domain": "bootstrap"},
        headers=_headers(auth_headers, key="bootstrap-pack"),
    )
    assert pack_response.status_code == 201, pack_response.text
    pack = pack_response.json()["data"]
    assert pack["current_version_id"] is None
    version_response = client.post(
        f"/api/v1/hotword-packs/{pack['pack_id']}/versions",
        json={"version": "v1", "task_type_id": "task_sales_quality"},
        headers=_headers(auth_headers, key="bootstrap-version"),
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()["data"]
    assert version["baseline_version_id"] is None
    for index, term in enumerate(("星越L", "银河E8", "领克08"), start=1):
        item = client.post(
            f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
            json={
                "canonical_term": term,
                "aliases": [],
                "category": "vehicle-model",
                "weight": 80 + index,
            },
            headers=_headers(auth_headers, key=f"bootstrap-item-{index}"),
        )
        assert item.status_code == 201, item.text

    validating = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 4,
            "status": "validating",
            "provider": "auris-audio-stack",
        },
        headers=_headers(auth_headers, key="bootstrap-validating"),
    )
    assert validating.status_code == 200, validating.text
    validating_data = validating.json()["data"]
    manifest_id = "sto-bootstrap-manifest"
    artifact_id = "sto-bootstrap-artifact"
    _register_storage_object(manifest_id, content_sha256=validating_data["content_sha256"])
    _register_storage_object(artifact_id, content_sha256="c" * 64)
    built = _complete_dagster_run(
        client,
        auth_headers,
        validating_data["build_run_id"],
        {
            "hotword_pack_version_id": version["version_id"],
            "content_sha256": validating_data["content_sha256"],
            "provider": "auris-audio-stack",
            "manifest_storage_object_id": manifest_id,
            "provider_artifact_ref": artifact_id,
            "artifact_sha256": "c" * 64,
        },
        key="bootstrap-build-completion",
    )
    assert built.status_code == 200, built.text

    eval_dataset_manifest_id = "sto-bootstrap-eval-dataset-manifest"
    _register_storage_object(
        eval_dataset_manifest_id,
        status="verified",
        content_sha256="d" * 64,
        source_type="eval_dataset_manifest",
        source_id="evalset-bootstrap-fixed-v1",
    )
    eval_dataset = client.post(
        "/api/v1/eval-datasets",
        json={
            "eval_dataset_id": "evalset-bootstrap-fixed-v1",
            "name": "首版本热词固定回归集",
            "capability": "asr_hotword",
            "dataset_version": "v1",
            "manifest_storage_object_id": eval_dataset_manifest_id,
            "manifest_sha256": "d" * 64,
            "sample_count": 40,
        },
        headers=_headers(auth_headers, key="bootstrap-eval-dataset-create"),
    )
    assert eval_dataset.status_code == 201, eval_dataset.text
    locked_eval_dataset = client.post(
        "/api/v1/eval-datasets/evalset-bootstrap-fixed-v1/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, key="bootstrap-eval-dataset-lock"),
    )
    assert locked_eval_dataset.status_code == 200, locked_eval_dataset.text

    def request_eval(key: str, expected_resource_version: int):
        response = client.post(
            f"/api/v1/hotword-pack-versions/{version['version_id']}/eval-runs",
            json={
                "eval_dataset_id": "evalset-bootstrap-fixed-v1",
                "provider": "auris-audio-stack",
                "expected_resource_version": expected_resource_version,
            },
            headers=_headers(auth_headers, key=key, token="model-token"),
        )
        assert response.status_code == 202, response.text
        return response.json()["data"]

    def eval_result(eval_data: dict, *, passing: bool, storage_ids: list[str]):
        return {
            "hotword_pack_version_id": version["version_id"],
            "baseline_version_id": None,
            "baseline_mode": "no_hotword",
            "baseline_ref": "baseline:no-hotword",
            "evaluated_term_ids": eval_data["evaluated_term_ids"],
            "evaluated_terms": eval_data["evaluated_terms"],
            "eval_dataset_id": "evalset-bootstrap-fixed-v1",
            "content_sha256": validating_data["content_sha256"],
            "manifest_storage_object_id": manifest_id,
            "provider": "auris-audio-stack",
            "provider_artifact_ref": artifact_id,
            "artifact_sha256": "c" * 64,
            "baseline_metrics": {
                "trusted_occurrences": 40,
                "unique_terms": 3,
                "error_rate": 0.30,
                "recall_rate": 0.60,
                "false_boost_rate": 0.01,
                "cer": 0.10,
                "wer": 0.20,
                "downstream_f1": 0.80,
                "p95_latency_ms": 1000,
                "cost_per_minute": 0.10,
            },
            "candidate_metrics": {
                "trusted_occurrences": 40,
                "unique_terms": 3,
                "error_rate": 0.20 if passing else 0.29,
                "recall_rate": 0.70 if passing else 0.61,
                "false_boost_rate": 0.014,
                "cer": 0.101,
                "wer": 0.201,
                "downstream_f1": 0.798,
                "p95_latency_ms": 1040,
                "cost_per_minute": 0.104,
            },
            "per_term_trusted_occurrences": {term: 10 for term in eval_data["evaluated_terms"]},
            "locked": True,
            "result_storage_object_ids": storage_ids,
        }

    first_eval = request_eval("bootstrap-eval-blocked", 6)
    assert first_eval["baseline_mode"] == "no_hotword"
    assert first_eval["baseline_ref"] == "baseline:no-hotword"
    assert set(first_eval["evaluated_terms"]) == {"星越L", "银河E8", "领克08"}
    empty_result = _complete_dagster_run(
        client,
        auth_headers,
        first_eval["run_id"],
        eval_result(first_eval, passing=True, storage_ids=[]),
        key="bootstrap-eval-empty-result",
    )
    assert empty_result.status_code == 422, empty_result.text
    assert empty_result.json()["error"]["code"] == "HOTWORD_EVAL_RESULT_STORAGE_REQUIRED"
    with SessionLocal() as session:
        run = session.get(RunRecord, first_eval["run_id"])
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert run is not None and run.status == "submitted"
        assert stored_version is not None and stored_version.status == "evaluating"

    _register_storage_object("sto-bootstrap-eval-blocked", content_sha256="d" * 64)
    blocked_completion = _complete_dagster_run(
        client,
        auth_headers,
        first_eval["run_id"],
        eval_result(
            first_eval,
            passing=False,
            storage_ids=["sto-bootstrap-eval-blocked"],
        ),
        key="bootstrap-eval-blocked-completion",
    )
    assert blocked_completion.status_code == 200, blocked_completion.text
    assert blocked_completion.json()["data"]["gate"]["passed"] is False
    blocked_approval = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 8,
            "status": "approved",
            "eval_run_id": first_eval["run_id"],
        },
        headers=_headers(auth_headers, key="bootstrap-blocked-approval", token="model-token"),
    )
    assert blocked_approval.status_code == 409

    ready_again = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={"expected_resource_version": 8, "status": "ready_for_eval"},
        headers=_headers(auth_headers, key="bootstrap-ready-again", token="model-token"),
    )
    assert ready_again.status_code == 200, ready_again.text
    passing_eval = request_eval("bootstrap-eval-passing", 9)
    _register_storage_object("sto-bootstrap-eval-passing", content_sha256="f" * 64)
    passing_completion = _complete_dagster_run(
        client,
        auth_headers,
        passing_eval["run_id"],
        eval_result(
            passing_eval,
            passing=True,
            storage_ids=["sto-bootstrap-eval-passing"],
        ),
        key="bootstrap-eval-passing-completion",
    )
    assert passing_completion.status_code == 200, passing_completion.text
    assert passing_completion.json()["data"]["gate"]["passed"] is True
    with SessionLocal() as session:
        eval_storage = session.get(StorageObject, "sto-bootstrap-eval-passing")
        assert eval_storage is not None
        eval_storage.content_sha256 = "0" * 64
        session.commit()
    drifted_evidence_approval = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 11,
            "status": "approved",
            "eval_run_id": passing_eval["run_id"],
        },
        headers=_headers(
            auth_headers,
            key="bootstrap-drifted-evidence-approval",
            token="model-token",
        ),
    )
    assert drifted_evidence_approval.status_code == 409
    assert (
        drifted_evidence_approval.json()["error"]["code"] == "STORAGE_OBJECT_CONTENT_HASH_MISMATCH"
    )
    with SessionLocal() as session:
        eval_storage = session.get(StorageObject, "sto-bootstrap-eval-passing")
        assert eval_storage is not None
        eval_storage.content_sha256 = "f" * 64
        session.commit()
    approved = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 11,
            "status": "approved",
            "eval_run_id": passing_eval["run_id"],
        },
        headers=_headers(auth_headers, key="bootstrap-approved", token="model-token"),
    )
    assert approved.status_code == 200, approved.text
    publish = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/publish",
        json={
            "expected_resource_version": 12,
            "eval_run_id": passing_eval["run_id"],
            "confirmation": "publish",
        },
        headers=_headers(auth_headers, key="bootstrap-publish"),
    )
    assert publish.status_code == 202, publish.text
    publish_data = publish.json()["data"]
    with SessionLocal() as session:
        publish_run = session.get(RunRecord, publish_data["run_id"])
        assert publish_run is not None
        publish_result = {
            "version_id": version["version_id"],
            "pack_id": pack["pack_id"],
            "eval_run_id": passing_eval["run_id"],
            "content_sha256": publish_run.payload["content_sha256"],
            "manifest_storage_object_id": publish_run.payload["manifest_storage_object_id"],
            "compiled_provider": publish_run.payload["compiled_provider"],
            "provider_artifact_ref": publish_run.payload["provider_artifact_ref"],
            "artifact_sha256": publish_run.payload["artifact_sha256"],
        }
    publish_completion = _complete_dagster_run(
        client,
        auth_headers,
        publish_data["run_id"],
        publish_result,
        key="bootstrap-publish-completion",
    )
    assert publish_completion.status_code == 200, publish_completion.text
    with SessionLocal() as session:
        stored_pack = session.get(HotwordPack, pack["pack_id"])
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert stored_pack is not None and stored_pack.current_version_id == version["version_id"]
        assert stored_version is not None and stored_version.status == "published"
        assert stored_version.payload["task_version_id"]


def test_hotword_build_retry_of_retry_preserves_frozen_origin_binding(client, auth_headers):
    pack = client.post(
        "/api/v1/hotword-packs",
        json={"name": "多级重试热词包", "language": "zh-CN", "domain": "retry-test"},
        headers=_headers(auth_headers, key="retry-pack"),
    ).json()["data"]
    version = client.post(
        f"/api/v1/hotword-packs/{pack['pack_id']}/versions",
        json={"version": "retry-v1"},
        headers=_headers(auth_headers, key="retry-version"),
    ).json()["data"]
    item = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
        json={
            "canonical_term": "银河E8",
            "aliases": [],
            "category": "vehicle-model",
            "weight": 80,
        },
        headers=_headers(auth_headers, key="retry-item"),
    )
    assert item.status_code == 201, item.text
    validating = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 2,
            "status": "validating",
            "provider": "auris-audio-stack",
        },
        headers=_headers(auth_headers, key="retry-build"),
    )
    assert validating.status_code == 200, validating.text
    source_run_id = validating.json()["data"]["build_run_id"]

    with SessionLocal() as session:
        source = session.get(RunRecord, source_run_id)
        assert source is not None
        source.status = "failed"
        session.commit()
    retry_one = client.post(
        f"/api/v1/runs/{source_run_id}/retries",
        json={"reason": "第一次重新编译"},
        headers=_headers(auth_headers, key="retry-build-one"),
    )
    assert retry_one.status_code == 202, retry_one.text
    retry_one_id = retry_one.json()["data"]["run_id"]
    assert retry_one.json()["data"]["origin_run_id"] == source_run_id

    with SessionLocal() as session:
        first_retry = session.get(RunRecord, retry_one_id)
        assert first_retry is not None
        first_retry.status = "failed"
        session.commit()
    retry_two = client.post(
        f"/api/v1/runs/{retry_one_id}/retries",
        json={"reason": "第二次重新编译"},
        headers=_headers(auth_headers, key="retry-build-two"),
    )
    assert retry_two.status_code == 202, retry_two.text
    retry_two_id = retry_two.json()["data"]["run_id"]
    assert retry_two.json()["data"]["origin_run_id"] == source_run_id
    with SessionLocal() as session:
        second_retry = session.get(RunRecord, retry_two_id)
        assert second_retry is not None
        result_ref = {
            "hotword_pack_version_id": version["version_id"],
            "content_sha256": second_retry.payload["content_sha256"],
            "provider": second_retry.payload["provider"],
            "manifest_storage_object_id": "sto-retry-manifest",
            "provider_artifact_ref": "sto-retry-provider-artifact",
            "artifact_sha256": "c" * 64,
        }
    _register_storage_object(
        result_ref["manifest_storage_object_id"],
        content_sha256=result_ref["content_sha256"],
    )
    _register_storage_object(
        result_ref["provider_artifact_ref"],
        content_sha256=result_ref["artifact_sha256"],
    )
    completed = _complete_dagster_run(
        client,
        auth_headers,
        retry_two_id,
        result_ref,
        key="retry-build-completion",
    )
    assert completed.status_code == 200, completed.text
    with SessionLocal() as session:
        completed_version = session.get(HotwordPackVersion, version["version_id"])
        assert completed_version is not None
        assert completed_version.status == "ready_for_eval"


def test_hotword_build_adapter_failure_retries_dead_letters_and_recovers(
    client,
    auth_headers,
):
    pack, version, validating = _create_validating_hotword_version(
        client,
        auth_headers,
        key="adapter-failure",
    )
    validating_data = validating.json()["data"]
    build_run_id = validating_data["build_run_id"]
    request_trace_id = validating.json()["meta"]["trace_id"]
    with SessionLocal() as session:
        run = session.get(RunRecord, build_run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == build_run_id).one()
        assert run is not None
        assert run.trace_id == request_trace_id
        assert run.payload["root_trace_id"] == pack["root_trace_id"]
        event.payload = {
            **event.payload,
            "simulate_adapter_failure": True,
            "adapter_error_code": "HOTWORD_PROVIDER_TIMEOUT",
            "adapter_error_message": "热词 provider 暂时不可用",
            "adapter_retryable": True,
            "retry_after_seconds": 0,
            "max_attempts": 2,
        }
        session.commit()

    assert process_aggregate_events([build_run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, build_run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == build_run_id).one()
        assert run is not None and run.status == "running"
        assert run.payload["dispatch_state"] == "retry_wait"
        assert run.payload["error_code"] == "HOTWORD_PROVIDER_TIMEOUT"
        assert event.status == "pending"
        assert event.attempt_count == 1

    assert process_aggregate_events([build_run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, build_run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == build_run_id).one()
        assert run is not None and run.status == "failed"
        assert run.payload["dispatch_state"] == "dead_letter"
        assert run.payload["dead_letter_event_id"] == event.event_id
        assert run.payload["retryable"] is True
        assert event.status == "dead_letter"
        assert event.attempt_count == 2

    retry = client.post(
        f"/api/v1/runs/{build_run_id}/retries",
        json={"reason": "provider 已恢复，重新构建热词包"},
        headers=_headers(auth_headers, key="adapter-failure-retry"),
    )
    assert retry.status_code == 202, retry.text
    retry_data = retry.json()["data"]
    retry_run_id = retry_data["run_id"]
    assert retry_data["retry_of_run_id"] == build_run_id
    assert retry_data["root_trace_id"] == pack["root_trace_id"]
    assert "simulate_adapter_failure" not in retry_data

    manifest_id = "sto-hotword-adapter-recovery-manifest"
    provider_artifact_id = "sto-hotword-adapter-recovery-provider"
    _register_storage_object(manifest_id, content_sha256=validating_data["content_sha256"])
    _register_storage_object(provider_artifact_id, content_sha256="9" * 64)
    recovered = _complete_dagster_run(
        client,
        auth_headers,
        retry_run_id,
        {
            "hotword_pack_version_id": version["version_id"],
            "content_sha256": validating_data["content_sha256"],
            "provider": "auris-audio-stack",
            "manifest_storage_object_id": manifest_id,
            "provider_artifact_ref": provider_artifact_id,
            "artifact_sha256": "9" * 64,
        },
        key="adapter-failure-retry-completion",
    )
    assert recovered.status_code == 200, recovered.text
    with SessionLocal() as session:
        retry_run = session.get(RunRecord, retry_run_id)
        stored_version = session.get(HotwordPackVersion, version["version_id"])
        assert retry_run is not None and retry_run.status == "success"
        assert retry_run.payload["root_trace_id"] == pack["root_trace_id"]
        assert stored_version is not None and stored_version.status == "ready_for_eval"


def test_audio_production_task_rejects_explicit_model_override(client, auth_headers):
    response = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["asr"],
            "task_version_id": "task_version_v3_2_1",
            "model_version": "audio-v2.3.1",
            "execution_mode": "production",
            "language": "zh-CN",
            "provider": "auris-audio-stack",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(auth_headers, key="audio-task-model-override"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_TASK_MODEL_BINDING_MISMATCH"


def test_audio_completion_receipt_persists_only_hotword_diagnostic_summary(client, auth_headers):
    requested = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["asr"],
            "task_version_id": "task_version_v3_2_1",
            "execution_mode": "production",
            "language": "zh-CN",
            "provider": "auris-audio-stack",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
            "return_word_timestamps": True,
            "run_key": "caller-selected-other-model",
        },
        headers=_headers(auth_headers, key="audio-hotword-summary-request"),
    )
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    _register_storage_object(
        "sto-word-ts-hotword-summary",
        content_sha256="d" * 64,
    )
    _register_storage_object(
        "sto-hotword-diagnostics-summary",
        content_sha256="e" * 64,
    )
    completed = _complete_dagster_run(
        client,
        auth_headers,
        run_id,
        {
            "audio_session_id": "S20250526-000128",
            "recording_id": "A-1001_20250526_122300",
            "capability_statuses": {"asr": {"status": "success"}},
            "asr_segments": [
                {
                    "start_ms": 100,
                    "end_ms": 900,
                    "speaker": "销售A",
                    "text": "星越L",
                    "confidence": 0.91,
                }
            ],
            "word_timestamps_storage_object_id": "sto-word-ts-hotword-summary",
            "hotword_diagnostics": {
                "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
                "matched_terms": ["星越L"],
                "missed_terms": ["银河E8"],
                "false_boosted_terms": ["星月L"],
                "provider_artifact_version": "v1.8",
                "diagnostics_storage_object_id": "sto-hotword-diagnostics-summary",
            },
        },
        key="audio-hotword-summary-completion",
    )
    assert completed.status_code == 200, completed.text

    with SessionLocal() as session:
        receipt = (
            session.query(RunCompletionReceipt).filter(RunCompletionReceipt.run_id == run_id).one()
        )
        stored = receipt.request_body["result_ref"]["hotword_diagnostics"]
        assert stored == {
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
            "matched_count": 1,
            "missed_count": 1,
            "false_boosted_count": 1,
            "provider_artifact_version": "v1.8",
            "diagnostics_storage_object_id": "sto-hotword-diagnostics-summary",
        }
        run = session.get(RunRecord, run_id)
        assert run is not None
        requested_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "audio_intelligence.requested",
            )
        )
        assert requested_event is not None
        assert run.payload["model_version"] == "asr_v2.3.1"
        assert requested_event.payload["model_version"] == "asr_v2.3.1"
        assert ":asr_v2.3.1:" in run.run_key
        assert run.run_key != "caller-selected-other-model"
        assert run.payload["result_ref"]["hotword_diagnostics"] == stored


def test_hotword_analysis_completion_materializes_scoped_statistics_and_event(client, auth_headers):
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="hotword-materialize",
        trace_id="trace_materialize_request",
        idempotency_key="hotword-materialize",
    )
    analysis_run_id = "hwanalysis_materialize_1"
    analysis_root_trace_id = "trace_hotword_pack_auto_sales"
    _register_storage_object(
        "sto-word-ts-1",
        status="verified",
        content_sha256="7" * 64,
        source_type="hotword_analysis",
        source_id=analysis_run_id,
        trace_id=analysis_root_trace_id,
        payload={"role": "word_timestamps"},
    )
    _register_storage_object(
        "sto-diagnostics-1",
        status="verified",
        content_sha256="8" * 64,
        source_type="hotword_analysis",
        source_id=analysis_run_id,
        trace_id=analysis_root_trace_id,
        payload={"role": "diagnostics"},
    )
    with SessionLocal() as session:
        run = RunRecord(
            run_id=analysis_run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="hotword_analysis",
            status="success",
            run_key="hotword-analysis:test",
            partition_key="aurora_auto/sales_qa",
            trace_id=ctx.trace_id,
            payload={"root_trace_id": analysis_root_trace_id},
        )
        session.add(run)
        session.flush()
        result = materialize_hotword_analysis_completion(
            session,
            ctx,
            run,
            {
                "result_ref": {
                    "metric_snapshots": [
                        {
                            "snapshot_id": "hwmetric-1",
                            "bucket_start": "2026-07-01T00:00:00+00:00",
                            "bucket_end": "2026-07-02T00:00:00+00:00",
                            "store_id": "BJ-AURORA-001",
                            "provider": "asr-provider-a",
                            "model_version": "asr-v2",
                            "standard_term": "星越L",
                            "expected_count": 10,
                            "correct_count": 7,
                            "weighted_error_count": 3,
                            "false_insert_count": 1,
                            "recognized_hotword_count": 20,
                            "impacted_session_count": 4,
                            "ground_truth_source": "human-confirmed",
                            "source_badcase_ids": ["A-4107"],
                            "evidence_confidence": 1.0,
                            "word_timestamps_storage_object_id": "sto-word-ts-1",
                            "diagnostics_storage_object_id": "sto-diagnostics-1",
                        }
                    ]
                }
            },
        )
        session.commit()
        assert result is not None and result["snapshot_count"] == 1
        snapshot = session.get(HotwordMetricSnapshot, "hwmetric-1")
        assert snapshot is not None and snapshot.root_trace_id == analysis_root_trace_id
        assert snapshot.evidence_confidence == 1.0
        assert snapshot.payload["ground_truth_source"] == "human-confirmed"
        assert snapshot.payload["source_badcase_ids"] == ["A-4107"]
        assert snapshot.payload["word_timestamps_storage_object_id"] == "sto-word-ts-1"
        event = (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "hotword_metrics.materialized")
            .one()
        )
        assert event.payload["metric_definition_version"] == "v1"
        assert event.payload["source_storage_object_ids"] == [
            "sto-diagnostics-1",
            "sto-word-ts-1",
        ]

    statistics = client.get(
        "/api/v1/hotword-statistics"
        "?date_from=2026-07-01&date_to=2026-07-01&store_id=BJ-AURORA-001"
        "&provider=asr-provider-a&model_version=asr-v2",
        headers=auth_headers,
    )
    assert statistics.status_code == 200, statistics.text
    data = statistics.json()["data"]
    assert data["summary"]["recall_rate"] == 0.7
    assert data["summary"]["error_rate"] == 0.3
    assert data["summary"]["false_boost_rate"] == 0.05
    assert data["summary"]["impacted_session_count"] == 4
    assert data["items"][0]["standard_term"] == "星越L"


def test_hotword_analysis_derives_discovery_confidence_instead_of_trusting_worker() -> None:
    run_id = "hwanalysis_discovery_confidence"
    root_trace_id = "trace_hotword_discovery_confidence"
    for storage_object_id, role, digest in (
        ("sto-discovery-word-ts", "word_timestamps", "1" * 64),
        ("sto-discovery-diagnostics", "diagnostics", "2" * 64),
    ):
        _register_storage_object(
            storage_object_id,
            status="verified",
            content_sha256=digest,
            source_type="hotword_analysis",
            source_id=run_id,
            trace_id=root_trace_id,
            payload={"role": role},
        )
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="hotword-discovery-confidence",
        trace_id="trace_hotword_discovery_request",
    )
    with SessionLocal() as session:
        run = RunRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="hotword_analysis",
            status="success",
            trace_id=ctx.trace_id,
            payload={"root_trace_id": root_trace_id},
        )
        session.add(run)
        session.flush()
        materialize_hotword_analysis_completion(
            session,
            ctx,
            run,
            {
                "result_ref": {
                    "metric_snapshots": [
                        {
                            "snapshot_id": "hwmetric-discovery-confidence",
                            "bucket_start": "2026-07-01T00:00:00+00:00",
                            "bucket_end": "2026-07-02T00:00:00+00:00",
                            "standard_term": "银河E8",
                            "expected_count": 5,
                            "correct_count": 0,
                            "weighted_error_count": 5,
                            "recognized_hotword_count": 0,
                            "evidence_confidence": 1.0,
                            "ground_truth_source": "discovery",
                            "word_timestamps_storage_object_id": "sto-discovery-word-ts",
                            "diagnostics_storage_object_id": "sto-discovery-diagnostics",
                        }
                    ]
                }
            },
        )
        snapshot = session.get(HotwordMetricSnapshot, "hwmetric-discovery-confidence")
        assert snapshot is not None
        assert snapshot.evidence_confidence == 0.4
        assert snapshot.payload["ground_truth_source"] == "discovery"


def test_hotword_analysis_worker_cannot_forge_human_confirmed_badcase() -> None:
    run_id = "hwanalysis_forged_human_badcase"
    root_trace_id = "trace_hotword_pack_auto_sales"
    for storage_object_id, role, digest in (
        ("sto-forged-word-ts", "word_timestamps", "3" * 64),
        ("sto-forged-diagnostics", "diagnostics", "4" * 64),
        ("sto-forged-badcase-evidence", "badcase_evidence", "5" * 64),
    ):
        _register_storage_object(
            storage_object_id,
            status="verified",
            content_sha256=digest,
            source_type="hotword_analysis",
            source_id=run_id,
            trace_id=root_trace_id,
            payload={"role": role},
        )
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="hotword-forged-human",
        trace_id="trace_hotword_forged_human_request",
    )
    with SessionLocal() as session:
        run = RunRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="hotword_analysis",
            status="success",
            trace_id=ctx.trace_id,
            payload={"root_trace_id": root_trace_id},
        )
        session.add(run)
        session.flush()
        with pytest.raises(ApiError) as exc:
            materialize_hotword_analysis_completion(
                session,
                ctx,
                run,
                {
                    "result_ref": {
                        "metric_snapshots": [
                            {
                                "snapshot_id": "hwmetric-forged-human",
                                "bucket_start": "2026-07-01T00:00:00+00:00",
                                "bucket_end": "2026-07-02T00:00:00+00:00",
                                "standard_term": "银河E8",
                                "expected_count": 1,
                                "correct_count": 0,
                                "ground_truth_source": "discovery",
                                "word_timestamps_storage_object_id": "sto-forged-word-ts",
                                "diagnostics_storage_object_id": "sto-forged-diagnostics",
                            }
                        ],
                        "badcase_candidates": [
                            {
                                "standard_term": "银河E8",
                                "recognized_text": "银河一八",
                                "error_type": "misrecognition",
                                "evidence_storage_object_id": "sto-forged-badcase-evidence",
                                "evidence_level": "human-confirmed",
                                "manual_correction_count": 2,
                                "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
                            }
                        ],
                    }
                },
            )
        assert exc.value.code == "HOTWORD_ANALYSIS_BADCASE_TRUST_FORBIDDEN"
        session.rollback()


def test_hotword_analysis_completion_registers_run_bound_evidence_descriptors(
    client, auth_headers
) -> None:
    requested = client.post(
        "/api/v1/hotword-analysis-runs",
        json={"hotword_pack_version_id": "hwpv-auto-sales-v1-8"},
        headers=_headers(auth_headers, key="analysis-descriptor-request", token="model-token"),
    )
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    word_id = "sto-analysis-descriptor-word"
    diagnostics_id = "sto-analysis-descriptor-diagnostics"
    result_ref = {
        "metric_snapshots": [
            {
                "snapshot_id": "hwmetric-analysis-descriptor",
                "bucket_start": "2026-07-01T00:00:00+00:00",
                "bucket_end": "2026-07-02T00:00:00+00:00",
                "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
                "standard_term": "星越L",
                "expected_count": 3,
                "correct_count": 2,
                "weighted_error_count": 1,
                "recognized_hotword_count": 2,
                "ground_truth_source": "human-confirmed",
                "source_badcase_ids": ["A-4107"],
                "word_timestamps_storage_object_id": word_id,
                "diagnostics_storage_object_id": diagnostics_id,
            }
        ],
        "storage_objects": [
            _run_storage_descriptor(
                run_id,
                word_id,
                role="word_timestamps",
                content_sha256="6" * 64,
            ),
            _run_storage_descriptor(
                run_id,
                diagnostics_id,
                role="diagnostics",
                content_sha256="7" * 64,
            ),
        ],
    }
    completed = _complete_dagster_run(
        client,
        auth_headers,
        run_id,
        result_ref,
        key="analysis-descriptor-completion",
    )
    assert completed.status_code == 200, completed.text
    registered = completed.json()["data"]["registered_storage_objects"]
    assert all("storage_object_id" not in item for item in registered)
    assert len(registered) == 2
    with SessionLocal() as session:
        for storage_object_id, role in (
            (word_id, "word_timestamps"),
            (diagnostics_id, "diagnostics"),
        ):
            storage_object = session.get(StorageObject, storage_object_id)
            assert storage_object is not None
            assert storage_object.source_type == "hotword_analysis"
            assert storage_object.source_id == run_id
            assert storage_object.trace_id == "trace_hotword_pack_auto_sales"
            assert storage_object.payload["role"] == role


def test_hotword_analysis_rejects_missing_snapshot_storage_reference() -> None:
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="hotword-missing-storage",
        trace_id="trace_hotword_missing_storage_request",
        idempotency_key="hotword-missing-storage",
    )
    _register_storage_object("sto-diagnostics-only", content_sha256="6" * 64)
    with SessionLocal() as session:
        run = RunRecord(
            run_id="hwanalysis_missing_storage",
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="hotword_analysis",
            status="success",
            run_key="hotword-analysis:missing-storage",
            partition_key="aurora_auto/sales_qa",
            trace_id=ctx.trace_id,
            payload={"root_trace_id": "trace_hotword_domain_root"},
        )
        session.add(run)
        session.flush()
        with pytest.raises(ApiError) as exc:
            materialize_hotword_analysis_completion(
                session,
                ctx,
                run,
                {
                    "result_ref": {
                        "metric_snapshots": [
                            {
                                "snapshot_id": "hwmetric-missing-storage",
                                "bucket_start": "2026-07-01T00:00:00+00:00",
                                "bucket_end": "2026-07-02T00:00:00+00:00",
                                "standard_term": "银河E8",
                                "expected_count": 1,
                                "correct_count": 1,
                                "word_timestamps_storage_object_id": "sto-does-not-exist",
                            }
                        ]
                    }
                },
            )
        assert exc.value.status_code == 422
        assert exc.value.code == "HOTWORD_METRIC_DIAGNOSTICS_REQUIRED"
        with pytest.raises(ApiError) as word_exc:
            materialize_hotword_analysis_completion(
                session,
                ctx,
                run,
                {
                    "result_ref": {
                        "metric_snapshots": [
                            {
                                "snapshot_id": "hwmetric-missing-word-ts",
                                "bucket_start": "2026-07-01T00:00:00+00:00",
                                "bucket_end": "2026-07-02T00:00:00+00:00",
                                "standard_term": "银河E8",
                                "expected_count": 1,
                                "correct_count": 1,
                                "diagnostics_storage_object_id": "sto-diagnostics-only",
                            }
                        ]
                    }
                },
            )
        assert word_exc.value.status_code == 422
        assert word_exc.value.code == "HOTWORD_METRIC_WORD_TIMESTAMPS_REQUIRED"
        assert session.get(HotwordMetricSnapshot, "hwmetric-missing-storage") is None
