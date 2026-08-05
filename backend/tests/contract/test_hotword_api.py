from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.models import (
    AsrAnnotationCorrection,
    AssetMaterialization,
    AuditLog,
    Badcase,
    HotwordPack,
    HotwordPackVersion,
    OutboxEvent,
    RunRecord,
    StorageObject,
)

pytestmark = pytest.mark.usefixtures("configured_test_legacy_generic_execution")


def _headers(
    auth_headers: dict[str, str],
    *,
    key: str | None = None,
    token: str = "dev-token",
) -> dict[str, str]:
    headers = {**auth_headers, "Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _create_pack(client, auth_headers, *, key: str = "hotword-pack-create"):
    return client.post(
        "/api/v1/hotword-packs",
        json={
            "name": f"测试热词包-{key}",
            "language": "zh-CN",
            "domain": "auto-sales",
        },
        headers=_headers(auth_headers, key=key),
    )


def _register_storage_object(
    storage_object_id: str,
    *,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
    source_type: str = "asr_hotword_evidence",
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
                object_key_sha256=hashlib.sha256(object_key.encode()).hexdigest(),
                source_type=source_type,
                source_id=storage_object_id,
                content_type="application/json",
                size_bytes=128,
                content_sha256="e" * 64,
                etag=f"etag-{storage_object_id}",
                status="registered",
                trace_id=f"trace-{storage_object_id}",
                payload={"status": "registered"},
            )
        )
        session.commit()


def test_pack_version_item_api_is_scoped_idempotent_and_audited(client, auth_headers) -> None:
    forbidden = client.post(
        "/api/v1/hotword-packs",
        json={"name": "越权词包", "language": "zh-CN", "domain": "test"},
        headers=_headers(auth_headers, key="forbidden-pack", token="annotator-token"),
    )
    assert forbidden.status_code == 403

    created = _create_pack(client, auth_headers)
    assert created.status_code == 201, created.text
    replayed = _create_pack(client, auth_headers)
    assert replayed.status_code == 201
    assert replayed.json() == created.json()
    pack = created.json()["data"]
    assert pack["root_trace_id"]

    conflict = client.post(
        "/api/v1/hotword-packs",
        json={"name": "不同请求", "language": "zh-CN", "domain": "auto-sales"},
        headers=_headers(auth_headers, key="hotword-pack-create"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    version_response = client.post(
        f"/api/v1/hotword-packs/{pack['pack_id']}/versions",
        json={"version": "v1.9", "baseline_version_id": None},
        headers=_headers(auth_headers, key="hotword-version-create"),
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()["data"]
    assert version["status"] == "draft"
    assert version["root_trace_id"] == pack["root_trace_id"]

    forged_build_output = client.patch(
        f"/api/v1/hotword-pack-versions/{version['version_id']}",
        json={
            "expected_resource_version": 1,
            "manifest_storage_object_id": "sto-client-forged-manifest",
        },
        headers=_headers(auth_headers, key="hotword-version-forged-build-output"),
    )
    assert forged_build_output.status_code == 422

    item_response = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
        json={
            "canonical_term": "星越L",
            "aliases": ["星越 L"],
            "category": "vehicle-model",
            "weight": 80,
        },
        headers=_headers(auth_headers, key="hotword-item-create"),
    )
    assert item_response.status_code == 201, item_response.text
    assert item_response.json()["data"]["normalized_term"] == "星越l"

    duplicate = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
        json={
            "canonical_term": "（ＳＴＡＲ）星越L",
            "aliases": [],
            "category": "vehicle-model",
            "weight": 50,
        },
        headers=_headers(auth_headers, key="hotword-item-duplicate"),
    )
    # This term is intentionally distinct; the normalized duplicate is tested below.
    assert duplicate.status_code == 201, duplicate.text
    distinct_item = duplicate.json()["data"]
    normalized_duplicate = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
        json={
            "canonical_term": "（星越L）",
            "aliases": [],
            "category": "vehicle-model",
            "weight": 50,
        },
        headers=_headers(auth_headers, key="hotword-item-normalized-duplicate"),
    )
    assert normalized_duplicate.status_code == 409
    assert normalized_duplicate.json()["error"]["code"] == "HOTWORD_ITEM_DUPLICATE"

    deleted = client.delete(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items/"
        f"{distinct_item['item_id']}?expected_resource_version=1",
        headers=_headers(auth_headers, key="hotword-item-delete"),
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True

    sensitive = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
        json={
            "canonical_term": "13800138000",
            "aliases": [],
            "category": "customer",
            "weight": 50,
        },
        headers=_headers(auth_headers, key="hotword-item-sensitive"),
    )
    assert sensitive.status_code == 422
    assert sensitive.json()["error"]["code"] == "HOTWORD_SENSITIVE_TERM_FORBIDDEN"

    disguised_name = client.post(
        f"/api/v1/hotword-pack-versions/{version['version_id']}/items",
        json={
            "canonical_term": "张先生",
            "aliases": [],
            "category": "vehicle-model",
            "weight": 50,
        },
        headers=_headers(auth_headers, key="hotword-item-disguised-name"),
    )
    assert disguised_name.status_code == 422
    assert disguised_name.json()["error"]["code"] == "HOTWORD_SENSITIVE_TERM_FORBIDDEN"

    listed = client.get("/api/v1/hotword-packs", headers=auth_headers)
    assert listed.status_code == 200
    assert pack["pack_id"] in [item["pack_id"] for item in listed.json()["data"]["items"]]

    with SessionLocal() as session:
        assert session.get(HotwordPack, pack["pack_id"]) is not None
        assert session.get(HotwordPackVersion, version["version_id"]) is not None
        assert session.query(AuditLog).filter(AuditLog.object_id == pack["pack_id"]).count() == 1
        event_types = {event.event_type for event in session.query(OutboxEvent).all()}
        assert "hotword_pack.created" in event_types
        assert "hotword_pack_version.created" in event_types


