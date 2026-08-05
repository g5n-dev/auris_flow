from pathlib import Path

import yaml

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "doc" / "backend-spec" / "openapi-v0.1.yaml"


def test_static_openapi_matches_human_review_readback_shapes() -> None:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]
    decision_response = document["paths"]["/human-review-tasks/{id}/decisions"]["post"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert decision_response["$ref"].endswith("/HumanReviewDecisionReceiptResponse")

    receipt = schemas["HumanReviewDecisionReceipt"]
    assert receipt["additionalProperties"] is False
    assert {
        "resource_type",
        "resource_id",
        "root_trace_id",
        "current_trace_id",
        "readback_url",
        "readback_urls",
        "affected_objects",
        "next_actions",
    } <= set(receipt["required"])
    assert receipt["properties"]["status"]["enum"] == [
        "success",
        "blocked",
        "escalated",
    ]
    affected_ref = receipt["properties"]["affected_objects"]["items"]["$ref"]
    assert affected_ref.endswith("/HumanReviewAffectedObject")
    assert set(schemas["HumanReviewAffectedObject"]["required"]) == {
        "type",
        "id",
        "readback_url",
    }
    assert schemas["HumanReviewAffectedObject"]["properties"]["resource_version"]["minimum"] == 1

    task_list_data = schemas["HumanReviewTaskListResponse"]["properties"]["data"]
    assert task_list_data["type"] == "object"
    assert task_list_data["required"] == ["items"]
    assert task_list_data["properties"]["items"]["type"] == "array"


def test_runtime_human_review_receipt_and_server_queue_are_readback_driven(
    client,
    auth_headers,
) -> None:
    queue = client.get(
        "/api/v1/human-review-tasks",
        params={"status": "pending", "queue": "amount_conflict"},
        headers=auth_headers,
    )
    assert queue.status_code == 200
    assert isinstance(queue.json()["data"]["items"], list)

    response = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "契约回读测试"},
        headers={
            **auth_headers,
            "Idempotency-Key": "human-review-receipt-contract",
        },
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["data"]
    assert receipt["resource_type"] == "human_review_decision"
    assert receipt["resource_id"] == receipt["decision_id"]
    assert receipt["root_trace_id"] == receipt["trace_id"]
    assert receipt["current_trace_id"] == response.json()["meta"]["trace_id"]
    assert receipt["readback_url"].startswith("/api/v1/human-review-tasks/")
    assert receipt["readback_url"] in receipt["readback_urls"]
    assert all(set(item) >= {"type", "id", "readback_url"} for item in receipt["affected_objects"])
    assert len({item["readback_url"] for item in receipt["affected_objects"]}) == len(
        receipt["affected_objects"]
    )
    assert receipt["next_actions"][0]["route"].startswith(
        "/api/v1/human-review-tasks?status=pending&queue="
    )

    readback = client.get(receipt["readback_url"], headers=auth_headers)
    assert readback.status_code == 200
    assert readback.json()["data"]["decision_id"] == receipt["decision_id"]

    affected_readbacks = []
    for affected in receipt["affected_objects"]:
        affected_readback = client.get(affected["readback_url"], headers=auth_headers)
        assert affected_readback.status_code == 200, affected_readback.text
        data = affected_readback.json()["data"]
        assert data["type"] == affected["type"]
        assert data["id"] == affected["id"]
        assert data["review_decision_id"] == receipt["decision_id"]
        if "resource_version" in affected:
            assert data["resource_version"] == affected["resource_version"]
        affected_readbacks.append(data)
    assert {(item["type"], item["id"]) for item in affected_readbacks} == {
        (item["type"], item["id"]) for item in receipt["affected_objects"]
    }


def test_human_review_affected_object_readback_is_decision_scoped(
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "作用域校验"},
        headers={
            **auth_headers,
            "Idempotency-Key": "human-review-readback-scope",
        },
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["data"]
    task_object = next(
        item for item in receipt["affected_objects"] if item["type"] == "human_review_task"
    )

    unbound = client.get(
        task_object["readback_url"].replace(
            "/human_review_task/hrt_amount_001",
            "/human_review_task/hrt_appeal_001",
        ),
        headers=auth_headers,
    )
    assert unbound.status_code == 404
    assert unbound.json()["error"]["code"] == "HUMAN_REVIEW_AFFECTED_OBJECT_NOT_FOUND"
