from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.domain.label_policy.canonical import sha256_json
from app.domain.label_policy.compiler import CompiledPolicy
from app.domain.label_policy.registry import PATH_REGISTRY_V1
from app.schemas.label_policy import (
    CompareExpression,
    Expression,
    LabelPolicyFacts,
    LogicalExpression,
    NotExpression,
    NullExpression,
    PolicyEffect,
    SetExpression,
)


class Truth(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


EFFECT_SEVERITY: dict[PolicyEffect, int] = {
    "pass": 0,
    "gray_only": 1,
    "require_review": 2,
    "block": 3,
}

ALLOWED_ACTIONS: dict[PolicyEffect, list[str]] = {
    "pass": ["save_candidate", "view_trace"],
    "gray_only": ["request_gray_release", "view_trace"],
    "require_review": ["create_human_review", "view_trace"],
    "block": ["resolve_conflict", "view_trace"],
}


@dataclass(frozen=True)
class PolicyCheckResult:
    rule_id: str
    truth: Truth
    effect: PolicyEffect
    reason_code: str
    priority: int
    evidence_group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "truth": self.truth.value,
            "effect": self.effect,
            "reason_code": self.reason_code,
            "priority": self.priority,
            "evidence_group": self.evidence_group,
        }


@dataclass(frozen=True)
class DecisionCore:
    verdict: PolicyEffect
    allowed_actions: list[str]
    primary_reason_code: str
    checks: list[PolicyCheckResult]
    matched_rule_ids: list[str]
    policy_sha256: str
    facts_sha256: str
    engine_version: str = "label-policy-evaluator/1.0.0"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "verdict": self.verdict,
            "allowed_actions": self.allowed_actions,
            "primary_reason_code": self.primary_reason_code,
            "checks": [check.to_dict() for check in self.checks],
            "matched_rule_ids": self.matched_rule_ids,
            "policy_sha256": self.policy_sha256,
            "facts_sha256": self.facts_sha256,
            "engine_version": self.engine_version,
        }
        return {**payload, "decision_sha256": sha256_json(payload)}


class ExecutionBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.steps = 0

    def consume(self) -> None:
        self.steps += 1
        if self.steps > self.maximum:
            raise RuntimeError("policy execution step budget exceeded")


def _truth_not(value: Truth) -> Truth:
    if value is Truth.TRUE:
        return Truth.FALSE
    if value is Truth.FALSE:
        return Truth.TRUE
    return Truth.UNKNOWN


def _resolve_rhs(expression: CompareExpression, thresholds: dict[str, int]) -> object:
    if expression.threshold is not None:
        return thresholds[expression.threshold]
    return expression.value


def _evaluate_expression(
    expression: Expression,
    facts: LabelPolicyFacts,
    thresholds: dict[str, int],
    budget: ExecutionBudget,
) -> Truth:
    budget.consume()
    if isinstance(expression, LogicalExpression):
        values = [
            _evaluate_expression(item, facts, thresholds, budget) for item in expression.items
        ]
        if expression.op == "all":
            if Truth.FALSE in values:
                return Truth.FALSE
            return Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.TRUE
        if Truth.TRUE in values:
            return Truth.TRUE
        return Truth.UNKNOWN if Truth.UNKNOWN in values else Truth.FALSE
    if isinstance(expression, NotExpression):
        return _truth_not(_evaluate_expression(expression.item, facts, thresholds, budget))

    value = PATH_REGISTRY_V1[expression.path].get(facts)
    if isinstance(expression, NullExpression):
        is_null = value is None
        expected = is_null if expression.op == "is_null" else not is_null
        return Truth.TRUE if expected else Truth.FALSE
    if isinstance(expression, SetExpression):
        if value is None:
            return Truth.UNKNOWN
        contained = value in expression.values
        matched = contained if expression.op == "in" else not contained
        return Truth.TRUE if matched else Truth.FALSE

    rhs = _resolve_rhs(expression, thresholds)
    if rhs is None:
        matched = value is None if expression.op == "eq" else value is not None
        return Truth.TRUE if matched else Truth.FALSE
    if value is None:
        return Truth.UNKNOWN
    comparators: dict[str, Callable[[Any, Any], bool]] = {
        "eq": operator.eq,
        "ne": operator.ne,
        "lt": operator.lt,
        "lte": operator.le,
        "gt": operator.gt,
        "gte": operator.ge,
    }
    return Truth.TRUE if comparators[expression.op](value, rhs) else Truth.FALSE


