from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    FeedbackExample,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    LabelAggregationPolicyVersion,
    LabelVersion,
    OutboxEvent,
    PromptAsset,
    PromptVersion,
    PromptVersionCandidate,
    RunRecord,
    StorageObject,
)
from app.services.read_policy_service import trace_reference_is_visible

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"


def _headers(
    auth_headers: dict[str, str],
    *,
    token: str,
    key: str,
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _seed_candidate(
    suffix: str,
    *,
    label_version_id: str = "label_v1_8_4",
    source_run_id: str | None = None,
) -> tuple[str, str, str]:
    asset_id = f"pa_prompt_review_{suffix}"
    parent_id = f"pv_prompt_review_parent_{suffix}"
    candidate_id = f"pv_prompt_review_candidate_{suffix}"
    review_task_id = f"hrt_prompt_review_{suffix}"
    trace_id = f"trace_prompt_review_{suffix}"
    candidate_payload = {
        "id": candidate_id,
        "candidate_id": candidate_id,
        "prompt_version_id": candidate_id,
        "prompt_asset_id": asset_id,
        "parent_version_id": parent_id,
        "label_version_id": label_version_id,
        "status": "candidate",
        "structured_diff": {"system": {"op": "replace"}},
        "review_task_id": review_task_id,
        "review_gate": {
            "required": True,
            "mode": "double-blind",
            "required_reviews": 2,
            "requires_adjudication_on_disagreement": True,
        },
        "trace_id": trace_id,
        **({"source_run_id": source_run_id} if source_run_id else {}),
    }
    task_payload = {
        "id": review_task_id,
        "review_task_id": review_task_id,
        "status": "pending",
        "review_status": "pending",
        "queue": "prompt_approval",
        "risk_level": "high",
        "review_mode": "double-blind",
        "required_reviews": 2,
        "target_refs": [{"type": "prompt_version_candidate", "id": candidate_id}],
        "trace_id": trace_id,
    }
    with SessionLocal() as session:
        session.add(
            PromptAsset(
                prompt_asset_id=asset_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name=f"Prompt review {suffix}",
                capability="labeling",
                label_version_id=label_version_id,
                status="active",
                current_version_id=parent_id,
                trace_id=trace_id,
                payload={},
            )
        )
        session.add_all(
            [
                PromptVersion(
                    prompt_version_id=parent_id,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    prompt_asset_id=asset_id,
                    version="1.0.0",
                    parent_version_id=None,
                    label_version_id=label_version_id,
                    schema_version="label-output-v1",
                    model_version="model-v1",
                    status="published",
                    template_json={"system": "parent", "user": "{{input}}"},
                    output_schema={"type": "object"},
                    generation_params={"temperature": 0},
                    structured_diff={},
                    source_badcase_refs=[],
                    content_sha256=_sha(parent_id),
                    trace_id=trace_id,
                ),
                PromptVersion(
                    prompt_version_id=candidate_id,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    prompt_asset_id=asset_id,
                    version="candidate-1",
                    parent_version_id=parent_id,
                    label_version_id=label_version_id,
                    schema_version="label-output-v1",
                    model_version="model-v1",
                    status="candidate",
                    template_json={"system": "candidate", "user": "{{input}}"},
                    output_schema={"type": "object"},
                    generation_params={"temperature": 0},
                    structured_diff={"system": {"op": "replace"}},
                    source_badcase_refs=["badcase-review"],
                    content_sha256=_sha(candidate_id),
                    trace_id=trace_id,
                ),
            ]
        )
        session.add(
            PromptVersionCandidate(
                candidate_id=candidate_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status="candidate",
                trace_id=trace_id,
                payload=candidate_payload,
            )
        )
        session.add(
            HumanReviewTask(
                review_task_id=review_task_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status="pending",
                trace_id=trace_id,
                payload=task_payload,
            )
        )
        session.add_all(
            [
                JsonResource(
                    collection="prompt_version_candidates",
                    resource_key=candidate_id,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="candidate",
                    trace_id=trace_id,
                    data=candidate_payload,
                ),
                JsonResource(
                    collection="human_review_tasks",
                    resource_key=review_task_id,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="pending",
                    trace_id=trace_id,
                    data=task_payload,
                ),
            ]
        )
        session.commit()
    return asset_id, candidate_id, review_task_id


def _submit(client, auth_headers, *, candidate_id: str, token: str, key: str, decision: str):
    return client.post(
        f"/api/v1/prompt-version-candidates/{candidate_id}/review-submissions",
        json={"decision": decision, "note": f"sealed {decision}"},
        headers=_headers(auth_headers, token=token, key=key),
    )


def _seed_evaluation_lock_bundle(
    client,
    auth_headers: dict[str, str],
    *,
    suffix: str,
    label_version_id: str,
    candidate_id: str,
    optimization_run_id: str,
) -> tuple[str, str, str]:
    policy_id = f"agg_prompt_lock_{suffix}"
    dataset_id = f"evalset_prompt_lock_{suffix}"
    storage_id = f"storage_prompt_lock_{suffix}"
    model_version = "model-v1"
    trace_id = f"trace_prompt_lock_{suffix}"
    content_sha256 = _sha(f"manifest:{suffix}")
    object_key = f"tenants/{TENANT_ID}/projects/{PROJECT_ID}/eval/{dataset_id}.jsonl"
    label_payload = {
        "id": label_version_id,
        "label_version_id": label_version_id,
        "status": "draft",
        "resource_version": 1,
        "trace_id": trace_id,
        "root_trace_id": trace_id,
    }
    with SessionLocal() as session:
        session.add(
            LabelVersion(
                label_version_id=label_version_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status="draft",
                resource_version=1,
                trace_id=trace_id,
                payload=label_payload,
            )
        )
        session.add(
            JsonResource(
                collection="label_versions",
                resource_key=label_version_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status="draft",
                trace_id=trace_id,
                data=dict(label_payload),
            )
        )
        session.add(
            LabelAggregationPolicyVersion(
                policy_version_id=policy_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                label_version_id=label_version_id,
                policy_version="1.0.0",
                mode="l1",
                status="active",
                source_weights={"llm": 1.0},
                calibration_versions={},
                thresholds={},
                label_definitions=[{"label_id": "quote_commitment", "kind": "boolean"}],
                canonical_sha256=_sha(f"policy:{suffix}"),
                trace_id=trace_id,
                payload={},
            )
        )
        session.add(
            StorageObject(
                storage_object_id=storage_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                provider="test",
                bucket="auris-test",
                object_key=object_key,
                object_key_sha256=_sha(object_key),
                source_type="eval_dataset_manifest",
                source_id=dataset_id,
                content_type="application/x-ndjson",
                size_bytes=2048,
                content_sha256=content_sha256,
                etag=f'"{content_sha256[:16]}"',
                status="verified",
                trace_id=trace_id,
                payload={"verified": True},
            )
        )
        session.add(
            RunRecord(
                run_id=optimization_run_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_type="label_optimization",
                status="success",
                run_key=f"label-opt:{optimization_run_id}",
                partition_key=f"{TENANT_ID}/{PROJECT_ID}/{label_version_id}",
                trace_id=trace_id,
                payload={
                    "label_version_id": label_version_id,
                    "prompt_version_id": f"pv_prompt_review_parent_{suffix}",
                    "prompt_candidate_ids": [candidate_id],
                    "model_version": model_version,
                    "aggregation_policy_version_id": policy_id,
                    "eval_dataset_version_id": dataset_id,
                    "trigger_hash": _sha(f"trigger:{suffix}"),
                },
            )
        )
        session.commit()

    created = client.post(
        "/api/v1/eval-datasets",
        headers=_headers(
            auth_headers,
            token="dev-token",
            key=f"prompt-lock-dataset-create-{suffix}",
        ),
        json={
            "eval_dataset_id": dataset_id,
            "name": f"Prompt lock dataset {suffix}",
            "capability": "labeling",
            "dataset_version": "1.0.0",
            "manifest_storage_object_id": storage_id,
            "manifest_sha256": content_sha256,
            "sample_count": 120,
            "source": "contract-test",
        },
    )
    assert created.status_code == 201, created.text
    locked = client.post(
        f"/api/v1/eval-datasets/{dataset_id}/lock",
        headers=_headers(
            auth_headers,
            token="dev-token",
            key=f"prompt-lock-dataset-lock-{suffix}",
        ),
        json={"expected_resource_version": 1, "confirmation": "lock"},
    )
    assert locked.status_code == 200, locked.text
    return policy_id, dataset_id, model_version


def test_generic_human_decision_cannot_bypass_prompt_double_blind_review(client, auth_headers):
    _, candidate_id, task_id = _seed_candidate("generic-block")

    response = client.post(
        f"/api/v1/human-review-tasks/{task_id}/decisions",
        json={"decision": "accepted"},
        headers=_headers(auth_headers, token="annotator-token", key="generic-block"),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == (
        "PROMPT_DOUBLE_BLIND_SPECIALIZED_SUBMISSION_REQUIRED"
    )
    with SessionLocal() as session:
        assert session.get(PromptVersion, candidate_id).status == "candidate"


def test_two_distinct_sealed_acceptances_approve_without_publishing(client, auth_headers):
    asset_id, candidate_id, task_id = _seed_candidate("accepted")

    first = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="annotator-token",
        key="prompt-review-accepted-a",
        decision="accepted",
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "in-review"
    assert first.json()["data"]["received_reviews"] == 1
    assert "peer_decision" not in first.json()["data"]
    with SessionLocal() as session:
        first_audit = (
            session.query(AuditLog)
            .filter(AuditLog.action == "prompt_review.submission.created")
            .one()
        )
        assert "decision" not in (first_audit.after_json or {})
        assert "field_diff" not in (first_audit.after_json or {})
        first_event = (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "prompt_review.submission.created")
            .one()
        )
        assert "decision" not in first_event.payload["data"]

    duplicate_reviewer = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="annotator-token",
        key="prompt-review-accepted-a-again",
        decision="accepted",
    )
    assert duplicate_reviewer.status_code == 409
    assert duplicate_reviewer.json()["error"]["code"] == "PROMPT_REVIEWER_ALREADY_SUBMITTED"

    second = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="dev-token",
        key="prompt-review-accepted-b",
        decision="accepted",
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["status"] == "approved"
    assert second.json()["data"]["received_reviews"] == 2

    replay = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="dev-token",
        key="prompt-review-accepted-b",
        decision="accepted",
    )
    assert replay.status_code == 201
    assert replay.json() == second.json()

    with SessionLocal() as session:
        asset = session.get(PromptAsset, asset_id)
        version = session.get(PromptVersion, candidate_id)
        candidate = session.get(PromptVersionCandidate, candidate_id)
        task = session.get(HumanReviewTask, task_id)
        assert asset is not None and asset.current_version_id != candidate_id
        assert version is not None and version.status == "approved"
        assert candidate is not None and candidate.status == "approved"
        assert task is not None and task.status == "success"
        assert (
            session.query(JsonResource)
            .filter(JsonResource.collection == "prompt_review_submissions")
            .count()
            == 2
        )
        assert session.query(HumanReviewDecision).count() == 1
        assert session.query(FeedbackExample).count() == 1
        assert session.query(AuditLog).filter(AuditLog.action.like("prompt_review.%")).count() >= 3
        assert (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type.like("prompt_review.%"))
            .count()
            >= 3
        )


