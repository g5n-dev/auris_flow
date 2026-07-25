from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from app.services.read_policy_service import RESOURCE_READ_POLICIES

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
RESOURCE_READ_CALLS = {
    "get_resource",
    "list_resource_data",
    "list_resource_page",
    "list_resources",
}

SensitiveSqlAccess = tuple[str, str, str, str]
SENSITIVE_SQL_MODELS = frozenset(
    {
        "AgentDecision",
        "AgentRun",
        "AssetLineageEdge",
        "AssetMaterialization",
        "AuditLog",
        "HumanReviewDecision",
        "HumanReviewTask",
        "OutboxDeliveryAttempt",
        "OutboxEvent",
        "RunRecord",
        "ToolCall",
        "TraceRef",
        "VoiceprintEnrollment",
    }
)
SENSITIVE_SQL_ACCESS_INVENTORY: Counter[SensitiveSqlAccess] = Counter(
    {
        ("api/routers/generic.py", "_scoped_export_record", "get", "RunRecord"): 1,
        ("api/routers/labels.py", "post_label_optimization_runs", "select", "RunRecord"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "AgentDecision"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "AgentRun"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "AssetLineageEdge"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "AssetMaterialization"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "AuditLog"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "HumanReviewDecision"): 2,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "HumanReviewTask"): 2,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "OutboxDeliveryAttempt"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "OutboxEvent"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "RunRecord"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "ToolCall"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "TraceRef"): 1,
        ("api/routers/traces.py", "get_traces_by_trace_id", "select", "VoiceprintEnrollment"): 1,
        ("identity_bootstrap.py", "_validate_evidence", "get", "TraceRef"): 1,
        ("identity_bootstrap.py", "_validate_evidence", "select", "AuditLog"): 1,
        ("identity_bootstrap.py", "bootstrap_identity", "get", "TraceRef"): 1,
        ("identity_bootstrap.py", "bootstrap_identity", "select", "AuditLog"): 1,
        ("qdrant_rebuild.py", "_authority_cutoff", "select", "OutboxEvent"): 1,
        (
            "qdrant_rebuild.py",
            "_load_candidates",
            "select",
            "OutboxDeliveryAttempt",
        ): 1,
        ("qdrant_rebuild.py", "_load_candidates", "select", "OutboxEvent"): 2,
        ("qdrant_rebuild.py", "enqueue_plan", "select", "AuditLog"): 1,
        (
            "qdrant_rebuild.py",
            "verify_plan",
            "select",
            "OutboxDeliveryAttempt",
        ): 1,
        ("qdrant_rebuild.py", "verify_plan", "select", "OutboxEvent"): 1,
        ("repositories/outbox_events.py", "claim_events", "select", "OutboxDeliveryAttempt"): 1,
        ("repositories/outbox_events.py", "claim_events", "select", "OutboxEvent"): 2,
        ("repositories/outbox_events.py", "insert_or_get_event", "select", "OutboxEvent"): 1,
        ("repositories/outbox_events.py", "lock_owned_claim", "select", "OutboxEvent"): 1,
        (
            "repositories/outbox_events.py",
            "owned_claim_trace_carrier",
            "select",
            "OutboxEvent",
        ): 1,
        ("repositories/run_records.py", "get_by_id", "get", "RunRecord"): 1,
        ("repositories/run_records.py", "list", "select", "RunRecord"): 1,
        ("services/agentic_execution_service.py", "record_agent_completion", "get", "AgentRun"): 1,
        ("services/agentic_execution_service.py", "record_agent_dispatch", "get", "AgentRun"): 1,
        (
            "services/audio_review_projection_service.py",
            "persist_voiceprint_enrollment_projection",
            "select",
            "VoiceprintEnrollment",
        ): 1,
        (
            "services/calibration_service.py",
            "_sync_assignment_review_task",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/closed_loop_review_service.py",
            "_review_bundle_for_update",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/data_asset_materialization_service.py",
            "list_asset_lineage_edges",
            "select",
            "AssetLineageEdge",
        ): 1,
        (
            "services/data_asset_materialization_service.py",
            "list_asset_materializations",
            "select",
            "AssetMaterialization",
        ): 1,
        (
            "services/data_asset_materialization_service.py",
            "_hotword_backfill_completion_target",
            "get",
            "AssetMaterialization",
        ): 2,
        (
            "services/data_asset_materialization_service.py",
            "upsert_asset_lineage_edges",
            "get",
            "AssetLineageEdge",
        ): 1,
        (
            "services/eval_binding_service.py",
            "revalidate_labeling_eval_manifest",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/eval_binding_service.py",
            "validate_labeling_eval_binding",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/human_review_service.py",
            "get_human_review_task_for_update",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/hotword_service.py",
            "_verify_eval_run",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/hotword_service.py",
            "publish_hotword_version",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/hotword_service.py",
            "validate_hotword_backfill_binding",
            "get",
            "AssetMaterialization",
        ): 1,
        (
            "services/hotword_rollback_service.py",
            "_assert_no_active_rollback",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/insight_closure_service.py",
            "_invalidate_failed_report_downstream",
            "get",
            "RunRecord",
        ): 1,
        (
            "services/insight_closure_service.py",
            "_load_report_metric_results",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/insight_closure_service.py",
            "create_insight_experiment_retry_attempt",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/knowledge_recall_service.py",
            "qdrant_dispatch_authority",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/knowledge_recall_service.py",
            "recall_from_local_dispatches",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_closed_loop_service.py",
            "get_label_extraction_run",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_fact_backfill_service.py",
            "_project_chains",
            "select",
            "HumanReviewDecision",
        ): 1,
        (
            "services/label_fact_set_service.py",
            "_creation_refs",
            "select",
            "AuditLog",
        ): 1,
        (
            "services/label_fact_set_service.py",
            "_creation_refs",
            "select",
            "OutboxEvent",
        ): 1,
        (
            "services/label_fact_temporal_service.py",
            "_validate_human_source",
            "select",
            "HumanReviewDecision",
        ): 1,
        (
            "services/label_lifecycle_service.py",
            "_environment_references",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_metric_scope_service.py",
            "_source_run",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_recomputation_service.py",
            "_execution_receipt",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_optimization_orchestrator.py",
            "_scope_runs",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_optimization_orchestrator.py",
            "get_trigger_scan_or_run",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_optimization_runtime_service.py",
            "_failed_extraction_rejections",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_optimization_runtime_service.py",
            "_latest_strong_critical_delta_ppm",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/label_policy_service.py",
            "_authoritative_candidate_facts",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/label_policy_service.py",
            "_authoritative_release_facts",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/label_policy_service.py",
            "evaluate_label_candidate",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/quality_appeal_service.py",
            "_appeal_review_task_for_update",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/quality_appeal_service.py",
            "_source_decision_for_update",
            "select",
            "HumanReviewDecision",
        ): 1,
        (
            "services/prompt_candidate_review_service.py",
            "_candidate_bundle_for_update",
            "select",
            "HumanReviewTask",
        ): 1,
        (
            "services/promptfoo_eval_adapter.py",
            "_validate_eval_run_dispatch_binding",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/prompt_release_service.py",
            "_prompt_approval_evidence_blockers",
            "select",
            "HumanReviewDecision",
        ): 1,
        (
            "services/release_gate_service.py",
            "_release_event",
            "select",
            "OutboxEvent",
        ): 1,
        (
            "services/release_gate_service.py",
            "_release_request_binding_reason",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/release_gate_service.py",
            "_release_decision_audit_matches",
            "select",
            "AuditLog",
        ): 1,
        (
            "services/release_gate_service.py",
            "decide_release_gate",
            "select",
            "OutboxEvent",
        ): 1,
        (
            "services/release_gate_service.py",
            "decide_release_gate",
            "select",
            "RunRecord",
        ): 1,
        ("services/run_service.py", "complete_run_from_receipt", "select", "RunRecord"): 1,
        ("services/run_service.py", "get_run", "select", "RunRecord"): 1,
        ("services/run_service.py", "retry_run", "select", "RunRecord"): 1,
        (
            "services/task_run_control_service.py",
            "_active_status_sync_control",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/task_run_control_service.py",
            "_source_task_run",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/task_run_control_service.py",
            "cancel_pending_task_run_dispatch",
            "select",
            "OutboxEvent",
        ): 1,
        (
            "services/task_run_monitor_service.py",
            "_due_candidates",
            "select",
            "RunRecord",
        ): 1,
        (
            "services/task_run_monitor_service.py",
            "_load_control_state",
            "select",
            "RunRecord",
        ): 2,
        (
            "workers/label_optimization_worker.py",
            "_create_eval_runs",
            "select",
            "RunRecord",
        ): 1,
        (
            "workers/label_optimization_worker.py",
            "_create_next_generation",
            "select",
            "RunRecord",
        ): 1,
        (
            "workers/label_optimization_worker.py",
            "_evaluate_round",
            "select",
            "RunRecord",
        ): 1,
        (
            "workers/label_optimization_worker.py",
            "_mark_session_stage",
            "select",
            "RunRecord",
        ): 1,
        (
            "workers/label_optimization_worker.py",
            "_reconcile_active_session",
            "select",
            "RunRecord",
        ): 2,
        (
            "workers/label_optimization_worker.py",
            "process_schedule_once",
            "select",
            "RunRecord",
        ): 1,
        ("workers/outbox_worker.py", "_delivery_attempt", "get", "OutboxDeliveryAttempt"): 1,
        (
            "workers/outbox_worker.py",
            "_previous_attempt_is_exact_dagster_absence",
            "select",
            "OutboxDeliveryAttempt",
        ): 1,
        (
            "workers/outbox_worker.py",
            "_task_run_control_source",
            "select",
            "RunRecord",
        ): 1,
        ("workers/outbox_worker.py", "_run_for_event", "select", "RunRecord"): 1,
    }
)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal_collection(call: ast.Call) -> str | None:
    if len(call.args) >= 3 and isinstance(call.args[2], ast.Constant):
        value = call.args[2].value
        return value if isinstance(value, str) else None
    for keyword in call.keywords:
        if keyword.arg == "collection" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            return value if isinstance(value, str) else None
    return None


