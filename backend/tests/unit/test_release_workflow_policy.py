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
    assert "provenance: false" in text
    assert "sbom: false" in text
    assert "provenance: mode=max" not in text
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
    ) < text.index("Render final digest-pinned production release Compose")
    assert ":latest" not in text


def test_release_image_tag_promotion_only_treats_authenticated_404_as_absent() -> None:
    assemble = _job_blocks(_workflow_text())["assemble-release"]
    promote_step = "Promote only verified and signed digests to immutable release tags"
    render_step = "Render final digest-pinned production release Compose"
    promotion = assemble[assemble.index(promote_step) : assemble.index(render_step)]

    assert "docker buildx imagetools inspect" not in promotion
    assert "resolve_ghcr_manifest_digest()" in promotion
    assert "https://ghcr.io/token" in promotion
    assert '"scope=repository:${repository_path}:pull"' in promotion
    assert "https://ghcr.io/v2/${repository_path}/manifests/${tag}" in promotion
    assert "--head" in promotion
    assert "--write-out '%{http_code}'" in promotion
    assert "Docker-Content-Digest" in promotion
    assert 'case "${http_status}" in' in promotion
    assert '"200")' in promotion
    assert '"404")' in promotion
    assert "GHCR manifest lookup returned HTTP ${http_status}; refusing promotion" in promotion
    assert 'resolved_digest="$(resolve_ghcr_manifest_digest' in promotion
    assert 'if [ "${resolved_digest}" = "missing" ]; then' in promotion
    assert "docker buildx imagetools create" in promotion
    assert 'created_digest="$(resolve_ghcr_manifest_digest' in promotion
    assert 'if [ "${created_digest}" != "${digest}" ]; then' in promotion
    assert 'elif [ "${resolved_digest}" != "${digest}" ]; then' in promotion
    assert "refusing to retarget immutable release image" in promotion
    assert promotion.index("docker buildx imagetools create") < promotion.index(
        'created_digest="$(resolve_ghcr_manifest_digest'
    )


def test_release_images_generate_and_immediately_verify_signed_attestations() -> None:
    build = _job_blocks(_workflow_text())["build-images"]
    attest_action = "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6"

    assert "attestations: write" in build
    assert "artifact-metadata:" not in build
    assert build.count(attest_action) == 2
    assert "id: provenance_attestation" in build
    assert "id: sbom_attestation" in build
    assert build.count("subject-name: ${{ env.IMAGE_REPOSITORY }}") == 2
    assert build.count("subject-digest: ${{ steps.build.outputs.digest }}") == 2
    assert build.count("push-to-registry: true") == 2
    assert build.count("create-storage-record: false") == 2
    assert "sbom-path: build/image-evidence/${{ matrix.image_id }}.cdx.json" in build
    provenance_action = build[
        build.index("id: provenance_attestation") : build.index("id: sbom_attestation")
    ]
    assert "sbom-path:" not in provenance_action
    assert "${{ steps.provenance_attestation.outputs.bundle-path }}" in build
    assert "${{ steps.sbom_attestation.outputs.bundle-path }}" in build
    assert "${IMAGE_ID}.provenance.sigstore.json" in build
    assert "${IMAGE_ID}.sbom.sigstore.json" in build

    verify_step = "Immediately verify and sanitize both local attestation bundles"
    record_step = "Record verified image evidence and bound file digests"
    assert build.index("Generate SLSA v1 provenance attestation") < build.index(verify_step)
    assert build.index("Generate CycloneDX SBOM attestation") < build.index(verify_step)
    assert build.index(verify_step) < build.index(record_step)
    verification = build[build.index(verify_step) : build.index(record_step)]
    assert verification.count("gh attestation verify") == 2
    assert verification.count('"oci://${IMAGE_REPOSITORY}@${IMAGE_DIGEST}"') == 2
    assert verification.count('--repo "${GITHUB_REPOSITORY}"') == 2
    assert (
        verification.count(
            '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/release-images.yml"'
        )
        == 2
    )
    for flag in (
        '--signer-digest "${SOURCE_COMMIT}"',
        '--source-digest "${SOURCE_COMMIT}"',
        '--source-ref "${GITHUB_REF}"',
        "--deny-self-hosted-runners",
        "--format json",
    ):
        assert verification.count(flag) == 2
    assert "--predicate-type https://slsa.dev/provenance/v1" in verification
    assert "--predicate-type https://cyclonedx.org/bom" in verification
    assert "${IMAGE_ID}.provenance.verification.json" in verification
    assert "${IMAGE_ID}.sbom.verification.json" in verification
    assert "scripts/release_image_evidence.py sanitize-verification" in verification
    assert verification.count('--sbom-path "build/image-evidence/${IMAGE_ID}.cdx.json"') == 1

    record = build[build.index(record_step) :]
    assert "scripts/release_image_evidence.py create" in record
    assert '--image-digest "${IMAGE_DIGEST}"' in record
    assert '--source-commit "${SOURCE_COMMIT}"' in record


