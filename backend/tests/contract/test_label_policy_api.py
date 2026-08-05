from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import (
    HumanReviewTask,
    JsonResource,
    LabelCandidate,
    LabelPolicyVersion,
    LabelVersion,
    RunRecord,
)
from app.schemas.label_policy import LabelPolicyDSL
from app.workers.outbox_worker import process_aggregate_events

pytestmark = pytest.mark.usefixtures("configured_test_legacy_generic_execution")

LABEL_VERSION_ID = "label_v1_9_0_rc2"
PUBLISHED_LABEL_VERSION_ID = "label_v1_8_4"
CANDIDATE_ID = "cand_af128_amount_conflict"
EVAL_RUN_ID = "evalrun_label_v190_shadow"
EVAL_DATASET_ID = "evalset_quote_risk_v12"
EVAL_DATASET_VERSION = "v12"
OPTIMIZATION_RUN_ID = "lor_label_v190_rc2"
METRIC_SCHEMA_VERSION = "label-eval-metrics/1"


def release_metrics(
    *,
    eligible_count: int = 1500,
    processed_count: int = 1500,
    skipped_count: int = 0,
    invalid_count: int = 0,
    abstain_count: int = 0,
    duplicate_count: int = 0,
    confusion_matrix: dict[str, int] | None = None,
    metric_schema_version: str = METRIC_SCHEMA_VERSION,
) -> dict:
    if confusion_matrix is None:
        classified_count = max(processed_count - abstain_count, 0)
        if classified_count == 1500:
            confusion_matrix = {
                "true_positive": 233,
                "false_positive": 30,
                "false_negative": 30,
                "true_negative": 1207,
            }
        else:
            confusion_matrix = {
                "true_positive": classified_count,
                "false_positive": 0,
                "false_negative": 0,
                "true_negative": 0,
            }
    return {
        "metric_schema_version": metric_schema_version,
        "eligible_count": eligible_count,
        "processed_count": processed_count,
        "skipped_count": skipped_count,
        "invalid_count": invalid_count,
        "abstain_count": abstain_count,
        "duplicate_count": duplicate_count,
        "confusion_matrix": confusion_matrix,
        "labeling_f1": 88.6,
        "conflict_rate": 4.0,
        "json_validity": 100.0,
        "blocking_regression_count": 0,
        "blocking_badcase_count": 0,
    }


def prepare_authoritative_release_facts(
    *,
    eval_run_id: str = EVAL_RUN_ID,
    eval_status: str = "success",
    label_version_id: str = LABEL_VERSION_ID,
    optimization_run_id: str = OPTIMIZATION_RUN_ID,
    dataset_id: str = EVAL_DATASET_ID,
    eval_dataset_version: str = EVAL_DATASET_VERSION,
    stored_dataset_version: str = EVAL_DATASET_VERSION,
    dataset_status: str = "locked",
    metrics: dict | None = None,
    declared_sample_count: int = 1500,
) -> None:
    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert label_version is not None
        label_version.payload = {
            **label_version.payload,
            "optimization_run_id": OPTIMIZATION_RUN_ID,
            "impacted_assets_confirmed": True,
            "downstream_incompatible_count": 0,
        }

        eval_run = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == eval_run_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert eval_run is not None
        eval_run.status = eval_status
        eval_run.data = {
            **eval_run.data,
            "eval_run_id": eval_run_id,
            "status": eval_status,
            "label_version_id": label_version_id,
            "optimization_run_id": optimization_run_id,
            "dataset_id": dataset_id,
            "dataset_version": eval_dataset_version,
            "metrics": metrics or release_metrics(),
        }

        dataset = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_datasets",
                JsonResource.resource_key == dataset_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert dataset is not None
        dataset.status = dataset_status
        dataset.data = {
            **dataset.data,
            "dataset_id": dataset_id,
            "dataset_version": stored_dataset_version,
            "sample_count": declared_sample_count,
            "status": dataset_status,
            "locked": dataset_status == "locked",
        }
        session.commit()


