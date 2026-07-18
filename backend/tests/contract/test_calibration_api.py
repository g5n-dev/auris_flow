from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import (
    CalibrationAdjudication,
    CalibrationAssignment,
    CalibrationRound,
    CalibrationSubmission,
    GoldAnnotation,
    GoldSetVersion,
    HumanReviewTask,
    OutboxEvent,
    Project,
    User,
)

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
REVIEWER_A = "u_annotator_001"
REVIEWER_B = "u_annotator_002"
ADJUDICATOR = "u_admin_001"


@pytest.fixture(autouse=True)
def seed_second_annotator() -> None:
    with SessionLocal() as session:
        project = session.get(Project, PROJECT_ID)
        assert project is not None
        if session.get(User, REVIEWER_B) is None:
            session.add(
                User(
                    user_id=REVIEWER_B,
                    tenant_id=TENANT_ID,
                    email="annotator.b@auris.local",
                    name="质检运营 B",
                    roles=["annotator"],
                    data={},
                )
            )
        project_data = deepcopy(project.data)
        project_data["member_user_ids"] = list(
            dict.fromkeys([*project_data.get("member_user_ids", []), REVIEWER_B])
        )
        members = [
            member
            for member in project_data.get("members", [])
            if member.get("user_id") != REVIEWER_B
        ]
        project_data["members"] = [*members, {"user_id": REVIEWER_B, "roles": ["annotator"]}]
        project.data = project_data
        session.commit()