def test_assembly_reverifies_downloaded_bundles_before_lock_and_rechecks_hashes() -> None:
    assemble = _job_blocks(_workflow_text())["assemble-release"]
    reverify_step = "Reverify local and OCI registry image attestations before image lock creation"
    resolve_step = "Resolve every Compose image to an immutable digest"

    assert "attestations: read" not in assemble
    assert reverify_step in assemble
    assert assemble.index("docker/login-action@") < assemble.index(reverify_step)
    assert assemble.index("pattern: image-evidence-*") < assemble.index(reverify_step)
    assert assemble.index(reverify_step) < assemble.index(resolve_step)
    reverify = assemble[assemble.index(reverify_step) : assemble.index(resolve_step)]
    assert "scripts/release_image_evidence.py verify" in reverify
    assert "scripts/release_image_evidence.py add-assembly-verifications" in reverify
    assert reverify.count("gh attestation verify") == 4
    assert reverify.count("--bundle-from-oci") == 2
    assert reverify.count('--repo "${GITHUB_REPOSITORY}"') == 4
    assert (
        reverify.count(
            '--signer-workflow "${GITHUB_REPOSITORY}/.github/workflows/release-images.yml"'
        )
        == 4
    )
    for flag in (
        '--signer-digest "${SOURCE_COMMIT}"',
        '--source-digest "${SOURCE_COMMIT}"',
        '--source-ref "${GITHUB_REF}"',
        "--deny-self-hosted-runners",
        "--format json",
    ):
        assert reverify.count(flag) == 4
    assert "--predicate-type https://slsa.dev/provenance/v1" in reverify
    assert "--predicate-type https://cyclonedx.org/bom" in reverify
    assert "${image_id}.provenance.sigstore.json" in reverify
    assert "${image_id}.sbom.sigstore.json" in reverify
    assert "${image_id}.assembly.provenance.verification.json" in reverify
    assert "${image_id}.assembly.sbom.verification.json" in reverify
    assert "${image_id}.assembly.registry.provenance.verification.json" in reverify
    assert "${image_id}.assembly.registry.sbom.verification.json" in reverify
    assert reverify.count('--sbom-path "build/release/images/${image_id}.cdx.json"') == 2

    lock_reverify = assemble[
        assemble.index("Reverify exact application digests from final image lock") : assemble.index(
            "Promote only verified and signed digests to immutable release tags"
        )
    ]
    assert "scripts/release_image_evidence.py verify" in lock_reverify
    assert "--require-assembly-verifications" in lock_reverify

    archive_reverify_step = "Reverify every image record immediately before evidence archival"
    archive_step = "Create deterministic source, deployment, and evidence archives"
    assert assemble.index(archive_reverify_step) < assemble.index(archive_step)
    archive_reverify = assemble[
        assemble.index(archive_reverify_step) : assemble.index(archive_step)
    ]
    assert archive_reverify.count("scripts/release_image_evidence.py verify") == 1
    assert "--require-assembly-verifications" in archive_reverify