def candidate_policy(*, revision: int = 1) -> dict:
    return {
        "dsl_version": "1.0",
        "policy_kind": "label-candidate",
        "policy_key": "contract-candidate-policy",
        "revision": revision,
        "fact_schema_version": "label-policy-facts/1",
        "thresholds": [],
        "rules": [
            {
                "rule_id": "candidate-request-is-valid",
                "priority": 100,
                "when": {
                    "op": "eq",
                    "path": "request.action",
                    "value": "evaluate_candidate",
                },
                "effect": "pass",
                "reason_code": "CANDIDATE_REQUEST_VALID",
            }
        ],
        "default_effect": "require_review",
    }


def validation_payload(*, activate: bool = True, revision: int = 1) -> dict:
    return {
        "policy": candidate_policy(revision=revision),
        "activate": activate,
        "expected_label_resource_version": 1,
    }


def evaluation_payload(*, expected_resource_version: int = 1) -> dict:
    return {
        "candidate_id": CANDIDATE_ID,
        "policy_version_id": "",
        "expected_candidate_resource_version": expected_resource_version,
        "create_human_review": True,
        "facts": {
            "candidate": {
                "source_type": "model_candidate",
                "confidence_ppm": 950_000,
                "version_matches": True,
                "overwrites_human": False,
                "business_document_conflict": False,
            },
            "evidence": {
                "total_count": 1,
                "valid_count": 1,
                "pending_count": 0,
                "cross_scope_count": 0,
            },
            "conflicts": {
                "open_count": 0,
                "high_risk_open_count": 0,
                "human_disagreement_count": 0,
                "equal_precedence_count": 0,
            },
        },
    }


def validate_candidate_policy(client, auth_headers, *, key: str = "label-policy-setup"):
    return client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/policy/validate",
        json=validation_payload(),
        headers={**auth_headers, "Idempotency-Key": key},
    )


def dispatch_run(run_id: str) -> tuple[str, str]:
    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted", run.payload
        dispatch = run.payload["dispatch"]
        adapter = str(dispatch["adapter"])
        external_id_key = {
            "dagster": "external_run_id",
            "object_storage": "storage_object_id",
            "external_callback": "callback_receipt_id",
        }[adapter]
        return adapter, str(dispatch["details"][external_id_key])


