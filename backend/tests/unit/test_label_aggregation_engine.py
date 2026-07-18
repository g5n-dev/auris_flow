from __future__ import annotations

from dataclasses import replace
from math import isclose

import pytest

from app.domain.label_aggregation import (
    AggregateDecision,
    AggregationMode,
    AggregationPolicy,
    LabelAggregationEngine,
    LabelDefinition,
    LabelKind,
    LabelObservation,
    RiskLevel,
    SourceType,
    SourceWeight,
    TimeSpan,
)


def label(
    *,
    label_id: str = "refund-request",
    name: str = "申请退款",
    aliases: tuple[str, ...] = ("退款申请",),
    kind: LabelKind = LabelKind.BOOLEAN,
    risk: RiskLevel = RiskLevel.LOW,
    parent_ids: tuple[str, ...] = (),
    mutex_group: str | None = None,
    numeric_tolerance: float = 0.0,
) -> LabelDefinition:
    return LabelDefinition(
        label_id=label_id,
        canonical_name=name,
        aliases=aliases,
        kind=kind,
        risk_level=risk,
        parent_ids=parent_ids,
        mutex_group=mutex_group,
        numeric_tolerance=numeric_tolerance,
    )


def observation(
    observation_id: str,
    *,
    raw_label: str = "申请退款",
    value: object = True,
    source_family: str = "model-a",
    source_type: SourceType = SourceType.MODEL,
    raw_confidence: float = 0.9,
    calibrated_confidence: float | None = 0.9,
    evidence_hash: str | None = "evidence-1",
    evidence_valid: bool = True,
    novel: bool = False,
) -> LabelObservation:
    return LabelObservation(
        observation_id=observation_id,
        subject_scope="audio-session",
        subject_key="session-001",
        raw_label=raw_label,
        value=value,
        source_family=source_family,
        source_type=source_type,
        raw_confidence=raw_confidence,
        calibrated_confidence=calibrated_confidence,
        evidence_hash=evidence_hash,
        evidence_valid=evidence_valid,
        trace_id="trace-001",
        novel=novel,
    )


def l2_policy(**changes: object) -> AggregationPolicy:
    defaults: dict[str, object] = {
        "mode": AggregationMode.L2,
        "l2_accept_threshold": 0.95,
        "categorical_margin": 0.15,
        "min_independent_sources": 1,
        "random_audit_rate": 0.0,
    }
    defaults.update(changes)
    return AggregationPolicy(**defaults)


def test_aliases_are_canonicalized_and_unknown_labels_become_taxonomy_suggestions():
    engine = LabelAggregationEngine([label()], AggregationPolicy())

    result = engine.aggregate(
        [
            observation("known", raw_label=" 退款申请 "),
            observation("unknown-a", raw_label="退款意愿"),
            observation("unknown-b", raw_label=" 退款意愿 ", evidence_hash="evidence-2"),
        ]
    )

    assert [(item.label_id, item.value) for item in result.aggregates] == [("refund-request", True)]
    assert len(result.unknown_suggestions) == 1
    suggestion = result.unknown_suggestions[0]
    assert suggestion.normalized_label == "退款意愿"
    assert suggestion.raw_labels == (" 退款意愿 ", "退款意愿")
    assert suggestion.observation_ids == ("unknown-a", "unknown-b")
    assert suggestion.reason_code == "UNKNOWN_LABEL_REQUIRES_TAXONOMY_REVIEW"


def test_alias_collisions_are_rejected_instead_of_silently_misrouting_observations():
    with pytest.raises(ValueError, match="ambiguous canonical label alias"):
        LabelAggregationEngine(
            [
                label(label_id="refund", name="退款", aliases=("退费",)),
                label(label_id="fee-return", name="退费", aliases=()),
            ],
            AggregationPolicy(),
        )


def test_same_evidence_and_source_family_are_deduplicated_and_hash_is_order_invariant():
    engine = LabelAggregationEngine([label()], AggregationPolicy())
    weaker = observation("obs-weak", calibrated_confidence=0.7)
    stronger = observation("obs-strong", calibrated_confidence=0.9)
    independent = observation(
        "obs-independent",
        source_family="model-b",
        evidence_hash="evidence-2",
        calibrated_confidence=0.8,
    )

    forward = engine.aggregate([weaker, stronger, independent])
    backward = engine.aggregate([independent, stronger, weaker])

    aggregate = forward.aggregates[0]
    included = [
        item.observation_id for item in aggregate.explanation.contributions if item.included
    ]
    excluded = [
        (item.observation_id, item.exclusion_reason)
        for item in aggregate.explanation.contributions
        if not item.included
    ]
    assert included == ["obs-independent", "obs-strong"]
    assert excluded == [("obs-weak", "CORRELATED_DUPLICATE")]
    assert forward.canonical_hash == backward.canonical_hash
    assert forward.to_dict() == backward.to_dict()