def _headers(
    auth_headers: dict[str, str],
    *,
    token: str,
    key: str | None = None,
) -> dict[str, str]:
    headers = {**auth_headers, "Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _grant_dual_reviewer_manager_role() -> str:
    with SessionLocal() as session:
        user = session.get(User, REVIEWER_A)
        project = session.get(Project, PROJECT_ID)
        assert user is not None and project is not None
        user.roles = list(dict.fromkeys([*(user.roles or []), "project_admin"]))
        project_data = deepcopy(project.data)
        members = []
        for member in project_data.get("members", []):
            if member.get("user_id") == REVIEWER_A:
                member = {
                    **member,
                    "roles": list(dict.fromkeys([*member.get("roles", []), "project_admin"])),
                }
            members.append(member)
        project_data["members"] = members
        project.data = project_data
        session.commit()
    profile = DevAuthProfile(
        email="dual-reviewer@auris.local",
        user_id=REVIEWER_A,
        name="双角色 reviewer",
        role_label="项目管理员 / reviewer",
        initials="双",
        roles=("annotator", "project_admin"),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


def _round_payload(*, suffix: str = "primary", sample_count: int = 4) -> dict[str, Any]:
    return {
        "dataset_id": "evalset_quote_risk_v12",
        "dataset_version": "v12",
        "label_version": "label_v1_8_4",
        "rubric_version": "rubric_quote_risk_v3",
        "reviewer_ids": [REVIEWER_A, REVIEWER_B],
        "adjudicator_id": ADJUDICATOR,
        "samples": [
            {
                "source_case_id": f"case-{suffix}-{index + 1}",
                "evidence_ref": f"evidence://calibration/{suffix}/{index + 1}",
            }
            for index in range(sample_count)
        ],
    }


def _create_round(client, auth_headers, *, suffix: str = "primary", sample_count: int = 4):
    response = client.post(
        "/api/v1/calibration-rounds",
        json=_round_payload(suffix=suffix, sample_count=sample_count),
        headers=_headers(
            auth_headers,
            token="dev-token",
            key=f"calibration-round-create-{suffix}",
        ),
    )
    assert response.status_code == 201, response.text
    return response


def _mine(client, auth_headers, *, round_id: str, token: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/api/v1/calibration-assignments?mine=true&round_id={round_id}",
        headers=_headers(auth_headers, token=token),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def _submit_vector(
    client,
    auth_headers,
    *,
    round_id: str,
    token: str,
    values: list[int],
    key_prefix: str,
) -> None:
    assignments = sorted(
        _mine(client, auth_headers, round_id=round_id, token=token),
        key=lambda item: item["ordinal"],
    )
    assert len(assignments) == len(values)
    for assignment, value in zip(assignments, values, strict=True):
        response = client.post(
            f"/api/v1/calibration-assignments/{assignment['assignment_id']}/submissions",
            json={
                "value": {"decision": "pass" if value == 1 else "fail"},
                "expected_resource_version": assignment["resource_version"],
            },
            headers=_headers(
                auth_headers,
                token=token,
                key=f"{key_prefix}-{assignment['ordinal']}",
            ),
        )
        assert response.status_code == 201, response.text
        response_data = response.json()["data"]
        assert "item_status" not in response_data
        assert "round_status" not in response_data


def _resolve_conflicts(client, auth_headers, *, round_id: str, key_prefix: str) -> None:
    conflicts_response = client.get(
        f"/api/v1/calibration-rounds/{round_id}/conflicts",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert conflicts_response.status_code == 200, conflicts_response.text
    conflicts = conflicts_response.json()["data"]["items"]
    for conflict in conflicts:
        claim = client.post(
            f"/api/v1/calibration-items/{conflict['item_id']}/adjudication-claims",
            json={"expected_resource_version": conflict["resource_version"]},
            headers=_headers(
                auth_headers,
                token="dev-token",
                key=f"{key_prefix}-claim-{conflict['item_id']}",
            ),
        )
        assert claim.status_code == 200, claim.text
        adjudication = client.post(
            f"/api/v1/calibration-items/{conflict['item_id']}/adjudications",
            json={
                "decision": "accept_a",
                "reason": "以 reviewer A 的证据结论作为裁决结果。",
                "expected_resource_version": claim.json()["data"]["resource_version"],
            },
            headers=_headers(
                auth_headers,
                token="dev-token",
                key=f"{key_prefix}-adjudicate-{conflict['item_id']}",
            ),
        )
        assert adjudication.status_code == 201, adjudication.text


def test_binary_rubric_ignores_reason_metadata_for_agreement(client, auth_headers):
    created = _create_round(client, auth_headers, suffix="reason-metadata", sample_count=2)
    round_id = created.json()["data"]["round_id"]
    reviewer_inputs = (
        (
            "annotator-token",
            [
                {"decision": "pass", "reason_code": "evidence_consistent"},
                {"decision": "fail", "reason_code": "evidence_conflict"},
            ],
        ),
        (
            "annotator-b-token",
            [
                {"decision": "pass", "reason_code": "other"},
                {"decision": "fail", "reason_code": "insufficient_evidence"},
            ],
        ),
    )
    for token, values in reviewer_inputs:
        assignments = sorted(
            _mine(client, auth_headers, round_id=round_id, token=token),
            key=lambda item: item["ordinal"],
        )
        for assignment, value in zip(assignments, values, strict=True):
            response = client.post(
                f"/api/v1/calibration-assignments/{assignment['assignment_id']}/submissions",
                json={"value": value},
                headers=_headers(
                    auth_headers,
                    token=token,
                    key=f"reason-metadata-{token}-{assignment['ordinal']}",
                ),
            )
            assert response.status_code == 201, response.text

    detail = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["status"] == "ready"
    assert data["agreed_count"] == 2
    assert data["conflict_count"] == 0
    assert data["cohen_kappa_micros"] == 1_000_000
    assert {item["review_outcome"] for item in data["items"]} == {"agreed"}


def test_binary_rubric_rejects_cross_sample_evidence_reference(client, auth_headers):
    created = _create_round(client, auth_headers, suffix="wrong-evidence", sample_count=1)
    round_id = created.json()["data"]["round_id"]
    assignment = _mine(client, auth_headers, round_id=round_id, token="annotator-token")[0]

    response = client.post(
        f"/api/v1/calibration-assignments/{assignment['assignment_id']}/submissions",
        json={
            "value": {
                "decision": "pass",
                "reason_code": "evidence_consistent",
                "evidence_refs": ["evidence://calibration/not-this-sample"],
            }
        },
        headers=_headers(
            auth_headers,
            token="annotator-token",
            key="wrong-evidence-ref",
        ),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CALIBRATION_RUBRIC_VALUE_INVALID"


def test_create_round_freezes_inputs_and_creates_paired_assignments(client, auth_headers):
    response = _create_round(client, auth_headers)
    data = response.json()["data"]

    assert data["dataset_id"] == "evalset_quote_risk_v12"
    assert data["dataset_version"] == "v12"
    assert data["label_version"] == "label_v1_8_4"
    assert data["rubric_version"] == "rubric_quote_risk_v3"
    assert len(data["sample_manifest_sha256"]) == 64
    assert data["sample_count"] == 4
    assert data["status"] == "in_review"
    assert "reviewer_ids" not in data
    assert "adjudicator_id" not in data

    with SessionLocal() as session:
        round_record = session.get(CalibrationRound, data["round_id"])
        assignments = (
            session.query(CalibrationAssignment)
            .filter(CalibrationAssignment.round_id == data["round_id"])
            .all()
        )
        tasks = (
            session.query(HumanReviewTask)
            .filter(
                HumanReviewTask.review_task_id.in_([item.review_task_id for item in assignments])
            )
            .all()
        )
        assert round_record is not None
        assert len(assignments) == 8
        assert {assignment.slot for assignment in assignments} == {"A", "B"}
        assert len(tasks) == 8
        assert all(task.payload["queue"] == "blind_calibration" for task in tasks)
        assert all("reviewer_id" not in task.payload for task in tasks)


@pytest.mark.parametrize(
    "reviewer_ids,adjudicator_id",
    [
        ([REVIEWER_A, REVIEWER_A], ADJUDICATOR),
        ([REVIEWER_A, REVIEWER_B], REVIEWER_A),
    ],
)
def test_create_round_rejects_invalid_participant_separation(
    client,
    auth_headers,
    reviewer_ids,
    adjudicator_id,
):
    payload = _round_payload()
    payload["reviewer_ids"] = reviewer_ids
    payload["adjudicator_id"] = adjudicator_id

    response = client.post(
        "/api/v1/calibration-rounds",
        json=payload,
        headers=_headers(auth_headers, token="dev-token", key="calibration-invalid-people"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_round_rejects_creator_as_blind_reviewer(client, auth_headers):
    response = client.post(
        "/api/v1/calibration-rounds",
        json=_round_payload(suffix="creator-reviewer"),
        headers=_headers(
            auth_headers,
            token=_grant_dual_reviewer_manager_role(),
            key="calibration-creator-reviewer",
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CALIBRATION_CREATOR_REVIEWER_FORBIDDEN"


def test_reviewer_views_stay_blind_and_generic_decision_is_rejected(client, auth_headers):
    created = _create_round(client, auth_headers)
    round_id = created.json()["data"]["round_id"]

    detail = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="annotator-token"),
    )
    assert detail.status_code == 200, detail.text
    detail_data = detail.json()["data"]
    assert detail_data["sealed"] is True
    assert detail_data["my_role"] in {"reviewer_a", "reviewer_b"}
    assert "reviewer_ids" not in detail_data
    assert "adjudicator_id" not in detail_data
    assert REVIEWER_B not in detail.text
    for hidden_metric in (
        "paired_submission_count",
        "agreed_count",
        "conflict_count",
        "adjudication_count",
        "excluded_count",
        "observed_agreement_ppm",
        "cohen_kappa_micros",
        "cohen_kappa_defined",
        "items",
    ):
        assert hidden_metric not in detail_data
    for forbidden in ("peer", "model_answer", "model_value", "gold", "submissions"):
        assert forbidden not in detail_data

    assignments = _mine(client, auth_headers, round_id=round_id, token="annotator-token")
    assert len(assignments) == 4
    assert {item["slot"] for item in assignments}.issubset({"A", "B"})
    assert len({item["slot"] for item in assignments}) == 1
    assert all("reviewer_id" not in item for item in assignments)
    assert all("value" not in item and "value_json" not in item for item in assignments)

    queue = client.get(
        "/api/v1/human-review-tasks?queue=blind_calibration",
        headers=_headers(auth_headers, token="annotator-token"),
    )
    assert queue.status_code == 200, queue.text
    projected_tasks = queue.json()["data"]["items"]
    assert len(projected_tasks) == 4
    assert all(task["assignee_id"] == REVIEWER_A for task in projected_tasks)
    assert all("reviewer_id" not in task for task in projected_tasks)
    assert REVIEWER_B not in queue.text

    manager_queue = client.get(
        "/api/v1/human-review-tasks?queue=blind_calibration",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert manager_queue.status_code == 200
    assert manager_queue.json()["data"]["items"] == []
    with SessionLocal() as session:
        peer_task = next(
            task
            for task in session.query(HumanReviewTask).all()
            if task.payload.get("queue") == "blind_calibration"
            and task.payload.get("assignee_id") == REVIEWER_B
        )
    peer_detail = client.get(
        f"/api/v1/human-review-tasks/{peer_task.review_task_id}",
        headers=_headers(auth_headers, token="annotator-token"),
    )
    assert peer_detail.status_code == 403

    generic_decision = client.post(
        f"/api/v1/human-review-tasks/{assignments[0]['review_task_id']}/decisions",
        json={"decision": "accepted"},
        headers=_headers(
            auth_headers,
            token="annotator-token",
            key="blind-calibration-generic-decision",
        ),
    )
    assert generic_decision.status_code == 409
    assert generic_decision.json()["error"]["code"] == (
        "BLIND_CALIBRATION_SPECIALIZED_SUBMISSION_REQUIRED"
    )


def test_full_blind_calibration_metrics_adjudication_and_append_only_gold_release(
    client,
    auth_headers,
):
    created = _create_round(client, auth_headers)
    round_id = created.json()["data"]["round_id"]
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-token",
        values=[1, 1, 0, 0],
        key_prefix="calibration-a",
    )
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-b-token",
        values=[1, 0, 0, 0],
        key_prefix="calibration-b",
    )

    detail = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert detail.status_code == 200, detail.text
    metrics = detail.json()["data"]
    assert metrics["paired_submission_count"] == 4
    assert metrics["agreed_count"] == 3
    assert metrics["conflict_count"] == 1
    assert metrics["observed_agreement_ppm"] == 750_000
    assert metrics["cohen_kappa_micros"] == 500_000
    assert metrics["cohen_kappa_defined"] is True
    assert metrics["status"] == "in_review"

    reviewer_conflicts = client.get(
        f"/api/v1/calibration-rounds/{round_id}/conflicts",
        headers=_headers(auth_headers, token="annotator-token"),
    )
    assert reviewer_conflicts.status_code == 403

    conflicts = client.get(
        f"/api/v1/calibration-rounds/{round_id}/conflicts",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert conflicts.status_code == 200, conflicts.text
    conflict_items = conflicts.json()["data"]["items"]
    assert len(conflict_items) == 1
    assert {entry["slot"] for entry in conflict_items[0]["submissions"]} == {"A", "B"}
    assert all("reviewer_id" not in entry for entry in conflict_items[0]["submissions"])

    forbidden_claim = client.post(
        f"/api/v1/calibration-items/{conflict_items[0]['item_id']}/adjudication-claims",
        json={"expected_resource_version": conflict_items[0]["resource_version"]},
        headers=_headers(
            auth_headers,
            token="annotator-token",
            key="calibration-reviewer-claim",
        ),
    )
    assert forbidden_claim.status_code == 403

    _resolve_conflicts(client, auth_headers, round_id=round_id, key_prefix="primary")
    ready = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "ready"
    assert ready.json()["data"]["adjudication_count"] == 1

    reviewer_ready = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="annotator-token"),
    )
    assert reviewer_ready.status_code == 200, reviewer_ready.text
    assert "reviewer_ids" not in reviewer_ready.json()["data"]
    assert "adjudicator_id" not in reviewer_ready.json()["data"]
    assert REVIEWER_B not in reviewer_ready.text
    assert ADJUDICATOR not in reviewer_ready.text

    first_release = client.post(
        f"/api/v1/calibration-rounds/{round_id}/gold-releases",
        json={
            "gold_set_key": "quote-risk-gold",
            "expected_resource_version": ready.json()["data"]["resource_version"],
        },
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="calibration-gold-release-v1",
        ),
    )
    assert first_release.status_code == 201, first_release.text
    first_gold = first_release.json()["data"]
    assert first_gold["version_number"] == 1
    assert first_gold["annotation_count"] == 4
    assert first_gold["conflict_count"] == 1

    versions_response = client.get(
        "/api/v1/gold-set-versions?gold_set_key=quote-risk-gold",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert versions_response.status_code == 200, versions_response.text
    assert (
        versions_response.json()["data"]["items"][0]["gold_set_version_id"]
        == (first_gold["gold_set_version_id"])
    )
    version_detail = client.get(
        f"/api/v1/gold-set-versions/{first_gold['gold_set_version_id']}",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert version_detail.status_code == 200, version_detail.text
    assert len(version_detail.json()["data"]["annotations"]) == 4

    for reviewer_token in ("annotator-token", "annotator-b-token"):
        forbidden_gold_list = client.get(
            "/api/v1/gold-set-versions?gold_set_key=quote-risk-gold",
            headers=_headers(auth_headers, token=reviewer_token),
        )
        assert forbidden_gold_list.status_code == 403
        forbidden_gold_detail = client.get(
            f"/api/v1/gold-set-versions/{first_gold['gold_set_version_id']}",
            headers=_headers(auth_headers, token=reviewer_token),
        )
        assert forbidden_gold_detail.status_code == 403

    dual_role_token = _grant_dual_reviewer_manager_role()
    dual_role_gold_list = client.get(
        "/api/v1/gold-set-versions?gold_set_key=quote-risk-gold",
        headers=_headers(auth_headers, token=dual_role_token),
    )
    assert dual_role_gold_list.status_code == 200
    assert dual_role_gold_list.json()["data"]["items"] == []
    dual_role_gold_detail = client.get(
        f"/api/v1/gold-set-versions/{first_gold['gold_set_version_id']}",
        headers=_headers(auth_headers, token=dual_role_token),
    )
    assert dual_role_gold_detail.status_code == 403
    assert dual_role_gold_detail.json()["error"]["code"] == "CALIBRATION_REVIEWER_GOLD_FORBIDDEN"

    with SessionLocal() as session:
        first_version = session.get(GoldSetVersion, first_gold["gold_set_version_id"])
        assert first_version is not None
        first_snapshot = {
            "round_id": first_version.round_id,
            "version_number": first_version.version_number,
            "annotation_manifest_sha256": first_version.annotation_manifest_sha256,
            "trace_id": first_version.trace_id,
        }
        first_annotations = {
            annotation.item_id: (
                deepcopy(annotation.value_json),
                annotation.canonical_value_sha256,
                annotation.resolution_source,
            )
            for annotation in session.query(GoldAnnotation)
            .filter(GoldAnnotation.gold_set_version_id == first_version.gold_set_version_id)
            .all()
        }

    second = _create_round(client, auth_headers, suffix="second", sample_count=1)
    second_round_id = second.json()["data"]["round_id"]
    _submit_vector(
        client,
        auth_headers,
        round_id=second_round_id,
        token="annotator-token",
        values=[1],
        key_prefix="second-a",
    )
    _submit_vector(
        client,
        auth_headers,
        round_id=second_round_id,
        token="annotator-b-token",
        values=[1],
        key_prefix="second-b",
    )
    second_ready = client.get(
        f"/api/v1/calibration-rounds/{second_round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    ).json()["data"]
    second_release = client.post(
        f"/api/v1/calibration-rounds/{second_round_id}/gold-releases",
        json={
            "gold_set_key": "quote-risk-gold",
            "expected_resource_version": second_ready["resource_version"],
        },
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="calibration-gold-release-v2",
        ),
    )
    assert second_release.status_code == 201, second_release.text
    assert second_release.json()["data"]["version_number"] == 2

    with SessionLocal() as session:
        unchanged_version = session.get(GoldSetVersion, first_gold["gold_set_version_id"])
        assert unchanged_version is not None
        assert {
            "round_id": unchanged_version.round_id,
            "version_number": unchanged_version.version_number,
            "annotation_manifest_sha256": unchanged_version.annotation_manifest_sha256,
            "trace_id": unchanged_version.trace_id,
        } == first_snapshot
        unchanged_annotations = {
            annotation.item_id: (
                annotation.value_json,
                annotation.canonical_value_sha256,
                annotation.resolution_source,
            )
            for annotation in session.query(GoldAnnotation)
            .filter(GoldAnnotation.gold_set_version_id == first_gold["gold_set_version_id"])
            .all()
        }
        assert unchanged_annotations == first_annotations
        assert (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.event_type == "calibration.gold_set.published",
                OutboxEvent.aggregate_id == first_gold["gold_set_version_id"],
            )
            .count()
            == 1
        )


def test_submission_requires_typed_frozen_rubric_value(client, auth_headers):
    created = _create_round(client, auth_headers, suffix="typed", sample_count=1)
    assignment = _mine(
        client,
        auth_headers,
        round_id=created.json()["data"]["round_id"],
        token="annotator-token",
    )[0]

    response = client.post(
        f"/api/v1/calibration-assignments/{assignment['assignment_id']}/submissions",
        json={
            "value": {"label": "arbitrary", "score": 0.99},
            "expected_resource_version": assignment["resource_version"],
        },
        headers=_headers(
            auth_headers,
            token="annotator-token",
            key="calibration-arbitrary-json",
        ),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_degenerate_round_marks_kappa_not_applicable(client, auth_headers):
    created = _create_round(client, auth_headers, suffix="kappa-na", sample_count=1)
    round_id = created.json()["data"]["round_id"]
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-token",
        values=[1],
        key_prefix="kappa-na-a",
    )
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-b-token",
        values=[1],
        key_prefix="kappa-na-b",
    )

    detail = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    ).json()["data"]
    assert detail["observed_agreement_ppm"] == 1_000_000
    assert detail["cohen_kappa_micros"] == 0
    assert detail["cohen_kappa_defined"] is False


def test_gold_release_blocks_when_exclusions_break_minimum_coverage(client, auth_headers):
    created = _create_round(client, auth_headers, suffix="coverage", sample_count=1)
    round_id = created.json()["data"]["round_id"]
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-token",
        values=[1],
        key_prefix="coverage-a",
    )
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-b-token",
        values=[0],
        key_prefix="coverage-b",
    )
    conflict = client.get(
        f"/api/v1/calibration-rounds/{round_id}/conflicts",
        headers=_headers(auth_headers, token="dev-token"),
    ).json()["data"]["items"][0]
    claim = client.post(
        f"/api/v1/calibration-items/{conflict['item_id']}/adjudication-claims",
        json={"expected_resource_version": conflict["resource_version"]},
        headers=_headers(auth_headers, token="dev-token", key="coverage-claim"),
    )
    assert claim.status_code == 200, claim.text
    adjudication = client.post(
        f"/api/v1/calibration-items/{conflict['item_id']}/adjudications",
        json={
            "decision": "exclude",
            "reason": "证据质量不足，排除该样本。",
            "expected_resource_version": claim.json()["data"]["resource_version"],
        },
        headers=_headers(auth_headers, token="dev-token", key="coverage-exclude"),
    )
    assert adjudication.status_code == 201, adjudication.text
    ready = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    ).json()["data"]

    release = client.post(
        f"/api/v1/calibration-rounds/{round_id}/gold-releases",
        json={
            "gold_set_key": "coverage-gold",
            "expected_resource_version": ready["resource_version"],
        },
        headers=_headers(auth_headers, token="dev-token", key="coverage-release"),
    )
    assert release.status_code == 409
    assert release.json()["error"]["code"] == "CALIBRATION_GOLD_COVERAGE_BLOCKED"