def test_policy_validation_is_readable_idempotent_and_detects_key_conflict(client, auth_headers):
    path = f"/api/v1/label-versions/{LABEL_VERSION_ID}/policy/validate"
    headers = {**auth_headers, "Idempotency-Key": "contract-policy-validate"}
    payload = validation_payload()

    first = client.post(path, json=payload, headers=headers)
    replay = client.post(path, json=payload, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    data = first.json()["data"]
    assert data["valid"] is True
    assert data["active"] is True
    assert data["label_resource_version"] == 2
    assert len(data["canonical_sha256"]) == 64
    assert replay.json()["data"]["policy_version_id"] == data["policy_version_id"]

    detail = client.get(
        f"/api/v1/label-policy-versions/{data['policy_version_id']}",
        headers=auth_headers,
    )
    assert detail.status_code == 200
    expected_source = LabelPolicyDSL.model_validate(candidate_policy()).model_dump(
        mode="json", exclude_none=True
    )
    assert detail.json()["data"]["policy"] == expected_source
    assert detail.json()["data"]["canonical_sha256"] == data["canonical_sha256"]

    conflict = client.post(
        path,
        json=validation_payload(revision=2),
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_candidate_evaluation_is_idempotent_and_creates_human_review(client, auth_headers):
    validation = validate_candidate_policy(client, auth_headers)
    assert validation.status_code == 201
    policy_version_id = validation.json()["data"]["policy_version_id"]
    payload = evaluation_payload()
    payload["policy_version_id"] = policy_version_id
    headers = {**auth_headers, "Idempotency-Key": "contract-policy-evaluate"}

    first = client.post("/api/v1/label-candidates/evaluate", json=payload, headers=headers)
    replay = client.post("/api/v1/label-candidates/evaluate", json=payload, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    data = first.json()["data"]
    assert data["verdict"] == "block"
    assert data["candidate_status"] == "blocked"
    assert data["candidate_resource_version"] == 2
    assert data["conflict_id"]
    assert data["review_task_id"]
    assert data["decision"]["primary_reason_code"] == "EVIDENCE_INTEGRITY_INVALID"
    assert replay.json()["data"]["evaluation_id"] == data["evaluation_id"]
    assert replay.json()["data"]["review_task_id"] == data["review_task_id"]

    semantic_replay_payload = dict(payload)
    semantic_replay_payload.pop("expected_candidate_resource_version")
    semantic_replay = client.post(
        "/api/v1/label-candidates/evaluate",
        json=semantic_replay_payload,
        headers={**auth_headers, "Idempotency-Key": "contract-policy-semantic-replay"},
    )
    assert semantic_replay.status_code == 201
    assert semantic_replay.json()["data"]["evaluation_id"] == data["evaluation_id"]
    assert semantic_replay.json()["data"]["replayed"] is True

    evaluation = client.get(
        f"/api/v1/label-policy-evaluations/{data['evaluation_id']}",
        headers=auth_headers,
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["data"]["decision_sha256"] == data["decision_sha256"]
    assert evaluation.json()["data"]["facts"]["candidate"]["source_type"] == "llm_candidate"
    assert evaluation.json()["data"]["facts"]["evidence"]["valid_count"] == 0
    assert evaluation.json()["data"]["facts"]["evidence"]["pending_count"] == 1
    assert evaluation.json()["data"]["facts"]["evidence"]["missing_checksum_count"] == 1
    assert (
        evaluation.json()["data"]["facts"]["evidence"]["artifacts"][0]["evidence_pack_id"]
        == "AF-128"
    )

    review = client.get(
        f"/api/v1/human-review-tasks/{data['review_task_id']}",
        headers=auth_headers,
    )
    assert review.status_code == 200
    assert review.json()["data"]["queue"] == "label_policy_conflict"
    assert review.json()["data"]["policy_evaluation_id"] == data["evaluation_id"]
    assert review.json()["data"]["candidate_id"] == CANDIDATE_ID

    changed = {**payload, "create_human_review": False}
    conflict = client.post(
        "/api/v1/label-candidates/evaluate",
        json=changed,
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_policy_and_candidate_optimistic_version_conflicts_are_rejected(client, auth_headers):
    policy_conflict = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/policy/validate",
        json={**validation_payload(), "expected_label_resource_version": 99},
        headers={**auth_headers, "Idempotency-Key": "contract-policy-version-conflict"},
    )
    assert policy_conflict.status_code == 409
    assert policy_conflict.json()["error"]["code"] == "LABEL_VERSION_CONFLICT"

    validation = validate_candidate_policy(
        client, auth_headers, key="contract-policy-candidate-conflict-setup"
    )
    assert validation.status_code == 201
    payload = evaluation_payload(expected_resource_version=99)
    payload["policy_version_id"] = validation.json()["data"]["policy_version_id"]
    candidate_conflict = client.post(
        "/api/v1/label-candidates/evaluate",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "contract-candidate-version-conflict"},
    )
    assert candidate_conflict.status_code == 409
    assert candidate_conflict.json()["error"]["code"] == "LABEL_CANDIDATE_CONFLICT"


def test_candidate_evaluation_rejects_validated_but_inactive_policy(client, auth_headers):
    active = validate_candidate_policy(client, auth_headers, key="active-policy-setup")
    assert active.status_code == 201
    inactive = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/policy/validate",
        json={
            **validation_payload(activate=False, revision=2),
            "expected_label_resource_version": 2,
        },
        headers={**auth_headers, "Idempotency-Key": "inactive-policy-setup"},
    )
    assert inactive.status_code == 201
    assert inactive.json()["data"]["active"] is False

    payload = evaluation_payload()
    payload["policy_version_id"] = inactive.json()["data"]["policy_version_id"]
    response = client.post(
        "/api/v1/label-candidates/evaluate",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "inactive-policy-evaluation"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_POLICY_NOT_ACTIVE"


def test_candidate_evaluation_recomputes_after_authoritative_evidence_changes(
    client,
    auth_headers,
):
    validation = validate_candidate_policy(
        client,
        auth_headers,
        key="candidate-recompute-policy",
    )
    assert validation.status_code == 201
    policy_version_id = validation.json()["data"]["policy_version_id"]
    payload = evaluation_payload()
    payload["policy_version_id"] = policy_version_id
    first = client.post(
        "/api/v1/label-candidates/evaluate",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-recompute-first"},
    )
    assert first.status_code == 201
    first_evaluation_id = first.json()["data"]["evaluation_id"]

    with SessionLocal() as session:
        evidence = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "evidence_packs",
                JsonResource.resource_key == "AF-128",
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        candidate = session.get(LabelCandidate, CANDIDATE_ID)
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert evidence is not None
        assert candidate is not None
        assert label_version is not None
        evidence.status = "verified"
        evidence.data = {
            **evidence.data,
            "status": "verified",
            "checksum_sha256": "a" * 64,
            "window_start_ms": 1_000,
            "window_end_ms": 9_000,
            "stale": False,
        }
        candidate.payload = {
            **candidate.payload,
            "label_resource_version": label_version.resource_version,
        }
        candidate.resource_version += 1
        session.commit()

    recompute_payload = evaluation_payload()
    recompute_payload.pop("expected_candidate_resource_version")
    recompute_payload["policy_version_id"] = policy_version_id
    second = client.post(
        "/api/v1/label-candidates/evaluate",
        json=recompute_payload,
        headers={**auth_headers, "Idempotency-Key": "candidate-recompute-second"},
    )
    assert second.status_code == 201
    second_data = second.json()["data"]
    assert second_data["evaluation_id"] != first_evaluation_id
    assert second_data["replayed"] is False

    evaluation = client.get(
        f"/api/v1/label-policy-evaluations/{second_data['evaluation_id']}",
        headers=auth_headers,
    )
    facts = evaluation.json()["data"]["facts"]
    assert facts["candidate"]["version_matches"] is True
    assert facts["evidence"]["valid_count"] == 1
    assert facts["evidence"]["missing_checksum_count"] == 0


def test_candidate_evaluation_fails_closed_for_unknown_compiler_version(
    client,
    auth_headers,
):
    validation = validate_candidate_policy(
        client,
        auth_headers,
        key="unsupported-compiler-policy",
    )
    assert validation.status_code == 201
    policy_version_id = validation.json()["data"]["policy_version_id"]
    with SessionLocal() as session:
        policy = session.get(LabelPolicyVersion, policy_version_id)
        assert policy is not None
        policy.compiler_version = "label-policy-compiler/0.0.0"
        session.commit()

    payload = evaluation_payload()
    payload["policy_version_id"] = policy_version_id
    response = client.post(
        "/api/v1/label-candidates/evaluate",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "unsupported-compiler-evaluate"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == ("LABEL_POLICY_ENGINE_VERSION_UNSUPPORTED")


def test_real_eval_run_projection_is_consumed_by_explicit_label_publish(
    client,
    auth_headers,
):
    run_id = "eval_run_contract_projection_success"
    dataset_id = "evalset_quote_risk_v12"
    with SessionLocal() as session:
        existing_projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == run_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert existing_projection is None

        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert label_version is not None
        label_version.payload = {
            **label_version.payload,
            "optimization_run_id": OPTIMIZATION_RUN_ID,
            "impacted_assets_confirmed": True,
            "downstream_incompatible_count": 0,
        }
        dataset = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_datasets",
                JsonResource.resource_key == dataset_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert dataset is not None
        dataset.status = "locked"
        dataset.data = {
            **dataset.data,
            "dataset_version": EVAL_DATASET_VERSION,
            "status": "locked",
            "locked": True,
        }
        session.commit()

    created = client.post(
        "/api/v1/eval-runs",
        json={
            "run_id": run_id,
            "dataset_id": dataset_id,
            "dataset_version": EVAL_DATASET_VERSION,
            "label_version_id": LABEL_VERSION_ID,
            "optimization_run_id": OPTIMIZATION_RUN_ID,
            "candidate_version": LABEL_VERSION_ID,
            "current_version": PUBLISHED_LABEL_VERSION_ID,
        },
        headers={**auth_headers, "Idempotency-Key": "eval-projection-create-success"},
    )
    assert created.status_code == 202, created.text
    created_data = created.json()["data"]
    assert created_data["run_id"] == run_id

    with SessionLocal() as session:
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == run_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert projection is not None
        assert projection.status == "pending"
        assert projection.trace_id == created_data["trace_id"]
        assert projection.data["eval_run_id"] == run_id

    adapter, external_id = dispatch_run(run_id)
    metrics = release_metrics()
    result_ref = {"artifact_uri": "s3://auris-test/eval-results/projection-success.json"}
    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": adapter,
            "status": "success",
            "completion_receipt_id": "eval-projection-completion-success",
            "external_id": external_id,
            "metrics": metrics,
            "result_ref": result_ref,
        },
        headers={**auth_headers, "Idempotency-Key": "eval-projection-receipt-success"},
    )
    assert completion.status_code == 200, completion.text

    with SessionLocal() as session:
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == run_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert projection is not None
        assert projection.status == "success"
        assert projection.data["status"] == "success"
        assert projection.data["metrics"] == metrics
        # The authoritative RunRecord retains the signed storage locator for
        # internal recovery, while the eval_runs domain projection must not
        # expose it through a resource that can feed public policy responses.
        assert projection.data["result_ref"] == {}
        run_record = session.get(RunRecord, run_id)
        assert run_record is not None
        assert run_record.payload["result_ref"] == result_ref

    published = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": run_id, "gray_traffic_ppm": 100_000},
        headers={**auth_headers, "Idempotency-Key": "eval-projection-explicit-publish"},
    )
    assert published.status_code == 202, published.text
    publish_run = published.json()["data"]
    assert publish_run["status"] == "pending"
    assert publish_run["release_policy_verdict"] == "gray_only"

    evaluation = client.get(
        f"/api/v1/label-policy-evaluations/{publish_run['release_policy_evaluation_id']}",
        headers=auth_headers,
    )
    assert evaluation.status_code == 200
    facts = evaluation.json()["data"]["facts"]
    assert facts["provenance"]["eval_run_id"] == run_id
    assert facts["provenance"]["optimization_run_id"] == OPTIMIZATION_RUN_ID
    assert facts["provenance"]["eval_dataset_version"] == EVAL_DATASET_VERSION
    assert facts["evaluation"]["status"] == "success"
    assert facts["evaluation"]["same_optimization_run"] is True
    assert facts["evaluation"]["eligible_count"] == 1500
    assert facts["evaluation"]["processed_count"] == 1500
    assert facts["evaluation"]["metric_schema_version"] == METRIC_SCHEMA_VERSION
    assert facts["evaluation"]["confusion_matrix"]["true_positive"] == 233
    assert facts["evaluation"]["labeling_f1_ppm"] == 886_000

    assert process_aggregate_events([publish_run["run_id"]]) == 1
    with SessionLocal() as session:
        release_run = session.get(RunRecord, publish_run["run_id"])
        released_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert release_run is not None
        assert released_version is not None
        assert release_run.status == "success"
        assert released_version.status == "gray_releasing"
        assert released_version.payload["release_run_id"] == publish_run["run_id"]


def test_failed_eval_run_and_retry_have_independent_authoritative_projections(
    client,
    auth_headers,
):
    run_id = "eval_run_contract_projection_failed"
    created = client.post(
        "/api/v1/eval-runs",
        json={
            "run_id": run_id,
            "dataset_id": "evalset_quote_risk_v12",
            "candidate_version": LABEL_VERSION_ID,
        },
        headers={**auth_headers, "Idempotency-Key": "eval-projection-create-failed"},
    )
    assert created.status_code == 202, created.text

    adapter, external_id = dispatch_run(run_id)
    metrics = {"processed_samples": 87, "failed_samples": 13}
    result_ref = {"partial_artifact_uri": "s3://auris-test/eval-results/projection-failed.json"}
    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": adapter,
            "status": "failed",
            "completion_receipt_id": "eval-projection-completion-failed",
            "external_id": external_id,
            "metrics": metrics,
            "result_ref": result_ref,
            "error_code": "EVAL_SAMPLE_FAILURE",
            "note": "contract failure",
        },
        headers={**auth_headers, "Idempotency-Key": "eval-projection-receipt-failed"},
    )
    assert completion.status_code == 200, completion.text

    with SessionLocal() as session:
        failed_projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == run_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert failed_projection is not None
        assert failed_projection.status == "failed"
        assert failed_projection.data["status"] == "failed"
        assert failed_projection.data["metrics"] == metrics
        assert failed_projection.data["result_ref"] == {}
        failed_run_record = session.get(RunRecord, run_id)
        assert failed_run_record is not None
        assert failed_run_record.payload["result_ref"] == result_ref

    retry = client.post(
        f"/api/v1/runs/{run_id}/retries",
        json={"reason": "retry failed eval projection"},
        headers={**auth_headers, "Idempotency-Key": "eval-projection-retry"},
    )
    assert retry.status_code == 202, retry.text
    retry_data = retry.json()["data"]
    retry_run_id = retry_data["run_id"]
    assert retry_run_id != run_id

    with SessionLocal() as session:
        projections = list(
            session.scalars(
                select(JsonResource).where(
                    JsonResource.collection == "eval_runs",
                    JsonResource.resource_key.in_([run_id, retry_run_id]),
                    JsonResource.tenant_id == "aurora_auto",
                    JsonResource.project_id == "sales_qa",
                )
            )
        )
        by_run_id = {projection.resource_key: projection for projection in projections}
        assert set(by_run_id) == {run_id, retry_run_id}

        assert by_run_id[run_id].status == "failed"

        retry_projection = by_run_id[retry_run_id]
        assert retry_projection.status == "pending"
        assert retry_projection.data["eval_run_id"] == retry_run_id
        assert retry_projection.data["retry_of_run_id"] == run_id