def test_badcase_hotword_projection_decision_and_statistics_filters(client, auth_headers) -> None:
    badcase_id = "A-TEST-4107"
    evidence_storage_object_id = "sto-badcase-a-test-4107"
    _register_storage_object(evidence_storage_object_id)
    missing_governance_refs = client.post(
        "/api/v1/badcases",
        json={
            "standard_term": "星越L",
            "recognized_text": "星月L",
            "error_type": "misrecognition",
            "evidence_level": "discovery",
        },
        headers=_headers(auth_headers, key="badcase-missing-governance", token="annotator-token"),
    )
    assert missing_governance_refs.status_code == 422

    forged_trust = client.post(
        "/api/v1/badcases",
        json={
            "standard_term": "星越L",
            "recognized_text": "星月L",
            "error_type": "misrecognition",
            "evidence_storage_object_id": evidence_storage_object_id,
            "evidence_level": "human-confirmed",
            "manual_correction_count": 2,
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(auth_headers, key="badcase-forged-trust", token="annotator-token"),
    )
    assert forged_trust.status_code == 422
    assert forged_trust.json()["error"]["code"] == "HOTWORD_BADCASE_EVIDENCE_UNTRUSTED"

    created = client.post(
        "/api/v1/badcases",
        json={
            "badcase_id": badcase_id,
            "capability": "asr-hotword",
            "standard_term": "星越L",
            "recognized_text": "星月L",
            "error_type": "misrecognition",
            "evidence_storage_object_id": evidence_storage_object_id,
            "evidence_level": "discovery",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
            "expected_count": 2,
            "correct_count": 0,
            "weighted_error_count": 2,
            "manual_correction_count": 0,
            "business_weight": 1.0,
            "downstream_impact": {"entity_f1_delta": -0.08},
        },
        headers=_headers(auth_headers, key="badcase-create", token="annotator-token"),
    )
    assert created.status_code == 201, created.text
    body = created.json()["data"]
    assert body["candidate_state"] == "suspected"
    assert body["priority_score"] > 0
    assert body["status"] == "pending-attribution"
    assert body["evidence_storage_object_id"] == evidence_storage_object_id
    assert body["evidence_ref"] == f"storage-object:{evidence_storage_object_id}"

    reused_evidence = client.post(
        "/api/v1/badcases",
        json={
            "standard_term": "星越L",
            "recognized_text": "星月L",
            "error_type": "misrecognition",
            "evidence_storage_object_id": evidence_storage_object_id,
            "evidence_level": "discovery",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(auth_headers, key="badcase-reused-evidence", token="annotator-token"),
    )
    assert reused_evidence.status_code == 409
    assert reused_evidence.json()["error"]["code"] == "HOTWORD_BADCASE_EVIDENCE_ALREADY_BOUND"

    bypassed = client.patch(
        f"/api/v1/badcases/{badcase_id}",
        json={"expected_resource_version": 1, "status": "pending-backflow"},
        headers=_headers(auth_headers, key="badcase-bypass", token="annotator-token"),
    )
    assert bypassed.status_code == 422

    patched = client.patch(
        f"/api/v1/badcases/{badcase_id}",
        json={
            "expected_resource_version": 1,
            "root_cause": "alias_gap",
            "fix_suggestion": "增加显式别名并提高权重",
        },
        headers=_headers(auth_headers, key="badcase-patch", token="annotator-token"),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["resource_version"] == 2

    stale = client.patch(
        f"/api/v1/badcases/{badcase_id}",
        json={"expected_resource_version": 1, "root_cause": "weight_issue"},
        headers=_headers(auth_headers, key="badcase-stale", token="annotator-token"),
    )
    assert stale.status_code == 409

    decided = client.post(
        f"/api/v1/badcases/{badcase_id}/decisions",
        json={
            "decision": "confirmed",
            "reason": "已核对音频证据窗口",
            "expected_resource_version": 2,
        },
        headers=_headers(auth_headers, key="badcase-decision", token="annotator-token"),
    )
    assert decided.status_code == 201, decided.text
    assert decided.json()["data"]["status"] == "pending-backflow"
    assert decided.json()["data"]["candidate_state"] == "confirmed"
    assert decided.json()["data"]["evidence_level"] == "human-confirmed"
    assert decided.json()["data"]["priority_score"] > body["priority_score"]

    source_pack = _create_pack(client, auth_headers, key="badcase-source-pack").json()["data"]
    source_version = client.post(
        f"/api/v1/hotword-packs/{source_pack['pack_id']}/versions",
        json={"version": "badcase-source-v1"},
        headers=_headers(auth_headers, key="badcase-source-version"),
    ).json()["data"]
    sourced_item = client.post(
        f"/api/v1/hotword-pack-versions/{source_version['version_id']}/items",
        json={
            "canonical_term": "星越L",
            "aliases": [],
            "category": "vehicle-model",
            "weight": 80,
            "source_type": "badcase",
            "source_badcase_id": badcase_id,
        },
        headers=_headers(auth_headers, key="badcase-source-item"),
    )
    assert sourced_item.status_code == 201, sourced_item.text

    listed = client.get(
        "/api/v1/badcases?capability=asr-hotword&error_type=misrecognition",
        headers=auth_headers,
    )
    assert listed.status_code == 200
    assert badcase_id in [item["badcase_id"] for item in listed.json()["data"]["items"]]

    with SessionLocal() as session:
        case = session.get(Badcase, badcase_id)
        source_version_record = session.get(HotwordPackVersion, "hwpv-auto-sales-v1-8")
        assert case is not None and source_version_record is not None
        assert case.evidence_storage_object_id == evidence_storage_object_id
        assert case.root_trace_id == source_version_record.root_trace_id == body["root_trace_id"]


def test_hotword_statistics_zero_denominators_are_unknown(client, auth_headers) -> None:
    response = client.get(
        "/api/v1/hotword-statistics?provider=provider-with-no-trusted-samples",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    summary = response.json()["data"]["summary"]
    assert summary["trusted_expected_count"] == 0
    assert summary["recognized_hotword_count"] == 0
    assert summary["coverage_rate"] is None
    assert summary["recall_rate"] is None
    assert summary["error_rate"] is None
    assert summary["false_boost_rate"] is None


def test_asr_annotation_correction_is_counted_as_discovery_without_polluting_trusted_kpis(
    client, auth_headers
) -> None:
    baseline = client.get(
        "/api/v1/hotword-statistics?hotword_pack_version_id=hwpv-auto-sales-v1-8",
        headers=auth_headers,
    )
    assert baseline.status_code == 200, baseline.text
    baseline_summary = baseline.json()["data"]["summary"]

    ordinary = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            "annotation_id": "ordinary-label-does-not-count",
            "track": "qa",
            "label": "金额冲突",
            "left": 43,
            "width": 8,
            "start_time": "12:27:12",
            "end_time": "12:28:48",
        },
        headers=_headers(auth_headers, key="ordinary-label-does-not-count"),
    )
    assert ordinary.status_code == 201, ordinary.text

    payload = {
        "annotation_id": "asr-correction-a-4107-1",
        "annotation_kind": "asr-transcript-correction",
        "confirmation": "record_correction",
        "track": "asr",
        "audio_session_id": "S20250526-000128",
        "recognized_text": "星月L",
        "corrected_text": "星越L",
        "error_type": "misrecognition",
        "evidence_window": "12:27:12 - 12:27:18",
        "evidence_storage_object_id": "storage_badcase_a_4107_evidence",
        "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        "source_badcase_id": "A-4107",
    }
    correction = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json=payload,
        headers=_headers(auth_headers, key="asr-correction-a-4107-1", token="annotator-token"),
    )
    assert correction.status_code == 201, correction.text
    correction_data = correction.json()["data"]
    assert correction_data["annotation_kind"] == "asr-transcript-correction"
    assert correction_data["status"] == "submitted"
    assert correction_data["stat_eligibility"] == "discovery-only"
    assert correction_data["eligible_for_release_gate"] is False
    assert correction_data["source_badcase_id"] == "A-4107"
    assert correction_data["root_trace_id"] == "trace_hotword_pack_auto_sales"
    assert correction_data["current_trace_id"] == correction.json()["meta"]["trace_id"]

    replay = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json=payload,
        headers=_headers(auth_headers, key="asr-correction-a-4107-1", token="annotator-token"),
    )
    assert replay.status_code == 201
    assert replay.json() == correction.json()

    semantic_duplicate = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={**payload, "annotation_id": "asr-correction-a-4107-duplicate"},
        headers=_headers(
            auth_headers,
            key="asr-correction-a-4107-duplicate",
            token="annotator-token",
        ),
    )
    assert semantic_duplicate.status_code == 200, semantic_duplicate.text
    assert semantic_duplicate.json()["data"]["deduplicated"] is True
    assert semantic_duplicate.json()["data"]["correction_id"] == correction_data["correction_id"]

    immutable = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={**payload, "evidence_window": "12:27:13 - 12:27:19"},
        headers=_headers(
            auth_headers,
            key="asr-correction-a-4107-immutable",
            token="annotator-token",
        ),
    )
    assert immutable.status_code == 409
    assert immutable.json()["error"]["code"] == "ASR_CORRECTION_IMMUTABLE"

    statistics = client.get(
        "/api/v1/hotword-statistics?hotword_pack_version_id=hwpv-auto-sales-v1-8",
        headers=auth_headers,
    )
    assert statistics.status_code == 200, statistics.text
    data = statistics.json()["data"]
    assert data["discovery_summary"] == {
        "annotation_correction_count": 1,
        "unique_terms": 1,
        "impacted_session_count": 1,
        "threshold_met_term_count": 0,
        "evidence_level": "discovery",
        "eligible_for_release_gate": False,
    }
    discovery_term = next(
        item for item in data["discovery_items"] if item["standard_term"] == "星越L"
    )
    assert discovery_term["annotation_correction_count"] == 1
    assert discovery_term["source_counts"] == {
        "listening_annotation": 1,
        "metric_snapshot": 0,
    }
    assert discovery_term["threshold_met"] is False
    assert discovery_term["suspected"] is True
    assert discovery_term["badcase_ids"] == ["A-4107"]
    assert discovery_term["correction_ids"] == [correction_data["correction_id"]]
    # 标注修正是发现信号，不能直接改变可信 KPI 或发布门禁分母。
    for field in (
        "trusted_expected_count",
        "correct_hit_count",
        "weighted_error_count",
        "recognized_hotword_count",
        "false_insertion_count",
        "coverage_rate",
        "recall_rate",
        "error_rate",
        "false_boost_rate",
    ):
        assert data["summary"][field] == baseline_summary[field]

    with SessionLocal() as session:
        corrections = session.query(AsrAnnotationCorrection).all()
        assert len(corrections) == 1
        assert corrections[0].source_badcase_id == "A-4107"
        seeded_badcase = session.get(Badcase, "A-4107")
        assert seeded_badcase is not None
        assert seeded_badcase.manual_correction_count == 2
        assert (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "asr_annotation.correction-recorded")
            .count()
            == 1
        )


