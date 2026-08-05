from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.models import (
    AudioRecording,
    EvidencePack,
    HumanReviewTask,
    JsonResource,
    RunRecord,
    StorageObject,
)
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

EVIDENCE_REVIEW_OVERLAY_FIELDS = frozenset(
    {
        "review_state",
        "review_task_id",
        "review_decision_id",
        "manual_decision",
        "decision_note",
        "decided_by",
        "decided_at",
        "action_trace_id",
    }
)
EVIDENCE_REVIEW_STATES = frozenset({"accepted", "modified", "rejected", "escalated"})
REVIEW_TARGET_COLLECTION_BY_TYPE = {
    "conversation_boundary": "conversation_boundaries",
    "event_link": "event_links",
    "label_candidate": "label_candidates",
}
REVIEW_TARGET_ASSET_BY_TYPE = {
    "conversation_boundary": "auris/audio/conversation_boundaries",
    "event_link": "auris/business/event_links",
    "label_candidate": "auris/label/candidates",
}


def get_scoped_evidence_pack(
    session: Session,
    ctx: RequestContext,
    evidence_pack_id: str,
    *,
    for_update: bool = False,
) -> EvidencePack:
    statement = select(EvidencePack).where(
        EvidencePack.evidence_pack_id == evidence_pack_id,
        EvidencePack.tenant_id == ctx.tenant_id,
        EvidencePack.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update()
    evidence = session.scalar(statement)
    if evidence is None:
        raise ApiError(
            "AUDIO_EVIDENCE_STRONG_BINDING_REQUIRED",
            "证据包缺少当前租户项目内的强类型不可变记录",
            409,
            details=[{"evidence_pack_id": evidence_pack_id}],
        )
    return evidence


def _valid_sha256(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    if len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized):
        return normalized
    return None


def _safe_overlay_text(value: object, *, maximum: int = 512) -> str | None:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 0x20 for character in normalized)
    ):
        return None
    return normalized


def assemble_scoped_evidence_pack(
    session: Session,
    ctx: RequestContext,
    evidence_pack_id: str,
) -> dict[str, Any]:
    evidence = get_scoped_evidence_pack(session, ctx, evidence_pack_id)
    projection = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == "evidence_packs",
            JsonResource.resource_key == evidence_pack_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
    )
    projection_data = (
        projection.data if projection is not None and isinstance(projection.data, dict) else {}
    )
    evidence_payload = evidence.payload if isinstance(evidence.payload, dict) else {}
    raw_asr_result = evidence_payload.get("asr_result")
    raw_asr_result = raw_asr_result if isinstance(raw_asr_result, dict) else {}
    segments_sha256 = _valid_sha256(raw_asr_result.get("segments_sha256"))
    data: dict[str, Any] = {
        "id": evidence.evidence_pack_id,
        "evidence_pack_id": evidence.evidence_pack_id,
        "schema_version": "audio-evidence-pack/1",
        "audio_session_id": evidence.audio_session_id,
        "recording_id": evidence.recording_id,
        "storage_object": {
            "storage_object_id": evidence.storage_object_id,
            "version_id": evidence.storage_object_version,
            "content_sha256": evidence.audio_sha256,
        },
        "asr_result": {
            "asr_result_id": evidence.asr_result_id,
            "version": evidence.asr_result_version,
            **({"segments_sha256": segments_sha256} if segments_sha256 else {}),
        },
        "time_window": {
            "start_ms": evidence.window_start_ms,
            "end_ms": evidence.window_end_ms,
        },
        "source_run_id": evidence.source_run_id,
        "evidence_sha256": evidence.evidence_sha256,
        "status": evidence.status,
        "resource_version": evidence.resource_version,
        "root_trace_id": evidence.root_trace_id,
        "current_trace_id": evidence.current_trace_id,
        "trace_id": evidence.root_trace_id,
    }
    output_sink_refs = _frozen_output_sink_refs(evidence_payload)
    if output_sink_refs:
        data["output_sink_refs"] = output_sink_refs
    legacy_metadata = evidence_payload.get("legacy_metadata")
    if isinstance(legacy_metadata, dict):
        title = legacy_metadata.get("title")
        if isinstance(title, str) and title.strip():
            data["title"] = title.strip()
    for field in EVIDENCE_REVIEW_OVERLAY_FIELDS:
        value = projection_data.get(field)
        if field in {"review_state", "manual_decision"}:
            if isinstance(value, str) and value in EVIDENCE_REVIEW_STATES:
                data[field] = value
        elif field == "action_trace_id":
            safe_value = _safe_overlay_text(value, maximum=128)
            if safe_value:
                data[field] = safe_value
        elif field in {"review_task_id", "review_decision_id"}:
            safe_value = _safe_overlay_text(value, maximum=128)
            if safe_value:
                data[field] = safe_value
        elif field in {"decision_note", "decided_by", "decided_at"}:
            safe_value = _safe_overlay_text(value)
            if safe_value:
                data[field] = safe_value
    raw_overrides = projection_data.get("review_overrides")
    if isinstance(raw_overrides, dict):
        overrides: dict[str, Any] = {}
        disposition = raw_overrides.get("recording_disposition")
        if disposition in {"main", "crosstalk", "duplicate"}:
            overrides["recording_disposition"] = disposition
        low_confidence = raw_overrides.get("low_confidence")
        if isinstance(low_confidence, bool):
            overrides["low_confidence"] = low_confidence
        if overrides:
            data["review_overrides"] = overrides
    data["label_candidates"] = [
        dict(candidate.data)
        for candidate in session.scalars(
            select(JsonResource)
            .where(
                JsonResource.collection == "label_candidates",
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.data["evidence_pack_id"].as_string() == evidence_pack_id,
            )
            .order_by(JsonResource.resource_key)
        )
        if isinstance(candidate.data, dict)
    ]
    return data


