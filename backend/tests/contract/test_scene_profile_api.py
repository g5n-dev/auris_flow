from __future__ import annotations

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import AgentRun, JsonResource, RunRecord, SceneProfileVersion
from app.services.scene_profile_service import materialize_scene_profile_generation_completion


def test_project_scene_profile_optional_read_preserves_unbound_empty_state(client, auth_headers):
    strict = client.get(
        "/api/v1/projects/sales_qa/scene-profile?environment=development",
        headers=auth_headers,
    )
    assert strict.status_code == 404, strict.text
    assert strict.json()["error"]["code"] == "SCENE_PROFILE_BINDING_MISSING"

    optional = client.get(
        "/api/v1/projects/sales_qa/scene-profile?environment=development&allow_missing=true",
        headers=auth_headers,
    )
    assert optional.status_code == 200, optional.text
    assert optional.json()["data"] is None


def _meeting_manifest() -> dict:
    return {
        "schema_version": "scene-profile/1",
        "scene_key": "meeting-minutes-quality",
        "display_name": "会议纪要质检",
        "description": "对多说话人会议的议题、行动项、决策和纪要一致性进行质检。",
        "locales": ["zh-CN"],
        "capabilities": ["audio-intelligence", "speaker-diarization", "labeling", "insight"],
        "roles": [
            {
                "role_key": "moderator",
                "display_name": "主持人",
                "description": "负责推进议程和确认结论",
            },
            {
                "role_key": "participant",
                "display_name": "参会人",
                "description": "参与讨论并承担行动项",
            },
        ],
        "entities": [
            {
                "object_key": "agenda-topic",
                "display_name": "议题",
                "schema_ref": "schema:meeting-topic/v1",
                "required": True,
            }
        ],
        "events": [
            {
                "object_key": "action-item",
                "display_name": "行动项",
                "schema_ref": "schema:meeting-action/v1",
                "required": False,
            }
        ],
        "document_types": [
            {
                "object_key": "meeting-minutes",
                "display_name": "会议纪要",
                "schema_ref": "schema:meeting-minutes/v1",
                "required": True,
            }
        ],
        "data_contract_refs": ["contract:meeting-audio/v1"],
        "task_type_refs": ["task_meeting_quality"],
        "label_version_refs": ["label_meeting_v1"],
        "prompt_version_refs": ["prompt_meeting_v1"],
        "knowledge_index_refs": ["ki_meeting_policy_v1"],
        "eval_dataset_version_refs": ["evalset_meeting_holdout_v1"],
        "metrics": [
            {
                "metric_key": "speaker-attribution-f1",
                "display_name": "说话人归属 F1",
                "unit": "ratio",
                "calculator_ref": "metric:speaker-attribution-f1/v1",
                "evidence_refs": ["speaker-turns", "gold"],
            },
            {
                "metric_key": "action-item-recall",
                "display_name": "行动项召回",
                "unit": "ratio",
                "calculator_ref": "metric:action-item-recall/v1",
                "evidence_refs": ["transcript", "minutes"],
            },
            {
                "metric_key": "minutes-consistency",
                "display_name": "纪要一致率",
                "unit": "ratio",
                "calculator_ref": "metric:minutes-consistency/v1",
                "evidence_refs": ["audio", "minutes"],
            },
        ],
        "release_requirements": [
            {
                "requirement_key": "core-speaker-gate",
                "gate_kind": "core_capability",
                "metric_key": "speaker-attribution-f1",
                "operator": "gte",
                "threshold_ppm": 800000,
            },
            {
                "requirement_key": "scene-action-gate",
                "gate_kind": "scene_eval",
                "metric_key": "action-item-recall",
                "operator": "gte",
                "threshold_ppm": 850000,
            },
            {
                "requirement_key": "project-minutes-gate",
                "gate_kind": "project_holdout",
                "metric_key": "minutes-consistency",
                "operator": "gte",
                "threshold_ppm": 900000,
            },
        ],
        "governance": {
            "human_review_required": True,
            "model_may_publish": False,
            "retention_policy_ref": "policy:retention/meeting-v1",
            "privacy_policy_ref": "policy:privacy/meeting-audio-v1",
        },
    }


def _seed_meeting_dependencies() -> None:
    _seed_manifest_dependencies(_meeting_manifest())