def test_label_publish_requires_explicit_eval_run_id_even_when_matching_run_exists(
    client,
    auth_headers,
):
    prepare_authoritative_release_facts()

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={},
        headers={**auth_headers, "Idempotency-Key": "release-policy-gate"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["details"][0]["field"] == "eval_run_id"


def test_label_publish_allows_gray_release_only_after_authoritative_gates_pass(
    client,
    auth_headers,
):
    prepare_authoritative_release_facts()

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID, "gray_traffic_ppm": 100_000},
        headers={**auth_headers, "Idempotency-Key": "release-policy-gray-pass"},
    )
    assert response.status_code == 202
    run = response.json()["data"]
    assert run["status"] == "pending"
    assert run["release_policy_verdict"] == "gray_only"

    evaluation = client.get(
        f"/api/v1/label-policy-evaluations/{run['release_policy_evaluation_id']}",
        headers=auth_headers,
    )
    assert evaluation.status_code == 200
    facts = evaluation.json()["data"]["facts"]
    decision = evaluation.json()["data"]["decision"]
    assert decision["primary_reason_code"] == "RELEASE_GATES_PASSED"
    assert facts["evaluation"]["status"] == "success"
    assert facts["evaluation"]["same_optimization_run"] is True
    assert facts["evaluation"]["sample_count"] == 1500
    assert facts["evaluation"]["eligible_count"] == 1500
    assert facts["evaluation"]["processed_count"] == 1500
    assert facts["evaluation"]["skipped_count"] == 0
    assert facts["evaluation"]["invalid_count"] == 0
    assert facts["evaluation"]["abstain_count"] == 0
    assert facts["evaluation"]["duplicate_count"] == 0
    assert facts["evaluation"]["effective_count"] == 1500
    assert facts["evaluation"]["effective_coverage_ppm"] == 1_000_000
    assert facts["evaluation"]["counts_conserved"] is True
    assert facts["evaluation"]["metric_schema_version"] == METRIC_SCHEMA_VERSION
    assert facts["evaluation"]["confusion_matrix"] == {
        "true_positive": 233,
        "false_positive": 30,
        "false_negative": 30,
        "true_negative": 1207,
    }
    assert facts["evaluation"]["labeling_f1_ppm"] == 886_000
    assert facts["evaluation"]["conflict_rate_ppm"] == 40_000
    assert facts["evaluation"]["json_validity_ppm"] == 1_000_000
    assert facts["impact"] == {
        "assets_confirmed": True,
        "downstream_incompatible_count": 0,
    }
    assert facts["release"]["rollback_available"] is True
    assert facts["release"]["gray_traffic_ppm"] == 100_000
    assert facts["provenance"]["optimization_run_id"] == OPTIMIZATION_RUN_ID
    assert facts["provenance"]["eval_dataset_id"] == EVAL_DATASET_ID
    assert facts["provenance"]["eval_dataset_version"] == EVAL_DATASET_VERSION