def test_release_docs_explain_signed_attestation_trust_and_buildkit_replacement() -> None:
    checklist = (ROOT / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    runbook = (ROOT / "doc" / "runbooks" / "release-supply-chain.md").read_text(encoding="utf-8")
    normalized = " ".join((checklist + "\n" + runbook).split())

    assert "SLSA v1 provenance attestation" in normalized
    assert "CycloneDX SBOM attestation" in normalized
    assert "gh attestation verify" in normalized
    assert "--deny-self-hosted-runners" in normalized
    assert "BuildKit automatic attestations are disabled" in normalized
    assert "signed final checksum set" in normalized
    assert "--bundle-from-oci" in normalized
    assert "not an independent trust anchor" in normalized
    assert "tag ruleset" in normalized
    assert "immutable release" in normalized


def test_release_workflow_binds_compose_and_manifest_to_same_commit() -> None:
    text = _workflow_text()
    release_gate = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    assert "bash scripts/verify_release.sh" in text
    assert "scripts/render_release_compose.py create-lock" in text
    assert "scripts/render_release_compose.py render" in text
    assert "scripts/verify_production_compose.py --release" in text
    assert "--profile '*' config --no-path-resolution" in text
    assert "scripts/render_release_compose.py manifest" in text
    assert '--source-commit "${SOURCE_COMMIT}"' in text
    assert "backend-python-audit.json" in release_gate
    assert "dagster-python-audit.json" in release_gate
    assert "npm-audit.json" in release_gate
    assert "scripts/finalize_release_evidence.py" in release_gate
    assert release_gate.index("npm audit --prefix") < release_gate.index(
        "scripts/finalize_release_evidence.py"
    )


def test_release_workflow_runs_final_digest_images_before_bundle_assembly() -> None:
    text = _workflow_text()
    assemble = _job_blocks(text)["assemble-release"]

    scan_step = "Scan every unique final Compose digest on both architectures"
    signature_step = "Reverify exact application digests from final image lock"
    promote_step = "Promote only verified and signed digests to immutable release tags"
    render_step = "Render final digest-pinned production release Compose"
    runtime_step = "Run final digest-pinned production path gate"
    bundle_step = "Assemble and sign the verified production release bundle"
    for step in (
        scan_step,
        signature_step,
        promote_step,
        render_step,
        runtime_step,
        bundle_step,
    ):
        assert step in assemble
    assert (
        assemble.index(scan_step)
        < assemble.index(signature_step)
        < assemble.index(promote_step)
        < assemble.index(render_step)
        < assemble.index(runtime_step)
        < assemble.index(bundle_step)
    )
    pinned_cosign = "sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159"
    assert assemble.count(pinned_cosign) == 1
    assert assemble.index(pinned_cosign) < assemble.index(signature_step)

    signature_block = assemble[assemble.index(signature_step) : assemble.index(promote_step)]
    assert "cosign verify" in signature_block
    assert '--certificate-identity "${WORKFLOW_IDENTITY}"' in signature_block
    assert (
        "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in signature_block
    )
    assert "build/release/images.lock.json" in signature_block
    assert "bff:bff,migrate,identity-bootstrap,worker,qdrant-backup-tool" in signature_block
    assert "dagster:dagster-code,dagster-webserver,dagster-daemon" in signature_block
    assert "edge:edge" in signature_block
    assert 'signed_image="$(jq -er \'.image\' "${record}")"' in signature_block
    assert '"${locked_image}" != "${signed_image}"' in signature_block

    runtime_block = assemble[assemble.index(runtime_step) : assemble.index(bundle_step)]
    assert "scripts/verify_production_path_runtime.py" in runtime_block
    assert "--prebuilt-release" in runtime_block
    assert "--base-compose production/compose.release.json" in runtime_block
    assert "--image-lock build/release/images.lock.json" in runtime_block
    assert '--release-tag "${RELEASE_TAG}"' in runtime_block
    assert '--source-commit "${SOURCE_COMMIT}"' in runtime_block
    assert "--artifact build/release/final-runtime/production-path-gate.json" in runtime_block
    assert "scripts/verify_production_path_gate.py release-evidence" in runtime_block
    assert '--expected-commit "${SOURCE_COMMIT}"' in runtime_block
    assert '--expected-release-tag "${RELEASE_TAG}"' in runtime_block
    assert "--release-compose production/compose.release.json" in runtime_block
    assert runtime_block.index("scripts/verify_production_path_runtime.py") < runtime_block.index(
        "scripts/verify_production_path_gate.py release-evidence"
    )
    assert "build/release/dependencies" not in runtime_block
    assert "build/release-evidence/production-path-gate.json" not in runtime_block
    assert "test ! -e build/release/final-runtime/production-path-gate.json" in runtime_block
    assert (
        "final-runtime"
        in assemble[
            assemble.index("Create deterministic source, deployment, and evidence archives") :
        ]
    )


def test_each_release_job_installs_cosign_once_before_its_actual_consumer() -> None:
    jobs = _job_blocks(_workflow_text())
    installer = "sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159"
    consumers = {
        "dependency-evidence": "Verify the complete release candidate in a clean runner",
        "build-images": "Keyless sign and verify the exact manifest digest",
        "assemble-release": "Reverify exact application digests from final image lock",
        "publish-release": "Reverify downloaded release evidence before publication",
    }

    assert installer not in jobs["release-context"]
    for job_name, consumer in consumers.items():
        block = jobs[job_name]
        assert block.count(installer) == 1, job_name
        assert consumer in block, job_name
        assert block.index(installer) < block.index(consumer), job_name


def test_release_workflow_binds_manual_and_push_runs_to_the_tag_commit() -> None:
    text = _workflow_text()

    assert "group: release-images-${{ inputs.release_tag || github.ref_name }}" in text
    assert "group: release-images-${{ github.ref }}" not in text
    assert "fetch-depth: 0" in text
    assert 'git cat-file -t "refs/tags/${REQUESTED_TAG}"' in text
    assert "release tag must be an annotated tag object" in text
    assert "refs/tags/${REQUESTED_TAG}^{commit}" in text
    assert '"${RESOLVED_COMMIT}" != "${GITHUB_SHA}"' in text
    assert '"${GITHUB_REPOSITORY}" != "g5n-dev/auris_flow"' in text
    assert "release trust policy only permits g5n-dev/auris_flow" in text
    assert '"${GITHUB_REF}" != "${expected_ref}"' in text
    assert '"${GITHUB_WORKFLOW_REF}" != "${expected_workflow_ref}"' in text
    assert "release workflow must execute from the exact requested tag ref" in text


def test_release_checklist_separates_pull_request_and_tag_only_controls() -> None:
    checklist = (ROOT / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    normalized = " ".join(checklist.split())

    assert "Default-branch ruleset" in normalized
    assert "Required checks must be produced on every pull request" in normalized
    assert "Release-tag ruleset" in normalized
    assert "must not be configured as default-branch required checks" in normalized


def test_release_bundle_contains_governance_migrations_and_signed_checksums() -> None:
    text = _workflow_text()
    assembler = (ROOT / "scripts" / "release_bundle.py").read_text(encoding="utf-8")

    for source in (
        "VERSION",
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
    assert text.count('--artifact "') == 6
    assert '--artifact "build/release/final-runtime/production-path-gate.json"' in text
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


def test_release_jobs_use_and_record_one_checksum_pinned_github_cli() -> None:
    text = _workflow_text()
    jobs = _job_blocks(text)
    expected_version = 'GH_CLI_VERSION: "2.96.0"'
    expected_archive_sha = (
        "GH_CLI_LINUX_AMD64_SHA256: "
        '"83d5c2ccad5498f58bf6368acb1ab32588cf43ab3a4b1c301bf36328b1c8bd60"'
    )

    assert text.count(expected_version) == 1
    assert text.count(expected_archive_sha) == 1
    assert (
        "GH_CLI_ASSET_URL: "
        '"https://github.com/cli/cli/releases/download/v2.96.0/'
        'gh_2.96.0_linux_amd64.tar.gz"'
    ) in text
    assert text.count("Install and verify checksum-pinned GitHub CLI") == 3
    assert text.count("sha256sum --check --strict") >= 4
    assert text.count('"${actual_version}" != "${GH_CLI_VERSION}"') == 3
    assert text.count("auris.release-toolchain.v1") == 3

    for job_name in ("build-images", "assemble-release", "publish-release"):
        block = jobs[job_name]
        install = block.index("Install and verify checksum-pinned GitHub CLI")
        assert install < block.index("gh "), job_name

    assemble = jobs["assemble-release"]
    publish = jobs["publish-release"]
    assert "build/release/gh-cli-toolchain.json" in assemble
    assert '--artifact "build/release/gh-cli-toolchain.json"' in assemble
    assert "gh-cli-toolchain.json" in publish
    assert 'cmp --silent "${RUNNER_TEMP}/gh-cli-toolchain.json"' in publish
    assert '"build/release/gh-cli-toolchain.json"' in publish


def test_release_workflow_publishes_permanent_approved_github_release_assets() -> None:
    text = _workflow_text()
    jobs = _job_blocks(text)
    publish = jobs["publish-release"]

    assert "needs: [release-context, assemble-release]" in publish
    assert "environment: release-publish" in publish
    assert "contents: write" in publish
    assert "packages: read" in publish
    assert "github.event_name == 'push'" in publish
    assert "refs/tags/${RELEASE_TAG}^{commit}" in publish
    assert '"${TAG_COMMIT}" != "${SOURCE_COMMIT}"' in publish
    assert "gh release create" in publish
    assert "--draft" in publish
    assert "gh release edit" in publish
    assert "--draft=false" in publish
    assert "--verify-tag" in publish
    assert "auris-flow-${RELEASE_TAG}-deployment.tar.gz" in text
    assert "auris-flow-${RELEASE_TAG}-source.tar.gz" in publish
    assert "auris-flow-${RELEASE_TAG}-evidence.tar.gz" in publish
    assert '"build/release/final-runtime/production-path-gate.json"' in publish
    assert "Reverify downloaded release evidence before publication" in publish
    assert "sha256sum --check --strict SHA256SUMS" in publish
    assert "signed checksum set must bind the evidence archive exactly once" in publish
    assert "scripts/release_image_evidence.py verify-archive" in publish
    assert publish.count('--image "') == 3
    assert publish.count("scripts/verify_production_path_gate.py release-evidence") == 1
    assert "--release-compose build/release/deployment/production/compose.yaml" in publish
    assert '--expected-commit "${SOURCE_COMMIT}"' in publish
    assert '--expected-release-tag "${RELEASE_TAG}"' in publish
    assert publish.index("sha256sum --check --strict SHA256SUMS") < publish.index(
        "scripts/release_image_evidence.py verify-archive"
    )
    assert publish.index("scripts/release_image_evidence.py verify-archive") < publish.index(
        "scripts/verify_production_path_gate.py release-evidence"
    )
    assert publish.count("verify_remote_annotated_tag") == 5
    assert "repos/${GITHUB_REPOSITORY}/git/ref/tags/${RELEASE_TAG}" in publish
    assert "repos/${GITHUB_REPOSITORY}/git/tags/${object_sha}" in publish
    assert '"${ref_type}" != "tag"' in publish
    assert '"${object_type}" != "commit"' in publish
    assert "draft remains unpublished" in publish
    assert "release is retained for incident review" in publish
    release_create = 'gh release create "${RELEASE_TAG}"'
    release_edit = 'gh release edit "${RELEASE_TAG}"'
    preflight = publish.rindex("verify_remote_annotated_tag", 0, publish.index(release_create))
    draft_postflight = publish.index("verify_remote_annotated_tag", publish.index(release_create))
    preflight_segment = publish[preflight : publish.index(release_create)]
    assert preflight_segment.strip().startswith("verify_remote_annotated_tag")
    assert "gh release" not in preflight_segment
    assert publish.index(release_create) < draft_postflight < publish.index(release_edit)
    assert publish.index("verify_release_image_tags", draft_postflight) < publish.index(
        release_edit
    )
    assert publish.index(release_edit) < publish.rindex("verify_remote_annotated_tag")
    assert "for image_id in bff dagster edge" in publish
    assert 'cosign verify "${locked_image}"' in publish
    assert "resolve_ghcr_manifest_digest()" in publish
    assert 'if [ "${remote_digest}" != "${digest}" ]; then' in publish
    assert "GHCR release tag does not resolve to the signed ${image_id} digest" in publish
    assert "publish_flags+=(--prerelease --latest=false)" in publish
    assert "publish_flags+=(--prerelease=false --latest)" in publish
    assert text.count("scripts/verify_production_path_gate.py release-evidence") == 2
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