def test_asr_annotation_correction_creates_discovery_badcase_and_blocks_unsafe_inputs(
    client, auth_headers
) -> None:
    evidence_id = "sto-asr-correction-new-term"
    _register_storage_object(evidence_id)
    payload = {
        "annotation_id": "asr-correction-new-term",
        "annotation_kind": "asr-transcript-correction",
        "confirmation": "record_correction",
        "track": "asr",
        "audio_session_id": "S20250526-000128",
        "recognized_text": "遥程守护模式",
        "corrected_text": "远程守护模式",
        "error_type": "misrecognition",
        "evidence_window": "12:30:00 - 12:30:05",
        "evidence_storage_object_id": evidence_id,
        "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
    }
    created = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json=payload,
        headers=_headers(auth_headers, key="asr-correction-new-term", token="annotator-token"),
    )
    assert created.status_code == 201, created.text
    created_data = created.json()["data"]
    assert created_data["source_badcase_id"].startswith("A-ANN-")

    with SessionLocal() as session:
        badcase = session.get(Badcase, created_data["source_badcase_id"])
        assert badcase is not None
        assert badcase.standard_term == "远程守护模式"
        assert badcase.evidence_level == "discovery"
        assert badcase.manual_correction_count == 1
        assert badcase.candidate_state == "suspected"
        assert badcase.status == "pending-attribution"

    statistics = client.get(
        "/api/v1/hotword-statistics?hotword_pack_version_id=hwpv-auto-sales-v1-8",
        headers=auth_headers,
    )
    discovery_term = next(
        item
        for item in statistics.json()["data"]["discovery_items"]
        if item["standard_term"] == "远程守护模式"
    )
    assert discovery_term["annotation_correction_count"] == 1
    assert discovery_term["eligible_for_release_gate"] is False

    no_change_evidence = "sto-asr-correction-no-change"
    _register_storage_object(no_change_evidence)
    no_change = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            **payload,
            "annotation_id": "asr-correction-no-change",
            "recognized_text": "（星越L）",
            "corrected_text": "星越L",
            "evidence_storage_object_id": no_change_evidence,
        },
        headers=_headers(auth_headers, key="asr-correction-no-change", token="annotator-token"),
    )
    assert no_change.status_code == 422
    assert no_change.json()["error"]["code"] == "ASR_CORRECTION_NO_CHANGE"

    sensitive_evidence = "sto-asr-correction-sensitive"
    _register_storage_object(sensitive_evidence)
    sensitive = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            **payload,
            "annotation_id": "asr-correction-sensitive",
            "recognized_text": "1380013800零",
            "corrected_text": "13800138000",
            "evidence_storage_object_id": sensitive_evidence,
        },
        headers=_headers(auth_headers, key="asr-correction-sensitive", token="annotator-token"),
    )
    assert sensitive.status_code == 422
    assert sensitive.json()["error"]["code"] == "HOTWORD_SENSITIVE_TERM_FORBIDDEN"

    forged_trust = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={**payload, "annotation_id": "forged-trust", "evidence_level": "human-confirmed"},
        headers=_headers(auth_headers, key="forged-trust", token="annotator-token"),
    )
    assert forged_trust.status_code == 422