def test_label_publish_does_not_fall_back_when_explicit_eval_run_is_unknown(
    client,
    auth_headers,
):
    prepare_authoritative_release_facts()

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": "evalrun_unknown_explicit"},
        headers={**auth_headers, "Idempotency-Key": "release-unknown-explicit-eval"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EVAL_RUN_NOT_FOUND"


def test_label_publish_does_not_read_eval_run_from_another_scope(client, auth_headers):
    foreign_eval_run_id = "evalrun_foreign_scope"
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="eval_runs",
                resource_key=foreign_eval_run_id,
                tenant_id="foreign_tenant",
                project_id="foreign_project",
                status="success",
                trace_id="trace_foreign_eval",
                data={"eval_run_id": foreign_eval_run_id, "status": "success"},
            )
        )
        session.commit()

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": foreign_eval_run_id},
        headers={**auth_headers, "Idempotency-Key": "release-foreign-scope-eval"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EVAL_RUN_NOT_FOUND"


def test_label_publish_rejects_eval_run_from_the_wrong_label_batch(client, auth_headers):
    prepare_authoritative_release_facts(label_version_id=PUBLISHED_LABEL_VERSION_ID)

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-wrong-label-batch"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_LABEL_VERSION_MISMATCH"


def test_label_publish_compares_real_optimization_run_ids(client, auth_headers):
    prepare_authoritative_release_facts(optimization_run_id="lor_unrelated_batch")

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-wrong-optimization-run"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_OPTIMIZATION_RUN_MISMATCH"


def test_label_publish_rejects_non_success_eval_run(client, auth_headers):
    prepare_authoritative_release_facts(eval_status="failed")

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-failed-eval-run"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EVAL_RUN_NOT_SUCCESSFUL"


def test_label_publish_rejects_unlocked_eval_dataset(client, auth_headers):
    prepare_authoritative_release_facts(dataset_status="ready")

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-unlocked-dataset"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EVAL_DATASET_NOT_LOCKED"


def test_label_publish_rejects_stale_eval_dataset_version(client, auth_headers):
    prepare_authoritative_release_facts(eval_dataset_version="v11")

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-stale-dataset-version"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EVAL_DATASET_VERSION_MISMATCH"


def test_label_publish_rejects_legacy_metric_schema(client, auth_headers):
    prepare_authoritative_release_facts(
        metrics=release_metrics(metric_schema_version="label-eval-metrics/0")
    )

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-legacy-metric-schema"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_METRIC_SCHEMA_UNSUPPORTED"


def test_label_publish_rejects_non_conserving_eval_counts(client, auth_headers):
    prepare_authoritative_release_facts(
        metrics=release_metrics(eligible_count=1500, processed_count=1499)
    )

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-non-conserving-counts"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EVAL_COUNTS_INCONSISTENT"


def test_label_publish_rejects_one_actual_processed_out_of_1500_declared(
    client,
    auth_headers,
):
    prepare_authoritative_release_facts(
        declared_sample_count=1500,
        metrics=release_metrics(
            eligible_count=1500,
            processed_count=1,
            skipped_count=1499,
        ),
    )

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-one-of-1500"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_PROCESSED_COUNT_TOO_SMALL"


def test_label_publish_dispatch_rechecks_actual_counts_before_materialization(
    client,
    auth_headers,
):
    prepare_authoritative_release_facts()
    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-counts-drift"},
    )
    assert response.status_code == 202
    publish_run_id = response.json()["data"]["run_id"]

    with SessionLocal() as session:
        eval_run = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == EVAL_RUN_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert eval_run is not None
        eval_run.data = {
            **eval_run.data,
            "metrics": release_metrics(
                eligible_count=1500,
                processed_count=1,
                skipped_count=1499,
            ),
        }
        session.commit()

    assert process_aggregate_events([publish_run_id]) == 1
    with SessionLocal() as session:
        publish_run = session.get(RunRecord, publish_run_id)
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert publish_run is not None
        assert label_version is not None
        assert publish_run.status == "blocked"
        assert label_version.status != "gray_releasing"
        assert publish_run.payload["release_dispatch_gate"] == {
            "allowed": False,
            "reason_code": "RELEASE_FACTS_INVALID",
            "validation_error_code": "LABEL_RELEASE_PROCESSED_COUNT_TOO_SMALL",
        }