def _synthetic_check(
    *,
    rule_id: str,
    effect: PolicyEffect,
    reason_code: str,
    priority: int,
) -> PolicyCheckResult:
    return PolicyCheckResult(
        rule_id=rule_id,
        truth=Truth.TRUE,
        effect=effect,
        reason_code=reason_code,
        priority=priority,
    )


def _hard_constraints(
    facts: LabelPolicyFacts,
    compiled: CompiledPolicy,
) -> list[PolicyCheckResult]:
    checks: list[PolicyCheckResult] = []
    policy_kind = compiled.policy.policy_kind
    if facts.target.same_scope is False or (facts.evidence.cross_scope_count or 0) > 0:
        checks.append(
            _synthetic_check(
                rule_id="system-scope-integrity",
                effect="block",
                reason_code="CROSS_SCOPE_EVIDENCE_BLOCKED",
                priority=10_000,
            )
        )
    if facts.candidate.overwrites_human is True:
        checks.append(
            _synthetic_check(
                rule_id="system-human-precedence",
                effect="block",
                reason_code="HUMAN_CONFIRMED_LABEL_IMMUTABLE",
                priority=9_999,
            )
        )
    if (facts.evidence.invalid_window_count or 0) > 0 or (
        facts.evidence.missing_checksum_count or 0
    ) > 0:
        checks.append(
            _synthetic_check(
                rule_id="system-evidence-integrity",
                effect="block",
                reason_code="EVIDENCE_INTEGRITY_INVALID",
                priority=9_998,
            )
        )
    if (
        policy_kind == "label-candidate"
        and facts.provenance.target_type == "label_candidate"
        and (
            facts.evidence.total_count in {None, 0}
            or (facts.evidence.pending_count or 0) > 0
            or (facts.evidence.stale_count or 0) > 0
        )
    ):
        checks.append(
            _synthetic_check(
                rule_id="system-evidence-not-ready",
                effect="require_review",
                reason_code="EVIDENCE_NOT_READY",
                priority=9_997,
            )
        )
    if facts.candidate.business_document_conflict is True:
        checks.append(
            _synthetic_check(
                rule_id="system-business-document-conflict",
                effect="require_review",
                reason_code="BUSINESS_DOCUMENT_CONFLICT_REQUIRES_REVIEW",
                priority=9_900,
            )
        )
    if (
        (facts.conflicts.high_risk_open_count or 0) > 0
        or (facts.conflicts.human_disagreement_count or 0) > 0
        or (facts.conflicts.equal_precedence_count or 0) > 0
    ):
        checks.append(
            _synthetic_check(
                rule_id="system-open-conflict",
                effect="require_review" if policy_kind == "label-candidate" else "block",
                reason_code="OPEN_LABEL_CONFLICT",
                priority=9_850,
            )
        )
    if policy_kind == "label-version-release" and facts.actor.kind in {"agent", "llm"}:
        checks.append(
            _synthetic_check(
                rule_id="system-agent-publish-denied",
                effect="block",
                reason_code="AGENT_CANNOT_PUBLISH_LABEL_VERSION",
                priority=10_000,
            )
        )
    if policy_kind == "label-version-release":
        required_release_facts = (
            facts.evaluation.status,
            facts.evaluation.same_optimization_run,
            facts.evaluation.dataset_locked,
            facts.evaluation.sample_count,
            facts.evaluation.labeling_f1_ppm,
            facts.evaluation.conflict_rate_ppm,
            facts.evaluation.json_validity_ppm,
            facts.evaluation.blocking_regression_count,
            facts.evaluation.blocking_badcase_count,
            facts.reviews.pending_count,
            facts.impact.assets_confirmed,
            facts.impact.downstream_incompatible_count,
            facts.release.rollback_available,
        )
        if any(value is None for value in required_release_facts):
            checks.append(
                _synthetic_check(
                    rule_id="system-release-facts-complete",
                    effect="block",
                    reason_code="RELEASE_FACTS_INCOMPLETE",
                    priority=9_997,
                )
            )
        elif (
            facts.evaluation.status != "success"
            or facts.evaluation.same_optimization_run is not True
            or facts.evaluation.dataset_locked is not True
            or (facts.evaluation.blocking_regression_count or 0) > 0
            or (facts.evaluation.blocking_badcase_count or 0) > 0
            or (facts.reviews.pending_count or 0) > 0
            or facts.impact.assets_confirmed is not True
            or (facts.impact.downstream_incompatible_count or 0) > 0
            or facts.release.rollback_available is not True
        ):
            checks.append(
                _synthetic_check(
                    rule_id="system-release-safety-gate",
                    effect="block",
                    reason_code="RELEASE_SAFETY_GATE_BLOCKED",
                    priority=9_997,
                )
            )

    source_type = facts.candidate.source_type
    competing_source_type = facts.candidate.competing_source_type
    precedence = compiled.policy.precedence.sources
    if policy_kind == "label-candidate" and (
        (source_type is None and facts.provenance.target_type == "label_candidate")
        or (source_type is not None and source_type not in precedence)
    ):
        checks.append(
            _synthetic_check(
                rule_id="system-source-precedence-unknown",
                effect="block",
                reason_code="CANDIDATE_SOURCE_NOT_GOVERNED",
                priority=9_996,
            )
        )
    elif competing_source_type is not None:
        if competing_source_type not in precedence:
            checks.append(
                _synthetic_check(
                    rule_id="system-competing-source-unknown",
                    effect="block",
                    reason_code="COMPETING_SOURCE_NOT_GOVERNED",
                    priority=9_996,
                )
            )
        elif source_type is not None:
            candidate_rank = precedence.index(source_type)
            competing_rank = precedence.index(competing_source_type)
            if candidate_rank >= competing_rank:
                checks.append(
                    _synthetic_check(
                        rule_id="system-source-precedence",
                        effect=(
                            "block"
                            if competing_source_type == "human_confirmed"
                            else "require_review"
                        ),
                        reason_code="HIGHER_PRECEDENCE_SOURCE_CONFLICT",
                        priority=9_995,
                    )
                )
    return checks


