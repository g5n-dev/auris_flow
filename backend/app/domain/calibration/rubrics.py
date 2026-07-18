from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CalibrationRubricProfile:
    version: str
    categories: frozenset[str]
    reason_codes: frozenset[str]

    def category_key(self, value: dict[str, Any]) -> str:
        decision = value.get("decision")
        if not isinstance(decision, str) or decision not in self.categories:
            raise ValueError(f"value is invalid for calibration rubric {self.version}")
        return decision

    def validate_submission(self, value: dict[str, Any], *, evidence_ref: str) -> None:
        self.category_key(value)
        reason_code = value.get("reason_code")
        if reason_code is not None and reason_code not in self.reason_codes:
            raise ValueError(f"reason_code is invalid for calibration rubric {self.version}")
        evidence_refs = value.get("evidence_refs") or []
        if evidence_refs and evidence_refs != [evidence_ref]:
            raise ValueError("evidence_refs must bind only to the current frozen sample evidence")

    def gold_value(self, value: dict[str, Any]) -> dict[str, str]:
        return {"decision": self.category_key(value)}


RUBRIC_PROFILES: dict[str, CalibrationRubricProfile] = {
    "rubric_evidence_consistency_v1": CalibrationRubricProfile(
        version="rubric_evidence_consistency_v1",
        categories=frozenset({"pass", "fail"}),
        reason_codes=frozenset(
            {
                "evidence_consistent",
                "evidence_conflict",
                "insufficient_evidence",
                "policy_exception",
                "other",
            }
        ),
    ),
    "rubric_quote_risk_v3": CalibrationRubricProfile(
        version="rubric_quote_risk_v3",
        categories=frozenset({"pass", "fail"}),
        reason_codes=frozenset(
            {
                "evidence_consistent",
                "evidence_conflict",
                "insufficient_evidence",
                "policy_exception",
                "other",
            }
        ),
    ),
}


def get_calibration_rubric(version: str) -> CalibrationRubricProfile:
    try:
        return RUBRIC_PROFILES[version]
    except KeyError as exc:
        raise ValueError(f"unsupported calibration rubric: {version}") from exc


__all__ = [
    "CalibrationRubricProfile",
    "RUBRIC_PROFILES",
    "get_calibration_rubric",
]