def test_correlated_lineage_deduplicates_conflicting_slices_without_using_prediction_value():
    engine = LabelAggregationEngine([label()], AggregationPolicy(random_audit_rate=0.0))
    weaker_positive = replace(
        observation("slice-positive", value=True, calibrated_confidence=0.7),
        evidence_hash="slice-a",
        correlation_group_id="locked-run-model-prompt-a",
        extraction_run_id="extract-a",
    )
    stronger_negative = replace(
        observation("slice-negative", value=False, calibrated_confidence=0.92),
        evidence_hash="slice-b",
        correlation_group_id="locked-run-model-prompt-a",
        extraction_run_id="extract-a",
    )

    aggregate = engine.aggregate([weaker_positive, stronger_negative]).aggregates[0]

    assert aggregate.value is False
    assert "CONFLICTING_VALUES_REQUIRE_REVIEW" not in aggregate.reason_codes
    contributions = {item.observation_id: item for item in aggregate.explanation.contributions}
    assert contributions["slice-negative"].included is True
    assert contributions["slice-positive"].exclusion_reason == "CORRELATED_DUPLICATE"


def test_overlapping_intervals_from_same_family_are_one_correlated_evidence_group():
    engine = LabelAggregationEngine([label()], AggregationPolicy(random_audit_rate=0.0))
    first = replace(
        observation("interval-a", calibrated_confidence=0.8),
        evidence_hash="hash-a",
        evidence_ref_id="audio-object-1",
        evidence_start=0,
        evidence_end=1000,
    )
    overlap = replace(
        observation("interval-b", calibrated_confidence=0.9),
        evidence_hash="hash-b",
        evidence_ref_id="audio-object-1",
        evidence_start=800,
        evidence_end=1800,
    )

    aggregate = engine.aggregate([first, overlap]).aggregates[0]

    included = [
        item.observation_id for item in aggregate.explanation.contributions if item.included
    ]
    assert included == ["interval-b"]


def test_calibrated_confidence_and_family_weights_drive_weighted_log_odds():
    engine = LabelAggregationEngine(
        [label()],
        AggregationPolicy(
            source_weights=(SourceWeight("model-a", 2.0),),
            random_audit_rate=0.0,
        ),
    )

    result = engine.aggregate([observation("obs", raw_confidence=0.99, calibrated_confidence=0.8)])

    aggregate = result.aggregates[0]
    # sigmoid(2 * logit(0.8)) = 16 / 17
    assert isclose(aggregate.score, 16 / 17, rel_tol=1e-12)
    contribution = aggregate.explanation.contributions[0]
    assert contribution.confidence_basis == "calibrated"
    assert isclose(contribution.weighted_log_odds, 2.0 * 1.3862943611198906)


def test_source_precedence_is_hard_and_human_confirmation_shadows_lower_sources():
    engine = LabelAggregationEngine([label()], l2_policy())
    model_claim = observation("model", value=True, calibrated_confidence=0.999)
    human_claim = observation(
        "human",
        value=False,
        source_family="review-team",
        source_type=SourceType.HUMAN_CONFIRMED,
        calibrated_confidence=0.6,
        evidence_hash="review-evidence",
    )

    aggregate = engine.aggregate([model_claim, human_claim]).aggregates[0]

    assert aggregate.value is False
    assert aggregate.decision is AggregateDecision.AUTO_ACCEPT
    assert aggregate.reason_codes[0] == "HUMAN_CONFIRMED_PRECEDENCE"
    contributions = {item.observation_id: item for item in aggregate.explanation.contributions}
    assert contributions["human"].included is True
    assert contributions["model"].exclusion_reason == "LOWER_SOURCE_PRECEDENCE"


def test_boolean_evidence_uses_signed_log_odds_and_l1_always_routes_model_output_to_review():
    engine = LabelAggregationEngine([label()], AggregationPolicy(random_audit_rate=0.0))
    positive = observation("positive", calibrated_confidence=0.9)
    negative = observation(
        "negative",
        value=False,
        source_family="model-b",
        evidence_hash="evidence-2",
        calibrated_confidence=0.6,
    )

    aggregate = engine.aggregate([positive, negative]).aggregates[0]

    assert aggregate.value is True
    assert isclose(aggregate.score, 6 / 7, rel_tol=1e-12)
    assert aggregate.decision is AggregateDecision.REQUIRE_REVIEW
    assert aggregate.reason_codes == (
        "CONFLICTING_VALUES_REQUIRE_REVIEW",
        "L1_HUMAN_REVIEW_REQUIRED",
    )