def test_asr_annotation_correction_threshold_scope_and_append_only_guards(
    client, auth_headers
) -> None:
    baseline = client.get(
        "/api/v1/hotword-statistics?hotword_pack_version_id=hwpv-auto-sales-v1-8",
        headers=auth_headers,
    ).json()["data"]["summary"]
    correction_ids: list[str] = []
    for index in (1, 2):
        evidence_id = f"sto-asr-correction-threshold-{index}"
        _register_storage_object(evidence_id)
        response = client.post(
            "/api/v1/audio-sessions/S20250526-000128/annotations",
            json={
                "annotation_id": f"asr-correction-threshold-{index}",
                "annotation_kind": "asr-transcript-correction",
                "confirmation": "record_correction",
                "track": "asr",
                "audio_session_id": "S20250526-000128",
                "recognized_text": "智架领航",
                "corrected_text": "智驾领航",
                "error_type": "misrecognition",
                "evidence_window": f"12:31:0{index} - 12:31:0{index + 1}",
                "evidence_storage_object_id": evidence_id,
                "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
            },
            headers=_headers(
                auth_headers,
                key=f"asr-correction-threshold-{index}",
                token="annotator-b-token",
            ),
        )
        assert response.status_code == 201, response.text
        correction_ids.append(response.json()["data"]["correction_id"])

    statistics = client.get(
        "/api/v1/hotword-statistics?hotword_pack_version_id=hwpv-auto-sales-v1-8",
        headers=auth_headers,
    ).json()["data"]
    item = next(
        item for item in statistics["discovery_items"] if item["standard_term"] == "智驾领航"
    )
    assert item["annotation_correction_count"] == 2
    assert item["threshold_met"] is True
    assert item["candidate_state"] == "suspected"
    assert item["eligible_for_release_gate"] is False
    assert statistics["discovery_summary"]["threshold_met_term_count"] == 1
    for field in (
        "trusted_expected_count",
        "correct_hit_count",
        "weighted_error_count",
        "recognized_hotword_count",
        "false_insertion_count",
        "coverage_rate",
        "recall_rate",
        "error_rate",
        "false_boost_rate",
    ):
        assert statistics["summary"][field] == baseline[field]

    cross_scope_evidence = "sto-asr-correction-cross-project"
    _register_storage_object(cross_scope_evidence, project_id="other_project")
    cross_scope = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            "annotation_id": "asr-correction-cross-project",
            "annotation_kind": "asr-transcript-correction",
            "confirmation": "record_correction",
            "track": "asr",
            "audio_session_id": "S20250526-000128",
            "recognized_text": "领航架驶",
            "corrected_text": "领航驾驶",
            "error_type": "misrecognition",
            "evidence_window": "12:32:01 - 12:32:02",
            "evidence_storage_object_id": cross_scope_evidence,
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(
            auth_headers,
            key="asr-correction-cross-project",
            token="annotator-token",
        ),
    )
    assert cross_scope.status_code == 403
    assert cross_scope.json()["error"]["code"] == "STORAGE_OBJECT_SCOPE_FORBIDDEN"

    with SessionLocal() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                update(AsrAnnotationCorrection)
                .where(AsrAnnotationCorrection.correction_id == correction_ids[0])
                .values(status="tampered")
            )
            session.commit()
        session.rollback()


