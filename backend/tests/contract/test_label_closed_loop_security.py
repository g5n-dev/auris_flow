from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    FeedbackExample,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    LabelAggregate,
    LabelAggregationRun,
    LabelExtractionRun,
    LabelFact,
    LabelNode,
    LabelTaxonomySuggestion,
    LabelVersion,
    LabelVersionItem,
    OutboxEvent,
)
from app.services.label_closed_loop_service import _create_label_fact

LABEL_VERSION_ID = "label_v1_9_0_rc2"
PROMPT_VERSION_ID = "prompt-security-v1"
MODEL_VERSION = "label-model-security-v1"
SCHEMA_VERSION = "label-observation/1"
EXTRACTION_RUN_ID = "lexr_security_001"
SUBJECT_KEY = "session-security-001"


def _service_context(trace_id: str) -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id=f"request-{trace_id}",
        trace_id=trace_id,
        idempotency_key=f"idem-{trace_id}",
    )


def _sha(value: object) -> str:
    body = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _headers(
    auth_headers: dict[str, str],
    key: str,
    *,
    token: str = "system-token",
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _seed_extraction_run() -> None:
    with SessionLocal() as session:
        session.add(
            LabelExtractionRun(
                extraction_run_id=EXTRACTION_RUN_ID,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                label_version_id=LABEL_VERSION_ID,
                prompt_version_id=PROMPT_VERSION_ID,
                model_version=MODEL_VERSION,
                schema_version=SCHEMA_VERSION,
                status="submitted",
                subject_scope="audio-session",
                subject_refs=[{"id": SUBJECT_KEY}],
                input_sha256=_sha({"subject": SUBJECT_KEY}),
                observation_count=0,
                trace_id="trace-security-extraction",
                payload={"trusted_adapter": "dagster"},
            )
        )
        session.commit()


def _observation(
    observation_id: str,
    *,
    raw_label: str = "退款申请",
    source_family: str = "model-a",
    value: bool = True,
) -> dict[str, object]:
    return {
        "observation_id": observation_id,
        "extraction_run_id": EXTRACTION_RUN_ID,
        "subject_scope": "audio-session",
        "subject_key": SUBJECT_KEY,
        "evidence_ref": {
            "type": "audio-segment",
            "id": f"segment-{observation_id}",
            "sha256": _sha({"evidence": observation_id}),
        },
        "label_version_id": LABEL_VERSION_ID,
        "raw_label": raw_label,
        "value": value,
        "value_type": "boolean",
        "source_family": source_family,
        "source_type": "model",
        "model_version": MODEL_VERSION,
        "prompt_version_id": PROMPT_VERSION_ID,
        "schema_version": SCHEMA_VERSION,
        "raw_confidence": 0.96,
        "input_sha256": _sha({"subject": SUBJECT_KEY}),
        "output_sha256": _sha(
            {"raw_label": raw_label, "source_family": source_family, "value": value}
        ),
    }


def _create_policy(
    client,
    auth_headers: dict[str, str],
    *,
    policy_id: str,
    risk_level: str,
) -> None:
    response = client.post(
        "/api/v1/label-aggregation-policies",
        json={
            "policy_version_id": policy_id,
            "label_version_id": LABEL_VERSION_ID,
            "policy_version": policy_id,
            "mode": "l1",
            "status": "active",
            "source_weights": {"model-a": 1.0, "model-b": 1.0},
            "thresholds": {
                "l2_accept_score": 0.95,
                "categorical_margin": 0.15,
                "temporal_iou": 0.6,
                "min_independent_sources": 2,
                "random_audit_rate": 0.05,
            },
            "label_definitions": [
                {
                    "label_id": "refund-request",
                    "canonical_name": "申请退款",
                    "aliases": ["退款申请"],
                    "kind": "boolean",
                    "risk_level": risk_level,
                    "parent_ids": [],
                }
            ],
        },
        headers=_headers(auth_headers, f"create-{policy_id}", token="dev-token"),
    )
    assert response.status_code == 201, response.text


def _create_observations(
    client, auth_headers: dict[str, str], *, unknown: bool = False
) -> list[str]:
    ids = ["lob_security_a", "lob_security_b"]
    for index, observation_id in enumerate(ids):
        response = client.post(
            "/api/v1/label-observations",
            json=_observation(
                observation_id,
                raw_label="未知退款话术" if unknown else "退款申请",
                source_family=f"model-{'a' if index == 0 else 'b'}",
            ),
            headers=_headers(auth_headers, f"create-{observation_id}"),
        )
        assert response.status_code == 201, response.text
    return ids


def _aggregate(
    client,
    auth_headers: dict[str, str],
    *,
    policy_id: str,
    observation_ids: list[str],
    run_id: str,
) -> dict:
    response = client.post(
        "/api/v1/label-aggregation-runs",
        json={
            "aggregation_run_id": run_id,
            "label_version_id": LABEL_VERSION_ID,
            "policy_version_id": policy_id,
            "observation_ids": observation_ids,
            "mode": "l1",
        },
        headers=_headers(auth_headers, f"create-{run_id}", token="dev-token"),
    )
    assert response.status_code == 202, response.text
    return response.json()["data"]


def test_observation_write_requires_system_and_locked_extraction_binding(client, auth_headers):
    _seed_extraction_run()
    body = _observation("lob_security_binding")

    human = client.post(
        "/api/v1/label-observations",
        json=body,
        headers=_headers(auth_headers, "human-observation", token="dev-token"),
    )
    assert human.status_code == 403
    assert human.json()["error"]["code"] == "LABEL_OBSERVATION_TRUSTED_WRITER_REQUIRED"

    human_source = client.post(
        "/api/v1/label-observations",
        json={**body, "source_type": "human-confirmed"},
        headers=_headers(auth_headers, "human-source-observation"),
    )
    assert human_source.status_code == 422

    for field, value in (
        ("prompt_version_id", "prompt-attacker-v1"),
        ("model_version", "model-attacker-v1"),
        ("schema_version", "schema-attacker-v1"),
        ("label_version_id", "label_v1_8_0"),
        ("subject_key", "session-outside-locked-run"),
        ("input_sha256", "0" * 64),
    ):
        response = client.post(
            "/api/v1/label-observations",
            json={**body, "observation_id": f"lob_mismatch_{field}", field: value},
            headers=_headers(auth_headers, f"mismatch-{field}"),
        )
        assert response.status_code == 409, (field, response.text)
        assert response.json()["error"]["code"] == "LABEL_OBSERVATION_RUN_BINDING_MISMATCH"

    accepted = client.post(
        "/api/v1/label-observations",
        json=body,
        headers=_headers(auth_headers, "system-observation"),
    )
    assert accepted.status_code == 201, accepted.text


def test_l2_policy_rejects_single_source_and_unlocked_calibration(client, auth_headers):
    single_source = client.post(
        "/api/v1/label-aggregation-policies",
        json={
            "policy_version_id": "lap_l2_single_source",
            "label_version_id": LABEL_VERSION_ID,
            "policy_version": "1.0.0",
            "mode": "l2",
            "status": "active",
            "source_weights": {"model-a": 1.0},
            "calibration_versions": {"model-a": "client-claimed-stable"},
            "thresholds": {"min_independent_sources": 1},
            "label_definitions": [
                {
                    "label_id": "refund-request",
                    "canonical_name": "申请退款",
                    "aliases": ["退款申请"],
                    "kind": "boolean",
                    "risk_level": "low",
                }
            ],
        },
        headers=_headers(auth_headers, "l2-single-source", token="dev-token"),
    )
    assert single_source.status_code == 422
    audit_disabled = client.post(
        "/api/v1/label-aggregation-policies",
        json={
            "policy_version_id": "lap_l2_audit_disabled",
            "label_version_id": LABEL_VERSION_ID,
            "policy_version": "1.0.0-audit-disabled",
            "mode": "l2",
            "status": "active",
            "source_weights": {"model-a": 1.0, "model-b": 1.0},
            "calibration_versions": {
                "model-a": "client-claimed-stable-a",
                "model-b": "client-claimed-stable-b",
            },
            "thresholds": {
                "min_independent_sources": 2,
                "random_audit_rate": 0.0,
            },
            "label_definitions": [
                {
                    "label_id": "refund-request",
                    "canonical_name": "申请退款",
                    "aliases": ["退款申请"],
                    "kind": "boolean",
                    "risk_level": "low",
                }
            ],
        },
        headers=_headers(auth_headers, "l2-audit-disabled", token="dev-token"),
    )
    assert audit_disabled.status_code == 422
    unlocked = client.post(
        "/api/v1/label-aggregation-policies",
        json={
            "policy_version_id": "lap_l2_unlocked_calibration",
            "label_version_id": LABEL_VERSION_ID,
            "policy_version": "1.0.1",
            "mode": "l2",
            "status": "active",
            "source_weights": {"model-a": 1.0, "model-b": 1.0},
            "calibration_versions": {
                "model-a": "client-claimed-stable-a",
                "model-b": "client-claimed-stable-b",
            },
            "thresholds": {"min_independent_sources": 2},
            "label_definitions": [
                {
                    "label_id": "refund-request",
                    "canonical_name": "申请退款",
                    "aliases": ["退款申请"],
                    "kind": "boolean",
                    "risk_level": "low",
                }
            ],
        },
        headers=_headers(auth_headers, "l2-unlocked-calibration", token="dev-token"),
    )
    assert unlocked.status_code == 409
    assert unlocked.json()["error"]["code"] == "L2_CALIBRATION_NOT_SERVER_LOCKED"


def test_l2_fact_can_never_supersede_active_human_fact(client) -> None:
    del client  # initialize the isolated test database
    with SessionLocal() as session:
        session.add(
            LabelVersionItem(
                label_version_item_id="lvi-human-authority-refund",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                label_version_id=LABEL_VERSION_ID,
                label_id="refund-request",
                canonical_name="申请退款",
                aliases=[],
                value_type="boolean",
                risk_level="low",
                mutual_exclusion_group=None,
                parent_ids=[],
                aggregation_rule={"mode": "presence"},
                status="active",
                definition_sha256=None,
                trace_id="trace-human-authority",
            )
        )
        human_aggregate = LabelAggregate(
            aggregate_id="lagg_human_authority",
            tenant_id="aurora_auto",
            project_id="sales_qa",
            aggregation_run_id="lagr_human_authority",
            label_version_id=LABEL_VERSION_ID,
            policy_version_id="lap_human_authority",
            calibration_version_ids=[],
            subject_scope="audio-session",
            subject_key="session-authority-001",
            label_id="refund-request",
            value_type="boolean",
            value_json=True,
            score=1.0,
            margin=1.0,
            risk_level="low",
            decision="require_review",
            status="accepted",
            reason_codes=[],
            explanation={},
            bucket_sha256="1" * 64,
            deterministic_hash="2" * 64,
            review_task_id="review-human-authority",
            trace_id="trace-human-authority",
        )
        l2_aggregate = LabelAggregate(
            aggregate_id="lagg_l2_authority",
            tenant_id="aurora_auto",
            project_id="sales_qa",
            aggregation_run_id="lagr_l2_authority",
            label_version_id=LABEL_VERSION_ID,
            policy_version_id="lap_l2_authority",
            calibration_version_ids=[],
            subject_scope="audio-session",
            subject_key="session-authority-001",
            label_id="refund-request",
            value_type="boolean",
            value_json=False,
            score=0.999,
            margin=0.99,
            risk_level="low",
            decision="auto_accept",
            status="accepted",
            reason_codes=[],
            explanation={},
            bucket_sha256="3" * 64,
            deterministic_hash="4" * 64,
            review_task_id=None,
            trace_id="trace-l2-authority",
        )
        session.add_all(
            [
                human_aggregate,
                l2_aggregate,
                HumanReviewTask(
                    review_task_id="review-human-authority",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id="trace-human-authority",
                    payload={},
                ),
            ]
        )
        session.flush()
        session.add(
            HumanReviewDecision(
                decision_id="decision-human-authority",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                review_task_id="review-human-authority",
                terminal_review_task_id="review-human-authority",
                status="success",
                trace_id="trace-human-authority",
                payload={
                    "decision": "accepted",
                    "affected_objects": [
                        {"type": "label_aggregate", "id": human_aggregate.aggregate_id}
                    ],
                    "after_json": {
                        "targets": {
                            f"label_aggregates:{human_aggregate.aggregate_id}": {"value": True}
                        }
                    },
                },
            )
        )
        session.flush()
        human_fact = _create_label_fact(
            session,
            _service_context("trace-human-authority"),
            human_aggregate,
            authority="human-confirmed",
            review_decision_id="decision-human-authority",
        )
        session.flush()
        retained = _create_label_fact(
            session,
            _service_context("trace-l2-authority"),
            l2_aggregate,
            authority="l2-auto-accepted",
            review_decision_id=None,
        )
        session.flush()

        assert retained.fact_id == human_fact.fact_id
        assert human_fact.status == "active"
        assert human_fact.authority == "human-confirmed"
        assert human_fact.value_json is True
        assert session.query(LabelFact).count() == 1


def test_generic_review_create_cannot_forge_closed_loop_target(client, auth_headers):
    response = client.post(
        "/api/v1/human-review-tasks",
        json={
            "id": "hrt_forged_closed_loop",
            "queue": "high_risk",
            "target_refs": [{"type": "label_aggregate", "id": "agg_forged"}],
        },
        headers=_headers(auth_headers, "forge-closed-loop-task", token="dev-token"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CLOSED_LOOP_SPECIALIZED_TASK_REQUIRED"


def test_high_risk_aggregate_requires_two_independent_reviews_and_promotes_gold(
    client, auth_headers
):
    _seed_extraction_run()
    _create_policy(
        client,
        auth_headers,
        policy_id="lap_security_high_risk",
        risk_level="high",
    )
    run = _aggregate(
        client,
        auth_headers,
        policy_id="lap_security_high_risk",
        observation_ids=_create_observations(client, auth_headers),
        run_id="lar_security_high_risk",
    )
    aggregate_id = run["aggregate_ids"][0]
    aggregate = client.get(f"/api/v1/label-aggregates/{aggregate_id}", headers=auth_headers).json()[
        "data"
    ]
    task_id = aggregate["review_task_id"]

    bypass = client.post(
        f"/api/v1/human-review-tasks/{task_id}/decisions",
        json={"decision": "accepted"},
        headers=_headers(auth_headers, "bypass-high-risk", token="dev-token"),
    )
    assert bypass.status_code == 409
    assert bypass.json()["error"]["code"] == "CLOSED_LOOP_DOUBLE_BLIND_REQUIRED"

    with SessionLocal() as session:
        task_resource = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == task_id,
            )
        )
        assert task_resource is not None
        task_resource.data = {
            **task_resource.data,
            "target_refs": [
                {"type": "label_aggregate", "id": aggregate_id},
                {"type": "label_aggregate", "id": "agg_not_bound"},
            ],
        }
        session.commit()
    forged_binding = client.post(
        f"/api/v1/label-aggregates/{aggregate_id}/review-submissions",
        json={"decision": "accepted"},
        headers=_headers(auth_headers, "forged-high-risk-binding", token="annotator-token"),
    )
    assert forged_binding.status_code == 409
    assert forged_binding.json()["error"]["code"] == "CLOSED_LOOP_REVIEW_TASK_BINDING_INVALID"
    with SessionLocal() as session:
        task_resource = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == task_id,
            )
        )
        assert task_resource is not None
        task_resource.data = {
            **task_resource.data,
            "target_refs": [{"type": "label_aggregate", "id": aggregate_id}],
        }
        session.commit()

    first = client.post(
        f"/api/v1/label-aggregates/{aggregate_id}/review-submissions",
        json={"decision": "accepted", "note": "证据一致"},
        headers=_headers(auth_headers, "high-risk-review-a", token="annotator-token"),
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["status"] == "in-review"

    duplicate_reviewer = client.post(
        f"/api/v1/label-aggregates/{aggregate_id}/review-submissions",
        json={"decision": "accepted"},
        headers=_headers(auth_headers, "high-risk-review-a-duplicate", token="annotator-token"),
    )
    assert duplicate_reviewer.status_code == 409

    second = client.post(
        f"/api/v1/label-aggregates/{aggregate_id}/review-submissions",
        json={"decision": "accepted", "note": "独立复核一致"},
        headers=_headers(auth_headers, "high-risk-review-b", token="annotator-b-token"),
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["status"] == "accepted"

    with SessionLocal() as session:
        strong = session.get(LabelAggregate, aggregate_id)
        aggregation_run = session.get(LabelAggregationRun, "lar_security_high_risk")
        feedback = session.scalar(
            select(FeedbackExample).where(FeedbackExample.target_id == aggregate_id)
        )
        assert strong is not None
        assert strong.status == "accepted"
        assert strong.review_task_id is None
        assert aggregation_run is not None
        assert aggregation_run.status == "completed"
        assert aggregation_run.payload["review_task_ids"] == []
        assert feedback is not None
        assert feedback.gold_status == "gold"
        assert session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "feedback_example.gold_promoted",
                OutboxEvent.aggregate_id == feedback.feedback_example_id,
            )
        )


