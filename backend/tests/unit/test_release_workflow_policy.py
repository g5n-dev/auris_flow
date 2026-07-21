from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "release-images.yml"
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"


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


def test_verify_workflow_uses_exact_action_commits() -> None:
    text = VERIFY_WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"^\s*- uses: ([^\s#]+)", text, re.MULTILINE)

    assert action_references
    assert all(
        reference.startswith("./") or re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference) is not None
        for reference in action_references
    )


def test_every_formal_workflow_pins_runner_and_action_commits() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

    assert workflows
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        runners = re.findall(r"^\s*runs-on:\s*([^\s#]+)", text, re.MULTILINE)
        actions = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", text, re.MULTILINE)
        assert runners, workflow
        assert set(runners) == {"ubuntu-24.04"}, workflow
        assert actions, workflow
        assert all(
            reference.startswith("./")
            or re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference) is not None
            for reference in actions
        ), workflow


def _job_blocks(text: str) -> dict[str, str]:
    jobs = text.split("\njobs:\n", 1)[1]
    matches = list(re.finditer(r"^  ([a-z][a-z0-9-]+):\n", jobs, re.MULTILINE))
    return {
        match.group(1): jobs[match.start() : matches[index + 1].start()]
        if index + 1 < len(matches)
        else jobs[match.start() :]
        for index, match in enumerate(matches)
    }


def test_release_and_verify_workflows_bound_every_job_runtime() -> None:
    release_jobs = _job_blocks(_workflow_text())
    verify_jobs = _job_blocks(VERIFY_WORKFLOW.read_text(encoding="utf-8"))

    assert set(release_jobs) == {
        "release-context",
        "dependency-evidence",
        "build-images",
        "assemble-release",
        "publish-release",
    }
    assert set(verify_jobs) == {
        "clean-clone-reproducibility",
        "verify",
        "release-verify",
    }
    for name, block in {**release_jobs, **verify_jobs}.items():
        assert re.search(r"^    timeout-minutes: [1-9]\d*$", block, re.MULTILINE), name


def test_codeql_workflow_bounds_the_matrix_runtime() -> None:
    jobs = _job_blocks(CODEQL_WORKFLOW.read_text(encoding="utf-8"))

    assert set(jobs) == {"analyze"}
    assert re.search(r"^    timeout-minutes: [1-9]\d*$", jobs["analyze"], re.MULTILINE)


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
    release_gate = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    assert "bash scripts/verify_release.sh" in text
    assert "scripts/render_release_compose.py create-lock" in text
    assert "scripts/render_release_compose.py render" in text
    assert "scripts/verify_production_compose.py --release" in text
    assert "scripts/render_release_compose.py manifest" in text
    assert '--source-commit "${SOURCE_COMMIT}"' in text
    assert "backend-python-audit.json" in release_gate
    assert "dagster-python-audit.json" in release_gate
    assert "npm-audit.json" in release_gate
    assert "scripts/finalize_release_evidence.py" in release_gate
    assert release_gate.index("npm audit --prefix") < release_gate.index(
        "scripts/finalize_release_evidence.py"
    )


def test_release_workflow_binds_manual_and_push_runs_to_the_tag_commit() -> None:
    text = _workflow_text()

    assert "group: release-images-${{ inputs.release_tag || github.ref_name }}" in text
    assert "group: release-images-${{ github.ref }}" not in text
    assert "fetch-depth: 0" in text
    assert "refs/tags/${REQUESTED_TAG}^{commit}" in text
    assert '"${RESOLVED_COMMIT}" != "${GITHUB_SHA}"' in text
    assert '"${GITHUB_REPOSITORY}" != "auris-flow/auris-flow"' in text
    assert "release trust policy only permits auris-flow/auris-flow" in text
    assert '"${GITHUB_REF}" != "${expected_ref}"' in text
    assert '"${GITHUB_WORKFLOW_REF}" != "${expected_workflow_ref}"' in text
    assert "release workflow must execute from the exact requested tag ref" in text


def test_release_bundle_contains_governance_migrations_and_signed_checksums() -> None:
    text = _workflow_text()
    assembler = (ROOT / "scripts" / "release_bundle.py").read_text(encoding="utf-8")

    for source in (
        "LICENSE",
        "NOTICE",
        "CHANGELOG.md",
        "THIRD_PARTY_NOTICES.md",
        "SECURITY.md",
        "RELEASE_CHECKLIST.md",
        "production/README.md",
        "doc/backend-spec/migration-plan.md",
        "doc/release/versioning-and-compatibility.md",
        "doc/runbooks/release-supply-chain.md",
        "doc/runbooks/key-rotation.md",
        "doc/runbooks/security-incident-response.md",
        "production/compose.oidc-confidential.yaml",
        "production/restore-compatibility.json",
        "production/deployment-bundle.README.md",
        "scripts/verify_production_compose.py",
    ):
        assert source in assembler
    assert "scripts/release_bundle.py assemble" in text
    assert "--output build/release/deployment" in text
    assert "--compose-file build/release/deployment/production/compose.yaml" in text
    assert "cosign sign-blob --yes --bundle" in text
    assert "cosign verify-blob" in text
    assert "release-metadata.sigstore.json" in text
    assert "build/release/SHA256SUMS.sigstore.json" in text
    assert text.count('--artifact "') == 4
    assert "include-hidden-files: true" in text