def test_rejected_badcase_no_longer_upgrades_top_hotword_statistics(client, auth_headers) -> None:
    listed = client.get(
        "/api/v1/badcases?capability=asr-hotword&limit=20",
        headers=auth_headers,
    )
    seeded = next(item for item in listed.json()["data"]["items"] if item["badcase_id"] == "A-4107")
    rejected = client.post(
        "/api/v1/badcases/A-4107/decisions",
        json={
            "decision": "rejected",
            "reason": "仲裁确认该片段不构成热词错识",
            "expected_resource_version": seeded["resource_version"],
        },
        headers=_headers(auth_headers, key="badcase-reject-seeded", token="annotator-token"),
    )
    assert rejected.status_code == 201, rejected.text
    rejected_data = rejected.json()["data"]
    assert rejected_data["status"] == "rejected"
    assert rejected_data["candidate_state"] == "suspected"
    assert rejected_data["evidence_level"] == "discovery"

    statistics = client.get(
        "/api/v1/hotword-statistics?hotword_pack_version_id=hwpv-auto-sales-v1-8",
        headers=auth_headers,
    )
    assert statistics.status_code == 200, statistics.text
    term = next(
        item for item in statistics.json()["data"]["items"] if item["standard_term"] == "星越L"
    )
    assert "A-4107" not in term["badcase_ids"]
    assert term["human_correction_count"] == 0
    assert term["evidence_level"] == "discovery"
    assert term["suspected"] is True