def test_taxonomy_disagreement_requires_independent_adjudication_and_syncs_target(
    client, auth_headers
):
    _seed_extraction_run()
    _create_policy(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy",
        risk_level="low",
    )
    run = _aggregate(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy",
        observation_ids=_create_observations(client, auth_headers, unknown=True),
        run_id="lar_security_taxonomy",
    )
    suggestion_id = run["taxonomy_suggestion_ids"][0]

    first = client.post(
        f"/api/v1/label-taxonomy-suggestions/{suggestion_id}/review-submissions",
        json={
            "decision": "accepted",
            "taxonomy_action": "alias",
            "canonical_target_label_id": "refund-request",
        },
        headers=_headers(auth_headers, "taxonomy-review-a", token="annotator-token"),
    )
    assert first.status_code == 201, first.text

    second = client.post(
        f"/api/v1/label-taxonomy-suggestions/{suggestion_id}/review-submissions",
        json={"decision": "rejected", "taxonomy_action": "reject"},
        headers=_headers(auth_headers, "taxonomy-review-b", token="annotator-b-token"),
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["status"] == "awaiting-adjudication"

    reviewer_cannot_adjudicate = client.post(
        f"/api/v1/label-taxonomy-suggestions/{suggestion_id}/adjudications",
        json={
            "decision": "accepted",
            "taxonomy_action": "alias",
            "canonical_target_label_id": "refund-request",
            "reason": "最终裁决",
        },
        headers=_headers(auth_headers, "taxonomy-self-adjudication", token="annotator-token"),
    )
    assert reviewer_cannot_adjudicate.status_code == 409

    adjudicated = client.post(
        f"/api/v1/label-taxonomy-suggestions/{suggestion_id}/adjudications",
        json={
            "decision": "accepted",
            "taxonomy_action": "alias",
            "canonical_target_label_id": "refund-request",
            "reason": "两名审核人意见冲突，按锁定标签定义归为 alias",
        },
        headers=_headers(auth_headers, "taxonomy-adjudication", token="dev-token"),
    )
    assert adjudicated.status_code == 201, adjudicated.text
    assert adjudicated.json()["data"]["status"] == "accepted"
    candidate_label_version_id = adjudicated.json()["data"]["candidate_label_version_id"]
    projection = client.get(
        "/api/v1/label-taxonomy-suggestions?status=accepted",
        headers=_headers(auth_headers, "taxonomy-list", token="dev-token"),
    )
    assert projection.status_code == 200, projection.text
    assert projection.json()["data"]["items"][0]["candidate_label_version_id"] == (
        candidate_label_version_id
    )

    with SessionLocal() as session:
        suggestion = session.get(LabelTaxonomySuggestion, suggestion_id)
        aggregation_run = session.get(LabelAggregationRun, "lar_security_taxonomy")
        feedback = session.scalar(
            select(FeedbackExample).where(FeedbackExample.target_id == suggestion_id)
        )
        assert suggestion is not None
        assert suggestion.status == "accepted"
        assert suggestion.proposed_action == "alias"
        assert suggestion.canonical_target_label_id == "refund-request"
        assert suggestion.review_task_id is None
        candidate_label_version_id = suggestion.payload["candidate_label_version_id"]
        candidate_version = session.get(LabelVersion, candidate_label_version_id)
        candidate_item = session.scalar(
            select(LabelVersionItem).where(
                LabelVersionItem.label_version_id == candidate_label_version_id,
                LabelVersionItem.label_id == "refund-request",
            )
        )
        assert candidate_version is not None
        assert candidate_version.status == "draft"
        assert candidate_version.payload["parent_label_version_id"] == LABEL_VERSION_ID
        assert candidate_version.payload["source_taxonomy_suggestion_id"] == suggestion_id
        assert candidate_version.payload["change_set"]["action"] == "alias"
        assert candidate_item is not None
        assert "未知退款话术" in candidate_item.aliases
        candidate_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "label_version.taxonomy_candidate_created",
                OutboxEvent.aggregate_id == candidate_label_version_id,
            )
        )
        suggestion_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "label_taxonomy_suggestion.review_completed",
                OutboxEvent.aggregate_id == suggestion_id,
            )
        )
        assert candidate_event is not None
        assert candidate_event.payload["data"]["source_taxonomy_suggestion_id"] == suggestion_id
        assert suggestion_event is not None
        assert suggestion_event.payload["data"]["candidate_label_version_id"] == (
            candidate_label_version_id
        )
        assert aggregation_run is not None
        assert aggregation_run.status == "completed"
        assert aggregation_run.payload["review_task_ids"] == []
        assert feedback is not None
        assert feedback.gold_status == "gold"

    replay = _aggregate(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy",
        observation_ids=["lob_security_a", "lob_security_b"],
        run_id="lar_security_taxonomy_replay",
    )
    assert replay["status"] == "completed"
    assert replay["review_task_ids"] == []
    assert "None" not in replay["review_task_ids"]