def test_release_bundle_contains_deterministic_commit_bound_source_archive() -> None:
    text = _workflow_text()

    archive_step = "Create deterministic source, deployment, and evidence archives"
    manifest_step = "Produce commit-bound checksums and release manifest"
    assert archive_step in text
    archive_command = (
        'git archive --format=tar --prefix="auris-flow-${RELEASE_TAG}-source/" "${SOURCE_COMMIT}"'
    )
    assert archive_command in text
    assert "gzip -n" in text
    assert "build/release/auris-flow-${RELEASE_TAG}-source.tar.gz" in text
    assert "build/release/auris-flow-${RELEASE_TAG}-evidence.tar.gz" in text
    assert text.index(archive_step) < text.index(manifest_step)
    assert '--transform="s,^deployment,auris-flow-${RELEASE_TAG}-deployment,"' in text
    assert '--transform="s,^,auris-flow-${RELEASE_TAG}-evidence/,"' in text
    assert "--directory=build/release" in text
    assert "deployment" in text


def test_deployment_archive_uses_stable_compose_and_bound_metadata() -> None:
    text = _workflow_text()
    deployment_readme = (ROOT / "production" / "deployment-bundle.README.md").read_text(
        encoding="utf-8"
    )

    assert "scripts/release_bundle.py assemble" in text
    assert "scripts/release_bundle.py verify" in text
    assert "production/compose.release.json" in text
    assert "build/release/deployment/production/compose.yaml" in text
    assert "build/release/deployment/production/.env.example" in text
    assert "build/release/production/compose.release.json" not in text
    assert "build/release/production/.env" not in text
    assert "--file production/compose.yaml" in deployment_readme
    assert "compose.release.json" not in deployment_readme
    assert "build/release" not in deployment_readme
    assert "images.lock.env" not in deployment_readme
    assert "install -m 0444" in deployment_readme
    assert "auris-flow-${RELEASE_TAG}-release-metadata.sigstore.json" in deployment_readme
    assert "production/release-metadata.sigstore.json" in deployment_readme
    assert "--verify-signature" in deployment_readme

    metadata_signing = text.index(
        '--bundle "build/release/auris-flow-${RELEASE_TAG}-release-metadata.sigstore.json"'
    )
    archive_creation = text.index("Create deterministic source, deployment, and evidence archives")
    assert metadata_signing < archive_creation
    assert (
        '--artifact "build/release/auris-flow-'
        "${{ needs.release-context.outputs.release_tag }}-"
        'release-metadata.sigstore.json"' in text
    )


def test_release_workflow_publishes_permanent_approved_github_release_assets() -> None:
    text = _workflow_text()
    jobs = _job_blocks(text)
    publish = jobs["publish-release"]

    assert "needs: [release-context, assemble-release]" in publish
    assert "environment: release-publish" in publish
    assert "contents: write" in publish
    assert "github.event_name == 'push'" in publish
    assert "refs/tags/${RELEASE_TAG}^{commit}" in publish
    assert '"${TAG_COMMIT}" != "${SOURCE_COMMIT}"' in publish
    assert "refusing to replace an existing GitHub Release" in publish
    assert "gh release create" in publish
    assert "--verify-tag" in publish
    assert "auris-flow-${RELEASE_TAG}-deployment.tar.gz" in text
    assert "auris-flow-${RELEASE_TAG}-source.tar.gz" in publish
    assert "auris-flow-${RELEASE_TAG}-evidence.tar.gz" in publish
    assert "Reverify downloaded release evidence before publication" in publish
    assert "sha256sum --check SHA256SUMS" in publish
    assert "include-hidden-files: true" in text
    assert text.index(
        "Create deterministic source, deployment, and evidence archives"
    ) < text.index("Produce commit-bound checksums and release manifest")
    assert text.index("Produce commit-bound checksums and release manifest") < text.index(
        "publish-release:"
    )


def test_dependency_evidence_never_uploads_a_stale_success_manifest() -> None:
    text = _workflow_text()
    dependency = _job_blocks(text)["dependency-evidence"]

    assert dependency.count("bash scripts/verify_release.sh") == 1
    assert "rm -f build/release-evidence/release-gate-manifest.json" not in dependency
    assert "scripts/generate_supply_chain_evidence.py" not in dependency
    assert "pip-audit" not in dependency
    assert "npm audit" not in dependency
    assert "scripts/finalize_release_evidence.py" not in dependency
    assert "name: dependency-evidence-${{ needs.release-context.outputs.source_commit }}" in text
    failed_artifact = (
        "name: dependency-evidence-failed-${{ needs.release-context.outputs.source_commit }}"
    )
    assert failed_artifact in text
    assert "if: success()" in text
    assert "if: failure()" in text


def test_strict_release_jobs_install_pinned_cosign_before_visual_gate() -> None:
    pinned_installer = "sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159"
    pinned_login = "docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772"
    release_dependency = _job_blocks(_workflow_text())["dependency-evidence"]
    verify_jobs = _job_blocks(VERIFY_WORKFLOW.read_text(encoding="utf-8"))
    strict_verify_job = verify_jobs["release-verify"]

    for block in (release_dependency, strict_verify_job):
        assert "packages: read" in block
        assert "fetch-depth: 0" in block
        assert pinned_login in block
        assert pinned_installer in block
        assert 'cosign-release: "v2.5.3"' in block
        assert block.index(pinned_login) < block.index("bash scripts/verify_release.sh")
        assert block.index(pinned_installer) < block.index("bash scripts/verify_release.sh")

    fast_verify_job = verify_jobs["verify"]
    assert "packages: read" not in fast_verify_job
    assert pinned_login not in fast_verify_job
    assert pinned_installer not in fast_verify_job
    assert "bash scripts/verify_fast.sh" in fast_verify_job


def test_final_dependency_evidence_manifest_requires_all_audits() -> None:
    release_gate = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
    finalization = release_gate[release_gate.rindex("scripts/finalize_release_evidence.py") :]

    assert "--require-audits" in finalization.split("\n\n", 1)[0]
