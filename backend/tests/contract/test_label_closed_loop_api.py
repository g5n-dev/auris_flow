from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    FeedbackExample,
    HumanReviewTask,
    LabelAggregate,
    LabelAggregationRun,
    LabelExtractionRun,
    LabelFact,
    LabelFactHead,
    LabelObservation,
    LabelVersion,
    OutboxEvent,
    PromptAsset,
    PromptVersion,
    ReleaseBundleHead,
    ReleaseDeployment,
    RunRecord,
)
from app.workers.outbox_worker import process_aggregate_events

LABEL_VERSION_ID = "label_v1_9_0_rc2"
POLICY_VERSION_ID = "lap_contract_v1"


def test_extraction_waits_for_real_completion_receipt_before_materializing_observations(
    client, auth_headers
):
    _seed_extraction_prompt()
    _create_policy(client, auth_headers)
    _seed_extraction_release_head()
    run_id = "lexr_contract_receipt"
    created = client.post(
        "/api/v1/label-extraction-runs",
        json={
            "extraction_run_id": run_id,
            "label_version_id": LABEL_VERSION_ID,
            "prompt_version_id": "prompt-contract-v1",
            "model_version": "label-model-contract-v1",
            "schema_version": "label-observation/1",
            "aggregation_policy_version_id": POLICY_VERSION_ID,
            "subject_scope": "audio-session",
            "subject_refs": [
                {
                    "id": "session-extraction-contract",
                    "evidence_ref": "segment-receipt",
                }
            ],
            "source_bindings": [
                {
                    "source_family": "model-a",
                    "source_type": "model",
                    "provider": "contract-provider",
                    "adapter": "contract-adapter",
                }
            ],
            "input_sha256": _sha({"subject": "session-extraction-contract"}),
            "execution_mode": "production",
        },
        headers={**auth_headers, "Idempotency-Key": "contract-create-extraction-run"},
    )
    assert created.status_code == 202, created.text
    assert created.json()["data"]["status"] == "queued"

    before_receipt = client.get(f"/api/v1/label-extraction-runs/{run_id}", headers=auth_headers)
    assert before_receipt.status_code == 200
    assert before_receipt.json()["data"]["observation_count"] == 0
    assert before_receipt.json()["data"]["status"] == "queued"

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        adapter = run.payload["dispatch"]["adapter"]
        external_id = run.payload["dispatch"]["details"]["external_run_id"]

    observation = _observation_payload(
        "lob_contract_receipt",
        source_family="model-a",
        confidence=0.97,
        evidence_id="segment-receipt",
    )
    observation.pop("extraction_run_id")
    observation.pop("label_version_id")
    observation.pop("model_version")
    observation.pop("prompt_version_id")
    observation.pop("schema_version")
    observation.pop("calibration_version_id")
    observation.pop("calibrated_confidence")
    observation["subject_key"] = "session-extraction-contract"
    observation["input_sha256"] = _sha({"subject": "session-extraction-contract"})
    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": adapter,
            "status": "success",
            "completion_receipt_id": "receipt-label-extraction-contract",
            "external_id": external_id,
            "result_ref": {"observations": [observation]},
        },
        headers={**auth_headers, "Idempotency-Key": "contract-complete-extraction-run"},
    )
    assert completion.status_code == 200, completion.text

    materialized = client.get(
        f"/api/v1/label-extraction-runs/{run_id}", headers=auth_headers
    ).json()["data"]
    assert materialized["status"] == "materialized"
    assert materialized["observation_count"] == 1
    detail = client.get("/api/v1/label-observations/lob_contract_receipt", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["extraction_run_id"] == run_id
    assert detail.json()["data"]["prompt_version_id"] == "prompt-contract-v1"
    assert materialized["aggregation_run_id"]
    assert len(materialized["aggregate_ids"]) == 1


def _sha(value: object) -> str:
    body = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _seed_extraction_prompt() -> None:
    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert label_version is not None
        label_version.status = "published"
        session.add(
            PromptAsset(
                prompt_asset_id="prompt-contract-asset",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                name="contract labeling prompt",
                capability="labeling",
                label_version_id=LABEL_VERSION_ID,
                status="active",
                current_version_id="prompt-contract-v1",
                trace_id="trace-contract-prompt",
                payload={},
            )
        )
        session.add(
            PromptVersion(
                prompt_version_id="prompt-contract-v1",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                prompt_asset_id="prompt-contract-asset",
                version="1.0.0",
                parent_version_id=None,
                label_version_id=LABEL_VERSION_ID,
                schema_version="label-observation/1",
                model_version="label-model-contract-v1",
                status="approved",
                template_json={"system": "contract"},
                output_schema={"type": "object"},
                generation_params={"temperature": 0},
                structured_diff={},
                source_badcase_refs=[],
                content_sha256=_sha({"prompt": "contract"}),
                trace_id="trace-contract-prompt",
            )
        )
        session.commit()


def _seed_extraction_release_head() -> None:
    with SessionLocal() as session:
        bundle_sha256 = _sha({"deployment": "rd_contract_extraction_head"})
        session.add(
            ReleaseDeployment(
                deployment_id="rd_contract_extraction_head",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                environment="production",
                status="completed",
                stage="completed",
                label_version_id=LABEL_VERSION_ID,
                prompt_version_id="prompt-contract-v1",
                model_version="label-model-contract-v1",
                aggregation_policy_version_id=POLICY_VERSION_ID,
                eval_dataset_version_id="evalset-contract-extraction",
                eval_run_id="evalrun-contract-extraction",
                rollback_target_deployment_id=None,
                bundle_sha256=bundle_sha256,
                rollout_percentage=100,
                blocked_reasons=[],
                monitor_metrics={},
                approved_by="u_admin_001",
                trace_id="trace-contract-extraction-head",
                payload={"root_trace_id": "trace-contract-extraction-head"},
            )
        )
        session.flush()
        session.add(
            ReleaseBundleHead(
                release_head_id="rbh_contract_extraction_head",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                environment="production",
                active_deployment_id="rd_contract_extraction_head",
                active_bundle_sha256=bundle_sha256,
                prompt_asset_id="prompt-contract-asset",
                prompt_version_id="prompt-contract-v1",
                label_version_id=LABEL_VERSION_ID,
                model_version="label-model-contract-v1",
                aggregation_policy_version_id=POLICY_VERSION_ID,
                eval_dataset_version_id="evalset-contract-extraction",
                generation=1,
                status="active",
                bootstrapped=True,
                activated_by_command_id=None,
                trace_id="trace-contract-extraction-head",
                payload={"root_trace_id": "trace-contract-extraction-head"},
            )
        )
        session.commit()


def _observation_payload(
    observation_id: str,
    *,
    source_family: str,
    confidence: float,
    evidence_id: str,
    subject_key: str = "session-contract-001",
) -> dict:
    return {
        "observation_id": observation_id,
        "extraction_run_id": "lexr_contract_001",
        "subject_scope": "audio-session",
        "subject_key": subject_key,
        "evidence_ref": {
            "type": "audio-segment",
            "id": evidence_id,
            "sha256": _sha({"evidence_id": evidence_id}),
        },
        "label_version_id": LABEL_VERSION_ID,
        "raw_label": "退款申请",
        "value": True,
        "value_type": "boolean",
        "source_family": source_family,
        "source_type": "model",
        "model_version": "label-model-contract-v1",
        "prompt_version_id": "prompt-contract-v1",
        "schema_version": "label-observation/1",
        "raw_confidence": confidence,
        "calibrated_confidence": confidence,
        "calibration_version_id": "cal-contract-v1",
        "input_sha256": _sha({"subject": subject_key}),
        "output_sha256": _sha({"label": "退款申请", "value": True, "confidence": confidence}),
    }


def _seed_extraction_for_observation(body: dict) -> None:
    with SessionLocal() as session:
        existing = session.get(LabelExtractionRun, body["extraction_run_id"])
        if existing is not None:
            evidence_ref = body["evidence_ref"]["id"]
            if not any(
                item.get("id") == body["subject_key"] and item.get("evidence_ref") == evidence_ref
                for item in existing.subject_refs
            ):
                existing.subject_refs = [
                    *existing.subject_refs,
                    {"id": body["subject_key"], "evidence_ref": evidence_ref},
                ]
                session.commit()
            return
        session.add(
            LabelExtractionRun(
                extraction_run_id=body["extraction_run_id"],
                tenant_id="aurora_auto",
                project_id="sales_qa",
                label_version_id=body["label_version_id"],
                prompt_version_id=body["prompt_version_id"],
                model_version=body["model_version"],
                schema_version=body["schema_version"],
                status="submitted",
                subject_scope=body["subject_scope"],
                subject_refs=[
                    {
                        "id": body["subject_key"],
                        "evidence_ref": body["evidence_ref"]["id"],
                    }
                ],
                input_sha256=body["input_sha256"],
                observation_count=0,
                trace_id="trace-contract-seeded-extraction",
                payload={"trusted_adapter": "test"},
            )
        )
        session.commit()


def _create_policy(client, auth_headers) -> dict:
    response = client.post(
        "/api/v1/label-aggregation-policies",
        json={
            "policy_version_id": POLICY_VERSION_ID,
            "label_version_id": LABEL_VERSION_ID,
            "policy_version": "1.0.0",
            "mode": "l1",
            "status": "active",
            "source_weights": {"model-a": 1.0, "model-b": 1.0},
            "thresholds": {
                "l2_accept_score": 0.95,
                "categorical_margin": 0.15,
                "temporal_iou": 0.6,
                "min_independent_sources": 1,
                "random_audit_rate": 0.05,
            },
            "label_definitions": [
                {
                    "label_id": "refund-request",
                    "canonical_name": "申请退款",
                    "aliases": ["退款申请"],
                    "kind": "boolean",
                    "risk_level": "low",
                    "parent_ids": [],
                }
            ],
        },
        headers={**auth_headers, "Idempotency-Key": "contract-create-aggregation-policy"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_observation_is_immutable_versioned_scoped_audited_and_outboxed(client, auth_headers):
    payload = _observation_payload(
        "lob_contract_a",
        source_family="model-a",
        confidence=0.96,
        evidence_id="segment-a",
    )
    _seed_extraction_for_observation(payload)
    headers = {
        **auth_headers,
        "Authorization": "Bearer system-token",
        "Idempotency-Key": "contract-observation-a",
    }

    created = client.post("/api/v1/label-observations", json=payload, headers=headers)
    replay = client.post("/api/v1/label-observations", json=payload, headers=headers)

    assert created.status_code == 201, created.text
    assert replay.status_code == 201, replay.text
    assert replay.json()["data"] == created.json()["data"]
    data = created.json()["data"]
    assert data["observation_id"] == payload["observation_id"]
    assert data["tenant_id"] == "aurora_auto"
    assert data["project_id"] == "sales_qa"
    assert data["trace_id"] == "trace-contract-seeded-extraction"
    assert data["evidence_ref"] == payload["evidence_ref"]

    changed = client.post(
        "/api/v1/label-observations",
        json={**payload, "raw_confidence": 0.50},
        headers=headers,
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    duplicate_identity = client.post(
        "/api/v1/label-observations",
        json={**payload, "observation_id": payload["observation_id"], "raw_confidence": 0.50},
        headers={**headers, "Idempotency-Key": "contract-observation-a-new-key"},
    )
    assert duplicate_identity.status_code == 409
    assert duplicate_identity.json()["error"]["code"] == "LABEL_OBSERVATION_IMMUTABLE"

    detail = client.get(
        f"/api/v1/label-observations/{payload['observation_id']}", headers=auth_headers
    )
    assert detail.status_code == 200
    assert detail.json()["data"] == data

    with SessionLocal() as session:
        observation = session.get(LabelObservation, payload["observation_id"])
        assert observation is not None
        assert observation.tenant_id == "aurora_auto"
        assert observation.project_id == "sales_qa"
        assert observation.input_sha256 == payload["input_sha256"]
        assert observation.output_sha256 == payload["output_sha256"]
        assert observation.evidence_sha256 == payload["evidence_ref"]["sha256"]
        assert (
            session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "label_observation.created",
                    AuditLog.object_id == payload["observation_id"],
                )
            )
            is not None
        )
        assert (
            session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "label_observation.created",
                    OutboxEvent.aggregate_id == payload["observation_id"],
                )
            )
            is not None
        )


