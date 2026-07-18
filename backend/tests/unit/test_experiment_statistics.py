from __future__ import annotations

from app.services.experiment_service import _sample_ratio_diagnostic

ARMS_50_50 = [
    {"arm_key": "control", "allocation_ppm": 500_000},
    {"arm_key": "candidate", "allocation_ppm": 500_000},
]


def test_sample_ratio_diagnostic_accepts_balanced_assignment():
    diagnostic = _sample_ratio_diagnostic(
        {"control": 500, "candidate": 500},
        ARMS_50_50,
    )

    assert diagnostic["detected"] is False
    assert diagnostic["p_value"] == 1.0
    assert diagnostic["observed_ppm"] == {
        "control": 500_000.0,
        "candidate": 500_000.0,
    }


def test_sample_ratio_diagnostic_blocks_severe_selective_completion():
    diagnostic = _sample_ratio_diagnostic(
        {"control": 950, "candidate": 50},
        ARMS_50_50,
    )

    assert diagnostic["detected"] is True
    assert diagnostic["p_value"] < diagnostic["alpha"]
    assert diagnostic["chi_square"] > 0
