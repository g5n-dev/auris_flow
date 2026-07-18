from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from app.core.database import SessionLocal
from app.models import GoldAnnotation, GoldSetVersion, Project, User

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
REVIEWER_B = "u_annotator_002"


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
    result = {**auth_headers, "Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def _create_round(client, auth_headers, *, suffix: str):
    response = client.post(
        "/api/v1/calibration-rounds",
        json={
            "dataset_id": "evalset_quote_risk_v12",
            "dataset_version": "v12",
            "label_version": "label_v1_8_4",
            "rubric_version": "rubric_quote_risk_v3",
            "reviewer_ids": ["u_annotator_001", REVIEWER_B],
            "adjudicator_id": "u_admin_001",
            "samples": [
                {
                    "source_case_id": f"concurrency-{suffix}",
                    "evidence_ref": f"evidence://calibration/concurrency/{suffix}",
                }
            ],
        },
        headers=_headers(
            auth_headers,
            token="dev-token",
            key=f"calibration-concurrency-create-{suffix}",
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _assignment(client, auth_headers, *, round_id: str, token: str):
    response = client.get(
        f"/api/v1/calibration-assignments?mine=true&round_id={round_id}",
        headers=_headers(auth_headers, token=token),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"][0]


def _submit(
    client,
    auth_headers,
    *,
    assignment_id: str,
    token: str,
    value: int,
    key: str,
):
    return client.post(
        f"/api/v1/calibration-assignments/{assignment_id}/submissions",
        json={
            "value": {"decision": "pass" if value == 1 else "fail"},
            "expected_resource_version": 1,
        },
        headers=_headers(auth_headers, token=token, key=key),
    )


def test_concurrent_submission_to_one_assignment_has_one_winner(client, auth_headers):
    round_data = _create_round(client, auth_headers, suffix="submission")
    assignment = _assignment(
        client,
        auth_headers,
        round_id=round_data["round_id"],
        token="annotator-token",
    )
    start = Barrier(2)

    def submit(key: str, value: int):
        start.wait(timeout=5)
        return _submit(
            client,
            auth_headers,
            assignment_id=assignment["assignment_id"],
            token="annotator-token",
            value=value,
            key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda args: submit(*args),
                [
                    ("calibration-concurrent-submission-a", 1),
                    ("calibration-concurrent-submission-b", 0),
                ],
            )
        )

    assert sorted(response.status_code for response in responses) == [201, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] in {
        "CALIBRATION_ASSIGNMENT_ALREADY_SUBMITTED",
        "CALIBRATION_ASSIGNMENT_VERSION_CONFLICT",
    }


def _prepare_conflict(client, auth_headers, *, suffix: str):
    round_data = _create_round(client, auth_headers, suffix=suffix)
    assignment_a = _assignment(
        client,
        auth_headers,
        round_id=round_data["round_id"],
        token="annotator-token",
    )
    assignment_b = _assignment(
        client,
        auth_headers,
        round_id=round_data["round_id"],
        token="annotator-b-token",
    )
    assert (
        _submit(
            client,
            auth_headers,
            assignment_id=assignment_a["assignment_id"],
            token="annotator-token",
            value=1,
            key=f"calibration-{suffix}-submit-a",
        ).status_code
        == 201
    )
    assert (
        _submit(
            client,
            auth_headers,
            assignment_id=assignment_b["assignment_id"],
            token="annotator-b-token",
            value=0,
            key=f"calibration-{suffix}-submit-b",
        ).status_code
        == 201
    )
    response = client.get(
        f"/api/v1/calibration-rounds/{round_data['round_id']}/conflicts",
        headers=_headers(auth_headers, token="dev-token"),
    )
    assert response.status_code == 200, response.text
    return round_data, response.json()["data"]["items"][0]


def test_concurrent_adjudication_claim_is_recoverable_for_same_adjudicator(client, auth_headers):
    _, conflict = _prepare_conflict(client, auth_headers, suffix="claim")
    start = Barrier(2)

    def claim(key: str):
        start.wait(timeout=5)
        return client.post(
            f"/api/v1/calibration-items/{conflict['item_id']}/adjudication-claims",
            json={"expected_resource_version": conflict["resource_version"]},
            headers=_headers(auth_headers, token="dev-token", key=key),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                claim,
                ["calibration-concurrent-claim-a", "calibration-concurrent-claim-b"],
            )
        )

    assert [response.status_code for response in responses] == [200, 200]
    payloads = [response.json()["data"] for response in responses]
    assert len({payload["resource_version"] for payload in payloads}) == 1
    assert sum(bool(payload.get("replayed")) for payload in payloads) == 1


def test_concurrent_adjudication_has_one_winner(client, auth_headers):
    _, conflict = _prepare_conflict(client, auth_headers, suffix="adjudication")
    claim = client.post(
        f"/api/v1/calibration-items/{conflict['item_id']}/adjudication-claims",
        json={"expected_resource_version": conflict["resource_version"]},
        headers=_headers(
            auth_headers,
            token="dev-token",
            key="calibration-concurrent-adjudication-claim",
        ),
    )
    assert claim.status_code == 200, claim.text
    claimed_version = claim.json()["data"]["resource_version"]
    start = Barrier(2)

    def adjudicate(key: str, decision: str):
        start.wait(timeout=5)
        return client.post(
            f"/api/v1/calibration-items/{conflict['item_id']}/adjudications",
            json={
                "decision": decision,
                "reason": f"Concurrent adjudication result: {decision}",
                "expected_resource_version": claimed_version,
            },
            headers=_headers(auth_headers, token="dev-token", key=key),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda args: adjudicate(*args),
                [
                    ("calibration-concurrent-adjudication-a", "accept_a"),
                    ("calibration-concurrent-adjudication-b", "accept_b"),
                ],
            )
        )

    assert sorted(response.status_code for response in responses) == [201, 409]


def test_concurrent_gold_release_has_one_winner_and_one_version(client, auth_headers):
    round_data = _create_round(client, auth_headers, suffix="release")
    assignment_a = _assignment(
        client,
        auth_headers,
        round_id=round_data["round_id"],
        token="annotator-token",
    )
    assignment_b = _assignment(
        client,
        auth_headers,
        round_id=round_data["round_id"],
        token="annotator-b-token",
    )
    assert (
        _submit(
            client,
            auth_headers,
            assignment_id=assignment_a["assignment_id"],
            token="annotator-token",
            value=1,
            key="calibration-release-submit-a",
        ).status_code
        == 201
    )
    assert (
        _submit(
            client,
            auth_headers,
            assignment_id=assignment_b["assignment_id"],
            token="annotator-b-token",
            value=1,
            key="calibration-release-submit-b",
        ).status_code
        == 201
    )
    ready = client.get(
        f"/api/v1/calibration-rounds/{round_data['round_id']}",
        headers=_headers(auth_headers, token="dev-token"),
    ).json()["data"]
    assert ready["status"] == "ready"
    start = Barrier(2)

    def release(key: str):
        start.wait(timeout=5)
        return client.post(
            f"/api/v1/calibration-rounds/{round_data['round_id']}/gold-releases",
            json={
                "gold_set_key": "concurrent-gold",
                "expected_resource_version": ready["resource_version"],
            },
            headers=_headers(auth_headers, token="dev-token", key=key),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                release,
                ["calibration-concurrent-release-a", "calibration-concurrent-release-b"],
            )
        )

    assert sorted(response.status_code for response in responses) == [201, 409]
    with SessionLocal() as session:
        versions = (
            session.query(GoldSetVersion)
            .filter(GoldSetVersion.round_id == round_data["round_id"])
            .all()
        )
        assert len(versions) == 1
        assert (
            session.query(GoldAnnotation)
            .filter(GoldAnnotation.gold_set_version_id == versions[0].gold_set_version_id)
            .count()
            == 1
        )