def test_disagreement_requires_distinct_review_arbitrator(client, auth_headers):
    asset_id, candidate_id, _ = _seed_candidate("adjudication")

    first = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="dev-token",
        key="prompt-review-conflict-a",
        decision="accepted",
    )
    assert first.status_code == 201, first.text
    second = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="annotator-b-token",
        key="prompt-review-conflict-b",
        decision="rejected",
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["status"] == "awaiting-adjudication"

    system_forbidden = client.post(
        f"/api/v1/prompt-version-candidates/{candidate_id}/adjudications",
        json={"decision": "accepted", "reason": "system must not replace human"},
        headers=_headers(
            auth_headers,
            token="system-token",
            key="prompt-review-adjudication-system-forbidden",
        ),
    )
    assert system_forbidden.status_code == 403

    forbidden = client.post(
        f"/api/v1/prompt-version-candidates/{candidate_id}/adjudications",
        json={"decision": "accepted", "reason": "admin is not arbitrator"},
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="prompt-review-adjudication-forbidden",
        ),
    )
    assert forbidden.status_code == 403

    resolved = client.post(
        f"/api/v1/prompt-version-candidates/{candidate_id}/adjudications",
        json={"decision": "accepted", "reason": "证据支持候选改进"},
        headers=_headers(
            auth_headers,
            token="annotator-token",
            key="prompt-review-adjudication-accepted",
        ),
    )
    assert resolved.status_code == 201, resolved.text
    assert resolved.json()["data"]["status"] == "approved"

    with SessionLocal() as session:
        asset = session.get(PromptAsset, asset_id)
        version = session.get(PromptVersion, candidate_id)
        adjudications = (
            session.query(JsonResource)
            .filter(JsonResource.collection == "prompt_review_adjudications")
            .all()
        )
        assert asset is not None and asset.current_version_id != candidate_id
        assert version is not None and version.status == "approved"
        assert len(adjudications) == 1
        assert adjudications[0].data["adjudicator_id"] == "u_annotator_001"


