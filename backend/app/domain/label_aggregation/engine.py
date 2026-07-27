from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.domain.label_aggregation.types import (
    AggregateDecision,
    AggregationBatch,
    AggregationExplanation,
    AggregationMode,
    AggregationPolicy,
    LabelAggregate,
    LabelDefinition,
    LabelKind,
    LabelObservation,
    ObservationContribution,
    RiskLevel,
    SourceType,
    TimeSpan,
    UnknownLabelSuggestion,
)

SOURCE_PRIORITY: dict[SourceType, int] = {
    SourceType.HUMAN_CONFIRMED: 6,
    SourceType.VERIFIED_BUSINESS_DOCUMENT: 5,
    SourceType.DETERMINISTIC_RULE: 4,
    SourceType.MODEL: 3,
    SourceType.LLM: 2,
    SourceType.INFERRED: 1,
}

_PROBABILITY_EPSILON = 1e-12
_PUBLIC_IDENTIFIER_HEX_TO_ALPHA = str.maketrans(
    "0123456789abcdef",
    "abcdefghijklmnop",
)


def normalize_text(value: str) -> str:
    """Return the stable comparison form used for label names and string values."""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _public_id_from_hex(prefix: str, digest: str, *, suffix_length: int) -> str:
    """Keep the domain pure while making deterministic public IDs PII-safe."""

    return f"{prefix}_{digest[:suffix_length].translate(_PUBLIC_IDENTIFIER_HEX_TO_ALPHA)}"


def _clamp_probability(value: float) -> float:
    return min(max(value, _PROBABILITY_EPSILON), 1 - _PROBABILITY_EPSILON)


def _logit(value: float) -> float:
    probability = _clamp_probability(value)
    return math.log(probability / (1 - probability))


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return round(1 / (1 + inverse), 15)
    exponential = math.exp(value)
    return round(exponential / (1 + exponential), 15)


def _value_key(value: object) -> str:
    if isinstance(value, TimeSpan):
        serializable: object = value.to_dict()
    elif isinstance(value, tuple):
        serializable = list(value)
    else:
        serializable = value
    return _canonical_bytes(serializable).decode("ascii")


@dataclass(frozen=True, slots=True)
class _CanonicalObservation:
    definition: LabelDefinition
    source: LabelObservation
    value: object

    @property
    def effective_confidence(self) -> float:
        if self.source.calibrated_confidence is not None:
            return self.source.calibrated_confidence
        return self.source.raw_confidence

    @property
    def confidence_basis(self) -> str:
        return "calibrated" if self.source.calibrated_confidence is not None else "raw"


@dataclass(frozen=True, slots=True)
class _PreparedObservations:
    included: tuple[_CanonicalObservation, ...]
    contributions: tuple[ObservationContribution, ...]
    source_precedence: SourceType


@dataclass(frozen=True, slots=True)
class _OperatorResult:
    value: object
    score: float
    margin: float | None
    operator: str
    conflict_reasons: tuple[str, ...] = ()
    candidate_scores: tuple[tuple[str, float], ...] = ()
    ancestor_label_ids: tuple[str, ...] = ()


