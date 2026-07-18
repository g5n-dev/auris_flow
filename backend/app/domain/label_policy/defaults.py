from __future__ import annotations

from app.schemas.label_policy import LabelPolicyDSL


def default_candidate_policy() -> LabelPolicyDSL:
    return LabelPolicyDSL.model_validate(
        {
            "dsl_version": "1.0",
            "policy_kind": "label-candidate",
            "policy_key": "default-label-candidate",
            "revision": 1,
            "fact_schema_version": "label-policy-facts/1",
            "thresholds": [{"key": "auto-accept-confidence", "type": "ratio_ppm", "value": 900000}],
            "rules": [
                {
                    "rule_id": "safe-candidate",
                    "priority": 100,
                    "when": {
                        "op": "all",
                        "items": [
                            {
                                "op": "in",
                                "path": "candidate.source_type",
                                "values": ["deterministic_rule", "model_candidate"],
                            },
                            {
                                "op": "gte",
                                "path": "candidate.confidence_ppm",
                                "threshold": "auto-accept-confidence",
                            },
                            {
                                "op": "eq",
                                "path": "candidate.version_matches",
                                "value": True,
                            },
                            {
                                "op": "gte",
                                "path": "evidence.valid_count",
                                "value": 1,
                            },
                            {"op": "eq", "path": "conflicts.open_count", "value": 0},
                        ],
                    },
                    "effect": "pass",
                    "reason_code": "SAFE_CANDIDATE_ACCEPTED",
                    "evidence_group": "candidate",
                }
            ],
            "default_effect": "require_review",
        }
    )


def default_release_policy() -> LabelPolicyDSL:
    return LabelPolicyDSL.model_validate(
        {
            "dsl_version": "1.0",
            "policy_kind": "label-version-release",
            "policy_key": "default-label-release",
            "revision": 1,
            "fact_schema_version": "label-policy-facts/1",
            "thresholds": [
                {"key": "min-sample-count", "type": "count", "value": 200},
                {"key": "min-f1", "type": "ratio_ppm", "value": 880000},
                {"key": "min-json-validity", "type": "ratio_ppm", "value": 990000},
                {"key": "max-conflict-rate", "type": "ratio_ppm", "value": 49999},
            ],
            "rules": [
                {
                    "rule_id": "release-gates-pass",
                    "priority": 100,
                    "when": {
                        "op": "all",
                        "items": [
                            {"op": "eq", "path": "evaluation.status", "value": "success"},
                            {
                                "op": "eq",
                                "path": "evaluation.same_optimization_run",
                                "value": True,
                            },
                            {"op": "eq", "path": "evaluation.dataset_locked", "value": True},
                            {
                                "op": "gte",
                                "path": "evaluation.sample_count",
                                "threshold": "min-sample-count",
                            },
                            {
                                "op": "gte",
                                "path": "evaluation.labeling_f1_ppm",
                                "threshold": "min-f1",
                            },
                            {
                                "op": "gte",
                                "path": "evaluation.json_validity_ppm",
                                "threshold": "min-json-validity",
                            },
                            {
                                "op": "lte",
                                "path": "evaluation.conflict_rate_ppm",
                                "threshold": "max-conflict-rate",
                            },
                            {
                                "op": "eq",
                                "path": "evaluation.blocking_regression_count",
                                "value": 0,
                            },
                            {
                                "op": "eq",
                                "path": "evaluation.blocking_badcase_count",
                                "value": 0,
                            },
                            {"op": "eq", "path": "reviews.pending_count", "value": 0},
                            {"op": "eq", "path": "reviews.rejected_count", "value": 0},
                            {"op": "eq", "path": "impact.assets_confirmed", "value": True},
                            {
                                "op": "eq",
                                "path": "impact.downstream_incompatible_count",
                                "value": 0,
                            },
                            {"op": "eq", "path": "release.rollback_available", "value": True},
                        ],
                    },
                    "effect": "gray_only",
                    "reason_code": "RELEASE_GATES_PASSED",
                    "evidence_group": "release",
                }
            ],
            "default_effect": "block",
        }
    )
