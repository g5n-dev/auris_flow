from __future__ import annotations

import pytest

from app.services.public_run_projection_service import public_run_projection


def test_public_projection_recursively_omits_composite_storage_locator_keys() -> None:
    source = {
        "safe_business_fields": {
            "storage_objective_id": "objective-public-001",
            "object_key_metric": 0.97,
            "object_uri_template_name": "artifact-template-public",
            "bucketed_count": 4,
            "source_bucket_count": 2,
            "children": [
                {
                    "result_storage_object_id": "storage-secret-001",
                    "nested": {
                        "artifactObjectKey": "tenants/internal/result.json",
                        "preview-object-uri": "s3://internal/result.json",
                        "primary bucket": "internal-bucket",
                        "business_object_id": "business-public-001",
                    },
                }
            ],
        }
    }

    projection = public_run_projection(source)

    safe = projection["safe_business_fields"]
    assert safe["storage_objective_id"] == "objective-public-001"
    assert safe["object_key_metric"] == 0.97
    assert safe["object_uri_template_name"] == "artifact-template-public"
    assert safe["bucketed_count"] == 4
    assert safe["source_bucket_count"] == 2
    assert safe["children"] == [{"nested": {"business_object_id": "business-public-001"}}]


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "//attacker.example/run",
        "https://attacker.example/run",
        "HTTP://attacker.example/run",
        "javascript:alert(1)",
        "https：//attacker.example/run",
        "traces/trace-safe\r\nLocation:https://attacker.example",
        "\\\\attacker.example\\run",
        "/%2f%2fattacker.example/run",
        "traces/%2e%2e/admin",
    ),
)
def test_public_next_action_omits_non_local_navigation_paths(unsafe_path: str) -> None:
    projection = public_run_projection(
        {
            "next_actions": [
                {
                    "key": "view_result",
                    "label": "View result",
                    "href": unsafe_path,
                    "route": unsafe_path,
                }
            ]
        }
    )

    assert projection == {"next_actions": [{"key": "view_result", "label": "View result"}]}


@pytest.mark.parametrize(
    "safe_path",
    (
        "/runs/run-public-001",
        "traces/trace-public-001",
        "human-review-tasks?queue=voiceprint_enrollment",
        "/api/v1/exports/export-public-001/download",
    ),
)
def test_public_next_action_preserves_compatible_local_navigation_paths(
    safe_path: str,
) -> None:
    projection = public_run_projection(
        {
            "next_actions": [
                {
                    "key": "view_result",
                    "label": "View result",
                    "href": safe_path,
                    "route": safe_path,
                }
            ]
        }
    )

    assert projection["next_actions"][0]["href"] == safe_path
    assert projection["next_actions"][0]["route"] == safe_path