def test_taxonomy_create_materializes_stable_high_risk_candidate_item(client, auth_headers):
    _seed_extraction_run()
    _create_policy(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy_create",
        risk_level="low",
    )
    run = _aggregate(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy_create",
        observation_ids=_create_observations(client, auth_headers, unknown=True),
        run_id="lar_security_taxonomy_create",
    )
    suggestion_id = run["taxonomy_suggestion_ids"][0]

    for token, key in (
        ("annotator-token", "taxonomy-create-a"),
        ("annotator-b-token", "taxonomy-create-b"),
    ):
        resolved = client.post(
            f"/api/v1/label-taxonomy-suggestions/{suggestion_id}/review-submissions",
            json={"decision": "accepted", "taxonomy_action": "create"},
            headers=_headers(auth_headers, key, token=token),
        )
        assert resolved.status_code == 201, resolved.text

    data = resolved.json()["data"]
    assert data["status"] == "accepted"
    assert data["candidate_label_version_id"]
    with SessionLocal() as session:
        suggestion = session.get(LabelTaxonomySuggestion, suggestion_id)
        assert suggestion is not None
        candidate_id = suggestion.payload["candidate_label_version_id"]
        created_label_id = suggestion.payload["created_label_id"]
        node = session.scalar(
            select(LabelNode).where(
                LabelNode.tenant_id == "aurora_auto",
                LabelNode.project_id == "sales_qa",
                LabelNode.label_id == created_label_id,
            )
        )
        item = session.scalar(
            select(LabelVersionItem).where(
                LabelVersionItem.label_version_id == candidate_id,
                LabelVersionItem.label_id == created_label_id,
            )
        )
        assert node is not None and node.canonical_name == "未知退款话术"
        assert item is not None
        assert item.risk_level == "high"
        assert item.status == "pending-configuration"
        assert item.aggregation_rule["blockers"]


