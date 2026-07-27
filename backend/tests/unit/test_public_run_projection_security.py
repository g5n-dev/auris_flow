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


def test_completion_storage_summaries_alias_internal_roles_and_omit_unknown_roles() -> None:
    projection = public_run_projection(
        {
            "completion_receipt": {
                "completion_receipt_id": "receipt-safe-001",
                "status": "success",
                "result_ref": {
                    "storage_objects": [
                        {"role": "provider_artifact", "content_sha256": "a" * 64},
                        {"role": "manifest", "content_sha256": "b" * 64},
                        {"role": "provider_private", "content_sha256": "c" * 64},
                    ]
                },
                "metrics": {},
            }
        }
    )

    assert projection["completion_receipt"]["result_ref"]["storage_objects"] == [
        {"role": "compiled_artifact", "content_sha256": "a" * 64},
        {"role": "manifest", "content_sha256": "b" * 64},
    ]


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


def test_public_projection_does_not_trust_phone_like_candidate_hash_shapes() -> None:
    # Candidate identifiers can originate in an external completion callback.
    # A hash-looking prefix alone is not proof that the value is server-derived.
    candidate_ids = [
        "prompt_opt_01e9cf247401778c920bb022",
        "prompt_opt_a13800138000bcccccccccccc",
    ]

    projection = public_run_projection({"prompt_candidate_ids": candidate_ids})

    assert projection["prompt_candidate_ids"][0] == candidate_ids[0]
    assert projection["prompt_candidate_ids"][1] == ("prompt_opt_a[REDACTED_PHONE]bcccccccccccc")


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("id", "customer_13800138000"),
        ("customer_id", "customer_13800138000"),
        ("customer_ids", "customer_13800138000"),
        ("customerId", "customer_13800138000"),
        ("customer-id", "customer_13800138000"),
        ("user_id", "user_13800138000"),
        ("contact_id", "contact_13800138000"),
        ("order_id", "13800138000"),
        ("recording_id", "rec_13800138000"),
        ("run_id", "run_13800138000"),
        ("source_id", "source_13800138000"),
        ("asset_key", "customer/13800138000"),
        ("partition_key", "tenant/13800138000"),
    ),
)
def test_public_projection_does_not_use_identifier_fields_as_phone_pii_bypass(
    field_name: str,
    value: str,
) -> None:
    projection = public_run_projection({field_name: value})

    assert "13800138000" not in projection[field_name]
    assert "[REDACTED_PHONE]" in projection[field_name]


def test_public_projection_rejects_phone_pii_disguised_as_a_hash_identifier() -> None:
    projection = public_run_projection({"customer_id": "customer_ab13800138000abcdef123456"})

    assert projection["customer_id"] == "customer_ab[REDACTED_PHONE]abcdef123456"


def test_public_projection_continues_to_redact_ordinary_phone_values() -> None:
    projection = public_run_projection(
        {
            "phone": "13800138000",
            "note": "联系 13800138000 复核候选",
        }
    )

    assert projection == {
        "phone": "[REDACTED_PII]",
        "note": "联系 [REDACTED_PHONE] 复核候选",
    }


def test_public_projection_preserves_server_issued_experiment_sha256_values() -> None:
    digest = "a13800138000b" + ("c" * 51)
    source = {
        "experiment_design_sha256": digest,
        "experiment_subject_key_sha256": digest,
        "experiment_variant_diff_sha256": digest,
        "task_version_behavior_sha256": digest,
        "task_version_binding_sha256": digest,
        "expected_executed_bundle_sha256": digest,
        "scene_profile_snapshot_sha256": digest,
        "experiment_completion": {
            "design_sha256": digest,
            "completion_receipt_sha256": digest,
        },
    }

    assert public_run_projection(source) == source


@pytest.mark.parametrize(
    "field_name",
    (
        "note_sha256",
        "customer_sha256",
        "private_key_sha256",
        "design_sha256",
        "completion_receipt_sha256",
    ),
)
def test_public_projection_does_not_trust_arbitrary_sha256_field_names(
    field_name: str,
) -> None:
    digest = "a13800138000b" + ("c" * 51)

    projection = public_run_projection({field_name: digest})

    assert "13800138000" not in projection[field_name]
    assert "[REDACTED_PHONE]" in projection[field_name]


@pytest.mark.parametrize(
    "invalid_digest",
    (
        "a13800138000b" + ("c" * 50),
        "A13800138000B" + ("C" * 51),
    ),
)
def test_experiment_completion_rejects_noncanonical_sha256_values(
    invalid_digest: str,
) -> None:
    projection = public_run_projection(
        {
            "experiment_completion": {
                "completion_receipt_sha256": invalid_digest,
            }
        }
    )

    projected_digest = projection["experiment_completion"]["completion_receipt_sha256"]
    assert "13800138000" not in projected_digest
    assert "[REDACTED_PHONE]" in projected_digest


def test_public_projection_identifier_arrays_omit_hosts_and_never_restore_secrets() -> None:
    credential_canary = "".join(("ghp", "_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "123456"))

    projection = public_run_projection(
        {
            "prompt_candidate_ids": ["attacker.example", credential_canary],
            "promptCandidateIds": ["attacker.example"],
        }
    )

    assert projection["prompt_candidate_ids"] == ["[REDACTED_SECRET]"]
    assert projection["promptCandidateIds"] == []
    assert credential_canary not in str(projection)


def test_public_projection_omits_internal_agent_tool_plan() -> None:
    projection = public_run_projection(
        {
            "status": "submitted",
            "agent_tool_plan": [
                {
                    "key": "write_candidate_version",
                    "tool": "mysql.write_draft",
                    "purpose": "只写候选版本",
                }
            ],
            "agent_policy": {
                "allowed_writes": ["candidate", "draft"],
                "forbidden_writes": ["production_prompt"],
            },
        }
    )

    assert projection == {
        "status": "submitted",
        "agent_policy": {
            "allowed_writes": ["candidate", "draft"],
            "forbidden_writes": ["production_prompt"],
        },
    }


def test_public_projection_recursively_omits_execution_provider() -> None:
    projection = public_run_projection(
        {
            "status": "submitted",
            "provider": "auris-audio-stack",
            "task_version_snapshot": {
                "provider": "auris-audio-stack",
                "version": "v3.2.1",
            },
        }
    )

    assert projection == {
        "status": "submitted",
        "task_version_snapshot": {"version": "v3.2.1"},
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
