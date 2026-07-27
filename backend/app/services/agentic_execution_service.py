from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.request_identifiers import public_id_from_hex
from app.models import AgentDecision, AgentRun, RunRecord, ToolCall, TraceRef

AGENTIC_RUN_TYPES = {
    "eval_feedback",
    "label_extraction",
    "label_optimization",
    "scene_profile_generation",
}

AGENT_WRITE_POLICY = {
    "allowed_writes": [
        "candidate",
        "draft",
        "human_review_task",
        "eval_run_draft",
        "report_draft",
        "scene_profile_candidate",
    ],
    "forbidden_writes": ["online_label", "production_prompt", "source_asset"],
}


def _scoped_id(prefix: str, tenant_id: str, project_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}|{project_id}|{key}".encode()).hexdigest()
    return public_id_from_hex(prefix, digest, suffix_length=20)


def is_agentic_run_type(run_type: str) -> bool:
    return run_type in AGENTIC_RUN_TYPES


def _tool_plan(run_type: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if run_type == "eval_feedback":
        return [
            {
                "key": "retrieve_badcases",
                "tool": "qdrant.recall",
                "purpose": "召回相似 badcase 和证据片段",
            },
            {
                "key": "diagnose_prompt_gap",
                "tool": "model.reason",
                "purpose": "分析 Prompt、规则或样本缺口",
            },
            {
                "key": "write_feedback_draft",
                "tool": "mysql.write_draft",
                "purpose": "只写回流草稿或人审任务",
            },
        ]
    if run_type == "label_optimization":
        return [
            {
                "key": "recall_label_samples",
                "tool": "qdrant.recall",
                "purpose": "召回标签候选和冲突样本",
            },
            {
                "key": "evaluate_label_gate",
                "tool": "eval.run",
                "purpose": "评估候选标签对命中率和误伤的影响",
            },
            {
                "key": "write_candidate_version",
                "tool": "mysql.write_draft",
                "purpose": "只写候选版本，不覆盖线上标签",
            },
        ]
    if run_type == "label_extraction":
        return [
            {
                "key": "extract_label_observations",
                "tool": "model.structured_generate",
                "purpose": "按锁定 Prompt、Schema、模型与标签版本生成原始 Observation",
            },
            {
                "key": "validate_observation_schema",
                "tool": "schema.validate",
                "purpose": "校验范围、证据、哈希和结构化输出，失败时拒绝物化",
            },
            {
                "key": "materialize_observations",
                "tool": "mysql.append_observations",
                "purpose": "仅追加不可变 Observation，等待聚合与人审",
            },
        ]
    if run_type == "scene_profile_generation":
        return [
            {
                "key": "retrieve_scene_context",
                "tool": "qdrant.recall",
                "purpose": "召回受控业务资料、样本、Schema 和既有场景版本",
            },
            {
                "key": "generate_scene_manifest",
                "tool": "model.structured_generate",
                "purpose": "生成 scene-profile/1 候选清单，禁止直接发布",
            },
            {
                "key": "validate_scene_manifest",
                "tool": "schema.validate",
                "purpose": "校验角色、实体、事件、指标、依赖和发布门禁结构",
            },
            {
                "key": "write_scene_candidate",
                "tool": "mysql.write_draft",
                "purpose": "只写候选场景版本，等待实名人工复核",
            },
        ]
    return [
        {
            "key": "dispatch_agent_run",
            "tool": "dagster.run_request",
            "purpose": f"提交 {run_type} 智能体运行",
        }
    ]


def _input_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    list_fields = {
        "badcase_refs": "badcase",
        "evidence_refs": "evidence",
        "asset_keys": "data_asset",
        "input_refs": "scene_input",
    }
    scalar_fields = {
        "eval_run_id": "eval_run",
        "dataset_id": "eval_dataset",
        "prompt_version": "prompt_version",
        "prompt_version_id": "prompt_version",
        "label_version_id": "label_version",
        "knowledge_index_id": "knowledge_index",
        "asset_key": "data_asset",
    }
    for field, ref_type in list_fields.items():
        values = payload.get(field)
        if isinstance(values, list):
            refs.extend(
                {"type": ref_type, "id": str(value), "source_field": field}
                for value in values
                if isinstance(value, str) and value
            )
    for field, ref_type in scalar_fields.items():
        value = payload.get(field)
        if isinstance(value, str) and value:
            refs.append({"type": ref_type, "id": value, "source_field": field})
    for item in payload.get("affected_objects", []):
        if isinstance(item, dict) and item.get("type") and item.get("id"):
            refs.append(
                {
                    "type": str(item["type"]),
                    "id": str(item["id"]),
                    "source_field": "affected_objects",
                }
            )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref["type"], ref["id"])
        if key not in seen:
            seen.add(key)
            deduped.append(ref)
    return deduped