def _seed_manifest_dependencies(manifest: dict) -> None:
    collections = (
        ("task_types", "task_type_refs", "active"),
        ("label_versions", "label_version_refs", "published"),
        ("prompt_versions", "prompt_version_refs", "published"),
        ("knowledge_indexes", "knowledge_index_refs", "active"),
        ("eval_datasets", "eval_dataset_version_refs", "locked"),
    )
    resources = [
        (collection, resource_key, status)
        for collection, field_name, status in collections
        for resource_key in manifest[field_name]
    ]
    resources.extend(
        ("data_contracts", resource_key, "active")
        for resource_key in manifest["data_contract_refs"]
    )
    resources.extend(
        ("schemas", item["schema_ref"], "active")
        for group in ("entities", "events", "document_types")
        for item in manifest[group]
        if item.get("schema_ref")
    )
    resources.extend(
        ("metric_calculators", metric["calculator_ref"], "active") for metric in manifest["metrics"]
    )
    resources.extend(
        [
            (
                "retention_policies",
                manifest["governance"]["retention_policy_ref"],
                "active",
            ),
            (
                "privacy_policies",
                manifest["governance"]["privacy_policy_ref"],
                "active",
            ),
        ]
    )
    with SessionLocal.begin() as session:
        for collection, resource_key, status in dict.fromkeys(resources):
            session.add(
                JsonResource(
                    collection=collection,
                    resource_key=resource_key,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status=status,
                    trace_id="trace_scene_meeting_seed",
                    data={
                        "id": resource_key,
                        "status": status,
                        "source": "scene-profile-contract-test",
                    },
                )
            )


def _insurance_claim_manifest() -> dict:
    return {
        "schema_version": "scene-profile/1",
        "scene_key": "insurance-claim-triage",
        "display_name": "保险理赔分流质检",
        "description": "对报案、查勘、欺诈信号和理赔时效进行证据化分流与质量治理。",
        "locales": ["zh-CN"],
        "capabilities": ["document-understanding", "labeling", "human-review", "insight"],
        "roles": [
            {
                "role_key": "claimant",
                "display_name": "报案人",
                "description": "提交事故事实和损失材料",
            },
            {
                "role_key": "adjuster",
                "display_name": "理赔审核员",
                "description": "核验证据并给出分流意见",
            },
        ],
        "entities": [
            {
                "object_key": "insurance-claim",
                "display_name": "理赔案件",
                "schema_ref": "schema:insurance-claim/v1",
                "required": True,
            },
            {
                "object_key": "insurance-policy",
                "display_name": "保险保单",
                "schema_ref": "schema:insurance-policy/v1",
                "required": True,
            },
        ],
        "events": [
            {
                "object_key": "loss-notice",
                "display_name": "出险报案",
                "schema_ref": "schema:loss-notice/v1",
                "required": True,
            }
        ],
        "document_types": [
            {
                "object_key": "adjustment-report",
                "display_name": "查勘报告",
                "schema_ref": "schema:adjustment-report/v1",
                "required": True,
            }
        ],
        "data_contract_refs": ["contract:insurance-claim/v1"],
        "task_type_refs": ["task_claim_triage"],
        "label_version_refs": ["label_claim_triage_v1"],
        "prompt_version_refs": ["prompt_claim_triage_v1"],
        "knowledge_index_refs": ["ki_claim_policy_v1"],
        "eval_dataset_version_refs": ["evalset_claim_holdout_v1"],
        "metrics": [
            {
                "metric_key": "claim-routing-accuracy",
                "display_name": "案件分流准确率",
                "unit": "ratio",
                "calculator_ref": "metric:claim-routing-accuracy/v1",
                "evidence_refs": ["claim", "human-decision"],
            },
            {
                "metric_key": "fraud-signal-precision",
                "display_name": "欺诈信号精确率",
                "unit": "ratio",
                "calculator_ref": "metric:fraud-signal-precision/v1",
                "evidence_refs": ["claim", "policy", "adjustment-report"],
                "risk_level": "critical",
                "human_review_required": True,
            },
            {
                "metric_key": "settlement-sla-compliance",
                "display_name": "理赔时效达标率",
                "unit": "ratio",
                "calculator_ref": "metric:settlement-sla-compliance/v1",
                "evidence_refs": ["loss-notice", "settlement"],
            },
        ],
        "release_requirements": [
            {
                "requirement_key": "core-routing-gate",
                "gate_kind": "core_capability",
                "metric_key": "claim-routing-accuracy",
                "operator": "gte",
                "threshold_ppm": 850000,
            },
            {
                "requirement_key": "scene-fraud-gate",
                "gate_kind": "scene_eval",
                "metric_key": "fraud-signal-precision",
                "operator": "gte",
                "threshold_ppm": 800000,
            },
            {
                "requirement_key": "project-sla-gate",
                "gate_kind": "project_holdout",
                "metric_key": "settlement-sla-compliance",
                "operator": "gte",
                "threshold_ppm": 900000,
            },
        ],
        "governance": {
            "human_review_required": True,
            "model_may_publish": False,
            "retention_policy_ref": "policy:retention/claim-v1",
            "privacy_policy_ref": "policy:privacy/claim-pii-v1",
        },
    }


