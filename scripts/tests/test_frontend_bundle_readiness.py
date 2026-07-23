from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_platform_readiness import (  # noqa: E402
    validate_frontend_bundle_release_lock,
)


def _approved_lock() -> dict[str, object]:
    source_commit = "a" * 40
    candidate_identity = (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/"
        "frontend-bundle-candidate.yml@refs/heads/main"
    )
    approval_identity = (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/"
        "frontend-bundle-promotion.yml@refs/heads/main"
    )
    return {
        "schema_version": 3,
        "kind": "auris-flow-frontend-bundle-lock",
        "status": "APPROVED",
        "reason": "Dual-signed protected promotion.",
        "candidate": {
            "artifact_ref": (
                "ghcr.io/g5n-dev/auris_flow/frontend-bundle-candidate@sha256:"
                + "1" * 64
            ),
            "signature_identity": candidate_identity,
            "signature_issuer": "https://token.actions.githubusercontent.com",
            "source_commit": source_commit,
            "repository_tree": "b" * 40,
            "frontend_tree": "c" * 40,
            "build_workflow_sha": source_commit,
            "candidate_sha256": "2" * 64,
            "bundle_report_sha256": "3" * 64,
            "vite_manifest_sha256": "4" * 64,
            "brotli_manifest_sha256": "5" * 64,
            "dist_inventory_sha256": "6" * 64,
            "package_lock_sha256": "7" * 64,
            "totals": {
                "jsRawBytes": 1,
                "jsBrotliBytes": 2,
                "allRawBytes": 3,
                "allBrotliBytes": 4,
            },
        },
        "approval": {
            "artifact_ref": (
                "ghcr.io/g5n-dev/auris_flow/frontend-bundle-approval@sha256:" + "8" * 64
            ),
            "statement_sha256": "9" * 64,
            "rebuild_evidence_sha256": "d" * 64,
            "approval_reference": "review-2026-001",
            "environment": "frontend-bundle-production",
            "promotion_workflow_sha": source_commit,
            "signature_identity": approval_identity,
            "signature_issuer": "https://token.actions.githubusercontent.com",
            "run_id": "12345",
            "run_attempt": 1,
        },
    }


class FrontendBundleReadinessTests(unittest.TestCase):
    def test_exact_approved_dual_signed_lock_passes(self) -> None:
        self.assertEqual([], validate_frontend_bundle_release_lock(_approved_lock()))

    def test_pending_lock_fails_closed(self) -> None:
        lock = {
            "artifact": None,
            "kind": "auris-flow-frontend-bundle-lock",
            "reason": "No approved artifact exists.",
            "schema_version": 1,
            "status": "PENDING",
        }

        failures = validate_frontend_bundle_release_lock(lock)

        self.assertTrue(any("PENDING" in failure for failure in failures))

    def test_malformed_approved_lock_cannot_pass_readiness(self) -> None:
        mutations = []

        missing_digest = copy.deepcopy(_approved_lock())
        del missing_digest["candidate"]["candidate_sha256"]  # type: ignore[index]
        mutations.append(missing_digest)

        wrong_kind = copy.deepcopy(_approved_lock())
        wrong_kind["kind"] = "lookalike-lock"
        mutations.append(wrong_kind)

        invalid_totals = copy.deepcopy(_approved_lock())
        invalid_totals["candidate"]["totals"]["jsRawBytes"] = True  # type: ignore[index]
        mutations.append(invalid_totals)

        invalid_run = copy.deepcopy(_approved_lock())
        invalid_run["approval"]["run_id"] = 12345  # type: ignore[index]
        mutations.append(invalid_run)

        branch_mismatch = copy.deepcopy(_approved_lock())
        branch_mismatch["approval"]["signature_identity"] = (  # type: ignore[index]
            "https://github.com/g5n-dev/auris_flow/.github/workflows/"
            "frontend-bundle-promotion.yml@refs/heads/release/v1"
        )
        mutations.append(branch_mismatch)

        for lock in mutations:
            with self.subTest(lock=lock):
                self.assertTrue(validate_frontend_bundle_release_lock(lock))


if __name__ == "__main__":
    unittest.main()