def test_generic_labeling_badcase_uses_shared_badcase_api_without_hotword_fields(
    client, auth_headers
):
    response = client.post(
        "/api/v1/badcases",
        json={
            "badcase_id": "badcase_label_contract",
            "capability": "labeling",
            "failure_reason": "evidence-missing",
            "severity": "high",
            "source_ref": {"type": "label-aggregate", "id": "agg-contract-missing"},
            "evidence_refs": [],
            "label_version_id": LABEL_VERSION_ID,
            "prompt_version_id": "prompt-contract-v1",
            "aggregate_id": "agg-contract-missing",
            "actual_value": True,
        },
        headers={**auth_headers, "Idempotency-Key": "contract-generic-label-badcase"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["capability"] == "labeling"
    assert data["failure_reason"] == "evidence-missing"
    assert data["hotword_pack_version_id"] is None

    listed = client.get("/api/v1/badcases?capability=labeling", headers=auth_headers)
    assert listed.status_code == 200
    items = listed.json()["data"]["items"]
    assert [item["badcase_id"] for item in items] == ["badcase_label_contract"]
    assert items[0]["source_ref"]["id"] == "agg-contract-missing"


def test_l1_aggregation_is_replayable_candidate_scoped_and_human_feedback_becomes_fact(
    client, auth_headers
):
    _create_policy(client, auth_headers)
    observation_ids = ["lob_contract_b1", "lob_contract_b2"]
    for index, (source_family, confidence) in enumerate(
        (("model-a", 0.96), ("model-b", 0.90)), start=1
    ):
        body = _observation_payload(
            observation_ids[index - 1],
            source_family=source_family,
            confidence=confidence,
            evidence_id=f"segment-b{index}",
        )
        _seed_extraction_for_observation(body)
        response = client.post(
            "/api/v1/label-observations",
            json=body,
            headers={
                **auth_headers,
                "Authorization": "Bearer system-token",
                "Idempotency-Key": f"contract-observation-b{index}",
            },
        )
        assert response.status_code == 201, response.text

    request_body = {
        "aggregation_run_id": "lar_contract_001",
        "label_version_id": LABEL_VERSION_ID,
        "policy_version_id": POLICY_VERSION_ID,
        "observation_ids": observation_ids,
        "mode": "l1",
    }
    response = client.post(
        "/api/v1/label-aggregation-runs",
        json=request_body,
        headers={**auth_headers, "Idempotency-Key": "contract-aggregation-run-1"},
    )
    assert response.status_code == 202, response.text
    run = response.json()["data"]
    assert run["status"] == "awaiting-review"
    assert run["observation_count"] == 2
    assert len(run["aggregate_ids"]) == 1

    aggregate_id = run["aggregate_ids"][0]
    detail = client.get(f"/api/v1/label-aggregates/{aggregate_id}", headers=auth_headers)
    assert detail.status_code == 200
    aggregate = detail.json()["data"]
    assert aggregate["label_id"] == "refund-request"
    assert aggregate["value"] is True
    assert aggregate["decision"] == "require-review"
    assert aggregate["review_task_id"]
    assert len(aggregate["deterministic_hash"]) == 64
    assert {member["observation_id"] for member in aggregate["members"]} == set(observation_ids)

    replay_body = {**request_body, "aggregation_run_id": "lar_contract_002"}
    replay = client.post(
        "/api/v1/label-aggregation-runs",
        json=replay_body,
        headers={**auth_headers, "Idempotency-Key": "contract-aggregation-run-2"},
    )
    assert replay.status_code == 202, replay.text
    replay_aggregate = client.get(
        f"/api/v1/label-aggregates/{replay.json()['data']['aggregate_ids'][0]}",
        headers=auth_headers,
    ).json()["data"]
    assert replay_aggregate["deterministic_hash"] == aggregate["deterministic_hash"]

    review_task_id = aggregate["review_task_id"]
    with SessionLocal() as session:
        review_task = session.get(HumanReviewTask, review_task_id)
        strong_aggregate = session.get(LabelAggregate, aggregate_id)
        assert review_task is not None
        assert review_task.payload["target_refs"] == [
            {"type": "label_aggregate", "id": aggregate_id}
        ]
        assert strong_aggregate is not None
        assert strong_aggregate.deterministic_hash == aggregate["deterministic_hash"]

    decision = client.post(
        f"/api/v1/human-review-tasks/{review_task_id}/decisions",
        json={"decision": "accepted", "note": "证据完整，确认退款申请"},
        headers={**auth_headers, "Idempotency-Key": "contract-aggregate-human-decision"},
    )
    assert decision.status_code == 200, decision.text
    decision_id = decision.json()["data"]["decision_id"]

    with SessionLocal() as session:
        feedback = session.scalar(
            select(FeedbackExample).where(FeedbackExample.review_decision_id == decision_id)
        )
        fact = session.scalar(
            select(LabelFact).where(LabelFact.human_review_decision_id == decision_id)
        )
        assert feedback is not None
        assert feedback.feedback_type == "human-confirmed"
        assert feedback.gold_status == "candidate"
        assert feedback.target_type == "label-aggregate"
        assert fact is not None
        assert fact.authority == "human-confirmed"
        assert fact.status == "recorded"
        assert fact.active_slot is None
        assert fact.aggregate_id is None
        assert fact.payload["reviewed_aggregate_id"] == aggregate_id
        fact_head = session.scalar(
            select(LabelFactHead).where(LabelFactHead.current_fact_id == fact.fact_id)
        )
        assert fact_head is not None
        assert fact_head.current_revision == fact.revision == 1
        assert fact.trace_id == aggregate["trace_id"]
        assert decision.json()["data"]["trace_id"] == aggregate["trace_id"]
        assert decision.json()["data"]["action_trace_id"] == decision.json()["meta"]["trace_id"]
        finalized_aggregate = session.get(LabelAggregate, aggregate_id)
        finalized_run = session.get(LabelAggregationRun, "lar_contract_001")
        assert finalized_aggregate is not None
        assert finalized_aggregate.review_task_id is None
        assert finalized_run is not None
        assert finalized_run.status == "completed"
        assert finalized_run.payload["review_task_ids"] == []


def test_low_risk_batch_decisions_are_candidate_scoped_and_return_per_item_results(
    client, auth_headers
):
    _create_policy(client, auth_headers)
    observation_ids = ["lob_batch_a", "lob_batch_b"]
    for index, observation_id in enumerate(observation_ids, start=1):
        body = _observation_payload(
            observation_id,
            source_family=f"model-{index}",
            confidence=0.96,
            evidence_id=f"segment-batch-{index}",
            subject_key=f"session-batch-{index}",
        )
        body["extraction_run_id"] = f"lexr_contract_batch_{index}"
        _seed_extraction_for_observation(body)
        response = client.post(
            "/api/v1/label-observations",
            json=body,
            headers={
                **auth_headers,
                "Authorization": "Bearer system-token",
                "Idempotency-Key": f"contract-batch-observation-{index}",
            },
        )
        assert response.status_code == 201, response.text

    run = client.post(
        "/api/v1/label-aggregation-runs",
        json={
            "aggregation_run_id": "lar_contract_batch",
            "label_version_id": LABEL_VERSION_ID,
            "policy_version_id": POLICY_VERSION_ID,
            "observation_ids": observation_ids,
            "mode": "l1",
        },
        headers={**auth_headers, "Idempotency-Key": "contract-batch-aggregation"},
    )
    assert run.status_code == 202, run.text
    aggregates = [
        client.get(f"/api/v1/label-aggregates/{aggregate_id}", headers=auth_headers).json()["data"]
        for aggregate_id in run.json()["data"]["aggregate_ids"]
    ]
    assert len(aggregates) == 2

    batch = client.post(
        "/api/v1/human-review-decision-batches",
        json={
            "items": [
                {
                    "review_task_id": aggregate["review_task_id"],
                    "decision": "accepted",
                    "note": "同标签同风险批量确认",
                }
                for aggregate in aggregates
            ]
        },
        headers={**auth_headers, "Idempotency-Key": "contract-low-risk-review-batch"},
    )
    assert batch.status_code == 200, batch.text
    data = batch.json()["data"]
    assert data["status"] == "completed", data
    assert data["counts"] == {"success": 2, "skipped": 0, "failed": 0}
    assert data["cohort"] == {
        "label_id": "refund-request",
        "risk_level": "low",
        "policy_version_id": POLICY_VERSION_ID,
    }
    assert all(item["decision_id"] for item in data["results"])

    with SessionLocal() as session:
        decision_ids = [str(item["decision_id"]) for item in data["results"]]
        facts = list(
            session.scalars(
                select(LabelFact).where(LabelFact.human_review_decision_id.in_(decision_ids))
            )
        )
        assert len(facts) == 2
        assert all(
            fact.source_kind == "human-decision"
            and fact.aggregate_id is None
            and fact.status == "recorded"
            and fact.active_slot is None
            for fact in facts
        )
        assert {fact.payload["reviewed_aggregate_id"] for fact in facts} == {
            item["aggregate_id"] for item in aggregates
        }
        fact_heads = list(
            session.scalars(
                select(LabelFactHead).where(
                    LabelFactHead.current_fact_id.in_([fact.fact_id for fact in facts])
                )
            )
        )
        assert len(fact_heads) == 2