def _release_approver_headers(client) -> dict[str, str]:
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "release.approver@auris.local", "password": "auris-demo"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["data"]["access_token"]
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-scene-reviewer",
    }


def test_scene_profile_human_lifecycle_and_project_binding(client, auth_headers):
    _seed_meeting_dependencies()
    create = client.post(
        "/api/v1/scene-profiles",
        headers={**auth_headers, "Idempotency-Key": "scene-meeting-create"},
        json={
            "scene_key": "meeting-minutes-quality",
            "name": "会议纪要质检",
            "description": "会议场景配置契约测试",
            "version": "v1.0.0",
            "source_type": "human",
            "manifest": _meeting_manifest(),
        },
    )
    assert create.status_code == 201, create.text
    data = create.json()["data"]
    profile_id = data["profile"]["scene_profile_id"]
    version_id = data["version"]["scene_profile_version_id"]
    assert data["version"]["status"] == "draft"
    assert data["version"]["manifest"]["scene_key"] == "meeting-minutes-quality"

    validate = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/validations",
        headers={**auth_headers, "Idempotency-Key": "scene-meeting-validate"},
        json={},
    )
    assert validate.status_code == 200, validate.text
    assert validate.json()["data"]["status"] == "validated"
    assert validate.json()["data"]["validation_report"]["status"] == "pass"

    self_review = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/reviews",
        headers={**auth_headers, "Idempotency-Key": "scene-meeting-self-review"},
        json={"decision": "approved", "reason": "创建者试图自审"},
    )
    assert self_review.status_code == 409
    assert self_review.json()["error"]["code"] == "SCENE_PROFILE_SEPARATION_OF_DUTIES"

    reviewer_headers = _release_approver_headers(client)
    review = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/reviews",
        headers={**reviewer_headers, "Idempotency-Key": "scene-meeting-review"},
        json={"decision": "approved", "reason": "会议场景依赖和门禁完整"},
    )
    assert review.status_code == 200, review.text
    assert review.json()["data"]["status"] == "approved"

    publish = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/publish",
        headers={**reviewer_headers, "Idempotency-Key": "scene-meeting-publish"},
        json={"reason": "通过独立复核后发布"},
    )
    assert publish.status_code == 200, publish.text
    published = publish.json()["data"]
    assert published["profile"]["current_published_version_id"] == version_id
    assert published["version"]["status"] == "published"

    current = client.get(
        "/api/v1/projects/sales_qa/scene-profile",
        headers=auth_headers,
    )
    assert current.status_code == 200, current.text
    expected_resource_version = current.json()["data"]["resource_version"]
    bind = client.put(
        "/api/v1/projects/sales_qa/scene-profile",
        headers={**reviewer_headers, "Idempotency-Key": "scene-meeting-bind"},
        json={
            "scene_profile_version_id": version_id,
            "environment": "production",
            "expected_resource_version": expected_resource_version,
        },
    )
    assert bind.status_code == 200, bind.text
    assert bind.json()["data"]["scene_profile_id"] == profile_id
    assert bind.json()["data"]["scene_profile_version_id"] == version_id

    meeting_metric = client.post(
        "/api/v1/insights/metric-runs",
        headers={**auth_headers, "Idempotency-Key": "scene-meeting-insight-run"},
        json={
            "metric_keys": ["action-item-recall"],
            "time_range": "2025-05-01/2025-05-31",
            "source": "scene-profile-genericity-contract",
        },
    )
    assert meeting_metric.status_code == 202, meeting_metric.text
    meeting_run = meeting_metric.json()["data"]
    assert meeting_run["scene_profile_id"] == profile_id
    assert meeting_run["scene_profile_version_id"] == version_id
    assert meeting_run["scene_profile_snapshot_sha256"] == published["version"]["manifest_sha256"]

    automotive_metric = client.post(
        "/api/v1/insights/metric-runs",
        headers={**auth_headers, "Idempotency-Key": "scene-meeting-automotive-reject"},
        json={
            "metric_keys": ["quote_consistency"],
            "time_range": "2025-05-01/2025-05-31",
            "source": "scene-profile-genericity-contract",
        },
    )
    assert automotive_metric.status_code == 422, automotive_metric.text
    assert automotive_metric.json()["error"]["code"] == "INSIGHT_METRIC_UNKNOWN"