def test_concurrent_first_publish_for_same_gold_series_is_retryable_and_sequential(
    client,
    auth_headers,
):
    ready_rounds: list[dict[str, object]] = []
    for suffix in ("series-a", "series-b"):
        round_data = _create_round(client, auth_headers, suffix=suffix)
        for token, value, key_suffix in (
            ("annotator-token", 1, "a"),
            ("annotator-b-token", 1, "b"),
        ):
            assignment = _assignment(
                client,
                auth_headers,
                round_id=round_data["round_id"],
                token=token,
            )
            response = _submit(
                client,
                auth_headers,
                assignment_id=assignment["assignment_id"],
                token=token,
                value=value,
                key=f"calibration-{suffix}-submit-{key_suffix}",
            )
            assert response.status_code == 201, response.text
        ready = client.get(
            f"/api/v1/calibration-rounds/{round_data['round_id']}",
            headers=_headers(auth_headers, token="dev-token"),
        ).json()["data"]
        assert ready["status"] == "ready"
        ready_rounds.append(ready)

    start = Barrier(2)

    def publish(ready: dict[str, object], key: str):
        start.wait(timeout=5)
        return client.post(
            f"/api/v1/calibration-rounds/{ready['round_id']}/gold-releases",
            json={
                "gold_set_key": "first-publish-series",
                "expected_resource_version": ready["resource_version"],
            },
            headers=_headers(auth_headers, token="dev-token", key=key),
        )

    keys = ["calibration-series-release-a", "calibration-series-release-b"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda pair: publish(*pair), zip(ready_rounds, keys, strict=True))
        )

    assert all(response.status_code in {201, 409} for response in responses)
    assert any(response.status_code == 201 for response in responses)
    for index, response in enumerate(responses):
        if response.status_code == 201:
            continue
        retry = client.post(
            f"/api/v1/calibration-rounds/{ready_rounds[index]['round_id']}/gold-releases",
            json={
                "gold_set_key": "first-publish-series",
                "expected_resource_version": ready_rounds[index]["resource_version"],
            },
            headers=_headers(auth_headers, token="dev-token", key=keys[index]),
        )
        assert retry.status_code == 201, retry.text

    with SessionLocal() as session:
        versions = (
            session.query(GoldSetVersion)
            .filter(GoldSetVersion.gold_set_key == "first-publish-series")
            .order_by(GoldSetVersion.version_number)
            .all()
        )
        assert [version.version_number for version in versions] == [1, 2]
        assert len({version.round_id for version in versions}) == 2