def test_categorical_uses_top_candidate_and_routes_small_margin_conflict_to_review():
    definition = label(kind=LabelKind.CATEGORICAL, name="退款原因", aliases=())
    engine = LabelAggregationEngine([definition], l2_policy())

    aggregate = engine.aggregate(
        [
            observation("quality", raw_label="退款原因", value="质量", calibrated_confidence=0.9),
            observation(
                "price",
                raw_label="退款原因",
                value="价格",
                source_family="model-b",
                evidence_hash="evidence-2",
                calibrated_confidence=0.8,
            ),
        ]
    ).aggregates[0]

    assert aggregate.value == "质量"
    assert isclose(aggregate.score, 0.9)
    assert isclose(aggregate.margin, 0.1)
    assert aggregate.decision is AggregateDecision.REQUIRE_REVIEW
    assert "CATEGORICAL_MARGIN_CONFLICT" in aggregate.reason_codes
    assert aggregate.explanation.candidate_scores == (("价格", 0.8), ("质量", 0.9))


def test_close_scores_in_a_mutex_group_are_never_auto_accepted_together():
    quality = label(
        label_id="refund-quality",
        name="质量退款",
        aliases=(),
        mutex_group="refund-reason",
    )
    price = label(
        label_id="refund-price",
        name="价格退款",
        aliases=(),
        mutex_group="refund-reason",
    )
    engine = LabelAggregationEngine([quality, price], l2_policy())

    result = engine.aggregate(
        [
            observation(
                "quality",
                raw_label="质量退款",
                calibrated_confidence=0.99,
            ),
            observation(
                "price",
                raw_label="价格退款",
                source_family="model-b",
                evidence_hash="evidence-2",
                calibrated_confidence=0.98,
            ),
        ]
    )

    assert {aggregate.decision for aggregate in result.aggregates} == {
        AggregateDecision.REQUIRE_REVIEW
    }
    assert all("MUTEX_GROUP_CONFLICT" in item.reason_codes for item in result.aggregates)


def test_multi_value_operator_keeps_each_supported_value_in_canonical_order():
    definition = label(kind=LabelKind.MULTI, name="用户诉求", aliases=())
    engine = LabelAggregationEngine([definition], AggregationPolicy())

    aggregate = engine.aggregate(
        [
            observation("refund", raw_label="用户诉求", value=("退款", "投诉")),
            observation(
                "consult",
                raw_label="用户诉求",
                value="咨询",
                source_family="model-b",
                evidence_hash="evidence-2",
                calibrated_confidence=0.8,
            ),
        ]
    ).aggregates[0]

    assert aggregate.value == ("咨询", "投诉", "退款")
    assert aggregate.explanation.operator == "multi-weighted-log-odds"


def test_numeric_operator_clusters_by_tolerance_but_never_averages_conflicting_amounts():
    definition = label(
        kind=LabelKind.NUMERIC,
        name="退款金额",
        aliases=(),
        numeric_tolerance=1.0,
    )
    engine = LabelAggregationEngine([definition], AggregationPolicy())

    aggregate = engine.aggregate(
        [
            observation("amount-a", raw_label="退款金额", value=100.0, calibrated_confidence=0.9),
            observation(
                "amount-near",
                raw_label="退款金额",
                value=100.5,
                source_family="model-b",
                evidence_hash="evidence-2",
                calibrated_confidence=0.8,
            ),
            observation(
                "amount-conflict",
                raw_label="退款金额",
                value=130.0,
                source_family="model-c",
                evidence_hash="evidence-3",
                calibrated_confidence=0.7,
            ),
        ]
    ).aggregates[0]

    assert aggregate.value == 100.0
    assert aggregate.value not in {100.25, 110.16666666666667}
    assert aggregate.decision is AggregateDecision.REQUIRE_REVIEW
    assert "NUMERIC_CLUSTER_CONFLICT" in aggregate.reason_codes


def test_temporal_operator_merges_sufficient_iou_and_keeps_distant_windows_as_conflict():
    definition = label(kind=LabelKind.TEMPORAL, name="投诉片段", aliases=())
    engine = LabelAggregationEngine(
        [definition],
        AggregationPolicy(temporal_iou_threshold=0.6),
    )

    aggregate = engine.aggregate(
        [
            observation(
                "window-a",
                raw_label="投诉片段",
                value=TimeSpan(0, 10),
                calibrated_confidence=0.9,
            ),
            observation(
                "window-overlap",
                raw_label="投诉片段",
                value=TimeSpan(1, 11),
                source_family="model-b",
                evidence_hash="evidence-2",
                calibrated_confidence=0.8,
            ),
            observation(
                "window-far",
                raw_label="投诉片段",
                value=TimeSpan(20, 30),
                source_family="model-c",
                evidence_hash="evidence-3",
                calibrated_confidence=0.7,
            ),
        ]
    ).aggregates[0]

    assert aggregate.value == TimeSpan(0, 11)
    assert aggregate.decision is AggregateDecision.REQUIRE_REVIEW
    assert "TEMPORAL_WINDOW_CONFLICT" in aggregate.reason_codes