def create_agent_execution_projection(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    event_type: str,
) -> dict[str, Any]:
    if not is_agentic_run_type(record.run_type):
        return {}
    agent_run_id = str(record.payload.get("agent_run_id") or record.run_id)
    tool_plan = _tool_plan(record.run_type, record.payload)
    refs = _input_refs(record.payload)
    now = datetime.now(UTC).isoformat()
    agent_payload = {
        "agent_run_id": agent_run_id,
        "source_run_id": record.run_id,
        "source_run_type": record.run_type,
        "event_type": event_type,
        "input_refs": refs,
        "tool_plan": tool_plan,
        "write_policy": AGENT_WRITE_POLICY,
        "created_from": "run.create",
        "created_at": now,
    }
    session.merge(
        AgentRun(
            agent_run_id=agent_run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status=record.status,
            trace_id=record.trace_id,
            payload=agent_payload,
        )
    )
    for index, tool in enumerate(tool_plan, start=1):
        tool_call_id = _scoped_id(
            "tool", ctx.tenant_id, ctx.project_id, f"{agent_run_id}:{tool['key']}"
        )
        session.merge(
            ToolCall(
                tool_call_id=tool_call_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="planned",
                trace_id=record.trace_id,
                payload={
                    "agent_run_id": agent_run_id,
                    "source_run_id": record.run_id,
                    "sequence": index,
                    **tool,
                },
            )
        )
    for ref in refs:
        trace_ref_id = _scoped_id(
            "trace_ref",
            ctx.tenant_id,
            ctx.project_id,
            f"{record.trace_id}:{agent_run_id}:{ref['type']}:{ref['id']}",
        )
        session.merge(
            TraceRef(
                trace_ref_id=trace_ref_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="active",
                trace_id=record.trace_id,
                payload={
                    "agent_run_id": agent_run_id,
                    "source_run_id": record.run_id,
                    "ref_role": "input",
                    **ref,
                },
            )
        )
    return {
        "agent_run_id": agent_run_id,
        "agent_policy": AGENT_WRITE_POLICY,
        "agent_tool_plan": tool_plan,
        "agent_input_refs": refs,
    }


def record_agent_dispatch(
    session: Session,
    record: RunRecord,
    dispatch_payload: dict[str, Any],
) -> None:
    if not is_agentic_run_type(record.run_type):
        return
    agent_run_id = str(record.payload.get("agent_run_id") or record.run_id)
    agent = session.get(AgentRun, agent_run_id)
    if agent:
        agent.status = record.status
        agent.payload = {
            **agent.payload,
            "dispatch": dispatch_payload,
            "dispatch_recorded_at": datetime.now(UTC).isoformat(),
        }
    tool_call_id = _scoped_id(
        "tool", record.tenant_id, record.project_id, f"{agent_run_id}:dispatch_agent_run"
    )
    session.merge(
        ToolCall(
            tool_call_id=tool_call_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            status="success",
            trace_id=record.trace_id,
            payload={
                "agent_run_id": agent_run_id,
                "source_run_id": record.run_id,
                "key": "dispatch_agent_run",
                "tool": "dagster.run_request",
                "purpose": "提交智能体运行到执行引擎",
                "dispatch": dispatch_payload,
            },
        )
    )


def record_agent_completion(
    session: Session,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> None:
    if not is_agentic_run_type(record.run_type):
        return
    agent_run_id = str(record.payload.get("agent_run_id") or record.run_id)
    agent = session.get(AgentRun, agent_run_id)
    if agent:
        agent.status = record.status
        agent.payload = {
            **agent.payload,
            "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
            "completed_at": completion_receipt.get("received_at") or datetime.now(UTC).isoformat(),
        }
    receipt_id = str(completion_receipt.get("completion_receipt_id") or record.run_id)
    decision_id = _scoped_id(
        "decision", record.tenant_id, record.project_id, f"{agent_run_id}:{receipt_id}"
    )
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        result_ref = {}
    session.merge(
        AgentDecision(
            decision_id=decision_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            status=record.status,
            trace_id=record.trace_id,
            payload={
                "agent_run_id": agent_run_id,
                "source_run_id": record.run_id,
                "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
                "decision_type": "completion",
                "result": record.status,
                "write_policy": AGENT_WRITE_POLICY,
                "result_ref": result_ref,
                "metrics": completion_receipt.get("metrics") or {},
            },
        )
    )
    if result_ref:
        trace_ref_id = _scoped_id(
            "trace_ref",
            record.tenant_id,
            record.project_id,
            f"{record.trace_id}:{agent_run_id}:result:{receipt_id}",
        )
        session.merge(
            TraceRef(
                trace_ref_id=trace_ref_id,
                tenant_id=record.tenant_id,
                project_id=record.project_id,
                status=record.status,
                trace_id=record.trace_id,
                payload={
                    "agent_run_id": agent_run_id,
                    "source_run_id": record.run_id,
                    "ref_role": "result",
                    "type": "agent_result",
                    "id": receipt_id,
                    "result_ref": result_ref,
                },
            )
        )