def test_badcase_rejects_cross_scope_evidence_storage_object(client, auth_headers) -> None:
    evidence_storage_object_id = "sto-badcase-cross-project"
    _register_storage_object(evidence_storage_object_id, project_id="other_project")
    created = client.post(
        "/api/v1/badcases",
        json={
            "standard_term": "银河E8",
            "recognized_text": "银河一八",
            "error_type": "misrecognition",
            "evidence_storage_object_id": evidence_storage_object_id,
            "evidence_level": "discovery",
            "manual_correction_count": 0,
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(auth_headers, key="badcase-cross-scope", token="annotator-token"),
    )
    assert created.status_code == 403
    assert created.json()["error"]["code"] == "STORAGE_OBJECT_SCOPE_FORBIDDEN"

    invalid_source_id = "sto-badcase-manifest-source"
    _register_storage_object(invalid_source_id, source_type="hotword_manifest")
    invalid_source = client.post(
        "/api/v1/badcases",
        json={
            "standard_term": "银河E8",
            "recognized_text": "银河一八",
            "error_type": "misrecognition",
            "evidence_storage_object_id": invalid_source_id,
            "evidence_level": "discovery",
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers=_headers(auth_headers, key="badcase-invalid-source", token="annotator-token"),
    )
    assert invalid_source.status_code == 422
    assert invalid_source.json()["error"]["code"] == "HOTWORD_BADCASE_EVIDENCE_SOURCE_INVALID"


def test_audio_request_requires_published_task_binding_and_allows_candidate_shadow(
    client, auth_headers
) -> None:
    pack = _create_pack(client, auth_headers, key="audio-pack").json()["data"]
    version = client.post(
        f"/api/v1/hotword-packs/{pack['pack_id']}/versions",
        json={"version": "candidate"},
        headers=_headers(auth_headers, key="audio-version"),
    ).json()["data"]
    with SessionLocal() as session:
        record = session.get(HotwordPackVersion, version["version_id"])
        assert record is not None
        record.status = "ready_for_eval"
        record.compiled_provider = "auris-audio-stack"
        record.provider_artifact_ref = "storage://compiled/test-hotword.json"
        record.manifest_storage_object_id = "sto-test-hotword-manifest"
        record.content_sha256 = "a" * 64
        record.payload = {**record.payload, "artifact_sha256": "b" * 64}
        session.commit()

    payload = {
        "capabilities": ["asr"],
        "execution_mode": "production",
        "language": "zh-CN",
        "hotword_pack_version_id": version["version_id"],
        "return_word_timestamps": True,
        "provider": "auris-audio-stack",
    }
    production = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json=payload,
        headers=_headers(auth_headers, key="audio-prod-hotword"),
    )
    assert production.status_code == 422
    assert production.json()["error"]["code"] == ("AUDIO_PRODUCTION_TASK_VERSION_REQUIRED")

    shadow = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={**payload, "execution_mode": "shadow"},
        headers=_headers(auth_headers, key="audio-shadow-hotword"),
    )
    assert shadow.status_code == 202, shadow.text
    with SessionLocal() as session:
        run = session.get(RunRecord, shadow.json()["data"]["run_id"])
        assert run is not None
        assert run.payload["hotword_pack_version_id"] == version["version_id"]
        assert run.payload["execution_mode"] == "shadow"