def _model_names_in_expression(node: ast.AST) -> set[str]:
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and item.id in SENSITIVE_SQL_MODELS
    } | {
        item.value.id
        for item in ast.walk(node)
        if isinstance(item, ast.Attribute)
        and isinstance(item.value, ast.Name)
        and item.value.id in SENSITIVE_SQL_MODELS
    }


def _direct_sensitive_sql_accesses() -> Counter[SensitiveSqlAccess]:
    accesses: Counter[SensitiveSqlAccess] = Counter()
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            call_name = _call_name(call)
            access_kind: str | None = None
            models: set[str] = set()
            if call_name == "select":
                access_kind = "select"
                for argument in call.args:
                    models.update(_model_names_in_expression(argument))
            elif call_name == "get" and call.args:
                access_kind = "get"
                models.update(_model_names_in_expression(call.args[0]))
            elif call_name == "query":
                access_kind = "query"
                for argument in call.args:
                    models.update(_model_names_in_expression(argument))
            if access_kind is None or not models:
                continue
            owner: ast.AST = call
            while owner in parents and not isinstance(
                owner, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                owner = parents[owner]
            function_name = (
                owner.name
                if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef)
                else "<module>"
            )
            for model in models:
                accesses[(str(path.relative_to(APP_ROOT)), function_name, access_kind, model)] += 1
    return accesses