def test_calibration_evidence_records_are_database_append_only(client, auth_headers):
    created = _create_round(client, auth_headers, suffix="append-only", sample_count=1)
    round_id = created.json()["data"]["round_id"]
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-token",
        values=[1],
        key_prefix="append-only-a",
    )
    _submit_vector(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-b-token",
        values=[0],
        key_prefix="append-only-b",
    )
    _resolve_conflicts(client, auth_headers, round_id=round_id, key_prefix="append-only")
    ready = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=_headers(auth_headers, token="dev-token"),
    ).json()["data"]
    release = client.post(
        f"/api/v1/calibration-rounds/{round_id}/gold-releases",
        json={
            "gold_set_key": "append-only-gold",
            "expected_resource_version": ready["resource_version"],
        },
        headers=_headers(auth_headers, token="dev-token", key="append-only-release"),
    )
    assert release.status_code == 201, release.text
    version_id = release.json()["data"]["gold_set_version_id"]

    with SessionLocal() as session:
        immutable_ids = [
            (
                CalibrationSubmission,
                session.query(CalibrationSubmission)
                .filter(CalibrationSubmission.round_id == round_id)
                .first()
                .submission_id,
            ),
            (
                CalibrationAdjudication,
                session.query(CalibrationAdjudication)
                .filter(CalibrationAdjudication.round_id == round_id)
                .one()
                .adjudication_id,
            ),
            (GoldSetVersion, version_id),
            (
                GoldAnnotation,
                session.query(GoldAnnotation)
                .filter(GoldAnnotation.gold_set_version_id == version_id)
                .one()
                .gold_annotation_id,
            ),
        ]

    for model, record_id in immutable_ids:
        with SessionLocal() as session:
            record = session.get(model, record_id)
            assert record is not None
            record.trace_id = "tampered-trace"
            with pytest.raises(DBAPIError, match="append-only calibration record"):
                session.commit()
            session.rollback()
        with SessionLocal() as session:
            record = session.get(model, record_id)
            assert record is not None
            session.delete(record)
            with pytest.raises(DBAPIError, match="append-only calibration record"):
                session.commit()
            session.rollback()


