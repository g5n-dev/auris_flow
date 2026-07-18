from __future__ import annotations

from fastapi.testclient import TestClient


def _headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def test_label_recompute_runtime_openapi_freezes_public_contract(
    client: TestClient,
) -> None:
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    expected = {
        "/api/v1/label-recompute-runs": ("201", "postLabelRecomputeRuns"),
        "/api/v1/label-recompute-runs/{run_id}/items/{item_id}/completions": (
            "200",
            "postLabelRecomputeRunItemsByIdCompletions",
        ),
        "/api/v1/label-recompute-runs/{run_id}/items/{item_id}/retries": (
            "200",
            "postLabelRecomputeRunItemsByIdRetries",
        ),
    }
    for path, (success_status, operation_id) in expected.items():
        operation = paths[path]["post"]
        assert operation["operationId"] == operation_id
        assert success_status in operation["responses"]
        assert {"400", "401", "403", "404", "409", "422"}.issubset(operation["responses"])

    schemas = document["components"]["schemas"]
    completion = schemas["LabelRecomputeRunItemCompletionRequest"]
    assert completion["additionalProperties"] is False
    assert "row_count" not in completion["properties"]
    assert "source_manifest_sha256" not in completion["properties"]
    assert "result_manifest_sha256" not in completion["properties"]

    for response_name in (
        "LabelRecomputeMutationResponse",
        "LabelRecomputeRunItemMutationResponse",
    ):
        properties = schemas[response_name]["properties"]
        assert "execution_run_id" not in properties
        assert "dagster_run_id" not in properties
        assert "dispatch" not in properties


def test_label_recompute_completion_rejects_caller_claimed_counts_and_hashes(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/label-recompute-runs/run-forged/items/item-forged/completions",
        headers=_headers(auth_headers, "recompute-forged-completion"),
        json={
            "attempt_generation": 1,
            "completion_receipt_id": "receipt-forged",
            "status": "success",
            "facts": [],
            "row_count": 999,
            "result_manifest_sha256": "f" * 64,
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    rejected = {detail["field"] for detail in payload["error"]["details"]}
    assert "body.row_count" in rejected
    assert "body.result_manifest_sha256" in rejected