def test_taxonomy_split_materializes_blocked_candidate_change_set(client, auth_headers):
    _seed_extraction_run()
    _create_policy(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy_split",
        risk_level="low",
    )
    run = _aggregate(
        client,
        auth_headers,
        policy_id="lap_security_taxonomy_split",
        observation_ids=_create_observations(client, auth_headers, unknown=True),
        run_id="lar_security_taxonomy_split",
    )
    suggestion_id = run["taxonomy_suggestion_ids"][0]
    for token, key in (
        ("annotator-token", "taxonomy-split-a"),
        ("annotator-b-token", "taxonomy-split-b"),
    ):
        resolved = client.post(
            f"/api/v1/label-taxonomy-suggestions/{suggestion_id}/review-submissions",
            json={"decision": "accepted", "taxonomy_action": "split"},
            headers=_headers(auth_headers, key, token=token),
        )
        assert resolved.status_code == 201, resolved.text

    with SessionLocal() as session:
        suggestion = session.get(LabelTaxonomySuggestion, suggestion_id)
        assert suggestion is not None
        candidate = session.get(LabelVersion, suggestion.payload["candidate_label_version_id"])
        assert candidate is not None
        assert candidate.status == "draft"
        assert candidate.payload["change_set"]["action"] == "split"
        assert candidate.payload["blockers"]
        assert candidate.payload["ready_for_publish"] is False
