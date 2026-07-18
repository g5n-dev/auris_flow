from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import PromptAsset, PromptVersion, PromptVersionCandidate, RunRecord
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

PCODE_REQUIRED_SECTIONS = frozenset(
    {
        "system",
        "label_definitions",
        "positive_examples",
        "negative_examples",
        "boundary_examples",
        "conflict_rules",
        "unknown_policy",
        "injection_defense",
        "post_processing",
    }
)


def _canonical_sha256(value: object) -> str:
    document = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _candidate_id(record: RunRecord, result_ref: dict[str, Any]) -> str:
    draft_ref = result_ref.get("draft_ref") or result_ref.get("candidate_id")
    if isinstance(draft_ref, str) and draft_ref:
        return draft_ref
    digest = hashlib.sha1(
        f"{record.tenant_id}|{record.project_id}|{record.run_id}".encode()
    ).hexdigest()[:12]
    return f"prompt_candidate_{digest}"


def materialize_prompt_candidate(
    session: Session,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> PromptVersionCandidate | None:
    if record.run_type != "eval_feedback" or record.status != "success":
        return None
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        result_ref = {}
    candidate_id = _candidate_id(record, result_ref)
    base_prompt_version = (
        record.payload.get("prompt_version")
        or record.payload.get("prompt_version_id")
        or result_ref.get("base_prompt_version")
        or "prompt_current"
    )
    payload = {
        "candidate_id": candidate_id,
        "status": "candidate",
        "source_run_id": record.run_id,
        "source_run_type": record.run_type,
        "agent_run_id": record.payload.get("agent_run_id") or record.run_id,
        "eval_run_id": record.payload.get("eval_run_id"),
        "feedback_task_id": record.payload.get("feedback_task_id"),
        "base_prompt_version": base_prompt_version,
        "target": record.payload.get("target"),
        "badcase_refs": record.payload.get("badcase_refs", []),
        "result_ref": result_ref,
        "metrics": completion_receipt.get("metrics") or {},
        "change_set_id": result_ref.get("change_set_id"),
        "object_uri": result_ref.get("object_uri"),
        "review_gate": {
            "required": True,
            "reason": "Agent 只能产出 Prompt 候选草稿，不能直接覆盖生产 Prompt。",
        },
        "write_policy": {
            "allowed_writes": ["candidate", "draft", "human_review_task", "eval_run_draft"],
            "forbidden_writes": ["production_prompt", "source_asset", "online_label"],
        },
        "affected_objects": [
            {"type": "eval_run", "id": record.payload.get("eval_run_id")},
            {"type": "feedback_task", "id": record.payload.get("feedback_task_id")},
            {"type": "prompt_version_candidate", "id": candidate_id},
        ],
    }
    candidate = session.scalar(
        select(PromptVersionCandidate).where(
            PromptVersionCandidate.candidate_id == candidate_id,
            PromptVersionCandidate.tenant_id == record.tenant_id,
            PromptVersionCandidate.project_id == record.project_id,
        )
    )
    if candidate is None:
        candidate = PromptVersionCandidate(
            candidate_id=candidate_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            status="candidate",
            trace_id=record.trace_id,
            payload=payload,
        )
        session.add(candidate)
    else:
        candidate.status = "candidate"
        candidate.trace_id = record.trace_id
        candidate.payload = payload
    return candidate


def materialize_optimization_prompt_candidates(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> list[PromptVersionCandidate]:
    """Persist real Prompt bodies from an optimization completion receipt.

    The compatibility ``PromptVersionCandidate`` remains a read projection; the
    immutable ``PromptVersion`` row is the authoritative artifact.
    """

    if record.run_type != "label_optimization" or record.status != "success":
        return []
    result_ref = completion_receipt.get("result_ref")
    raw_candidates = result_ref.get("prompt_candidates") if isinstance(result_ref, dict) else None
    if not isinstance(raw_candidates, list) or not 2 <= len(raw_candidates) <= 5:
        raise ApiError(
            "PROMPT_OPTIMIZATION_CANDIDATE_COUNT_INVALID",
            "优化成功回执必须包含 2–5 个 Prompt 候选",
            422,
        )

    parent_version_id = str(record.payload.get("prompt_version_id") or "")
    label_version_id = str(record.payload.get("label_version_id") or "")
    model_version = str(record.payload.get("model_version") or "")
    parent = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == parent_version_id,
            PromptVersion.tenant_id == record.tenant_id,
            PromptVersion.project_id == record.project_id,
        )
    )
    if parent is None:
        raise ApiError(
            "PROMPT_OPTIMIZATION_PARENT_NOT_FOUND",
            "优化运行锁定的父 PromptVersion 不存在",
            409,
        )
    asset = session.scalar(
        select(PromptAsset).where(
            PromptAsset.prompt_asset_id == parent.prompt_asset_id,
            PromptAsset.tenant_id == record.tenant_id,
            PromptAsset.project_id == record.project_id,
        )
    )
    if asset is None or parent.label_version_id != label_version_id:
        raise ApiError(
            "PROMPT_OPTIMIZATION_SCOPE_MISMATCH",
            "优化运行的 PromptAsset、父版本与标签版本绑定不一致",
            409,
        )

    expected_max = int((record.payload.get("budget") or {}).get("candidates_per_round") or 5)
    if len(raw_candidates) > min(max(expected_max, 2), 5):
        raise ApiError(
            "PROMPT_OPTIMIZATION_CANDIDATE_BUDGET_EXCEEDED",
            "候选数量超过本次运行锁定预算",
            422,
        )

    compatibility_candidates: list[PromptVersionCandidate] = []
    prompt_version_ids: list[str] = []
    review_task_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict):
            raise ApiError(
                "PROMPT_OPTIMIZATION_CANDIDATE_INVALID",
                "Prompt 候选必须是对象",
                422,
                details=[{"index": index}],
            )
        prompt_version_id = str(
            raw.get("prompt_version_id") or raw.get("candidate_id") or f"pv_{record.run_id}_{index}"
        )
        if not prompt_version_id or prompt_version_id in seen_ids:
            raise ApiError(
                "PROMPT_OPTIMIZATION_CANDIDATE_ID_INVALID",
                "Prompt 候选 ID 不能为空或重复",
                422,
            )
        seen_ids.add(prompt_version_id)
        template = raw.get("template")
        output_schema = raw.get("output_schema")
        structured_diff = raw.get("structured_diff")
        generation_params = raw.get("generation_params") or {}
        source_badcase_refs = raw.get("source_badcase_refs") or []
        if not isinstance(template, dict):
            raise ApiError(
                "PROMPT_OPTIMIZATION_TEMPLATE_REQUIRED",
                "候选必须持久化真实 Prompt template",
                422,
                details=[{"index": index}],
            )
        missing_sections = sorted(PCODE_REQUIRED_SECTIONS - set(template))
        if missing_sections:
            raise ApiError(
                "PROMPT_PCODE_SECTIONS_MISSING",
                "Prompt 候选缺少 P-CODE 必需段落",
                422,
                details=[{"index": index, "sections": missing_sections}],
            )
        if not isinstance(output_schema, dict) or not output_schema:
            raise ApiError(
                "PROMPT_OPTIMIZATION_SCHEMA_REQUIRED",
                "Prompt 候选必须包含非空输出 Schema",
                422,
            )
        if not isinstance(structured_diff, dict) or not structured_diff:
            raise ApiError(
                "PROMPT_OPTIMIZATION_DIFF_REQUIRED",
                "Prompt 候选必须包含相对父版本的结构化 diff",
                422,
            )
        if not isinstance(generation_params, dict) or not isinstance(source_badcase_refs, list):
            raise ApiError(
                "PROMPT_OPTIMIZATION_METADATA_INVALID",
                "生成参数和 badcase 来源格式无效",
                422,
            )
        if session.get(PromptVersion, prompt_version_id) is not None:
            raise ApiError(
                "PROMPT_VERSION_ID_CONFLICT",
                "优化候选 PromptVersion ID 已存在",
                409,
                details=[{"prompt_version_id": prompt_version_id}],
            )

        schema_version = str(raw.get("schema_version") or parent.schema_version)
        content_document = {
            "prompt_asset_id": asset.prompt_asset_id,
            "parent_version_id": parent.prompt_version_id,
            "label_version_id": label_version_id,
            "schema_version": schema_version,
            "model_version": model_version,
            "template": template,
            "output_schema": output_schema,
            "generation_params": generation_params,
        }
        content_sha256 = _canonical_sha256(content_document)
        duplicate_content = session.scalar(
            select(PromptVersion.prompt_version_id).where(
                PromptVersion.tenant_id == record.tenant_id,
                PromptVersion.project_id == record.project_id,
                PromptVersion.content_sha256 == content_sha256,
            )
        )
        if duplicate_content is not None:
            raise ApiError(
                "PROMPT_CONTENT_CONFLICT",
                "优化候选内容与既有 PromptVersion 重复",
                409,
                details=[{"prompt_version_id": duplicate_content}],
            )
        version = PromptVersion(
            prompt_version_id=prompt_version_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            prompt_asset_id=asset.prompt_asset_id,
            version=str(raw.get("version") or f"candidate-{index}"),
            parent_version_id=parent.prompt_version_id,
            label_version_id=label_version_id,
            schema_version=schema_version,
            model_version=model_version,
            status="candidate",
            template_json=template,
            output_schema=output_schema,
            generation_params=generation_params,
            structured_diff=structured_diff,
            source_badcase_refs=[str(item) for item in source_badcase_refs],
            content_sha256=content_sha256,
            trace_id=record.trace_id,
        )
        session.add(version)

        review_task_id = f"hrt_{_canonical_sha256(['prompt', prompt_version_id])[:24]}"
        candidate_payload = {
            "id": prompt_version_id,
            "candidate_id": prompt_version_id,
            "prompt_version_id": prompt_version_id,
            "prompt_asset_id": asset.prompt_asset_id,
            "parent_version_id": parent.prompt_version_id,
            "label_version_id": label_version_id,
            "model_version": model_version,
            "schema_version": schema_version,
            "status": "candidate",
            "template": template,
            "output_schema": output_schema,
            "generation_params": generation_params,
            "structured_diff": structured_diff,
            "source_badcase_refs": [str(item) for item in source_badcase_refs],
            "content_sha256": content_sha256,
            "source_run_id": record.run_id,
            "metrics": raw.get("metrics") or {},
            "review_task_id": review_task_id,
            "review_gate": {
                "required": True,
                "mode": "double-blind",
                "required_reviews": 2,
                "requires_adjudication_on_disagreement": True,
            },
            "trace_id": record.trace_id,
        }
        candidate = PromptVersionCandidate(
            candidate_id=prompt_version_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            status="candidate",
            trace_id=record.trace_id,
            payload=candidate_payload,
        )
        session.add(candidate)
        upsert_resource(
            session,
            ctx,
            "prompt_version_candidates",
            prompt_version_id,
            candidate_payload,
            status="candidate",
            trace_id=record.trace_id,
            audit_action="prompt_version_candidate.projection_created",
        )
        task_payload = {
            "id": review_task_id,
            "review_task_id": review_task_id,
            "status": "pending",
            "review_status": "pending",
            "queue": "prompt_approval",
            "risk_level": "high",
            "review_mode": "double-blind",
            "required_reviews": 2,
            "target_refs": [{"type": "prompt_version_candidate", "id": prompt_version_id}],
            "source_run_id": record.run_id,
            "trace_id": record.trace_id,
        }
        upsert_resource(
            session,
            ctx,
            "human_review_tasks",
            review_task_id,
            task_payload,
            status="pending",
            trace_id=record.trace_id,
            audit_action="human_review_task.prompt_candidate_created",
        )
        record_audit(
            session,
            ctx,
            action="prompt_version_candidate.created",
            object_type="prompt_version",
            object_id=prompt_version_id,
            after=candidate_payload,
            trace_id=record.trace_id,
        )
        enqueue_event(
            session,
            ctx,
            event_type="prompt_version_candidate.created",
            aggregate_type="prompt_version_candidate",
            aggregate_id=prompt_version_id,
            payload=candidate_payload,
        )
        compatibility_candidates.append(candidate)
        prompt_version_ids.append(prompt_version_id)
        review_task_ids.append(review_task_id)

    record.payload = {
        **record.payload,
        "stage": "awaiting-review",
        "business_status": "awaiting-review",
        "prompt_candidate_ids": prompt_version_ids,
        "prompt_candidate_review_task_ids": review_task_ids,
        "materialized_candidate_count": len(prompt_version_ids),
        "next_action": {
            "code": "review-prompt-candidates",
            "label": "双盲审核 Prompt 候选后进入锁定离线评测",
        },
        "next_actions": [
            {
                "code": "review-prompt-candidates",
                "label": "双盲审核 Prompt 候选后进入锁定离线评测",
            }
        ],
    }
    return compatibility_candidates