def test_matching_modified_reviews_materialize_child_without_approving_parent(client, auth_headers):
    asset_id, candidate_id, _ = _seed_candidate("modified")
    body = {
        "decision": "modified",
        "note": "需要重新生成候选",
        "field_diff": {
            "template": {
                "before": {"system": "candidate", "user": "{{input}}"},
                "after": {"system": "safer candidate", "user": "{{input}}"},
                "reason": "补齐防注入约束",
            },
            "output_schema": {
                "before": {"type": "object"},
                "after": {
                    "type": "object",
                    "required": ["labels"],
                    "properties": {"labels": {"type": "array"}},
                },
                "reason": "强制结构化标签数组",
            },
            "generation_params": {
                "before": {"temperature": 0},
                "after": {"temperature": 0, "top_p": 0.9},
                "reason": "锁定可重放采样参数",
            },
        },
    }
    for token, key in (
        ("annotator-token", "prompt-review-modified-a"),
        ("dev-token", "prompt-review-modified-b"),
    ):
        response = client.post(
            f"/api/v1/prompt-version-candidates/{candidate_id}/review-submissions",
            json=body,
            headers=_headers(auth_headers, token=token, key=key),
        )
        assert response.status_code == 201, response.text

    child_id = response.json()["data"]["child_prompt_version_id"]

    with SessionLocal() as session:
        asset = session.get(PromptAsset, asset_id)
        version = session.get(PromptVersion, candidate_id)
        candidate = session.get(PromptVersionCandidate, candidate_id)
        child = session.get(PromptVersion, child_id)
        child_candidate = session.get(PromptVersionCandidate, child_id)
        submissions = (
            session.query(JsonResource)
            .filter(JsonResource.collection == "prompt_review_submissions")
            .all()
        )
        assert asset is not None and asset.current_version_id != candidate_id
        assert version is not None and version.status == "revision-required"
        assert version.output_schema == {"type": "object"}
        assert version.generation_params == {"temperature": 0}
        assert candidate is not None and candidate.status == "revision-required"
        assert candidate.payload["child_prompt_version_id"] == child_id
        assert child is not None and child.status == "candidate"
        assert child.parent_version_id == candidate_id
        assert child.template_json == {
            "system": "safer candidate",
            "user": "{{input}}",
        }
        assert child.output_schema == body["field_diff"]["output_schema"]["after"]
        assert child.generation_params == body["field_diff"]["generation_params"]["after"]
        assert child.structured_diff == body["field_diff"]
        assert len(child.content_sha256) == 64
        assert child_candidate is not None and child_candidate.status == "candidate"
        child_task_id = child_candidate.payload["review_task_id"]
        child_task = session.get(HumanReviewTask, child_task_id)
        assert child_task is not None and child_task.status == "pending"
        assert child_task.payload["review_mode"] == "double-blind"
        assert child_task.payload["target_refs"] == [
            {"type": "prompt_version_candidate", "id": child_id}
        ]
        assert all(item.data["field_diff"] == body["field_diff"] for item in submissions)
        assert session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "prompt_version_candidate.revision_created",
                OutboxEvent.aggregate_id == child_id,
            )
        )
        assert session.scalar(
            select(AuditLog).where(
                AuditLog.action == "prompt_version.revision_materialized",
                AuditLog.object_id == child_id,
            )
        )


