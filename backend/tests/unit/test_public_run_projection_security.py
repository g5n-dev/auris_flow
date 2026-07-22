from __future__ import annotations

import pytest

from app.services.public_run_projection_service import public_run_payload, public_run_projection


def test_public_projection_recursively_omits_composite_storage_locator_keys() -> None:
    source = {
        "safe_business_fields": {
            "storage_objective_id": "objective-public-001",
            "object_key_metric": 0.97,
            "object_uri_template_name": "artifact-template-public",
            "bucketed_count": 4,
            "source_bucket_count": 2,
            "url": "https://internal.example/exact-url",
            "uri": "s3://internal-bucket/exact-uri",
            "artifact_url": "https://internal.example/suffix-url",
            "manifest_uri": "s3://internal-bucket/suffix-uri",
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


def test_only_completion_receipt_metrics_and_result_refs_use_strict_allowlists() -> None:
    projection = public_run_projection(
        {
            "metrics": {
                "domain_quality_score": 0.97,
                "object_uri_template_name": "domain-artifact-template",
                "download_url": "https://internal.example/domain-report.json",
            },
            "domain_summary": {
                "metrics": {
                    "business_conversion_rate": 0.42,
                    "bucketed_count": 4,
                }
            },
            "completion_receipt": {
                "completion_receipt_id": "receipt-public-001",
                "status": "success",
                "metrics": {
                    "materialized_partitions": 3,
                    "processed": 8,
                    "domain_quality_score": 0.97,
                    "object_uri_template_name": "must-not-cross-receipt-boundary",
                },
            },
            "result_ref": {
                "artifact_id": "artifact-public-001",
                "domain_quality_score": 0.97,
                "object_uri_template_name": "must-not-cross-result-ref-boundary",
            },
        }
    )

    assert projection["metrics"] == {
        "domain_quality_score": 0.97,
        "object_uri_template_name": "domain-artifact-template",
    }
    assert projection["domain_summary"]["metrics"] == {
        "business_conversion_rate": 0.42,
        "bucketed_count": 4,
    }
    assert projection["completion_receipt"]["metrics"] == {
        "materialized_partitions": 3,
        "processed": 8,
    }
    assert projection["result_ref"] == {"artifact_id": "artifact-public-001"}


@pytest.mark.parametrize(
    "network_locator",
    (
        "10.0.0.1",
        "127.0.0.1:9000",
        "2001:db8::1",
        "[::1]:9000",
    ),
)
def test_completion_result_ids_do_not_expose_ip_locators(network_locator: str) -> None:
    projection = public_run_projection(
        {
            "completion_receipt": {
                "completion_receipt_id": "receipt-public-001",
                "status": "success",
                "result_ref": {
                    "artifact_id": network_locator,
                    "action_id": network_locator,
                },
            }
        }
    )

    assert projection["completion_receipt"]["result_ref"] == {}


def test_completion_summary_omits_free_text_and_noncanonical_identifiers() -> None:
    projection = public_run_projection(
        {
            "completion_receipt": {
                "completion_receipt_id": "http://internal.example/receipt",
                "status": "failed",
                "note": "artifact at http://minio:9000/tenant-a/private.wav",
                "error_code": "http://internal.example/error",
                "result_ref": {},
                "metrics": {},
            }
        }
    )

    assert projection["completion_receipt"] == {
        "status": "failed",
        "result_ref": {},
        "metrics": {},
    }


def test_run_payload_closes_top_level_completion_aliases() -> None:
    projection = public_run_payload(
        {
            "status": "failed",
            "completion_receipt": {
                "completion_receipt_id": "receipt-safe-001",
                "status": "failed",
                "result_ref": {},
                "metrics": {
                    "processed": 1,
                    "unsafe_locator": "s3://private-bucket/result.json",
                },
                "note": "connect 10.0.0.1:9000 failed",
                "error_code": "SAFE_FAILURE",
            },
            "metrics": {
                "processed": 1,
                "unsafe_locator": "s3://private-bucket/result.json",
                "nested": {"safe": "http://10.0.0.1:9000/secret"},
            },
            "error": "failed reading s3://private-bucket/result.json",
            "error_code": "http://internal.example/error",
            "terminal_reason": "http://internal.example/error",
        }
    )

    assert projection["completion_receipt"] == {
        "completion_receipt_id": "receipt-safe-001",
        "status": "failed",
        "result_ref": {},
        "metrics": {"processed": 1},
        "error_code": "SAFE_FAILURE",
    }
    assert projection["metrics"] == {"processed": 1}
    assert "error" not in projection
    assert "error_code" not in projection
    assert "terminal_reason" not in projection


def test_public_projection_omits_credential_and_network_locator_canaries() -> None:
    projection = public_run_projection(
        {
            "access_key_id": "AKIA" + ("A" * 16),
            "provider_evidence": "internal-provider-receipt",
            "safe_uri": "s3://internal-bucket/private.wav",
            "safe_ip": "connect 10.0.0.1:9000 failed",
            "safe_host": "pod worker-0.internal unavailable",
            "note": "Authorization: Bearer production-token",
            "business_message": "evaluation completed",
        }
    )

    assert projection == {
        "note": "[REDACTED_SECRET]",
        "business_message": "evaluation completed",
    }


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