def test_insurance_scene_replaces_automotive_runtime_catalog(client, auth_headers):
    manifest = _insurance_claim_manifest()
    _seed_manifest_dependencies(manifest)
    create = client.post(
        "/api/v1/scene-profiles",
        headers={**auth_headers, "Idempotency-Key": "scene-claim-create"},
        json={
            "scene_key": manifest["scene_key"],
            "name": manifest["display_name"],
            "description": manifest["description"],
            "version": "v1.0.0",
            "source_type": "human",
            "manifest": manifest,
        },
    )
    assert create.status_code == 201, create.text
    profile_id = create.json()["data"]["profile"]["scene_profile_id"]
    version_id = create.json()["data"]["version"]["scene_profile_version_id"]

    validate = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/validations",
        headers={**auth_headers, "Idempotency-Key": "scene-claim-validate"},
        json={},
    )
    assert validate.status_code == 200, validate.text
    reviewer_headers = _release_approver_headers(client)
    review = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/reviews",
        headers={**reviewer_headers, "Idempotency-Key": "scene-claim-review"},
        json={"decision": "approved", "reason": "保险理赔依赖和治理门禁完整"},
    )
    assert review.status_code == 200, review.text
    publish = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/publish",
        headers={**reviewer_headers, "Idempotency-Key": "scene-claim-publish"},
        json={"reason": "保险理赔场景通过独立复核"},
    )
    assert publish.status_code == 200, publish.text

    current = client.get("/api/v1/projects/sales_qa/scene-profile", headers=auth_headers)
    assert current.status_code == 200, current.text
    bind = client.put(
        "/api/v1/projects/sales_qa/scene-profile",
        headers={**reviewer_headers, "Idempotency-Key": "scene-claim-bind"},
        json={
            "scene_profile_version_id": version_id,
            "environment": "production",
            "expected_resource_version": current.json()["data"]["resource_version"],
        },
    )
    assert bind.status_code == 200, bind.text

    run = client.post(
        "/api/v1/insights/metric-runs",
        headers={**auth_headers, "Idempotency-Key": "scene-claim-insight-run"},
        json={
            "metric_keys": ["fraud-signal-precision"],
            "time_range": "2025-06-01/2025-06-30",
            "source": "scene-profile-genericity-contract",
        },
    )
    assert run.status_code == 202, run.text
    payload = run.json()["data"]
    assert payload["scene_profile_id"] == profile_id
    assert payload["scene_profile_version_id"] == version_id
    assert payload["metric_definitions"] == [
        {
            "metric_key": "fraud-signal-precision",
            "label": "欺诈信号精确率",
            "unit": "ratio",
            "formula": "metric:fraud-signal-precision/v1",
            "owner": "保险理赔分流质检",
            "calculator_ref": "metric:fraud-signal-precision/v1",
            "metric_family": "general",
            "label_version_applicability": "none",
            "evidence_refs": ["claim", "policy", "adjustment-report"],
            "risk_level": "critical",
            "human_review_required": True,
        }
    ]

    eval_run = client.post(
        "/api/v1/eval-runs",
        headers={**auth_headers, "Idempotency-Key": "scene-claim-eval-run"},
        json={
            "dataset_id": "evalset-insurance-claim-triage-v1",
            "capability": "generic",
        },
    )
    assert eval_run.status_code == 202, eval_run.text
    eval_payload = eval_run.json()["data"]
    assert eval_payload["scene_profile_id"] == profile_id
    assert eval_payload["scene_profile_version_id"] == version_id
    assert eval_payload["scene_profile_snapshot_sha256"] == bind.json()["data"]["manifest_sha256"]
    assert eval_payload["locked_versions"] == {
        "scene_profile_id": profile_id,
        "scene_profile_version_id": version_id,
        "scene_profile_snapshot_sha256": bind.json()["data"]["manifest_sha256"],
    }

    automotive_metric = client.post(
        "/api/v1/insights/metric-runs",
        headers={**auth_headers, "Idempotency-Key": "scene-claim-automotive-reject"},
        json={
            "metric_keys": ["quote_consistency"],
            "time_range": "2025-06-01/2025-06-30",
            "source": "scene-profile-genericity-contract",
        },
    )
    assert automotive_metric.status_code == 422, automotive_metric.text
    assert automotive_metric.json()["error"]["code"] == "INSIGHT_METRIC_UNKNOWN"


