from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.domain.label_policy import PolicyCompileError, compile_policy, evaluate_policy
from app.domain.label_policy.compiler import MAX_AST_DEPTH
from app.schemas.label_policy import (
    MAX_POLICY_SOURCE_BYTES,
    LabelPolicyDSL,
    LabelPolicyFacts,
    LabelPolicyValidationRequest,
    parse_strict_json_request,
)


def policy_payload(
    expression: dict,
    *,
    policy_kind: str = "label-candidate",
    effect: str = "block",
    default_effect: str = "pass",
) -> dict:
    return {
        "dsl_version": "1.0",
        "policy_kind": policy_kind,
        "policy_key": f"unit-{policy_kind}-policy",
        "revision": 1,
        "fact_schema_version": "label-policy-facts/1",
        "thresholds": [],
        "rules": [
            {
                "rule_id": "unit-rule",
                "priority": 100,
                "when": expression,
                "effect": effect,
                "reason_code": "UNIT_RULE_MATCHED",
            }
        ],
        "default_effect": default_effect,
    }


def compiled_policy(
    expression: dict,
    *,
    policy_kind: str = "label-candidate",
    effect: str = "block",
    default_effect: str = "pass",
):
    return compile_policy(
        LabelPolicyDSL.model_validate(
            policy_payload(
                expression,
                policy_kind=policy_kind,
                effect=effect,
                default_effect=default_effect,
            )
        )
    )


def test_canonical_hash_is_invariant_to_semantically_irrelevant_ordering():
    first = {
        "dsl_version": "1.0",
        "policy_kind": "label-candidate",
        "policy_key": "canonical-order-policy",
        "revision": 7,
        "fact_schema_version": "label-policy-facts/1",
        "thresholds": [
            {"key": "min-evidence", "type": "count", "value": 1},
            {"key": "min-confidence", "type": "ratio_ppm", "value": 900_000},
        ],
        "rules": [
            {
                "rule_id": "safe-candidate",
                "priority": 200,
                "when": {
                    "op": "all",
                    "items": [
                        {
                            "op": "in",
                            "path": "candidate.source_type",
                            "values": ["model_candidate", "deterministic_rule"],
                        },
                        {
                            "op": "gte",
                            "path": "candidate.confidence_ppm",
                            "threshold": "min-confidence",
                        },
                    ],
                },
                "effect": "pass",
                "reason_code": "SAFE_CANDIDATE",
            },
            {
                "rule_id": "has-evidence",
                "priority": 100,
                "when": {
                    "op": "gte",
                    "path": "evidence.valid_count",
                    "threshold": "min-evidence",
                },
                "effect": "gray_only",
                "reason_code": "EVIDENCE_PRESENT",
            },
        ],
        "default_effect": "require_review",
    }
    reordered = deepcopy(first)
    reordered["thresholds"].reverse()
    reordered["rules"].reverse()
    reordered["rules"][1]["when"]["items"].reverse()
    reordered["rules"][1]["when"]["items"][1]["values"].reverse()

    compiled_first = compile_policy(LabelPolicyDSL.model_validate(first))
    compiled_reordered = compile_policy(LabelPolicyDSL.model_validate(reordered))

    assert compiled_first.source_sha256 != compiled_reordered.source_sha256
    assert compiled_first.canonical_sha256 == compiled_reordered.canonical_sha256
    assert compiled_first.canonical_ast == compiled_reordered.canonical_ast


