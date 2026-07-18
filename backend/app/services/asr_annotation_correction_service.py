from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import AsrAnnotationCorrection, Badcase
from app.schemas.hotwords import AsrTranscriptCorrectionRequest, HotwordBadcaseCreateRequest
from app.services.audio_intelligence_service import validate_scoped_storage_object_reference
from app.services.audit_service import record_audit
from app.services.hotword_service import (
    BADCASE_EVIDENCE_SOURCE_TYPES,
    create_badcase,
    ensure_hotword_is_not_sensitive,
    get_hotword_version,
    normalize_hotword,
)
from app.services.outbox_service import enqueue_event

ASR_CORRECTION_WRITE_ROLES = frozenset({"project_admin", "review_arbitrator", "annotator"})


def _normalized_text(value: str) -> str:
    if not value:
        return ""
    return normalize_hotword(value)


def _sha256_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _correction_data(
    record: AsrAnnotationCorrection,
    *,
    deduplicated: bool,
) -> dict[str, Any]:
    return {
        "id": record.annotation_id,
        "annotation_id": record.annotation_id,
        "correction_id": record.correction_id,
        "annotation_kind": "asr-transcript-correction",
        "status": record.status,
        "standard_term": record.standard_term,
        "error_type": record.error_type,
        "evidence_window": record.evidence_window,
        "evidence_storage_object_id": record.evidence_storage_object_id,
        "hotword_pack_version_id": record.hotword_pack_version_id,
        "source_badcase_id": record.source_badcase_id,
        "stat_eligibility": "discovery-only",
        "evidence_level": "discovery",
        "eligible_for_release_gate": False,
        "deduplicated": deduplicated,
        "correction_fingerprint": record.correction_fingerprint,
        "semantic_sha256": record.semantic_sha256,
        "root_trace_id": record.root_trace_id,
        "source_trace_id": record.source_trace_id,
        "current_trace_id": record.current_trace_id,
        "trace_id": record.current_trace_id,
        "affected_objects": [
            {"type": "audio_session", "id": record.audio_session_id},
            {"type": "asr_annotation_correction", "id": record.correction_id},
            {"type": "badcase", "id": record.source_badcase_id},
            {"type": "hotword_pack_version", "id": record.hotword_pack_version_id},
        ],
        "next_actions": [
            {
                "key": "review_badcase",
                "label": "进入 ASR 热词 Badcase 人审",
                "route": f"evaluation/badcase/{record.source_badcase_id}",
            }
        ],
    }


def _validate_linked_badcase(
    badcase: Badcase,
    *,
    body: AsrTranscriptCorrectionRequest,
    normalized_standard_term: str,
) -> None:
    if badcase.capability != "asr-hotword":
        raise ApiError(
            "ASR_CORRECTION_BADCASE_CAPABILITY_INVALID",
            "标注修正只能关联 ASR 热词 Badcase",
            409,
        )
    if badcase.evidence_storage_object_id != body.evidence_storage_object_id:
        raise ApiError(
            "ASR_CORRECTION_BADCASE_EVIDENCE_MISMATCH",
            "标注修正与 Badcase 的词级证据不一致",
            409,
        )
    if badcase.hotword_pack_version_id != body.hotword_pack_version_id:
        raise ApiError(
            "ASR_CORRECTION_BADCASE_VERSION_MISMATCH",
            "标注修正与 Badcase 的热词包版本不一致",
            409,
        )
    if (
        not badcase.standard_term
        or normalize_hotword(badcase.standard_term) != normalized_standard_term
    ):
        raise ApiError(
            "ASR_CORRECTION_BADCASE_TERM_MISMATCH",
            "一个词级证据只能记录其绑定标准词的修正",
            409,
        )


