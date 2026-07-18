from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.calibration.canonical import canonical_json_bytes

METRIC_SCALE = 1_000_000


def _round_scaled_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    scaled = numerator * METRIC_SCALE
    if scaled >= 0:
        return (scaled + denominator // 2) // denominator
    return -((-scaled + denominator // 2) // denominator)


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    paired_submission_count: int
    agreed_count: int
    conflict_count: int
    adjudication_count: int
    observed_agreement_ppm: int
    cohen_kappa_micros: int
    cohen_kappa_defined: bool

    def __post_init__(self) -> None:
        counts = (
            self.paired_submission_count,
            self.agreed_count,
            self.conflict_count,
            self.adjudication_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("calibration counts must be non-negative")
        if self.agreed_count + self.conflict_count != self.paired_submission_count:
            raise ValueError("agreed_count + conflict_count must equal paired_submission_count")
        if self.adjudication_count > self.conflict_count:
            raise ValueError("adjudication_count cannot exceed conflict_count")
        if not 0 <= self.observed_agreement_ppm <= METRIC_SCALE:
            raise ValueError("observed_agreement_ppm is outside the supported range")
        if not -METRIC_SCALE <= self.cohen_kappa_micros <= METRIC_SCALE:
            raise ValueError("cohen_kappa_micros is outside the supported range")
        if not self.cohen_kappa_defined and self.cohen_kappa_micros != 0:
            raise ValueError("undefined Cohen's kappa must use the neutral stored value 0")

    @classmethod
    def empty(cls) -> CalibrationMetrics:
        return cls(
            paired_submission_count=0,
            agreed_count=0,
            conflict_count=0,
            adjudication_count=0,
            observed_agreement_ppm=0,
            cohen_kappa_micros=0,
            cohen_kappa_defined=False,
        )


def calculate_calibration_metrics(
    reviewer_a: Sequence[Any],
    reviewer_b: Sequence[Any],
    *,
    adjudication_count: int = 0,
) -> CalibrationMetrics:
    """Calculate agreement and Cohen's kappa from exact integer counts."""
    if len(reviewer_a) != len(reviewer_b):
        raise ValueError("reviewer vectors must have the same length")
    if adjudication_count < 0:
        raise ValueError("adjudication_count must be non-negative")
    if not reviewer_a:
        if adjudication_count:
            raise ValueError("adjudication_count cannot exceed conflict_count")
        return CalibrationMetrics.empty()

    canonical_a = [canonical_json_bytes(value) for value in reviewer_a]
    canonical_b = [canonical_json_bytes(value) for value in reviewer_b]
    paired_count = len(canonical_a)
    agreed_count = sum(left == right for left, right in zip(canonical_a, canonical_b, strict=True))
    conflict_count = paired_count - agreed_count
    if adjudication_count > conflict_count:
        raise ValueError("adjudication_count cannot exceed conflict_count")

    observed_agreement_ppm = _round_scaled_ratio(agreed_count, paired_count)
    counts_a = Counter(canonical_a)
    counts_b = Counter(canonical_b)
    expected_match_numerator = sum(
        count_a * counts_b.get(label, 0) for label, count_a in counts_a.items()
    )
    kappa_denominator = paired_count * paired_count - expected_match_numerator
    if kappa_denominator == 0:
        # When both reviewers only use one category, expected agreement is 1
        # and Cohen's kappa is mathematically undefined. Persist zero together
        # with an explicit flag so consumers never present it as perfect 1.0.
        cohen_kappa_micros = 0
        cohen_kappa_defined = False
    else:
        kappa_numerator = agreed_count * paired_count - expected_match_numerator
        cohen_kappa_micros = _round_scaled_ratio(kappa_numerator, kappa_denominator)
        cohen_kappa_defined = True

    return CalibrationMetrics(
        paired_submission_count=paired_count,
        agreed_count=agreed_count,
        conflict_count=conflict_count,
        adjudication_count=adjudication_count,
        observed_agreement_ppm=observed_agreement_ppm,
        cohen_kappa_micros=cohen_kappa_micros,
        cohen_kappa_defined=cohen_kappa_defined,
    )


compute_calibration_metrics = calculate_calibration_metrics