class LabelAggregationEngine:
    """Pure, deterministic label canonicalization and aggregation engine."""

    def __init__(
        self,
        labels: Iterable[LabelDefinition],
        policy: AggregationPolicy,
    ) -> None:
        definitions = tuple(sorted(labels, key=lambda item: item.label_id))
        if not definitions:
            raise ValueError("at least one label definition is required")
        label_ids = [definition.label_id for definition in definitions]
        if len(label_ids) != len(set(label_ids)):
            raise ValueError("label_id values must be unique")

        aliases: dict[str, LabelDefinition] = {}
        for definition in definitions:
            for raw_alias in (definition.canonical_name, *definition.aliases):
                alias = normalize_text(raw_alias)
                if not alias:
                    raise ValueError("canonical label aliases must not be empty")
                existing = aliases.get(alias)
                if existing is not None and existing.label_id != definition.label_id:
                    raise ValueError(
                        "ambiguous canonical label alias "
                        f"{raw_alias!r}: {existing.label_id!r} and {definition.label_id!r}"
                    )
                aliases[alias] = definition

        self._definitions = definitions
        self._aliases = aliases
        self._policy = policy
        self._policy_hash = _sha256(self._policy_payload())
        self._label_set_hash = _sha256(self._label_payload())

    def aggregate(self, observations: Iterable[LabelObservation]) -> AggregationBatch:
        canonical_groups: dict[tuple[str, str, str], list[_CanonicalObservation]] = defaultdict(
            list
        )
        unknown_groups: dict[str, list[LabelObservation]] = defaultdict(list)

        for observation in observations:
            normalized_label = normalize_text(observation.raw_label)
            definition = self._aliases.get(normalized_label)
            if definition is None:
                unknown_groups[normalized_label].append(observation)
                continue
            canonical_groups[
                (observation.subject_scope, observation.subject_key, definition.label_id)
            ].append(
                _CanonicalObservation(
                    definition=definition,
                    source=observation,
                    value=self._normalize_value(definition, observation.value),
                )
            )

        base_aggregates = tuple(
            self._aggregate_group(group)
            for _, group in sorted(canonical_groups.items(), key=lambda item: item[0])
        )
        aggregates = self._apply_mutex_constraints(base_aggregates)
        suggestions = tuple(
            UnknownLabelSuggestion(
                normalized_label=normalized_label,
                raw_labels=tuple(sorted({item.raw_label for item in group})),
                observation_ids=tuple(sorted(item.observation_id for item in group)),
            )
            for normalized_label, group in sorted(unknown_groups.items())
        )
        payload = {
            "aggregates": [aggregate.to_dict() for aggregate in aggregates],
            "unknown_suggestions": [suggestion.to_dict() for suggestion in suggestions],
            "policy_hash": self._policy_hash,
            "label_set_hash": self._label_set_hash,
            "engine_version": "label-aggregation/1.0.0",
        }
        return AggregationBatch(
            aggregates=aggregates,
            unknown_suggestions=suggestions,
            policy_hash=self._policy_hash,
            label_set_hash=self._label_set_hash,
            canonical_hash=_sha256(payload),
        )

    def _aggregate_group(
        self,
        observations: Sequence[_CanonicalObservation],
    ) -> LabelAggregate:
        prepared = self._prepare_observations(observations)
        definition = observations[0].definition
        operator_result = self._apply_operator(definition, prepared.included)
        decision, reason_codes = self._route_decision(
            definition,
            prepared,
            operator_result,
        )
        explanation = AggregationExplanation(
            operator=operator_result.operator,
            source_precedence=prepared.source_precedence,
            independent_source_families=tuple(
                sorted({item.source.source_family for item in prepared.included})
            ),
            contributions=prepared.contributions,
            candidate_scores=operator_result.candidate_scores,
        )
        identity_payload = {
            "subject_scope": observations[0].source.subject_scope,
            "subject_key": observations[0].source.subject_key,
            "label_id": definition.label_id,
            "policy_hash": self._policy_hash,
        }
        aggregate_id = _public_id_from_hex(
            "agg",
            _sha256(identity_payload),
            suffix_length=24,
        )
        aggregate_payload = {
            **identity_payload,
            "aggregate_id": aggregate_id,
            "value": self._serialize_value(operator_result.value),
            "score": operator_result.score,
            "margin": operator_result.margin,
            "decision": decision.value,
            "reason_codes": list(reason_codes),
            "ancestor_label_ids": list(operator_result.ancestor_label_ids),
            "explanation": explanation.to_dict(),
        }
        return LabelAggregate(
            aggregate_id=aggregate_id,
            subject_scope=observations[0].source.subject_scope,
            subject_key=observations[0].source.subject_key,
            label_id=definition.label_id,
            value=operator_result.value,
            score=operator_result.score,
            margin=operator_result.margin,
            decision=decision,
            reason_codes=reason_codes,
            ancestor_label_ids=operator_result.ancestor_label_ids,
            explanation=explanation,
            canonical_hash=_sha256(aggregate_payload),
        )

    def _prepare_observations(
        self,
        observations: Sequence[_CanonicalObservation],
    ) -> _PreparedObservations:
        correlated: dict[str, list[_CanonicalObservation]] = defaultdict(list)
        interval_candidates: dict[tuple[str, str], list[_CanonicalObservation]] = defaultdict(list)
        for item in sorted(observations, key=lambda value: value.source.observation_id):
            source = item.source
            if source.correlation_group_id:
                # A trusted extraction manifest groups repeated slices from the
                # same provider/model/prompt/run. Prediction value is deliberately
                # absent: contradictory duplicates are still one correlated source.
                correlated[f"lineage:{source.correlation_group_id}"].append(item)
            elif (
                source.evidence_ref_id
                and source.evidence_start is not None
                and source.evidence_end is not None
            ):
                interval_candidates[(source.source_family, source.evidence_ref_id)].append(item)
            elif source.evidence_hash:
                correlated[f"evidence:{source.source_family}:{source.evidence_hash}"].append(item)
            else:
                correlated[f"missing:{source.observation_id}"].append(item)

        for (source_family, evidence_ref_id), candidates in sorted(interval_candidates.items()):
            ranked_intervals = sorted(
                candidates,
                key=lambda value: (
                    float(value.source.evidence_start or 0),
                    float(value.source.evidence_end or 0),
                    value.source.observation_id,
                ),
            )
            cluster_index = 0
            cluster_end: float | None = None
            for item in ranked_intervals:
                start = float(item.source.evidence_start or 0)
                end = float(item.source.evidence_end or 0)
                if cluster_end is None or start >= cluster_end:
                    cluster_index += 1
                    cluster_end = end
                else:
                    cluster_end = max(cluster_end, end)
                correlated[f"interval:{source_family}:{evidence_ref_id}:{cluster_index}"].append(
                    item
                )

        included_candidates: list[_CanonicalObservation] = []
        excluded_reasons: dict[str, str] = {}
        for group in correlated.values():
            ranked = sorted(
                group,
                key=lambda item: (
                    -SOURCE_PRIORITY[item.source.source_type],
                    -item.effective_confidence,
                    item.source.observation_id,
                ),
            )
            included_candidates.append(ranked[0])
            excluded_reasons.update(
                {
                    duplicate.source.observation_id: "CORRELATED_DUPLICATE"
                    for duplicate in ranked[1:]
                }
            )

        maximum_priority = max(
            SOURCE_PRIORITY[item.source.source_type] for item in included_candidates
        )
        included = tuple(
            sorted(
                (
                    item
                    for item in included_candidates
                    if SOURCE_PRIORITY[item.source.source_type] == maximum_priority
                ),
                key=lambda item: item.source.observation_id,
            )
        )
        for item in included_candidates:
            if SOURCE_PRIORITY[item.source.source_type] < maximum_priority:
                excluded_reasons[item.source.observation_id] = "LOWER_SOURCE_PRECEDENCE"

        contributions = tuple(
            self._build_contribution(
                item,
                included=item.source.observation_id not in excluded_reasons,
                exclusion_reason=excluded_reasons.get(item.source.observation_id),
            )
            for item in sorted(observations, key=lambda item: item.source.observation_id)
        )
        source_precedence = max(
            (item.source.source_type for item in included),
            key=lambda source_type: SOURCE_PRIORITY[source_type],
        )
        return _PreparedObservations(
            included=included,
            contributions=contributions,
            source_precedence=source_precedence,
        )

    def _build_contribution(
        self,
        item: _CanonicalObservation,
        *,
        included: bool,
        exclusion_reason: str | None,
    ) -> ObservationContribution:
        negative_boolean = (
            item.definition.kind in {LabelKind.BOOLEAN, LabelKind.HIERARCHY} and not item.value
        )
        sign = -1 if negative_boolean else 1
        source_weight = self._policy.weight_for(item.source.source_family)
        return ObservationContribution(
            observation_id=item.source.observation_id,
            source_family=item.source.source_family,
            source_type=item.source.source_type,
            source_priority=SOURCE_PRIORITY[item.source.source_type],
            effective_confidence=item.effective_confidence,
            confidence_basis=item.confidence_basis,
            source_weight=source_weight,
            weighted_log_odds=sign * source_weight * _logit(item.effective_confidence),
            included=included,
            exclusion_reason=exclusion_reason,
        )

    def _apply_operator(
        self,
        definition: LabelDefinition,
        observations: Sequence[_CanonicalObservation],
    ) -> _OperatorResult:
        if definition.kind in {LabelKind.BOOLEAN, LabelKind.HIERARCHY}:
            return self._aggregate_boolean(definition, observations)
        if definition.kind is LabelKind.CATEGORICAL:
            return self._aggregate_categorical(observations)
        if definition.kind is LabelKind.MULTI:
            return self._aggregate_multi(observations)
        if definition.kind is LabelKind.NUMERIC:
            return self._aggregate_numeric(definition, observations)
        if definition.kind is LabelKind.TEMPORAL:
            return self._aggregate_temporal(observations)
        raise ValueError(f"unsupported label kind: {definition.kind}")

    def _aggregate_boolean(
        self,
        definition: LabelDefinition,
        observations: Sequence[_CanonicalObservation],
    ) -> _OperatorResult:
        total = _logit(self._policy.prior_probability)
        values: set[bool] = set()
        for item in observations:
            value = bool(item.value)
            values.add(value)
            sign = 1 if value else -1
            total += (
                sign
                * self._policy.weight_for(item.source.source_family)
                * _logit(item.effective_confidence)
            )
        posterior = _sigmoid(total)
        value = posterior >= 0.5
        score = posterior if value else 1 - posterior
        conflicts = ("CONFLICTING_VALUES_REQUIRE_REVIEW",) if len(values) > 1 else ()
        return _OperatorResult(
            value=value,
            score=score,
            margin=None,
            operator=(
                "hierarchy-leaf-rollup"
                if definition.kind is LabelKind.HIERARCHY
                else "boolean-log-odds"
            ),
            conflict_reasons=conflicts,
            ancestor_label_ids=definition.parent_ids if value else (),
        )

    def _candidate_scores(
        self,
        observations_by_value: dict[str, list[_CanonicalObservation]],
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        for value, observations in observations_by_value.items():
            total = _logit(self._policy.prior_probability)
            total += sum(
                self._policy.weight_for(item.source.source_family)
                * _logit(item.effective_confidence)
                for item in observations
            )
            scores[value] = _sigmoid(total)
        return scores

    def _aggregate_categorical(
        self,
        observations: Sequence[_CanonicalObservation],
    ) -> _OperatorResult:
        grouped: dict[str, list[_CanonicalObservation]] = defaultdict(list)
        for item in observations:
            grouped[str(item.value)].append(item)
        scores = self._candidate_scores(grouped)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        value, score = ranked[0]
        runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = score - runner_up_score
        conflicts = (
            ("CATEGORICAL_MARGIN_CONFLICT",)
            if len(ranked) > 1 and margin < self._policy.categorical_margin
            else ()
        )
        return _OperatorResult(
            value=value,
            score=score,
            margin=margin,
            operator="categorical-top-posterior",
            conflict_reasons=conflicts,
            candidate_scores=tuple(sorted(scores.items())),
        )

    def _aggregate_multi(
        self,
        observations: Sequence[_CanonicalObservation],
    ) -> _OperatorResult:
        grouped: dict[str, list[_CanonicalObservation]] = defaultdict(list)
        for item in observations:
            values = item.value if isinstance(item.value, tuple) else (item.value,)
            for value in values:
                grouped[str(value)].append(item)
        scores = self._candidate_scores(grouped)
        selected = tuple(
            sorted(
                value
                for value, score in scores.items()
                if score >= self._policy.multi_selection_threshold
            )
        )
        selected_scores = [scores[value] for value in selected]
        score = min(selected_scores) if selected_scores else max(scores.values(), default=0.0)
        return _OperatorResult(
            value=selected,
            score=score,
            margin=None,
            operator="multi-weighted-log-odds",
            candidate_scores=tuple(sorted(scores.items())),
        )

    def _aggregate_numeric(
        self,
        definition: LabelDefinition,
        observations: Sequence[_CanonicalObservation],
    ) -> _OperatorResult:
        ranked = sorted(
            observations,
            key=lambda item: (self._as_number(item.value), item.source.observation_id),
        )
        clusters: list[list[_CanonicalObservation]] = []
        for item in ranked:
            outside_tolerance = (
                not clusters
                or abs(self._as_number(item.value) - self._as_number(clusters[-1][0].value))
                > definition.numeric_tolerance
            )
            if outside_tolerance:
                clusters.append([item])
            else:
                clusters[-1].append(item)
        cluster_scores = [self._support_score(cluster) for cluster in clusters]
        winning_index = min(
            range(len(clusters)),
            key=lambda index: (
                -cluster_scores[index],
                self._as_number(clusters[index][0].value),
            ),
        )
        winning_cluster = clusters[winning_index]
        representative = sorted(
            winning_cluster,
            key=lambda item: (-item.effective_confidence, item.source.observation_id),
        )[0]
        return _OperatorResult(
            value=representative.value,
            score=cluster_scores[winning_index],
            margin=None,
            operator="numeric-authoritative-cluster",
            conflict_reasons=("NUMERIC_CLUSTER_CONFLICT",) if len(clusters) > 1 else (),
            candidate_scores=tuple(
                sorted(
                    (
                        _value_key(cluster[0].value),
                        cluster_scores[index],
                    )
                    for index, cluster in enumerate(clusters)
                )
            ),
        )

    def _aggregate_temporal(
        self,
        observations: Sequence[_CanonicalObservation],
    ) -> _OperatorResult:
        ranked = sorted(
            observations,
            key=lambda item: (
                self._as_time_span(item.value).start,
                self._as_time_span(item.value).end,
                item.source.observation_id,
            ),
        )
        clusters: list[list[_CanonicalObservation]] = []
        for item in ranked:
            matching_cluster = next(
                (
                    cluster
                    for cluster in clusters
                    if any(
                        self._temporal_iou(
                            self._as_time_span(item.value), self._as_time_span(member.value)
                        )
                        >= self._policy.temporal_iou_threshold
                        for member in cluster
                    )
                ),
                None,
            )
            if matching_cluster is None:
                clusters.append([item])
            else:
                matching_cluster.append(item)
        cluster_scores = [self._support_score(cluster) for cluster in clusters]
        winning_index = min(
            range(len(clusters)),
            key=lambda index: (
                -cluster_scores[index],
                self._as_time_span(clusters[index][0].value).start,
            ),
        )
        winning_cluster = clusters[winning_index]
        merged = TimeSpan(
            start=min(self._as_time_span(item.value).start for item in winning_cluster),
            end=max(self._as_time_span(item.value).end for item in winning_cluster),
        )
        return _OperatorResult(
            value=merged,
            score=cluster_scores[winning_index],
            margin=None,
            operator="temporal-iou-merge",
            conflict_reasons=("TEMPORAL_WINDOW_CONFLICT",) if len(clusters) > 1 else (),
            candidate_scores=tuple(
                sorted(
                    (
                        _value_key(
                            TimeSpan(
                                min(self._as_time_span(item.value).start for item in cluster),
                                max(self._as_time_span(item.value).end for item in cluster),
                            )
                        ),
                        cluster_scores[index],
                    )
                    for index, cluster in enumerate(clusters)
                )
            ),
        )

    def _support_score(self, observations: Sequence[_CanonicalObservation]) -> float:
        total = _logit(self._policy.prior_probability)
        total += sum(
            self._policy.weight_for(item.source.source_family) * _logit(item.effective_confidence)
            for item in observations
        )
        return _sigmoid(total)

    def _route_decision(
        self,
        definition: LabelDefinition,
        prepared: _PreparedObservations,
        result: _OperatorResult,
    ) -> tuple[AggregateDecision, tuple[str, ...]]:
        review_reasons = list(result.conflict_reasons)
        if any(
            not item.source.evidence_hash or not item.source.evidence_valid
            for item in prepared.included
        ):
            review_reasons.append("EVIDENCE_INTEGRITY_REQUIRES_REVIEW")
        if any(item.source.novel for item in prepared.included):
            review_reasons.append("NOVEL_OBSERVATION_REQUIRES_REVIEW")

        if prepared.source_precedence is SourceType.HUMAN_CONFIRMED and not review_reasons:
            return AggregateDecision.AUTO_ACCEPT, ("HUMAN_CONFIRMED_PRECEDENCE",)

        if self._policy.mode is AggregationMode.L1:
            review_reasons.append("L1_HUMAN_REVIEW_REQUIRED")
            return AggregateDecision.REQUIRE_REVIEW, tuple(dict.fromkeys(review_reasons))

        if definition.risk_level is not RiskLevel.LOW:
            review_reasons.append("RISK_LEVEL_REQUIRES_REVIEW")
        if any(item.source.calibrated_confidence is None for item in prepared.included):
            review_reasons.append("CALIBRATION_REQUIRED_FOR_L2")
        independent_sources = {item.source.source_family for item in prepared.included}
        if len(independent_sources) < self._policy.min_independent_sources:
            review_reasons.append("INSUFFICIENT_INDEPENDENT_SOURCES")
        if review_reasons:
            return AggregateDecision.REQUIRE_REVIEW, tuple(dict.fromkeys(review_reasons))

        if result.score < self._policy.l2_accept_threshold:
            return AggregateDecision.ABSTAIN, ("CONFIDENCE_BELOW_L2_THRESHOLD",)
        audit_key = {
            "subject_scope": prepared.included[0].source.subject_scope,
            "subject_key": prepared.included[0].source.subject_key,
            "label_id": definition.label_id,
            "policy_hash": self._policy_hash,
        }
        if self._is_random_audit(audit_key):
            return AggregateDecision.REQUIRE_REVIEW, ("L2_RANDOM_AUDIT",)
        return AggregateDecision.AUTO_ACCEPT, ("L2_POLICY_ELIGIBLE",)

    def _apply_mutex_constraints(
        self,
        aggregates: tuple[LabelAggregate, ...],
    ) -> tuple[LabelAggregate, ...]:
        definitions = {definition.label_id: definition for definition in self._definitions}
        mutex_groups: dict[tuple[str, str, str], list[LabelAggregate]] = defaultdict(list)
        for aggregate in aggregates:
            mutex_group = definitions[aggregate.label_id].mutex_group
            if mutex_group and self._value_is_active(aggregate.value):
                mutex_groups[(aggregate.subject_scope, aggregate.subject_key, mutex_group)].append(
                    aggregate
                )

        replacements: dict[str, LabelAggregate] = {}
        for competing in mutex_groups.values():
            if len(competing) < 2:
                continue
            ranked = sorted(
                competing,
                key=lambda item: (
                    -SOURCE_PRIORITY[item.explanation.source_precedence],
                    -item.score,
                    item.label_id,
                ),
            )
            winner = ranked[0]
            runner_up = ranked[1]
            same_precedence = (
                winner.explanation.source_precedence is runner_up.explanation.source_precedence
            )
            close_score = winner.score - runner_up.score < self._policy.categorical_margin
            if same_precedence and close_score:
                for aggregate in ranked:
                    replacements[aggregate.aggregate_id] = self._rewrite_aggregate(
                        aggregate,
                        decision=AggregateDecision.REQUIRE_REVIEW,
                        reason_code="MUTEX_GROUP_CONFLICT",
                        margin=winner.score - runner_up.score,
                    )
                continue
            for aggregate in ranked[1:]:
                if aggregate.decision is AggregateDecision.AUTO_ACCEPT:
                    replacements[aggregate.aggregate_id] = self._rewrite_aggregate(
                        aggregate,
                        decision=AggregateDecision.ABSTAIN,
                        reason_code="MUTEX_SUPPRESSED_BY_HIGHER_PRECEDENCE",
                        margin=winner.score - aggregate.score,
                    )

        return tuple(replacements.get(item.aggregate_id, item) for item in aggregates)

    def _rewrite_aggregate(
        self,
        aggregate: LabelAggregate,
        *,
        decision: AggregateDecision,
        reason_code: str,
        margin: float,
    ) -> LabelAggregate:
        updated = replace(
            aggregate,
            decision=decision,
            reason_codes=tuple(dict.fromkeys((*aggregate.reason_codes, reason_code))),
            margin=margin,
        )
        payload = updated.to_dict()
        payload.pop("canonical_hash")
        payload["policy_hash"] = self._policy_hash
        return replace(updated, canonical_hash=_sha256(payload))

    @staticmethod
    def _value_is_active(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, tuple):
            return bool(value)
        return value is not None

    def _is_random_audit(self, audit_key: object) -> bool:
        if self._policy.random_audit_rate <= 0:
            return False
        if self._policy.random_audit_rate >= 1:
            return True
        sample = int(_sha256(audit_key)[:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        return sample < self._policy.random_audit_rate

    def _normalize_value(self, definition: LabelDefinition, value: object) -> object:
        if definition.kind in {LabelKind.BOOLEAN, LabelKind.HIERARCHY}:
            if not isinstance(value, bool):
                raise ValueError(f"{definition.label_id} requires a boolean value")
            return value
        if definition.kind in {LabelKind.CATEGORICAL, LabelKind.MULTI}:
            raw_values: tuple[object, ...]
            if definition.kind is LabelKind.MULTI and isinstance(value, (tuple, list, set)):
                raw_values = tuple(value)
            else:
                raw_values = (value,)
            if not raw_values or any(not isinstance(item, str) for item in raw_values):
                raise ValueError(f"{definition.label_id} requires non-empty string values")
            normalized_values = tuple(
                sorted(
                    {normalize_text(str(item)) for item in raw_values if normalize_text(str(item))}
                )
            )
            if not normalized_values:
                raise ValueError(f"{definition.label_id} requires non-empty string values")
            return normalized_values if definition.kind is LabelKind.MULTI else normalized_values[0]
        if definition.kind is LabelKind.NUMERIC:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{definition.label_id} requires a numeric value")
            if not math.isfinite(float(value)):
                raise ValueError(f"{definition.label_id} requires a finite numeric value")
            return float(value)
        if definition.kind is LabelKind.TEMPORAL:
            return self._as_time_span(value)
        raise ValueError(f"unsupported label kind: {definition.kind}")

    @staticmethod
    def _as_number(value: object) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ValueError("numeric aggregation received a non-numeric canonical value")

    @staticmethod
    def _as_time_span(value: object) -> TimeSpan:
        if isinstance(value, TimeSpan):
            return value
        if (
            isinstance(value, (tuple, list))
            and len(value) == 2
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)
        ):
            return TimeSpan(float(value[0]), float(value[1]))
        raise ValueError("temporal labels require a TimeSpan or a numeric (start, end) pair")

    @staticmethod
    def _temporal_iou(left: TimeSpan, right: TimeSpan) -> float:
        intersection = max(0.0, min(left.end, right.end) - max(left.start, right.start))
        union = max(left.end, right.end) - min(left.start, right.start)
        if union == 0:
            return 1.0 if left == right else 0.0
        return intersection / union

    @staticmethod
    def _serialize_value(value: object) -> object:
        if isinstance(value, TimeSpan):
            return value.to_dict()
        if isinstance(value, tuple):
            return list(value)
        return value

    def _policy_payload(self) -> dict[str, Any]:
        return {
            "mode": self._policy.mode.value,
            "source_weights": sorted(
                (
                    {
                        "source_family": source_weight.source_family,
                        "weight": source_weight.weight,
                    }
                    for source_weight in self._policy.source_weights
                ),
                key=lambda item: item["source_family"],
            ),
            "prior_probability": self._policy.prior_probability,
            "l2_accept_threshold": self._policy.l2_accept_threshold,
            "categorical_margin": self._policy.categorical_margin,
            "temporal_iou_threshold": self._policy.temporal_iou_threshold,
            "multi_selection_threshold": self._policy.multi_selection_threshold,
            "min_independent_sources": self._policy.min_independent_sources,
            "random_audit_rate": self._policy.random_audit_rate,
        }

    def _label_payload(self) -> list[dict[str, Any]]:
        return [
            {
                "label_id": definition.label_id,
                "canonical_name": normalize_text(definition.canonical_name),
                "aliases": sorted(normalize_text(alias) for alias in definition.aliases),
                "kind": definition.kind.value,
                "risk_level": definition.risk_level.value,
                "parent_ids": list(definition.parent_ids),
                "mutex_group": definition.mutex_group,
                "numeric_tolerance": definition.numeric_tolerance,
            }
            for definition in self._definitions
        ]