def _resolve_or_create_badcase(
    session: Session,
    ctx: RequestContext,
    *,
    body: AsrTranscriptCorrectionRequest,
    standard_term: str,
    normalized_standard_term: str,
    correction_id: str,
) -> str:
    evidence_case = session.scalar(
        select(Badcase).where(
            Badcase.tenant_id == ctx.tenant_id,
            Badcase.project_id == ctx.project_id,
            Badcase.evidence_storage_object_id == body.evidence_storage_object_id,
        )
    )
    if body.source_badcase_id:
        requested_case = session.scalar(
            select(Badcase).where(
                Badcase.tenant_id == ctx.tenant_id,
                Badcase.project_id == ctx.project_id,
                Badcase.badcase_id == body.source_badcase_id,
            )
        )
        if requested_case is None:
            raise ApiError("BADCASE_NOT_FOUND", "关联的 ASR 热词 Badcase 不存在", 404)
        if evidence_case is not None and evidence_case.badcase_id != requested_case.badcase_id:
            raise ApiError(
                "ASR_CORRECTION_EVIDENCE_ALREADY_BOUND",
                "词级证据已绑定其他 ASR 热词 Badcase",
                409,
            )
        evidence_case = requested_case

    if evidence_case is not None:
        _validate_linked_badcase(
            evidence_case,
            body=body,
            normalized_standard_term=normalized_standard_term,
        )
        return evidence_case.badcase_id

    badcase_id = f"A-ANN-{hashlib.sha256(correction_id.encode()).hexdigest()[:12].upper()}"
    expected_count = 0 if body.error_type == "false_boost" else 1
    weighted_error_count = 0.0 if body.error_type == "false_boost" else 1.0
    created = create_badcase(
        session,
        ctx,
        HotwordBadcaseCreateRequest(
            badcase_id=badcase_id,
            capability="asr-hotword",
            standard_term=standard_term,
            recognized_text=body.recognized_text,
            error_type=body.error_type,
            evidence_storage_object_id=body.evidence_storage_object_id,
            evidence_ref=f"audio-session:{body.audio_session_id or ''}#{body.evidence_window}",
            evidence_level="discovery",
            expected_count=expected_count,
            correct_count=0,
            weighted_error_count=weighted_error_count,
            manual_correction_count=1,
            business_weight=1.0,
            downstream_impact={
                "source": "asr_annotation_correction",
                "audio_session_id": body.audio_session_id,
                "annotation_id": body.annotation_id,
                "correction_id": correction_id,
            },
            root_cause=body.error_type,
            fix_suggestion="进入热词 Badcase 人审，确认后再加入候选词包",
            hotword_pack_version_id=body.hotword_pack_version_id,
        ),
        trusted_evidence=True,
    )
    return str(created["badcase_id"])