def test_modified_adjudication_materializes_a_fresh_double_blind_child(client, auth_headers):
    _, candidate_id, _ = _seed_candidate("modified-adjudication")
    for decision, token, key in (
        ("accepted", "dev-token", "prompt-modified-adjudication-a"),
        ("rejected", "annotator-b-token", "prompt-modified-adjudication-b"),
    ):
        response = _submit(
            client,
            auth_headers,
            candidate_id=candidate_id,
            token=token,
            key=key,
            decision=decision,
        )
        assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "awaiting-adjudication"

    resolved = client.post(
        f"/api/v1/prompt-version-candidates/{candidate_id}/adjudications",
        json={
            "decision": "modified",
            "reason": "仲裁要求补齐输出字段",
            "field_diff": {
                "output_schema": {
                    "before": {"type": "object"},
                    "after": {"type": "object", "required": ["labels"]},
                    "reason": "标签字段必须存在",
                }
            },
        },
        headers=_headers(
            auth_headers,
            token="annotator-token",
            key="prompt-modified-adjudication-final",
        ),
    )
    assert resolved.status_code == 201, resolved.text
    data = resolved.json()["data"]
    assert data["status"] == "revision-required"
    assert data["next_action"] == "review-child-candidate"
    with SessionLocal() as session:
        child = session.get(PromptVersion, data["child_prompt_version_id"])
        child_candidate = session.get(PromptVersionCandidate, data["child_prompt_version_id"])
        assert child is not None and child.parent_version_id == candidate_id
        assert child.output_schema == {"type": "object", "required": ["labels"]}
        assert child_candidate is not None
        assert session.get(HumanReviewTask, child_candidate.payload["review_task_id"]).status == (
            "pending"
        )