def test_label_publish_rejects_insufficient_effective_coverage(client, auth_headers):
    prepare_authoritative_release_facts(
        metrics=release_metrics(
            eligible_count=1500,
            processed_count=1400,
            skipped_count=100,
        )
    )

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": "release-low-effective-coverage"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LABEL_RELEASE_EFFECTIVE_COVERAGE_TOO_LOW"


@pytest.mark.parametrize("review_status", ["blocked", "rejected", "escalated"])
def test_label_publish_counts_blocking_human_review_terminal_states(
    client,
    auth_headers,
    review_status,
):
    prepare_authoritative_release_facts()
    with SessionLocal() as session:
        session.add(
            HumanReviewTask(
                review_task_id=f"hrt_release_{review_status}",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status=review_status,
                trace_id=f"trace_release_{review_status}",
                payload={"candidate_id": CANDIDATE_ID},
            )
        )
        session.commit()

    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": f"release-review-{review_status}"},
    )

    assert response.status_code == 202
    run = response.json()["data"]
    assert run["status"] == "blocked"
    assert run["release_policy_verdict"] == "block"
    evaluation = client.get(
        f"/api/v1/label-policy-evaluations/{run['release_policy_evaluation_id']}",
        headers=auth_headers,
    )
    assert evaluation.status_code == 200
    review_facts = evaluation.json()["data"]["facts"]["reviews"]
    assert review_facts["pending_count"] == 1