def test_calibration_resources_are_scope_isolated(client, auth_headers):
    created = _create_round(client, auth_headers)
    round_id = created.json()["data"]["round_id"]
    assignment_id = _mine(
        client,
        auth_headers,
        round_id=round_id,
        token="annotator-token",
    )[0]["assignment_id"]

    with SessionLocal() as session:
        from app.models import Project, Tenant

        session.add(
            Tenant(
                tenant_id="other_tenant",
                tenant_code="other_tenant",
                name="Other tenant",
                status="active",
                data={},
            )
        )
        session.add(
            Project(
                project_id="other_project",
                tenant_id="other_tenant",
                name="Other project",
                status="active",
                data={},
            )
        )
        session.commit()

    foreign_headers = {
        "Authorization": "Bearer system-token",
        "X-Tenant-Id": "other_tenant",
        "X-Project-Id": "other_project",
        "X-Request-Id": "calibration-cross-scope",
    }
    detail = client.get(
        f"/api/v1/calibration-rounds/{round_id}",
        headers=foreign_headers,
    )
    assert detail.status_code == 404

    foreign_submission = client.post(
        f"/api/v1/calibration-assignments/{assignment_id}/submissions",
        json={"value": {"decision": "pass"}},
        headers={**foreign_headers, "Idempotency-Key": "calibration-cross-scope-submit"},
    )
    assert foreign_submission.status_code == 404