def test_literal_resource_reads_are_registered() -> None:
    literal_reads: dict[str, set[str]] = {}
    for path in APP_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in RESOURCE_READ_CALLS:
                continue
            collection = _literal_collection(node)
            if collection:
                literal_reads.setdefault(collection, set()).add(
                    f"{path.relative_to(APP_ROOT)}:{node.lineno}"
                )

    unregistered = {
        collection: sorted(locations)
        for collection, locations in literal_reads.items()
        if collection not in RESOURCE_READ_POLICIES
    }
    assert not unregistered, f"资源读取策略未注册：{unregistered}"


def test_direct_sensitive_sql_reads_match_reviewed_application_inventory() -> None:
    actual = _direct_sensitive_sql_accesses()

    assert actual == SENSITIVE_SQL_ACCESS_INVENTORY, (
        "敏感强表直查发生变化，必须逐项完成读取策略评审后更新清单："
        f"新增={actual - SENSITIVE_SQL_ACCESS_INVENTORY}，"
        f"缺失={SENSITIVE_SQL_ACCESS_INVENTORY - actual}"
    )


def test_direct_sensitive_sql_api_inventory_and_trace_guards_are_explicit() -> None:
    router_accesses = Counter(
        {
            access: count
            for access, count in _direct_sensitive_sql_accesses().items()
            if access[0].startswith("api/routers/")
        }
    )
    expected_router_accesses = Counter(
        {
            access: count
            for access, count in SENSITIVE_SQL_ACCESS_INVENTORY.items()
            if access[0].startswith("api/routers/")
        }
    )
    assert router_accesses == expected_router_accesses

    generic_source = (APP_ROOT / "api" / "routers" / "generic.py").read_text(encoding="utf-8")
    for required_scope_check in (
        'record.run_type != "export"',
        "record.tenant_id != ctx.tenant_id",
        "record.project_id != ctx.project_id",
    ):
        assert required_scope_check in generic_source

    path = APP_ROOT / "api" / "routers" / "traces.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "get_traces_by_trace_id"
    )
    called_names = {_call_name(call) for call in ast.walk(function) if isinstance(call, ast.Call)}
    required_guards = {
        "_public_trace_span",
        "require_trace_read",
        "can_read_resource_collection",
        "can_read_human_review_task",
        "trace_reference_is_visible",
        "trace_value_is_visible",
    }
    assert required_guards <= called_names, (
        f"Trace 聚合路由缺少显式读取保护：{sorted(required_guards - called_names)}"
    )

    generic_path = APP_ROOT / "api" / "routers" / "generic.py"
    generic_tree = ast.parse(
        generic_path.read_text(encoding="utf-8"),
        filename=str(generic_path),
    )

    def generic_function(function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
        return next(
            node
            for node in ast.walk(generic_tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        )

    scoped_export_source = ast.unparse(generic_function("_scoped_export_record"))
    for required_scope_check in (
        "record.run_type != 'export'",
        "record.tenant_id != ctx.tenant_id",
        "record.project_id != ctx.project_id",
    ):
        assert required_scope_check in scoped_export_source

    for endpoint_name in (
        "post_exports",
        "get_exports_by_id",
        "get_exports_by_id_download",
        "head_exports_by_id_download",
    ):
        endpoint_calls = {
            _call_name(call)
            for call in ast.walk(generic_function(endpoint_name))
            if isinstance(call, ast.Call)
        }
        assert "_scoped_export_record" in endpoint_calls, (
            f"导出端点 {endpoint_name} 未通过统一 tenant/project/run_type 读取边界"
        )


def test_task_run_control_sensitive_sql_scope_guards_are_explicit() -> None:
    def function_source(relative_path: str, function_name: str) -> str:
        path = APP_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function_name
        )
        return ast.unparse(function)

    source_task_run = function_source("services/task_run_control_service.py", "_source_task_run")
    for required_ctx_scope_check in (
        "RunRecord.tenant_id == ctx.tenant_id",
        "RunRecord.project_id == ctx.project_id",
        "RunRecord.run_type == 'task_run'",
    ):
        assert required_ctx_scope_check in source_task_run

    active_control = function_source(
        "services/task_run_control_service.py", "_active_status_sync_control"
    )
    for required_source_scope_check in (
        "RunRecord.tenant_id == source.tenant_id",
        "RunRecord.project_id == source.project_id",
        "RunRecord.run_key == source.run_id",
        "RunRecord.run_type == 'task_run_status_sync'",
    ):
        assert required_source_scope_check in active_control

    pending_dispatch = function_source(
        "services/task_run_control_service.py", "cancel_pending_task_run_dispatch"
    )
    for required_source_event_scope_check in (
        "OutboxEvent.tenant_id == source.tenant_id",
        "OutboxEvent.project_id == source.project_id",
        "OutboxEvent.aggregate_id == source.run_id",
        "OutboxEvent.aggregate_type == 'task_run'",
    ):
        assert required_source_event_scope_check in pending_dispatch

    control_state = function_source("services/task_run_monitor_service.py", "_load_control_state")
    for required_bulk_scope_guard in (
        "RunRecord.run_id.in_(planned_control_ids)",
        "tuple_(RunRecord.tenant_id, RunRecord.project_id, RunRecord.run_key).in_(source_keys)",
    ):
        assert required_bulk_scope_guard in control_state

    due_candidates = function_source("services/task_run_monitor_service.py", "_due_candidates")
    for required_global_monitor_guard in (
        "RunRecord.run_type == 'task_run'",
        "or_(deadline_due, status_sync_due)",
        "with_for_update(skip_locked=True)",
    ):
        assert required_global_monitor_guard in due_candidates

    worker_source = function_source("workers/outbox_worker.py", "_task_run_control_source")
    for required_event_scope_check in (
        "RunRecord.tenant_id == event.tenant_id",
        "RunRecord.project_id == event.project_id",
        "RunRecord.run_id == source_run_id",
        "RunRecord.run_type == 'task_run'",
    ):
        assert required_event_scope_check in worker_source