@pytest.mark.parametrize(
    ("expression", "version_matches", "expected_truth", "expected_verdict"),
    [
        (
            {
                "op": "all",
                "items": [
                    {"op": "eq", "path": "candidate.confidence_ppm", "value": 1},
                    {"op": "eq", "path": "candidate.version_matches", "value": True},
                ],
            },
            False,
            None,
            "pass",
        ),
        (
            {
                "op": "all",
                "items": [
                    {"op": "eq", "path": "candidate.confidence_ppm", "value": 1},
                    {"op": "eq", "path": "candidate.version_matches", "value": True},
                ],
            },
            True,
            "unknown",
            "require_review",
        ),
        (
            {
                "op": "any",
                "items": [
                    {"op": "eq", "path": "candidate.confidence_ppm", "value": 1},
                    {"op": "eq", "path": "candidate.version_matches", "value": True},
                ],
            },
            True,
            "true",
            "block",
        ),
        (
            {
                "op": "any",
                "items": [
                    {"op": "eq", "path": "candidate.confidence_ppm", "value": 1},
                    {"op": "eq", "path": "candidate.version_matches", "value": True},
                ],
            },
            False,
            "unknown",
            "require_review",
        ),
        (
            {
                "op": "not",
                "item": {"op": "eq", "path": "candidate.confidence_ppm", "value": 1},
            },
            True,
            "unknown",
            "require_review",
        ),
    ],
)
def test_three_valued_logic_is_kleene_safe(
    expression, version_matches, expected_truth, expected_verdict
):
    decision = evaluate_policy(
        compiled_policy(expression),
        LabelPolicyFacts.model_validate(
            {"candidate": {"confidence_ppm": None, "version_matches": version_matches}}
        ),
    )
    rule_checks = [check for check in decision.checks if check.rule_id == "unit-rule"]

    assert decision.verdict == expected_verdict
    if expected_truth is None:
        assert rule_checks == []
    else:
        assert [check.truth.value for check in rule_checks] == [expected_truth]


@pytest.mark.parametrize(
    ("policy_kind", "expected_verdict", "expected_reason"),
    [
        ("label-candidate", "require_review", "POLICY_FACT_UNKNOWN"),
        ("label-version-release", "block", "RELEASE_FACTS_INCOMPLETE"),
    ],
)
def test_unknown_facts_converge_to_safe_effect(
    policy_kind,
    expected_verdict,
    expected_reason,
):
    decision = evaluate_policy(
        compiled_policy(
            {"op": "gte", "path": "evaluation.sample_count", "value": 200},
            policy_kind=policy_kind,
        ),
        LabelPolicyFacts(),
    )

    assert decision.verdict == expected_verdict
    assert decision.primary_reason_code == expected_reason
    rule_check = next(check for check in decision.checks if check.rule_id == "unit-rule")
    assert rule_check.truth.value == "unknown"


def test_llm_candidate_is_forced_into_human_review_even_when_policy_passes():
    decision = evaluate_policy(
        compiled_policy(
            {"op": "eq", "path": "candidate.version_matches", "value": True},
            effect="pass",
        ),
        LabelPolicyFacts.model_validate(
            {
                "candidate": {
                    "source_type": "llm_candidate",
                    "version_matches": True,
                }
            }
        ),
    )

    assert decision.verdict == "require_review"
    assert decision.primary_reason_code == "LLM_CANDIDATE_REQUIRES_REVIEW"
    assert "system-llm-human-loop" in decision.matched_rule_ids
    assert decision.allowed_actions[0] == "create_human_review"


def test_candidate_cannot_overwrite_a_human_confirmed_label():
    decision = evaluate_policy(
        compiled_policy(
            {"op": "eq", "path": "candidate.version_matches", "value": True},
            effect="pass",
        ),
        LabelPolicyFacts.model_validate(
            {
                "candidate": {
                    "source_type": "model_candidate",
                    "version_matches": True,
                    "overwrites_human": True,
                }
            }
        ),
    )

    assert decision.verdict == "block"
    assert decision.primary_reason_code == "HUMAN_CONFIRMED_LABEL_IMMUTABLE"
    assert decision.allowed_actions[0] == "resolve_conflict"


def test_unknown_registered_path_is_rejected_by_compiler():
    policy = LabelPolicyDSL.model_validate(
        policy_payload({"op": "eq", "path": "candidate.unregistered", "value": True})
    )

    with pytest.raises(PolicyCompileError) as error:
        compile_policy(policy)

    assert error.value.code == "POLICY_PATH_UNKNOWN"
    assert error.value.path == "candidate.unregistered"


