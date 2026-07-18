from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "release-images.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_has_minimum_explicit_permissions_and_pinned_actions() -> None:
    text = _workflow_text()

    assert "permissions:\n  contents: read" in text
    assert "      id-token: write" in text
    assert "      packages: write" in text
    assert "pull_request_target" not in text
    action_references = re.findall(r"^\s*- uses: ([^\s#]+)", text, re.MULTILINE)
    assert action_references
    assert all(
        reference.startswith("./") or re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference) is not None
        for reference in action_references
    )


def test_release_workflow_enforces_multiarch_scan_sign_verify_and_no_latest() -> None:
    text = _workflow_text()

    assert "platforms: linux/amd64,linux/arm64" in text
    assert "severity: HIGH,CRITICAL" in text
    assert 'exit-code: "1"' in text
    assert "cosign sign --yes" in text
    assert "cosign verify" in text
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in text
    assert "provenance: mode=max" in text
    assert "sbom: true" in text
    assert "TRIVY_PLATFORM: linux/amd64" in text
    assert "TRIVY_PLATFORM: linux/arm64" in text
    assert "Scan every unique final Compose digest on both architectures" in text
    assert "scripts/render_release_compose.py scan-plan" in text
    assert "--ignorefile /dev/null" in text
    assert "Promote only verified and signed digests to immutable release tags" in text
    assert "refusing to retarget immutable release image" in text
    assert "tags: ${{ env.IMAGE_BUILD_REFERENCE }}" in text
    assert text.index("Scan every unique final Compose digest on both architectures") < text.index(
        "Promote only verified and signed digests to immutable release tags"
    )
    assert text.index(
        "Promote only verified and signed digests to immutable release tags"
    ) < text.index("Render and verify the production release bundle")
    assert ":latest" not in text


def test_release_workflow_binds_compose_and_manifest_to_same_commit() -> None:
    text = _workflow_text()

    assert "bash scripts/verify_release.sh" in text
    assert "scripts/render_release_compose.py create-lock" in text
    assert "scripts/render_release_compose.py render" in text
    assert "scripts/verify_production_compose.py --release" in text
    assert "scripts/render_release_compose.py manifest" in text
    assert '--source-commit "${SOURCE_COMMIT}"' in text
    assert "backend-python-audit.json" in text
    assert "dagster-python-audit.json" in text
    assert "npm-audit.json" in text