def evaluate_policy(compiled: CompiledPolicy, facts: LabelPolicyFacts) -> DecisionCore:
    threshold_values = {threshold.key: threshold.value for threshold in compiled.policy.thresholds}
    budget = ExecutionBudget(compiled.execution_step_budget)
    checks = _hard_constraints(facts, compiled)
    matched = False
    for rule in compiled.policy.rules:
        truth = _evaluate_expression(rule.when, facts, threshold_values, budget)
        if truth is Truth.TRUE:
            matched = True
            checks.append(
                PolicyCheckResult(
                    rule_id=rule.rule_id,
                    truth=truth,
                    effect=rule.effect,
                    reason_code=rule.reason_code,
                    priority=rule.priority,
                    evidence_group=rule.evidence_group,
                )
            )
        elif truth is Truth.UNKNOWN:
            matched = True
            checks.append(
                PolicyCheckResult(
                    rule_id=rule.rule_id,
                    truth=truth,
                    effect=(
                        "block"
                        if compiled.policy.policy_kind == "label-version-release"
                        else "require_review"
                    ),
                    reason_code="POLICY_FACT_UNKNOWN",
                    priority=rule.priority,
                    evidence_group=rule.evidence_group,
                )
            )
    if not matched:
        checks.append(
            _synthetic_check(
                rule_id="policy-default",
                effect=compiled.policy.default_effect,
                reason_code="POLICY_DEFAULT_EFFECT",
                priority=0,
            )
        )

    if (
        compiled.policy.policy_kind == "label-candidate"
        and facts.candidate.source_type == "llm_candidate"
        and all(check.effect not in {"block", "require_review"} for check in checks)
    ):
        checks.append(
            _synthetic_check(
                rule_id="system-llm-human-loop",
                effect="require_review",
                reason_code="LLM_CANDIDATE_REQUIRES_REVIEW",
                priority=9_700,
            )
        )

    ordered = sorted(
        checks,
        key=lambda check: (
            -EFFECT_SEVERITY[check.effect],
            -check.priority,
            check.rule_id,
            check.reason_code,
        ),
    )
    primary = ordered[0]
    facts_payload = facts.model_dump(mode="json", exclude_none=False)
    return DecisionCore(
        verdict=primary.effect,
        allowed_actions=ALLOWED_ACTIONS[primary.effect],
        primary_reason_code=primary.reason_code,
        checks=ordered,
        matched_rule_ids=sorted(
            check.rule_id for check in ordered if check.rule_id != "policy-default"
        ),
        policy_sha256=compiled.canonical_sha256,
        facts_sha256=sha256_json(facts_payload),
    )