def _sha256_document(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _required_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise ApiError(
            "AUDIO_EVIDENCE_BINDING_INVALID",
            "音频证据缺少精确对象或结果版本绑定",
            409,
            details=[{"field": field}],
        )
    return normalized


def _successful_asr(result_ref: dict[str, Any]) -> bool:
    statuses = result_ref.get("capability_statuses")
    asr_status = statuses.get("asr") if isinstance(statuses, dict) else None
    return isinstance(asr_status, dict) and asr_status.get("status") == "success"


def _asr_window(result_ref: dict[str, Any]) -> tuple[int, int, str]:
    raw_segments = result_ref.get("asr_segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ApiError(
            "AUDIO_EVIDENCE_ASR_REQUIRED",
            "成功的 ASR 结果缺少可复核时间窗",
            409,
        )
    starts: list[int] = []
    ends: list[int] = []
    for segment in raw_segments:
        if not isinstance(segment, dict):
            raise ApiError(
                "AUDIO_EVIDENCE_ASR_INVALID",
                "ASR 证据片段结构不合法",
                409,
            )
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise ApiError(
                "AUDIO_EVIDENCE_ASR_INVALID",
                "ASR 证据片段时间窗不合法",
                409,
            )
        starts.append(start_ms)
        ends.append(end_ms)
    return min(starts), max(ends), _sha256_document(raw_segments)


def _exact_input_object(
    session: Session,
    record: RunRecord,
) -> tuple[dict[str, Any], StorageObject]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    raw_input = payload.get("input_object")
    if not isinstance(raw_input, dict):
        raise ApiError(
            "AUDIO_EVIDENCE_OBJECT_VERSION_REQUIRED",
            "生成证据包前必须绑定已验证录音对象的精确版本",
            409,
        )
    storage_object_id = _required_text(
        raw_input.get("storage_object_id"),
        field="input_object.storage_object_id",
        maximum=128,
    )
    version_id = _required_text(
        raw_input.get("version_id"),
        field="input_object.version_id",
        maximum=512,
    )
    content_sha256 = _required_text(
        raw_input.get("content_sha256"),
        field="input_object.content_sha256",
        maximum=64,
    ).casefold()
    if len(content_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_sha256
    ):
        raise ApiError(
            "AUDIO_EVIDENCE_OBJECT_VERSION_REQUIRED",
            "生成证据包前必须绑定已验证录音对象的精确版本",
            409,
        )
    storage_object = session.scalar(
        select(StorageObject).where(
            StorageObject.storage_object_id == storage_object_id,
            StorageObject.tenant_id == record.tenant_id,
            StorageObject.project_id == record.project_id,
        )
    )
    storage_payload = (
        storage_object.payload
        if storage_object is not None and isinstance(storage_object.payload, dict)
        else {}
    )
    if (
        storage_object is None
        or storage_object.status not in {"verified", "active"}
        or storage_object.content_sha256 != content_sha256
        or storage_payload.get("object_version_id") != version_id
    ):
        raise ApiError(
            "AUDIO_EVIDENCE_OBJECT_BINDING_MISMATCH",
            "录音对象当前版本与音频智能运行冻结输入不一致",
            409,
        )
    return raw_input, storage_object


def _frozen_output_sink_refs(payload: dict[str, Any]) -> list[str]:
    raw_refs = payload.get("output_sink_refs")
    if raw_refs is None:
        return []
    if not isinstance(raw_refs, list) or len(raw_refs) > 64:
        raise ApiError(
            "AUDIO_EVIDENCE_OUTPUT_SINK_BINDING_INVALID",
            "音频智能运行冻结的 output_sink_refs 无效",
            409,
        )
    refs: list[str] = []
    observed: set[str] = set()
    for raw_ref in raw_refs:
        ref = raw_ref.strip() if isinstance(raw_ref, str) else ""
        if not ref or len(ref) > 256 or any(ord(character) < 0x20 for character in ref):
            raise ApiError(
                "AUDIO_EVIDENCE_OUTPUT_SINK_BINDING_INVALID",
                "音频智能运行冻结的 output_sink_refs 无效",
                409,
            )
        if ref not in observed:
            observed.add(ref)
            refs.append(ref)
    return refs


def _existing_outputs(
    session: Session,
    record: RunRecord,
    *,
    evidence_pack_id: str,
    review_task_id: str,
    evidence_sha256: str,
) -> list[dict[str, Any]] | None:
    evidence = session.get(EvidencePack, evidence_pack_id)
    task = session.get(HumanReviewTask, review_task_id)
    if evidence is None and task is None:
        return None
    if (
        evidence is None
        or task is None
        or evidence.tenant_id != record.tenant_id
        or evidence.project_id != record.project_id
        or task.tenant_id != record.tenant_id
        or task.project_id != record.project_id
        or evidence.evidence_sha256 != evidence_sha256
        or task.payload.get("evidence_pack_id") != evidence_pack_id
    ):
        raise ApiError(
            "AUDIO_EVIDENCE_MATERIALIZATION_CONFLICT",
            "既有证据包或人审任务与本次音频结果不一致",
            409,
        )
    outputs: list[dict[str, Any]] = [
        {
            "collection": "evidence_packs",
            "id": evidence_pack_id,
            "asset_key": "auris/audio/evidence_packs",
            "status": evidence.status,
        },
        {
            "collection": "human_review_tasks",
            "id": review_task_id,
            "asset_key": "auris/review/audio_evidence_queue",
            "status": task.status,
        },
    ]
    for target in task.payload.get("target_refs") or []:
        if not isinstance(target, dict):
            continue
        target_type = str(target.get("type") or "")
        collection = REVIEW_TARGET_COLLECTION_BY_TYPE.get(target_type)
        target_id = target.get("id")
        if collection is None or not isinstance(target_id, str):
            continue
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == collection,
                JsonResource.resource_key == target_id,
                JsonResource.tenant_id == record.tenant_id,
                JsonResource.project_id == record.project_id,
            )
        )
        if projection is None:
            raise ApiError(
                "AUDIO_EVIDENCE_MATERIALIZATION_CONFLICT",
                "既有人审任务引用的受控目标尚未物化",
                409,
            )
        outputs.append(
            {
                "collection": collection,
                "id": target_id,
                "asset_key": REVIEW_TARGET_ASSET_BY_TYPE[target_type],
                "status": projection.status,
            }
        )
    return outputs


