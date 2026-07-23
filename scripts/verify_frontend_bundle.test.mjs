import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  frontendBundleVerifiedChecks,
  validateApprovalOciManifest,
  validateApprovalStatement,
  validateCandidateOciManifest,
  validateRebuildVerification
} from "./verify_frontend_bundle.mjs";

const sha256 = (character) => character.repeat(64);
const gitObject = (character) => character.repeat(40);

function approvedLock() {
  return {
    schema_version: 3,
    kind: "auris-flow-frontend-bundle-lock",
    status: "APPROVED",
    reason: "Dual-signed protected promotion.",
    candidate: {
      artifact_ref:
        "ghcr.io/g5n-dev/auris_flow/frontend-bundle-candidate@sha256:" +
        sha256("1"),
      candidate_sha256: sha256("2"),
      source_commit: gitObject("3"),
      repository_tree: gitObject("4"),
      frontend_tree: gitObject("5"),
      build_workflow_sha: gitObject("3"),
      signature_identity:
        "https://github.com/g5n-dev/auris_flow/.github/workflows/" +
        "frontend-bundle-candidate.yml@refs/heads/main",
      signature_issuer: "https://token.actions.githubusercontent.com",
      bundle_report_sha256: sha256("6"),
      vite_manifest_sha256: sha256("7"),
      brotli_manifest_sha256: sha256("8"),
      dist_inventory_sha256: sha256("9"),
      package_lock_sha256: sha256("a"),
      totals: {
        jsRawBytes: 100,
        jsBrotliBytes: 50,
        allRawBytes: 200,
        allBrotliBytes: 80
      }
    },
    approval: {
      artifact_ref:
        "ghcr.io/g5n-dev/auris_flow/frontend-bundle-approval@sha256:" +
        sha256("b"),
      statement_sha256: sha256("c"),
      rebuild_evidence_sha256: sha256("d"),
      approval_reference: "CAB-2026-0718",
      environment: "frontend-bundle-production",
      promotion_workflow_sha: gitObject("3"),
      signature_identity:
        "https://github.com/g5n-dev/auris_flow/.github/workflows/" +
        "frontend-bundle-promotion.yml@refs/heads/main",
      signature_issuer: "https://token.actions.githubusercontent.com",
      run_id: "123456789",
      run_attempt: 1
    }
  };
}

function approvalStatement(lock) {
  return {
    schema_version: "auris.frontend-bundle-approval.v1",
    kind: "auris-flow-frontend-bundle-approval",
    status: "APPROVED",
    candidate: structuredClone(lock.candidate),
    rebuild: {
      evidence_sha256: lock.approval.rebuild_evidence_sha256,
      frontend_tree: lock.candidate.frontend_tree,
      dist_inventory_sha256: lock.candidate.dist_inventory_sha256,
      bundle_report_sha256: lock.candidate.bundle_report_sha256,
      package_lock_sha256: lock.candidate.package_lock_sha256
    },
    approval: {
      reference: lock.approval.approval_reference,
      environment: lock.approval.environment,
      repository: "g5n-dev/auris_flow",
      ref: "refs/heads/main",
      event: "workflow_dispatch",
      workflow_sha: lock.approval.promotion_workflow_sha,
      signature_identity: lock.approval.signature_identity,
      signature_issuer: lock.approval.signature_issuer,
      run_id: lock.approval.run_id,
      run_attempt: lock.approval.run_attempt
    }
  };
}

test("candidate and approval OCI manifests bind signed immutable provenance", () => {
  const lock = approvedLock();
  const candidateManifest = {
    artifactType: "application/vnd.auris-flow.frontend-bundle-candidate.v1",
    annotations: {
      "org.opencontainers.image.revision": lock.candidate.source_commit,
      "org.opencontainers.image.source":
        "https://github.com/g5n-dev/auris_flow",
      "io.auris.frontend.candidate-sha256": lock.candidate.candidate_sha256,
      "io.auris.frontend.frontend-tree": lock.candidate.frontend_tree,
      "io.auris.frontend.job-workflow-sha": lock.candidate.build_workflow_sha
    }
  };
  const approvalManifest = {
    artifactType: "application/vnd.auris-flow.frontend-bundle-approval.v1",
    annotations: {
      "org.opencontainers.image.revision": lock.candidate.source_commit,
      "org.opencontainers.image.source":
        "https://github.com/g5n-dev/auris_flow",
      "io.auris.frontend.approval-statement-sha256":
        lock.approval.statement_sha256,
      "io.auris.frontend.candidate-ref": lock.candidate.artifact_ref,
      "io.auris.frontend.candidate-sha256": lock.candidate.candidate_sha256,
      "io.auris.frontend.job-workflow-sha":
        lock.approval.promotion_workflow_sha,
      "io.auris.frontend.rebuild-evidence-sha256":
        lock.approval.rebuild_evidence_sha256
    }
  };

  assert.doesNotThrow(() =>
    validateCandidateOciManifest(candidateManifest, lock.candidate)
  );
  assert.doesNotThrow(() => validateApprovalOciManifest(approvalManifest, lock));
  candidateManifest.annotations["io.auris.frontend.frontend-tree"] = gitObject("f");
  assert.throws(
    () => validateCandidateOciManifest(candidateManifest, lock.candidate),
    /frontend-tree/
  );
  approvalManifest.annotations["io.auris.frontend.candidate-ref"] += "-tampered";
  assert.throws(
    () => validateApprovalOciManifest(approvalManifest, lock),
    /candidate-ref/
  );
});