def record_asr_annotation_correction(
    session: Session,
    ctx: RequestContext,
    *,
    audio_session_id: str,
    audio_session_data: dict[str, Any],
    body: AsrTranscriptCorrectionRequest,
) -> tuple[dict[str, Any], bool]:
    if body.audio_session_id is not None and body.audio_session_id != audio_session_id:
        raise ApiError(
            "AUDIO_SESSION_MISMATCH",
            "ASR 标注修正与当前音频会话不一致",
            409,
        )

    version = get_hotword_version(session, ctx, body.hotword_pack_version_id)
    evidence = validate_scoped_storage_object_reference(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        storage_object_id=body.evidence_storage_object_id,
        purpose="ASR 标注修正词级证据",
    )
    if evidence.source_type not in BADCASE_EVIDENCE_SOURCE_TYPES:
        raise ApiError(
            "ASR_CORRECTION_EVIDENCE_SOURCE_INVALID",
            "StorageObject 不是允许的 ASR 热词词级证据类型",
            422,
            details=[
                {
                    "storage_object_id": evidence.storage_object_id,
                    "source_type": evidence.source_type,
                    "allowed_source_types": sorted(BADCASE_EVIDENCE_SOURCE_TYPES),
                }
            ],
        )
    source_trace_id = str(evidence.trace_id or "").strip()
    if not source_trace_id:
        raise ApiError(
            "ASR_CORRECTION_EVIDENCE_TRACE_REQUIRED",
            "ASR 标注修正证据必须绑定可追踪的 trace",
            409,
        )

    standard_term = body.corrected_text or body.recognized_text
    normalized_standard_term = normalize_hotword(standard_term)
    normalized_recognized = _normalized_text(body.recognized_text)
    normalized_corrected = _normalized_text(body.corrected_text)
    if normalized_recognized and normalized_recognized == normalized_corrected:
        raise ApiError(
            "ASR_CORRECTION_NO_CHANGE",
            "规范化后识别文本与正确文本一致，不产生热词修正信号",
            422,
        )
    ensure_hotword_is_not_sensitive(standard_term, "asr-annotation-correction")

    semantic_sha256 = _sha256_payload(
        {
            "recognized_text": normalized_recognized,
            "corrected_text": normalized_corrected,
            "error_type": body.error_type,
        }
    )
    correction_fingerprint = _sha256_payload(
        {
            "audio_session_id": audio_session_id,
            "evidence_storage_object_id": body.evidence_storage_object_id,
            "evidence_window": unicodedata.normalize("NFKC", body.evidence_window).strip(),
            "hotword_pack_version_id": body.hotword_pack_version_id,
            "semantic_sha256": semantic_sha256,
        }
    )

    existing_annotation = session.scalar(
        select(AsrAnnotationCorrection).where(
            AsrAnnotationCorrection.tenant_id == ctx.tenant_id,
            AsrAnnotationCorrection.project_id == ctx.project_id,
            AsrAnnotationCorrection.annotation_id == body.annotation_id,
        )
    )
    if existing_annotation is not None:
        if existing_annotation.correction_fingerprint == correction_fingerprint:
            return _correction_data(existing_annotation, deduplicated=True), True
        raise ApiError(
            "ASR_CORRECTION_IMMUTABLE",
            "已提交的 ASR 标注修正不可覆盖，请使用新的词级证据提交新观察",
            409,
        )

    semantic_duplicate = session.scalar(
        select(AsrAnnotationCorrection).where(
            AsrAnnotationCorrection.tenant_id == ctx.tenant_id,
            AsrAnnotationCorrection.project_id == ctx.project_id,
            AsrAnnotationCorrection.correction_fingerprint == correction_fingerprint,
        )
    )
    if semantic_duplicate is not None:
        return _correction_data(semantic_duplicate, deduplicated=True), True

    evidence_duplicate = session.scalar(
        select(AsrAnnotationCorrection).where(
            AsrAnnotationCorrection.tenant_id == ctx.tenant_id,
            AsrAnnotationCorrection.project_id == ctx.project_id,
            AsrAnnotationCorrection.evidence_storage_object_id == body.evidence_storage_object_id,
        )
    )
    if evidence_duplicate is not None:
        raise ApiError(
            "ASR_CORRECTION_EVIDENCE_ALREADY_COUNTED",
            "该词级证据已记录其他 ASR 标注修正，不能重复计数",
            409,
        )

    correction_id = f"asrc_{correction_fingerprint[:24]}"
    source_badcase_id = _resolve_or_create_badcase(
        session,
        ctx,
        body=body,
        standard_term=standard_term,
        normalized_standard_term=normalized_standard_term,
        correction_id=correction_id,
    )
    store_id_value = audio_session_data.get("store_id")
    store_id = str(store_id_value) if isinstance(store_id_value, str) else None
    model_value = audio_session_data.get("model_version") or ctx.model_version
    model_version = str(model_value) if isinstance(model_value, str) else None
    provider = str(version.compiled_provider) if version.compiled_provider else None
    observed_value = audio_session_data.get("started_at")
    try:
        observed_at = (
            datetime.fromisoformat(observed_value)
            if isinstance(observed_value, str)
            else datetime.now(UTC)
        )
    except ValueError:
        observed_at = datetime.now(UTC)
    record = AsrAnnotationCorrection(
        correction_id=correction_id,
        annotation_id=body.annotation_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        audio_session_id=audio_session_id,
        observed_at=observed_at,
        status="submitted",
        standard_term=standard_term,
        normalized_term=normalized_standard_term,
        recognized_text=body.recognized_text,
        corrected_text=body.corrected_text,
        error_type=body.error_type,
        evidence_storage_object_id=body.evidence_storage_object_id,
        evidence_window=body.evidence_window,
        hotword_pack_version_id=body.hotword_pack_version_id,
        source_badcase_id=source_badcase_id,
        store_id=store_id,
        provider=provider,
        model_version=model_version,
        evidence_level="discovery",
        correction_fingerprint=correction_fingerprint,
        semantic_sha256=semantic_sha256,
        root_trace_id=version.root_trace_id,
        source_trace_id=source_trace_id,
        current_trace_id=ctx.trace_id,
        payload={
            "source_asr_segment_id": body.source_asr_segment_id,
            "submitted_by": ctx.user_id,
            "annotation_kind": "asr-transcript-correction",
            "eligible_for_release_gate": False,
        },
    )
    session.add(record)
    session.flush()
    data = _correction_data(record, deduplicated=False)
    audit_projection = {
        key: data[key]
        for key in (
            "annotation_id",
            "correction_id",
            "status",
            "evidence_storage_object_id",
            "hotword_pack_version_id",
            "source_badcase_id",
            "stat_eligibility",
            "correction_fingerprint",
            "semantic_sha256",
            "root_trace_id",
            "source_trace_id",
            "current_trace_id",
        )
    }
    record_audit(
        session,
        ctx,
        action="asr_annotation.correction-recorded",
        object_type="asr_annotation_correction",
        object_id=correction_id,
        after=audit_projection,
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="asr_annotation.correction-recorded",
        aggregate_type="asr_annotation_correction",
        aggregate_id=correction_id,
        payload=audit_projection,
    )
    return data, False