def test_approved_prompt_can_lock_its_label_version_for_evaluation(client, auth_headers):
    suffix = "eval-lock-approved"
    label_version_id = f"label_prompt_lock_{suffix}"
    optimization_run_id = f"run_prompt_lock_{suffix}"
    _, candidate_id, review_task_id = _seed_candidate(
        suffix,
        label_version_id=label_version_id,
        source_run_id=optimization_run_id,
    )
    policy_id, dataset_id, model_version = _seed_evaluation_lock_bundle(
        client,
        auth_headers,
        suffix=suffix,
        label_version_id=label_version_id,
        candidate_id=candidate_id,
        optimization_run_id=optimization_run_id,
    )
    for index, token in enumerate(("annotator-token", "dev-token"), start=1):
        reviewed = _submit(
            client,
            auth_headers,
            candidate_id=candidate_id,
            token=token,
            key=f"prompt-eval-lock-review-{index}",
            decision="accepted",
        )
        assert reviewed.status_code == 201, reviewed.text
    assert reviewed.json()["data"]["status"] == "approved"

    response = client.post(
        f"/api/v1/label-versions/{label_version_id}/evaluation-lock",
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="prompt-eval-lock-approved",
        ),
        json={
            "expected_resource_version": 1,
            "prompt_version_id": candidate_id,
            "model_version": model_version,
            "aggregation_policy_version_id": policy_id,
            "eval_dataset_version_id": dataset_id,
            "optimization_run_id": optimization_run_id,
            "confirmation": "lock-for-evaluation",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["label_version_id"] == label_version_id
    assert data["status"] == "locked"
    assert data["resource_version"] == 2
    assert data["prompt_version_id"] == candidate_id
    assert data["optimization_run_id"] == optimization_run_id
    assert len(data["snapshot_sha256"]) == 64
    assert data["materialized"] is True

    replay_with_fresh_intent = client.post(
        f"/api/v1/label-versions/{label_version_id}/evaluation-lock",
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="prompt-eval-lock-approved-fresh-intent",
        ),
        json={
            "expected_resource_version": 2,
            "prompt_version_id": candidate_id,
            "model_version": model_version,
            "aggregation_policy_version_id": policy_id,
            "eval_dataset_version_id": dataset_id,
            "optimization_run_id": optimization_run_id,
            "confirmation": "lock-for-evaluation",
        },
    )
    assert replay_with_fresh_intent.status_code == 200, replay_with_fresh_intent.text
    replay_data = replay_with_fresh_intent.json()["data"]
    assert replay_data["status"] == "locked"
    assert replay_data["resource_version"] == 2
    assert replay_data["snapshot_sha256"] == data["snapshot_sha256"]
    assert replay_data["materialized"] is False

    with SessionLocal() as session:
        label = session.get(LabelVersion, label_version_id)
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == label_version_id,
            )
        )
        assert label is not None and label.status == "locked"
        assert label.resource_version == 2
        assert label.payload["evaluation_lock"]["snapshot_sha256"] == data["snapshot_sha256"]
        assert projection is not None and projection.status == "locked"
        assert projection.data["resource_version"] == 2
        assert session.scalar(
            select(AuditLog).where(
                AuditLog.action == "label_version.evaluation_locked",
                AuditLog.object_id == label_version_id,
            )
        )
        assert session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "label_version.evaluation_locked",
                OutboxEvent.aggregate_id == label_version_id,
            )
        )
        prompt_review_decision = session.scalar(
            select(HumanReviewDecision).where(
                HumanReviewDecision.review_task_id == review_task_id,
            )
        )
        assert prompt_review_decision is not None
        review_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "human_review.decision.created",
                OutboxEvent.aggregate_id == prompt_review_decision.decision_id,
            )
        )
        assert review_event is not None
        assert review_event.payload["data"]["prompt_version_candidate_id"] == candidate_id
        assert "submission_ids" not in review_event.payload["data"]
        assert "note" not in review_event.payload["data"]
        review_event.payload = {
            **review_event.payload,
            "adapter_dispatch": {
                "adapter": "projection",
                "operation": "record_event",
                "status": "success",
                "details": {
                    "event_type": review_event.event_type,
                    "aggregate_type": review_event.aggregate_type,
                    "aggregate_id": review_event.aggregate_id,
                },
            },
        }
        assert trace_reference_is_visible(
            {
                "type": review_event.aggregate_type,
                "id": review_event.aggregate_id,
                "payload": review_event.payload,
            },
            RequestContext(
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                user_id="demo-user",
                roles=("project_admin",),
                request_id="trace-visibility-test",
                trace_id=review_event.payload["trace_id"],
            ),
            visible_review_task_ids={review_task_id},
            visible_review_decision_ids={prompt_review_decision.decision_id},
        ), review_event.payload