def test_compiler_rejects_threshold_dimension_mismatch():
    payload = policy_payload(
        {
            "op": "gte",
            "path": "candidate.confidence_ppm",
            "threshold": "minimum-confidence",
        }
    )
    payload["thresholds"] = [{"key": "minimum-confidence", "type": "count", "value": 900_000}]
    policy = LabelPolicyDSL.model_validate(payload)

    with pytest.raises(PolicyCompileError) as error:
        compile_policy(policy)

    assert error.value.code == "POLICY_THRESHOLD_DIMENSION_MISMATCH"
    assert error.value.path == "candidate.confidence_ppm"


@pytest.mark.parametrize(
    ("operator", "expected_verdict"),
    [("eq", "block"), ("ne", "pass")],
)
def test_null_literal_equality_has_explicit_semantics(operator, expected_verdict):
    decision = evaluate_policy(
        compiled_policy(
            {
                "op": operator,
                "path": "candidate.confidence_ppm",
                "value": None,
            }
        ),
        LabelPolicyFacts.model_validate(
            {"candidate": {"confidence_ppm": None, "version_matches": True}}
        ),
    )

    assert decision.verdict == expected_verdict
    rules = [check for check in decision.checks if check.rule_id == "unit-rule"]
    if operator == "eq":
        assert [rule.truth.value for rule in rules] == ["true"]
    else:
        assert rules == []


def test_unknown_operator_is_rejected_by_schema():
    payload = policy_payload({"op": "eq", "path": "candidate.version_matches", "value": True})
    payload["rules"][0]["when"]["op"] = "xor"

    with pytest.raises(ValidationError):
        LabelPolicyDSL.model_validate(payload)


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        (
            lambda policy: json.dumps(
                {
                    "policy": {
                        **policy,
                        "thresholds": [{"key": "fraction", "type": "ratio_ppm", "value": 0.5}],
                    },
                    "activate": False,
                }
            ).encode(),
            "POLICY_JSON_INVALID",
        ),
        (
            lambda policy: (
                '{"policy":' + json.dumps(policy) + ',"activate":false,"activate":true}'
            ).encode(),
            "POLICY_JSON_INVALID",
        ),
        (
            lambda _policy: b"{}" + b" " * MAX_POLICY_SOURCE_BYTES,
            "POLICY_RESOURCE_LIMIT",
        ),
    ],
)
def test_strict_json_rejects_float_duplicate_key_and_oversized_body(raw, expected_code):
    policy = policy_payload({"op": "eq", "path": "candidate.version_matches", "value": True})

    with pytest.raises(ApiError) as error:
        parse_strict_json_request(
            raw(policy),
            LabelPolicyValidationRequest,
            max_bytes=MAX_POLICY_SOURCE_BYTES,
        )

    assert error.value.code == expected_code
    assert error.value.status_code in {413, 422}


def test_compiler_rejects_excessive_ast_depth():
    expression: dict = {
        "op": "eq",
        "path": "candidate.version_matches",
        "value": True,
    }
    for _ in range(MAX_AST_DEPTH):
        expression = {"op": "not", "item": expression}
    policy = LabelPolicyDSL.model_validate(policy_payload(expression))

    with pytest.raises(PolicyCompileError) as error:
        compile_policy(policy)

    assert error.value.code == "POLICY_AST_TOO_DEEP"


def test_compiler_rejects_excessive_ast_node_count():
    payload = policy_payload({"op": "eq", "path": "candidate.version_matches", "value": True})
    payload["rules"] = [
        {
            "rule_id": f"rule-{index:03d}",
            "priority": index,
            "when": {
                "op": "all",
                "items": [
                    {"op": "eq", "path": "candidate.version_matches", "value": True},
                    {"op": "eq", "path": "candidate.overwrites_human", "value": False},
                    {"op": "eq", "path": "target.same_scope", "value": True},
                    {"op": "eq", "path": "conflicts.open_count", "value": 0},
                ],
            },
            "effect": "pass",
            "reason_code": f"RULE_{index:03d}",
        }
        for index in range(128)
    ]
    policy = LabelPolicyDSL.model_validate(payload)

    with pytest.raises(PolicyCompileError) as error:
        compile_policy(policy)

    assert error.value.code == "POLICY_AST_TOO_LARGE"
