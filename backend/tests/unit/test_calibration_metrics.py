from __future__ import annotations

import pytest

from app.domain.calibration import (
    CalibrationMetrics,
    calculate_calibration_metrics,
    canonical_value_sha256,
    get_calibration_rubric,
)


def test_binary_calibration_metrics_use_integer_scales() -> None:
    metrics = calculate_calibration_metrics([1, 1, 0, 0], [1, 0, 0, 0])

    assert metrics == CalibrationMetrics(
        paired_submission_count=4,
        agreed_count=3,
        conflict_count=1,
        adjudication_count=0,
        observed_agreement_ppm=750_000,
        cohen_kappa_micros=500_000,
        cohen_kappa_defined=True,
    )


def test_multiclass_kappa_is_calculated_without_floating_point() -> None:
    metrics = calculate_calibration_metrics(["a", "b", "c"], ["a", "b", "b"])

    assert metrics.observed_agreement_ppm == 666_667
    assert metrics.cohen_kappa_micros == 500_000
    assert metrics.cohen_kappa_defined is True
    assert metrics.conflict_count == 1


def test_complete_systematic_disagreement_has_negative_one_kappa() -> None:
    metrics = calculate_calibration_metrics([0, 0, 1, 1], [1, 1, 0, 0])

    assert metrics.observed_agreement_ppm == 0
    assert metrics.cohen_kappa_micros == -1_000_000
    assert metrics.cohen_kappa_defined is True
    assert metrics.conflict_count == 4


def test_empty_pair_set_has_zero_metrics() -> None:
    assert calculate_calibration_metrics([], []) == CalibrationMetrics.empty()


def test_degenerate_perfect_agreement_is_recorded_as_perfect() -> None:
    metrics = calculate_calibration_metrics(["same", "same"], ["same", "same"])

    assert metrics.observed_agreement_ppm == 1_000_000
    assert metrics.cohen_kappa_micros == 0
    assert metrics.cohen_kappa_defined is False


def test_adjudication_count_cannot_exceed_conflicts() -> None:
    with pytest.raises(ValueError, match="adjudication_count"):
        calculate_calibration_metrics([1, 1], [1, 0], adjudication_count=2)


def test_reviewer_vectors_must_be_paired() -> None:
    with pytest.raises(ValueError, match="same length"):
        calculate_calibration_metrics([1], [1, 0])


def test_canonical_hash_ignores_object_key_order_but_preserves_array_order() -> None:
    first = {"label": "risk", "evidence": [{"end": 2, "start": 1}]}
    reordered = {"evidence": [{"start": 1, "end": 2}], "label": "risk"}
    reversed_evidence = {"label": "risk", "evidence": [2, 1]}

    assert canonical_value_sha256(first) == canonical_value_sha256(reordered)
    assert canonical_value_sha256(first) != canonical_value_sha256(reversed_evidence)


def test_non_finite_numbers_are_not_canonical_json() -> None:
    with pytest.raises(ValueError, match="JSON"):
        canonical_value_sha256({"score": float("nan")})


def test_binary_rubric_uses_decision_as_category_and_gold_value() -> None:
    rubric = get_calibration_rubric("rubric_quote_risk_v3")
    first = {
        "decision": "pass",
        "reason_code": "evidence_consistent",
        "evidence_refs": ["evidence://calibration/one"],
    }
    second = {
        "decision": "pass",
        "reason_code": "other",
        "evidence_refs": [],
    }

    rubric.validate_submission(first, evidence_ref="evidence://calibration/one")
    rubric.validate_submission(second, evidence_ref="evidence://calibration/one")

    assert rubric.category_key(first) == rubric.category_key(second) == "pass"
    assert rubric.gold_value(first) == {"decision": "pass"}


def test_binary_rubric_rejects_cross_sample_evidence() -> None:
    rubric = get_calibration_rubric("rubric_quote_risk_v3")

    with pytest.raises(ValueError, match="current frozen sample"):
        rubric.validate_submission(
            {
                "decision": "fail",
                "reason_code": "evidence_conflict",
                "evidence_refs": ["evidence://calibration/another"],
            },
            evidence_ref="evidence://calibration/current",
        )