test("signed rebuild verification is an exact projection of the candidate", () => {
  const lock = approvedLock();
  const candidateDocument = {
    source: { frontend_tree: lock.candidate.frontend_tree },
    artifacts: {
      bundle_report_sha256: lock.candidate.bundle_report_sha256,
      vite_manifest_sha256: lock.candidate.vite_manifest_sha256,
      brotli_manifest_sha256: lock.candidate.brotli_manifest_sha256,
      dist_inventory_sha256: lock.candidate.dist_inventory_sha256
    },
    build_contract: {
      package_lock_sha256: lock.candidate.package_lock_sha256,
      node_version: "v22.17.0"
    },
    totals: lock.candidate.totals
  };
  const rebuildVerification = {
    schema_version: "auris.frontend-bundle-rebuild-verification.v1",
    kind: "auris-flow-frontend-bundle-rebuild-verification",
    status: "VERIFIED",
    candidate: structuredClone(lock.candidate),
    rebuild: {
      artifacts: candidateDocument.artifacts,
      build_contract: candidateDocument.build_contract,
      totals: candidateDocument.totals,
      frontend_tree: lock.candidate.frontend_tree,
      dist_file_count: 80
    }
  };

  assert.doesNotThrow(() =>
    validateRebuildVerification(
      rebuildVerification,
      lock,
      candidateDocument,
      80
    )
  );
  rebuildVerification.rebuild.dist_file_count = 79;
  assert.throws(
    () =>
      validateRebuildVerification(
        rebuildVerification,
        lock,
        candidateDocument,
        80
      ),
    /rebuild payload/
  );
});

test("approval statement is an exact projection of candidate, rebuild and authority", () => {
  const lock = approvedLock();
  const statement = approvalStatement(lock);
  assert.doesNotThrow(() => validateApprovalStatement(statement, lock));

  const tamperedCandidate = structuredClone(statement);
  tamperedCandidate.candidate.totals.jsRawBytes += 1;
  assert.throws(
    () => validateApprovalStatement(tamperedCandidate, lock),
    /approval candidate/
  );

  const tamperedAuthority = structuredClone(statement);
  tamperedAuthority.approval.run_id = "987654321";
  assert.throws(
    () => validateApprovalStatement(tamperedAuthority, lock),
    /approval authority/
  );

  const extra = structuredClone(statement);
  extra.approval.approved_by = "manual";
  assert.throws(() => validateApprovalStatement(extra, lock), /fields are not exact/);
});

test("release verifier has a closed evidence contract and no local candidate override", async () => {
  assert.deepEqual(frontendBundleVerifiedChecks, [
    "approval-cosign-signature",
    "approval-statement-binding",
    "approved-lock",
    "candidate-cosign-signature",
    "candidate-lock-binding",
    "candidate-oci-provenance",
    "candidate-source-ancestor",
    "current-release-build-binding",
    "exact-candidate-payload",
    "frontend-subtree-unchanged"
  ]);
  const source = await readFile(new URL("./verify_frontend_bundle.mjs", import.meta.url), "utf8");
  assert.match(source, /--certificate-github-workflow-sha/);
  assert.match(source, /--certificate-github-workflow-repository/);
  assert.match(source, /--certificate-github-workflow-ref/);
  assert.match(source, /--certificate-github-workflow-trigger/);
  assert.doesNotMatch(source, /--candidate-dir|--artifact-ref|--skip/);
  const releaseShell = await readFile(
    new URL("./verify_release.sh", import.meta.url),
    "utf8"
  );
  assert.equal(
    releaseShell.match(/verify_frontend_bundle\.mjs verify-release/g)?.length,
    1
  );
  assert.match(releaseShell, /--output "\$\{EVIDENCE_DIR\}\/frontend-bundle\.json"/);
  const verifyWorkflow = await readFile(
    new URL("../.github/workflows/verify.yml", import.meta.url),
    "utf8"
  );
  assert.match(
    verifyWorkflow,
    /verify:[\s\S]*?AURIS_RELEASE_CHECK:\s*"0"[\s\S]*?verify_fast\.sh/
  );
  assert.match(
    verifyWorkflow,
    /release-verify:[\s\S]*?startsWith\(github\.ref, 'refs\/tags\/v'\)/
  );
  assert.match(
    verifyWorkflow,
    /github\.event_name == 'workflow_dispatch' && inputs\.release_check/
  );
});