def test_model_actor_cannot_validate_or_publish_scene_candidate(client, auth_headers):
    _seed_meeting_dependencies()
    create = client.post(
        "/api/v1/scene-profiles",
        headers={**auth_headers, "Idempotency-Key": "scene-model-guard-create"},
        json={
            "scene_key": "meeting-minutes-quality",
            "name": "会议纪要质检",
            "description": "模型权限边界测试",
            "version": "v1.0.0",
            "manifest": _meeting_manifest(),
        },
    )
    assert create.status_code == 201, create.text
    version_id = create.json()["data"]["version"]["scene_profile_version_id"]
    model_headers = {
        **auth_headers,
        "Authorization": "Bearer model-token",
        "Idempotency-Key": "scene-model-validate-blocked",
    }
    validate = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/validations",
        headers=model_headers,
        json={},
    )
    assert validate.status_code == 403
    assert validate.json()["error"]["code"] == "HUMAN_ACTOR_REQUIRED"

    publish = client.post(
        f"/api/v1/scene-profile-versions/{version_id}/publish",
        headers={**model_headers, "Idempotency-Key": "scene-model-publish-blocked"},
        json={"reason": "模型尝试直接发布场景"},
    )
    assert publish.status_code == 403
    assert publish.json()["error"]["code"] in {"FORBIDDEN", "HUMAN_ACTOR_REQUIRED"}


def test_scene_profile_generation_materializes_candidate_only(client, auth_headers):
    _seed_meeting_dependencies()
    generate = client.post(
        "/api/v1/scene-profile-generation-runs",
        headers={**auth_headers, "Idempotency-Key": "scene-meeting-generate"},
        json={
            "scene_key": "meeting-minutes-quality",
            "name": "会议纪要质检",
            "description": "由模型生成会议场景候选",
            "version": "v2.0.0-rc1",
            "objective": "根据会议音频、纪要 Schema 和评测要求生成可审查的场景配置候选。",
            "model_ref": "model:scene-planner/v1",
            "input_refs": ["schema:meeting-minutes/v1", "evalset_meeting_holdout_v1"],
        },
    )
    assert generate.status_code == 202, generate.text
    run_id = generate.json()["data"]["run_id"]
    with SessionLocal.begin() as session:
        record = session.scalar(select(RunRecord).where(RunRecord.run_id == run_id))
        assert record is not None
        assert record.payload["candidate_only"] is True
        assert record.payload["publish_allowed"] is False
        agent = session.scalar(select(AgentRun).where(AgentRun.agent_run_id == run_id))
        assert agent is not None
        assert "scene_profile_candidate" in agent.payload["write_policy"]["allowed_writes"]
        materialized = materialize_scene_profile_generation_completion(
            session,
            record,
            {
                "completion_receipt_id": "receipt_scene_meeting_generation",
                "result_ref": {"scene_profile_manifest": _meeting_manifest()},
            },
        )
        assert materialized is not None
        assert materialized.status == "candidate"
        assert materialized.source_type == "model"
        assert materialized.published_by is None
        version = session.get(SceneProfileVersion, materialized.scene_profile_version_id)
        assert version is materialized


def test_scene_profile_list_uses_stable_cursor_pagination(client, auth_headers):
    for suffix in ("a", "b"):
        manifest = _meeting_manifest()
        manifest["scene_key"] = f"pagination-scene-{suffix}"
        created = client.post(
            "/api/v1/scene-profiles",
            headers={
                **auth_headers,
                "Idempotency-Key": f"scene-pagination-create-{suffix}",
            },
            json={
                "scene_profile_id": f"scene_pagination_{suffix}",
                "scene_profile_version_id": f"scenev_pagination_{suffix}",
                "scene_key": manifest["scene_key"],
                "name": f"分页场景 {suffix.upper()}",
                "description": "验证场景目录使用稳定的不透明游标。",
                "version": "v1.0.0",
                "manifest": manifest,
            },
        )
        assert created.status_code == 201, created.text

    first = client.get("/api/v1/scene-profiles?limit=1", headers=auth_headers)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["meta"]["total"] >= 2
    assert first_body["meta"]["limit"] == 1
    assert first_body["meta"]["next_cursor"]

    second = client.get(
        f"/api/v1/scene-profiles?limit=1&cursor={first_body['meta']['next_cursor']}",
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert (
        second.json()["data"]["items"][0]["scene_profile_id"]
        != first_body["data"]["items"][0]["scene_profile_id"]
    )

    invalid = client.get("/api/v1/scene-profiles?cursor=invalid", headers=auth_headers)
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"