def test_gray_release_rejects_full_traffic_and_system_approval(client, auth_headers):
    full_traffic = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"gray_traffic_ppm": 1_000_000},
        headers={**auth_headers, "Idempotency-Key": "release-full-traffic"},
    )
    assert full_traffic.status_code == 422
    assert full_traffic.json()["error"]["code"] == "VALIDATION_ERROR"

    system = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={},
        headers={
            **auth_headers,
            "Authorization": "Bearer system-token",
            "Idempotency-Key": "release-system-approval",
        },
    )
    assert system.status_code == 403
    assert system.json()["error"]["code"] == "AGENT_LABEL_PUBLISH_FORBIDDEN"


def test_published_label_version_rejects_patch_and_policy_activation(client, auth_headers):
    patch = client.patch(
        f"/api/v1/label-versions/{PUBLISHED_LABEL_VERSION_ID}",
        json={"description": "published versions are immutable"},
        headers={**auth_headers, "Idempotency-Key": "contract-published-label-patch"},
    )
    assert patch.status_code == 409
    assert patch.json()["error"]["code"] == "PUBLISHED_LABEL_VERSION_IMMUTABLE"

    activation = client.post(
        f"/api/v1/label-versions/{PUBLISHED_LABEL_VERSION_ID}/policy/validate",
        json=validation_payload(),
        headers={**auth_headers, "Idempotency-Key": "contract-published-policy-activation"},
    )
    assert activation.status_code == 409
    assert activation.json()["error"]["code"] == "PUBLISHED_LABEL_VERSION_IMMUTABLE"