def _materialize_review_targets(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    result_ref: dict[str, Any],
    evidence_pack_id: str,
    evidence_sha256: str,
    audio_session_id: str,
    recording_id: str,
    root_trace_id: str,
    window_start_ms: int,
    window_end_ms: int,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    common = {
        "audio_session_id": audio_session_id,
        "recording_id": recording_id,
        "evidence_pack_id": evidence_pack_id,
        "source_run_id": record.run_id,
        "root_trace_id": root_trace_id,
        "current_trace_id": record.trace_id,
        "trace_id": root_trace_id,
    }
    target_refs: list[dict[str, str]] = []
    outputs: list[dict[str, Any]] = []

    boundary_id = public_id_from_hex(
        "boundary_audio",
        hashlib.sha256(f"{evidence_sha256}:boundary".encode()).hexdigest(),
        suffix_length=24,
    )
    boundary_data = {
        "id": boundary_id,
        "boundary_id": boundary_id,
        **common,
        "start_ms": window_start_ms,
        "end_ms": window_end_ms,
        "version": 1,
        "review_state": "pending",
        "editable_fields": [
            "start_ms",
            "end_ms",
            "decision",
            "merged_slice_ids",
            "split_slice_ids",
            "extension_ids",
        ],
        "status": "pending",
        "asset_key": REVIEW_TARGET_ASSET_BY_TYPE["conversation_boundary"],
    }
    upsert_resource(
        session,
        ctx,
        "conversation_boundaries",
        boundary_id,
        boundary_data,
        status="pending",
        trace_id=root_trace_id,
        audit_action="audio_evidence.conversation_boundary.materialized",
    )
    target_refs.append({"type": "conversation_boundary", "id": boundary_id})
    outputs.append(
        {
            "collection": "conversation_boundaries",
            "id": boundary_id,
            "asset_key": boundary_data["asset_key"],
            "status": "pending",
        }
    )

    raw_review_outputs = result_ref.get("review_outputs")
    review_outputs = raw_review_outputs if isinstance(raw_review_outputs, dict) else {}
    for index, event_link in enumerate(review_outputs.get("event_links") or []):
        identity = _sha256_document(
            {
                "evidence_sha256": evidence_sha256,
                "kind": "event_link",
                "index": index,
                "result": event_link,
            }
        )
        event_link_id = public_id_from_hex("event_link_audio", identity, suffix_length=24)
        event_link_data = {
            "id": event_link_id,
            **common,
            **event_link,
            "review_state": "pending",
            "status": "pending",
            "asset_key": REVIEW_TARGET_ASSET_BY_TYPE["event_link"],
            "evidence_refs": [{"evidence_pack_id": evidence_pack_id}],
        }
        upsert_resource(
            session,
            ctx,
            "event_links",
            event_link_id,
            event_link_data,
            status="pending",
            trace_id=root_trace_id,
            audit_action="audio_evidence.event_link.materialized",
        )
        target_refs.append({"type": "event_link", "id": event_link_id})
        outputs.append(
            {
                "collection": "event_links",
                "id": event_link_id,
                "asset_key": event_link_data["asset_key"],
                "status": "pending",
            }
        )
    for index, candidate in enumerate(review_outputs.get("label_candidates") or []):
        identity = _sha256_document(
            {
                "evidence_sha256": evidence_sha256,
                "kind": "label_candidate",
                "index": index,
                "result": candidate,
            }
        )
        candidate_id = public_id_from_hex("candidate_audio", identity, suffix_length=24)
        candidate_data = {
            "id": candidate_id,
            "candidate_id": candidate_id,
            **common,
            **candidate,
            "human_state": "pending",
            "status": "pending",
            "asset_key": REVIEW_TARGET_ASSET_BY_TYPE["label_candidate"],
        }
        upsert_resource(
            session,
            ctx,
            "label_candidates",
            candidate_id,
            candidate_data,
            status="pending",
            trace_id=root_trace_id,
            audit_action="audio_evidence.label_candidate.materialized",
        )
        target_refs.append({"type": "label_candidate", "id": candidate_id})
        outputs.append(
            {
                "collection": "label_candidates",
                "id": candidate_id,
                "asset_key": candidate_data["asset_key"],
                "status": "pending",
            }
        )
    return target_refs, outputs


def materialize_audio_evidence_review(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    result_ref: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    capabilities = payload.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or "asr" not in capabilities
        or not _successful_asr(result_ref)
    ):
        return []
    # Legacy/demo runs may not freeze an exact object version. They remain
    # useful for diagnostics, but are deliberately ineligible for evidence or
    # human-review materialization.
    if not isinstance(payload.get("input_object"), dict):
        return []

    audio_session_id = _required_text(
        payload.get("audio_session_id") or result_ref.get("audio_session_id"),
        field="audio_session_id",
        maximum=128,
    )
    recording_id = _required_text(
        payload.get("recording_id") or result_ref.get("recording_id"),
        field="recording_id",
        maximum=128,
    )
    recording = session.scalar(
        select(AudioRecording).where(
            AudioRecording.recording_id == recording_id,
            AudioRecording.tenant_id == record.tenant_id,
            AudioRecording.project_id == record.project_id,
        )
    )
    if recording is None:
        if get_settings().auris_dagster_adapter.strip().lower() != "real":
            # Legacy/local diagnostic runs may predate strong recording
            # registration. They can materialize diagnostic tracks, but are not
            # eligible to produce authoritative evidence or a review task.
            return []
        raise ApiError(
            "AUDIO_EVIDENCE_RECORDING_NOT_FOUND",
            "音频智能结果绑定的录音不存在",
            409,
        )
    try:
        input_object, storage_object = _exact_input_object(session, record)
    except ApiError as exc:
        if get_settings().auris_dagster_adapter.strip().lower() != "real" and exc.code in {
            "AUDIO_EVIDENCE_OBJECT_VERSION_REQUIRED",
            "AUDIO_EVIDENCE_OBJECT_BINDING_MISMATCH",
        }:
            return []
        raise
    window_start_ms, window_end_ms, asr_segments_sha256 = _asr_window(result_ref)
    asr_result_id = f"{audio_session_id}:asr:{record.run_id}"
    root_trace_id = _required_text(
        payload.get("root_trace_id") or recording.payload.get("root_trace_id") or record.trace_id,
        field="root_trace_id",
        maximum=128,
    )
    output_sink_refs = _frozen_output_sink_refs(payload)
    evidence_core_document = {
        "schema_version": "audio-evidence-pack/1",
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "audio_session_id": audio_session_id,
        "recording_id": recording_id,
        "storage_object": {
            "storage_object_id": storage_object.storage_object_id,
            "version_id": input_object["version_id"],
            "content_sha256": input_object["content_sha256"],
        },
        "asr_result": {
            "asr_result_id": asr_result_id,
            "version": record.run_id,
            "segments_sha256": asr_segments_sha256,
        },
        "time_window": {
            "start_ms": window_start_ms,
            "end_ms": window_end_ms,
        },
        "source_run_id": record.run_id,
        "root_trace_id": root_trace_id,
    }
    evidence_sha256 = _sha256_document(evidence_core_document)
    evidence_document = {
        **evidence_core_document,
        **({"output_sink_refs": output_sink_refs} if output_sink_refs else {}),
    }
    evidence_pack_id = public_id_from_hex(
        "evidence_audio",
        evidence_sha256,
        suffix_length=24,
    )
    review_task_id = public_id_from_hex(
        "hrt_audio",
        evidence_sha256,
        suffix_length=24,
    )
    existing = _existing_outputs(
        session,
        record,
        evidence_pack_id=evidence_pack_id,
        review_task_id=review_task_id,
        evidence_sha256=evidence_sha256,
    )
    if existing is not None:
        return existing

    rooted_ctx = replace(
        ctx,
        trace_id=root_trace_id,
        parent_trace_id=(
            record.trace_id if record.trace_id != root_trace_id else ctx.parent_trace_id
        ),
        correlation_id=root_trace_id,
    )
    evidence = EvidencePack(
        evidence_pack_id=evidence_pack_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        audio_session_id=audio_session_id,
        recording_id=recording_id,
        storage_object_id=storage_object.storage_object_id,
        storage_object_version=str(input_object["version_id"]),
        audio_sha256=str(input_object["content_sha256"]),
        asr_result_id=asr_result_id,
        asr_result_version=record.run_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        evidence_sha256=evidence_sha256,
        status="ready",
        source_run_id=record.run_id,
        resource_version=1,
        root_trace_id=root_trace_id,
        current_trace_id=record.trace_id,
        payload=evidence_document,
    )
    session.add(evidence)
    evidence_projection = {
        "id": evidence_pack_id,
        "evidence_pack_id": evidence_pack_id,
        **evidence_document,
        "evidence_sha256": evidence_sha256,
        "status": "ready",
        "resource_version": 1,
        "current_trace_id": record.trace_id,
        "trace_id": root_trace_id,
    }
    upsert_resource(
        session,
        rooted_ctx,
        "evidence_packs",
        evidence_pack_id,
        evidence_projection,
        status="ready",
        trace_id=root_trace_id,
        audit_action="audio_evidence_pack.materialized",
    )
    review_target_refs, review_target_outputs = _materialize_review_targets(
        session,
        rooted_ctx,
        record,
        result_ref=result_ref,
        evidence_pack_id=evidence_pack_id,
        evidence_sha256=evidence_sha256,
        audio_session_id=audio_session_id,
        recording_id=recording_id,
        root_trace_id=root_trace_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    task_payload = {
        "id": review_task_id,
        "review_task_id": review_task_id,
        "status": "pending",
        "queue": "audio_evidence_review",
        "review_mode": "single",
        "reason": "音频智能处理已生成可复核证据",
        "audio_session_id": audio_session_id,
        "recording_id": recording_id,
        "evidence_pack_id": evidence_pack_id,
        "source_run_id": record.run_id,
        "source_trace_id": root_trace_id,
        "root_trace_id": root_trace_id,
        "trace_id": root_trace_id,
        **({"output_sink_refs": output_sink_refs} if output_sink_refs else {}),
        "target_refs": [
            {
                "type": "evidence_pack",
                "id": evidence_pack_id,
            },
            *review_target_refs,
        ],
        "evidence_refs": [
            {
                "type": "storage_object",
                "id": storage_object.storage_object_id,
                "version_id": input_object["version_id"],
            },
            {
                "type": "asr_result",
                "id": asr_result_id,
                "version": record.run_id,
            },
        ],
        "allowed_decisions": ["accepted", "modified", "rejected"],
    }
    upsert_resource(
        session,
        rooted_ctx,
        "human_review_tasks",
        review_task_id,
        task_payload,
        status="pending",
        trace_id=root_trace_id,
        audit_action="audio_evidence_review_task.created",
    )
    enqueue_event(
        session,
        rooted_ctx,
        event_type="audio_evidence_pack.materialized",
        aggregate_type="evidence_pack",
        aggregate_id=evidence_pack_id,
        payload={
            **evidence_projection,
            "resource_version": 1,
        },
    )
    enqueue_event(
        session,
        rooted_ctx,
        event_type="human_review_task.created",
        aggregate_type="human_review_task",
        aggregate_id=review_task_id,
        payload=task_payload,
    )
    record_audit(
        session,
        rooted_ctx,
        action="audio_evidence.vertical_chain_created",
        object_type="audio_session",
        object_id=audio_session_id,
        after={
            "evidence_pack_id": evidence_pack_id,
            "review_task_id": review_task_id,
            "root_trace_id": root_trace_id,
        },
        trace_id=root_trace_id,
    )
    return [
        {
            "collection": "evidence_packs",
            "id": evidence_pack_id,
            "asset_key": "auris/audio/evidence_packs",
            "status": "ready",
        },
        {
            "collection": "human_review_tasks",
            "id": review_task_id,
            "asset_key": "auris/review/audio_evidence_queue",
            "status": "pending",
        },
        *review_target_outputs,
    ]