def test_label_evaluation_lock_rejects_prompt_without_two_human_reviews(client, auth_headers):
    suffix = "eval-lock-unreviewed"
    label_version_id = f"label_prompt_lock_{suffix}"
    optimization_run_id = f"run_prompt_lock_{suffix}"
    _, candidate_id, _ = _seed_candidate(
        suffix,
        label_version_id=label_version_id,
        source_run_id=optimization_run_id,
    )
    policy_id, dataset_id, model_version = _seed_evaluation_lock_bundle(
        client,
        auth_headers,
        suffix=suffix,
        label_version_id=label_version_id,
        candidate_id=candidate_id,
        optimization_run_id=optimization_run_id,
    )
    first = _submit(
        client,
        auth_headers,
        candidate_id=candidate_id,
        token="annotator-token",
        key="prompt-eval-lock-only-one-review",
        decision="accepted",
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "in-review"

    response = client.post(
        f"/api/v1/label-versions/{label_version_id}/evaluation-lock",
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="prompt-eval-lock-reject-unreviewed",
        ),
        json={
            "expected_resource_version": 1,
            "prompt_version_id": candidate_id,
            "model_version": model_version,
            "aggregation_policy_version_id": policy_id,
            "eval_dataset_version_id": dataset_id,
            "optimization_run_id": optimization_run_id,
            "confirmation": "lock-for-evaluation",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LABEL_EVALUATION_PROMPT_NOT_APPROVED"
    with SessionLocal() as session:
        label = session.get(LabelVersion, label_version_id)
        assert label is not None and label.status == "draft"
        assert label.resource_version == 1