def test_hierarchy_rolls_positive_leaf_up_to_ancestors_but_never_infers_children():
    parent = label(
        label_id="service-issue",
        name="服务问题",
        aliases=(),
        kind=LabelKind.HIERARCHY,
    )
    leaf = label(
        label_id="rude-service",
        name="服务态度恶劣",
        aliases=("态度恶劣",),
        kind=LabelKind.HIERARCHY,
        parent_ids=("service-issue",),
    )
    engine = LabelAggregationEngine([parent, leaf], AggregationPolicy())

    result = engine.aggregate([observation("leaf", raw_label="态度恶劣", value=True)])

    assert len(result.aggregates) == 1
    aggregate = result.aggregates[0]
    assert aggregate.label_id == "rude-service"
    assert aggregate.ancestor_label_ids == ("service-issue",)
    assert all(item.label_id != "service-issue" for item in result.aggregates)


@pytest.mark.parametrize(
    ("changes", "expected_decision", "expected_reason"),
    [
        ({}, AggregateDecision.AUTO_ACCEPT, "L2_POLICY_ELIGIBLE"),
        (
            {"calibrated_confidence": None},
            AggregateDecision.REQUIRE_REVIEW,
            "CALIBRATION_REQUIRED_FOR_L2",
        ),
        (
            {"evidence_hash": None},
            AggregateDecision.REQUIRE_REVIEW,
            "EVIDENCE_INTEGRITY_REQUIRES_REVIEW",
        ),
        (
            {"novel": True},
            AggregateDecision.REQUIRE_REVIEW,
            "NOVEL_OBSERVATION_REQUIRES_REVIEW",
        ),
        (
            {"calibrated_confidence": 0.8},
            AggregateDecision.ABSTAIN,
            "CONFIDENCE_BELOW_L2_THRESHOLD",
        ),
    ],
)
def test_l2_routes_only_calibrated_low_risk_conflict_free_evidence_automatically(
    changes: dict[str, object],
    expected_decision: AggregateDecision,
    expected_reason: str,
):
    engine = LabelAggregationEngine([label()], l2_policy())
    item = observation("obs", calibrated_confidence=0.99)

    aggregate = engine.aggregate([replace(item, **changes)]).aggregates[0]

    assert aggregate.decision is expected_decision
    assert expected_reason in aggregate.reason_codes


def test_high_risk_and_insufficient_independent_sources_remain_in_human_loop():
    high_risk = (
        LabelAggregationEngine(
            [label(risk=RiskLevel.HIGH)],
            l2_policy(),
        )
        .aggregate([observation("high", calibrated_confidence=0.99)])
        .aggregates[0]
    )
    insufficient_sources = (
        LabelAggregationEngine(
            [label()],
            l2_policy(min_independent_sources=2),
        )
        .aggregate([observation("single", calibrated_confidence=0.99)])
        .aggregates[0]
    )

    assert high_risk.decision is AggregateDecision.REQUIRE_REVIEW
    assert "RISK_LEVEL_REQUIRES_REVIEW" in high_risk.reason_codes
    assert insufficient_sources.decision is AggregateDecision.REQUIRE_REVIEW
    assert "INSUFFICIENT_INDEPENDENT_SOURCES" in insufficient_sources.reason_codes


def test_l2_random_audit_is_deterministic_and_explained():
    engine = LabelAggregationEngine(
        [label()],
        l2_policy(random_audit_rate=1.0),
    )

    first = engine.aggregate([observation("obs", calibrated_confidence=0.99)])
    second = engine.aggregate([observation("obs", calibrated_confidence=0.99)])

    assert first.canonical_hash == second.canonical_hash
    assert first.aggregates[0].decision is AggregateDecision.REQUIRE_REVIEW
    assert "L2_RANDOM_AUDIT" in first.aggregates[0].reason_codes


def test_invalid_confidence_and_temporal_windows_fail_closed():
    with pytest.raises(ValueError, match="confidence"):
        observation("invalid", raw_confidence=1.1)
    with pytest.raises(ValueError, match="start must not exceed end"):
        TimeSpan(2, 1)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TimeSpan(float("nan"), 1),
        lambda: SourceWeight("model-a", float("inf")),
        lambda: label(numeric_tolerance=float("nan")),
    ],
)
def test_non_finite_configuration_is_rejected_to_keep_hashes_portable(factory):
    with pytest.raises(ValueError, match="finite"):
        factory()