def test_hotword_controlled_backfill_requires_frozen_scoped_lineage_and_is_idempotent(
    client,
    auth_headers,
) -> None:
    path = "/api/v1/data-assets/auris/model/asr_transcripts/backfills"
    impact_scope = {
        "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        "eval_run_id": "evalrun_hotword_v18_seed",
        "task_version_id": "task_version_v3_2_1",
        "materialization_id": "mat_asr_20250526_122300",
        "overwrite_history": False,
    }
    payload = {
        "reason": "对当前项目受控回填 ASR 新转写资产",
        "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/hotword-v1.8",
        "impact_scope": impact_scope,
    }
    headers = _headers(auth_headers, key="hotword-backfill-contract")
    created = client.post(path, json=payload, headers=headers)
    assert created.status_code == 202, created.text
    assert created.json()["data"]["root_trace_id"] == "trace_hotword_pack_auto_sales"
    assert created.json()["data"]["impact_scope"]["source_asset_key"] == (
        "auris/model/asr_transcripts"
    )
    replayed = client.post(path, json=payload, headers=headers)
    assert replayed.status_code == 202
    assert replayed.json() == created.json()
    conflict = client.post(
        path,
        json={**payload, "reason": "不同请求不得复用同一幂等键"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    forbidden = client.post(
        path,
        json=payload,
        headers=_headers(
            auth_headers,
            key="hotword-backfill-rbac",
            token="annotator-token",
        ),
    )
    assert forbidden.status_code == 403

    overwrite = client.post(
        path,
        json={
            **payload,
            "impact_scope": {**impact_scope, "overwrite_history": True},
        },
        headers=_headers(auth_headers, key="hotword-backfill-overwrite"),
    )
    assert overwrite.status_code == 409
    assert overwrite.json()["error"]["code"] == "HOTWORD_BACKFILL_HISTORY_IMMUTABLE"

    missing_binding = client.post(
        path,
        json={
            **payload,
            "impact_scope": {"hotword_pack_version_id": "hwpv-auto-sales-v1-8"},
        },
        headers=_headers(auth_headers, key="hotword-backfill-missing-binding"),
    )
    assert missing_binding.status_code == 422
    assert missing_binding.json()["error"]["code"] == "HOTWORD_BACKFILL_BINDING_REQUIRED"

    wrong_task = client.post(
        path,
        json={
            **payload,
            "impact_scope": {**impact_scope, "task_version_id": "task_version_other"},
        },
        headers=_headers(auth_headers, key="hotword-backfill-task-mismatch"),
    )
    assert wrong_task.status_code == 409
    assert wrong_task.json()["error"]["code"] == "HOTWORD_BACKFILL_TASK_MISMATCH"

    wrong_asset = client.post(
        path,
        json={
            **payload,
            "impact_scope": {
                **impact_scope,
                "materialization_id": "mat_label_20250526_122300",
            },
        },
        headers=_headers(auth_headers, key="hotword-backfill-asset-mismatch"),
    )
    assert wrong_asset.status_code == 409
    assert wrong_asset.json()["error"]["code"] == (
        "HOTWORD_BACKFILL_MATERIALIZATION_ASSET_MISMATCH"
    )

    with SessionLocal() as session:
        session.add(
            AssetMaterialization(
                materialization_id="mat_asr_foreign_scope",
                tenant_id="other_tenant",
                project_id="other_project",
                status="success",
                trace_id="trace_foreign_materialization",
                payload={"asset_key": "auris/model/asr_transcripts"},
            )
        )
        session.commit()
    cross_scope = client.post(
        path,
        json={
            **payload,
            "impact_scope": {
                **impact_scope,
                "materialization_id": "mat_asr_foreign_scope",
            },
        },
        headers=_headers(auth_headers, key="hotword-backfill-cross-scope"),
    )
    assert cross_scope.status_code == 403
    assert cross_scope.json()["error"]["code"] == (
        "HOTWORD_BACKFILL_MATERIALIZATION_SCOPE_FORBIDDEN"
    )
