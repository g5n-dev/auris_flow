#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  atomicWrite,
  canonicalJson,
  sha256,
  verifyCandidateDirectory
} from "../prototype/auris-flow-ui/scripts/frontend-bundle-candidate.mjs";
import {
  FRONTEND_BUNDLE_APPROVAL_KIND,
  buildDistInventory,
  distInventorySha256,
  frontendBundleLockPath,
  frontendBundleLockPatterns,
  loadFrontendBundleLock,
  sha256CanonicalJsonFile,
  sha256File,
  validateBundleTotals,
  validateFrontendBundleLock
} from "../prototype/auris-flow-ui/scripts/frontend-bundle-lock.mjs";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const uiRoot = join(root, "prototype/auris-flow-ui");
const distDir = join(uiRoot, "dist");
const checkerPath = join(uiRoot, "scripts/check-bundle-budget.mjs");
const evidencePath = join(root, "build/release-evidence/frontend-bundle.json");
const officialRepository = "g5n-dev/auris_flow";
const officialSource = `https://github.com/${officialRepository}`;
const candidateMediaType =
  "application/vnd.auris-flow.frontend-bundle-candidate.v1";
const approvalMediaType =
  "application/vnd.auris-flow.frontend-bundle-approval.v1";
const candidateDirectoryEntries = Object.freeze([
  "SHA256SUMS",
  "brotli-manifest.json",
  "bundle-report.json",
  "candidate.json",
  "dist-inventory.json",
  "vite-manifest.json"
]);
export const frontendBundleVerifiedChecks = Object.freeze([
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

const {
  gitObjectPattern,
  sha256Pattern,
  officialArtifactPattern,
  officialIdentityPattern,
  officialApprovalArtifactPattern,
  officialApprovalIdentityPattern
} = frontendBundleLockPatterns;

function exactKeys(value, keys) {
  return (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    canonicalJson(Object.keys(value).sort()) === canonicalJson([...keys].sort())
  );
}

function requireExactKeys(value, keys, label) {
  if (!exactKeys(value, keys)) {
    throw new Error(`${label} fields are not exact`);
  }
}

function requireEqual(actual, expected, label) {
  if (canonicalJson(actual) !== canonicalJson(expected)) {
    throw new Error(`${label} does not match the approved lock`);
  }
}

function requireDigest(value, label) {
  if (!sha256Pattern.test(value ?? "")) {
    throw new Error(`${label} must be a SHA-256 digest`);
  }
}

function requireGitObject(value, label) {
  if (!gitObjectPattern.test(value ?? "")) {
    throw new Error(`${label} must be an exact Git object`);
  }
}

function requireRegularFile(path, label) {
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) {
    throw new Error(`${label} must be a regular non-linked file`);
  }
  return stat;
}

function requireRealDirectory(path, label) {
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a real directory`);
  }
}

function run(command, args, { cwd = root, capture = true } = {}) {
  const completed = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
    timeout: 120_000
  });
  if (completed.error) {
    throw new Error(`${command} is required for frontend bundle verification`);
  }
  if (completed.signal) {
    throw new Error(`${command} was terminated by ${completed.signal}`);
  }
  if (completed.status !== 0) {
    throw new Error(`${command} failed during frontend bundle verification`);
  }
  return completed.stdout?.trim() ?? "";
}

function git(...args) {
  return run("git", args);
}

function workflowBranch(identity, pattern, label) {
  const match = pattern.exec(identity ?? "");
  const branch = match?.groups?.branch;
  if (!branch || branch.includes("..") || branch.includes("//") || branch.endsWith("/")) {
    throw new Error(`${label} identity is invalid`);
  }
  return branch;
}

function verifyCosign(reference, identity, workflowSha, identityPattern) {
  const branch = workflowBranch(identity, identityPattern, "Cosign workflow");
  run("cosign", [
    "verify",
    reference,
    "--certificate-identity",
    identity,
    "--certificate-oidc-issuer",
    "https://token.actions.githubusercontent.com",
    "--certificate-github-workflow-sha",
    workflowSha,
    "--certificate-github-workflow-repository",
    officialRepository,
    "--certificate-github-workflow-ref",
    `refs/heads/${branch}`,
    "--certificate-github-workflow-trigger",
    "workflow_dispatch"
  ]);
}

function fetchOciManifest(reference) {
  const output = run("oras", ["manifest", "fetch", reference]);
  let manifest;
  try {
    manifest = JSON.parse(output);
  } catch {
    throw new Error("OCI manifest is not valid JSON");
  }
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new Error("OCI manifest must be an object");
  }
  return manifest;
}

export function validateCandidateOciManifest(manifest, candidate) {
  if (manifest.artifactType !== candidateMediaType) {
    throw new Error("candidate OCI artifactType is invalid");
  }
  const annotations = manifest.annotations;
  if (!annotations || typeof annotations !== "object" || Array.isArray(annotations)) {
    throw new Error("candidate OCI annotations are missing");
  }
  const expected = {
    "io.auris.frontend.candidate-sha256": candidate.candidate_sha256,
    "io.auris.frontend.frontend-tree": candidate.frontend_tree,
    "io.auris.frontend.job-workflow-sha": candidate.build_workflow_sha,
    "org.opencontainers.image.revision": candidate.source_commit,
    "org.opencontainers.image.source": officialSource
  };
  for (const [key, value] of Object.entries(expected)) {
    if (annotations[key] !== value) {
      throw new Error(`candidate OCI annotation does not match: ${key}`);
    }
  }
}

export function validateApprovalOciManifest(manifest, lock) {
  if (manifest.artifactType !== approvalMediaType) {
    throw new Error("approval OCI artifactType is invalid");
  }
  const annotations = manifest.annotations;
  if (!annotations || typeof annotations !== "object" || Array.isArray(annotations)) {
    throw new Error("approval OCI annotations are missing");
  }
  const expected = {
    "io.auris.frontend.approval-statement-sha256":
      lock.approval.statement_sha256,
    "io.auris.frontend.candidate-ref": lock.candidate.artifact_ref,
    "io.auris.frontend.candidate-sha256": lock.candidate.candidate_sha256,
    "io.auris.frontend.job-workflow-sha": lock.approval.promotion_workflow_sha,
    "io.auris.frontend.rebuild-evidence-sha256":
      lock.approval.rebuild_evidence_sha256,
    "org.opencontainers.image.revision": lock.candidate.source_commit,
    "org.opencontainers.image.source": officialSource
  };
  for (const [key, value] of Object.entries(expected)) {
    if (annotations[key] !== value) {
      throw new Error(`approval OCI annotation does not match: ${key}`);
    }
  }
}

function candidateProjection(candidate) {
  return {
    artifact_ref: candidate.artifact_ref,
    candidate_sha256: candidate.candidate_sha256,
    source_commit: candidate.source_commit,
    repository_tree: candidate.repository_tree,
    frontend_tree: candidate.frontend_tree,
    build_workflow_sha: candidate.build_workflow_sha,
    bundle_report_sha256: candidate.bundle_report_sha256,
    vite_manifest_sha256: candidate.vite_manifest_sha256,
    brotli_manifest_sha256: candidate.brotli_manifest_sha256,
    dist_inventory_sha256: candidate.dist_inventory_sha256,
    package_lock_sha256: candidate.package_lock_sha256,
    signature_identity: candidate.signature_identity,
    signature_issuer: candidate.signature_issuer,
    totals: candidate.totals
  };
}

export function validateApprovalStatement(statement, lock) {
  requireExactKeys(
    statement,
    ["approval", "candidate", "kind", "rebuild", "schema_version", "status"],
    "approval statement"
  );
  if (
    statement.schema_version !== "auris.frontend-bundle-approval.v1" ||
    statement.kind !== FRONTEND_BUNDLE_APPROVAL_KIND ||
    statement.status !== "APPROVED"
  ) {
    throw new Error("approval statement identity or status is invalid");
  }
  requireExactKeys(
    statement.candidate,
    [
      "artifact_ref",
      "brotli_manifest_sha256",
      "build_workflow_sha",
      "bundle_report_sha256",
      "candidate_sha256",
      "dist_inventory_sha256",
      "frontend_tree",
      "package_lock_sha256",
      "repository_tree",
      "signature_identity",
      "signature_issuer",
      "source_commit",
      "totals",
      "vite_manifest_sha256"
    ],
    "approval candidate"
  );
  requireEqual(statement.candidate, candidateProjection(lock.candidate), "approval candidate");
  requireExactKeys(
    statement.rebuild,
    [
      "bundle_report_sha256",
      "dist_inventory_sha256",
      "evidence_sha256",
      "frontend_tree",
      "package_lock_sha256"
    ],
    "approval rebuild"
  );
  requireEqual(
    statement.rebuild,
    {
      bundle_report_sha256: lock.candidate.bundle_report_sha256,
      dist_inventory_sha256: lock.candidate.dist_inventory_sha256,
      evidence_sha256: lock.approval.rebuild_evidence_sha256,
      frontend_tree: lock.candidate.frontend_tree,
      package_lock_sha256: lock.candidate.package_lock_sha256
    },
    "approval rebuild"
  );
  requireExactKeys(
    statement.approval,
    [
      "environment",
      "event",
      "ref",
      "reference",
      "repository",
      "run_attempt",
      "run_id",
      "signature_identity",
      "signature_issuer",
      "workflow_sha"
    ],
    "approval authority"
  );
  const branch = workflowBranch(
    lock.approval.signature_identity,
    officialApprovalIdentityPattern,
    "approval"
  );
  requireEqual(
    statement.approval,
    {
      environment: lock.approval.environment,
      event: "workflow_dispatch",
      ref: `refs/heads/${branch}`,
      reference: lock.approval.approval_reference,
      repository: officialRepository,
      run_attempt: lock.approval.run_attempt,
      run_id: lock.approval.run_id,
      signature_identity: lock.approval.signature_identity,
      signature_issuer: lock.approval.signature_issuer,
      workflow_sha: lock.approval.promotion_workflow_sha
    },
    "approval authority"
  );
}

function verifyCandidateLockBinding(candidateDocument, verified, lock) {
  const expected = lock.candidate;
  requireEqual(
    {
      commit: candidateDocument.source.commit,
      frontend_tree: candidateDocument.source.frontend_tree,
      repository_tree: candidateDocument.source.repository_tree
    },
    {
      commit: expected.source_commit,
      frontend_tree: expected.frontend_tree,
      repository_tree: expected.repository_tree
    },
    "candidate source"
  );
  if (verified.candidate_sha256 !== expected.candidate_sha256) {
    throw new Error("candidate document SHA-256 does not match the lock");
  }
  requireEqual(
    candidateDocument.artifacts,
    {
      brotli_manifest_sha256: expected.brotli_manifest_sha256,
      bundle_report_sha256: expected.bundle_report_sha256,
      dist_inventory_sha256: expected.dist_inventory_sha256,
      vite_manifest_sha256: expected.vite_manifest_sha256
    },
    "candidate artifact hashes"
  );
  if (
    candidateDocument.build_contract.package_lock_sha256 !==
    expected.package_lock_sha256
  ) {
    throw new Error("candidate package lock does not match the approved lock");
  }
  requireEqual(candidateDocument.totals, expected.totals, "candidate totals");
}

function verifyRepositoryBinding(sourceCommit, lock) {
  requireGitObject(sourceCommit, "release source commit");
  const head = git("rev-parse", "--verify", "HEAD^{commit}");
  if (head !== sourceCommit) {
    throw new Error("release source commit does not equal HEAD");
  }
  if (git("status", "--porcelain=v1", "--untracked-files=all")) {
    throw new Error("frontend bundle release verification requires a clean Git tree");
  }
  const ancestor = spawnSync(
    "git",
    ["merge-base", "--is-ancestor", lock.candidate.source_commit, sourceCommit],
    { cwd: root, stdio: "ignore", timeout: 30_000 }
  );
  if (ancestor.status !== 0) {
    throw new Error("candidate source commit is not an ancestor of release HEAD");
  }
  if (
    git("rev-parse", `${lock.candidate.source_commit}^{tree}`) !==
    lock.candidate.repository_tree
  ) {
    throw new Error("candidate repository tree does not match its source commit");
  }
  const candidateFrontendTree = git(
    "rev-parse",
    `${lock.candidate.source_commit}:prototype/auris-flow-ui`
  );
  const releaseFrontendTree = git(
    "rev-parse",
    "HEAD:prototype/auris-flow-ui"
  );
  if (
    candidateFrontendTree !== lock.candidate.frontend_tree ||
    releaseFrontendTree !== lock.candidate.frontend_tree
  ) {
    throw new Error("frontend subtree changed after the signed candidate");
  }
}

function pullArtifact(reference, destination) {
  mkdirSync(destination, { mode: 0o755 });
  requireRealDirectory(destination, "OCI pull destination");
  run("oras", ["pull", reference, "--output", destination]);
}

function readCanonicalJsonFile(path, label, sizeLimit = 1024 * 1024) {
  const stat = requireRegularFile(path, label);
  if (stat.size > sizeLimit) {
    throw new Error(`${label} exceeds the size limit`);
  }
  const raw = readFileSync(path, "utf8");
  const value = JSON.parse(raw);
  if (raw !== canonicalJson(value)) {
    throw new Error(`${label} is not canonical JSON`);
  }
  return { path, raw, value };
}

function readApprovalArtifact(directory) {
  requireRealDirectory(directory, "approval artifact directory");
  const entries = readdirSync(directory).sort();
  if (
    canonicalJson(entries) !==
    canonicalJson(["approval-statement.json", "rebuild-verification.json"])
  ) {
    throw new Error(
      "approval OCI artifact must contain only approval-statement.json and rebuild-verification.json"
    );
  }
  return {
    statement: readCanonicalJsonFile(
      join(directory, "approval-statement.json"),
      "approval statement"
    ),
    rebuild: readCanonicalJsonFile(
      join(directory, "rebuild-verification.json"),
      "rebuild verification"
    )
  };
}

export function validateRebuildVerification(
  rebuildVerification,
  lock,
  candidateDocument,
  distFileCount
) {
  requireExactKeys(
    rebuildVerification,
    ["candidate", "kind", "rebuild", "schema_version", "status"],
    "rebuild verification"
  );
  if (
    rebuildVerification.schema_version !==
      "auris.frontend-bundle-rebuild-verification.v1" ||
    rebuildVerification.kind !==
      "auris-flow-frontend-bundle-rebuild-verification" ||
    rebuildVerification.status !== "VERIFIED"
  ) {
    throw new Error("rebuild verification identity or status is invalid");
  }
  requireEqual(
    rebuildVerification.candidate,
    lock.candidate,
    "rebuild candidate"
  );
  requireExactKeys(
    rebuildVerification.rebuild,
    [
      "artifacts",
      "build_contract",
      "dist_file_count",
      "frontend_tree",
      "totals"
    ],
    "rebuild payload"
  );
  requireEqual(
    rebuildVerification.rebuild,
    {
      artifacts: candidateDocument.artifacts,
      build_contract: candidateDocument.build_contract,
      dist_file_count: distFileCount,
      frontend_tree: candidateDocument.source.frontend_tree,
      totals: candidateDocument.totals
    },
    "rebuild payload"
  );
}

function currentBundleReport() {
  const completed = spawnSync(process.execPath, [checkerPath], {
    cwd: uiRoot,
    encoding: "utf8",
    env: {
      ...process.env,
      AURIS_REQUIRE_APPROVED_BUNDLE: "1"
    },
    maxBuffer: 64 * 1024 * 1024,
    timeout: 120_000
  });
  if (completed.error || completed.signal || completed.status !== 0) {
    throw new Error("current frontend build does not match the approved bundle lock");
  }
  let report;
  try {
    report = JSON.parse(completed.stdout);
  } catch {
    throw new Error("frontend bundle checker did not emit JSON");
  }
  if (
    report.status !== "ok" ||
    report.failures?.length !== 0 ||
    report.approvedBundleGate?.required !== true ||
    report.approvedBundleGate?.status !== "approved" ||
    report.approvedBundleGate?.failures?.length !== 0
  ) {
    throw new Error("frontend bundle checker did not prove the approved build");
  }
  return report;
}

function reportTotals(report) {
  return {
    jsRawBytes: report?.totals?.js?.rawBytes,
    jsBrotliBytes: report?.totals?.js?.brotliBytes,
    allRawBytes: report?.totals?.all?.rawBytes,
    allBrotliBytes: report?.totals?.all?.brotliBytes
  };
}

function verifyCurrentBuild(report, lock) {
  const totals = reportTotals(report);
  const totalFailures = validateBundleTotals(totals, "current bundle totals");
  if (totalFailures.length) throw new Error(totalFailures.join("; "));
  requireEqual(totals, lock.candidate.totals, "current bundle totals");
  const inventory = buildDistInventory(distDir);
  const inventoryDigest = distInventorySha256(inventory);
  if (
    report.distInventory?.sha256 !== inventoryDigest ||
    inventoryDigest !== lock.candidate.dist_inventory_sha256
  ) {
    throw new Error("current dist inventory does not match the approved lock");
  }
  const checks = {
    package_lock_sha256: sha256File(join(uiRoot, "package-lock.json")),
    vite_manifest_sha256: sha256CanonicalJsonFile(
      join(distDir, ".vite/manifest.json")
    ),
    brotli_manifest_sha256: sha256CanonicalJsonFile(
      join(distDir, ".vite/brotli-manifest.json")
    )
  };
  for (const [key, value] of Object.entries(checks)) {
    if (value !== lock.candidate[key]) {
      throw new Error(`current ${key} does not match the approved lock`);
    }
  }
}

function ensureEvidenceOutput(output) {
  if (resolve(output) !== evidencePath) {
    throw new Error(
      "frontend bundle evidence output must be build/release-evidence/frontend-bundle.json"
    );
  }
  const buildDir = join(root, "build");
  const evidenceDir = dirname(evidencePath);
  requireRealDirectory(buildDir, "release build directory");
  requireRealDirectory(evidenceDir, "release evidence directory");
  if (basename(evidencePath) !== "frontend-bundle.json") {
    throw new Error("frontend bundle evidence filename is invalid");
  }
  try {
    requireRegularFile(evidencePath, "existing frontend bundle evidence");
    rmSync(evidencePath);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
}

export async function verifyReleaseFrontendBundle({ sourceCommit, output }) {
  ensureEvidenceOutput(output);
  requireRegularFile(frontendBundleLockPath, "frontend bundle lock");
  const lockRaw = readFileSync(frontendBundleLockPath);
  const lock = loadFrontendBundleLock();
  const lockFailures = validateFrontendBundleLock(lock, { requireApproved: true });
  if (lockFailures.length) {
    throw new Error(`frontend bundle lock is not approved: ${lockFailures.join("; ")}`);
  }
  verifyRepositoryBinding(sourceCommit, lock);

  verifyCosign(
    lock.candidate.artifact_ref,
    lock.candidate.signature_identity,
    lock.candidate.build_workflow_sha,
    officialIdentityPattern
  );
  validateCandidateOciManifest(
    fetchOciManifest(lock.candidate.artifact_ref),
    lock.candidate
  );
  verifyCosign(
    lock.approval.artifact_ref,
    lock.approval.signature_identity,
    lock.approval.promotion_workflow_sha,
    officialApprovalIdentityPattern
  );
  validateApprovalOciManifest(
    fetchOciManifest(lock.approval.artifact_ref),
    lock
  );

  const temporary = mkdtempSync(join(tmpdir(), "auris-frontend-bundle-release."));
  try {
    const candidateDirectory = join(temporary, "candidate");
    pullArtifact(lock.candidate.artifact_ref, candidateDirectory);
    if (
      canonicalJson(readdirSync(candidateDirectory).sort()) !==
      canonicalJson(candidateDirectoryEntries)
    ) {
      throw new Error("candidate OCI artifact entries are not exact");
    }
    const verified = await verifyCandidateDirectory(candidateDirectory);
    verifyCandidateLockBinding(verified.candidate, verified, lock);

    const approvalDirectory = join(temporary, "approval");
    pullArtifact(lock.approval.artifact_ref, approvalDirectory);
    const approval = readApprovalArtifact(approvalDirectory);
    if (sha256(approval.statement.raw) !== lock.approval.statement_sha256) {
      throw new Error("approval statement SHA-256 does not match the lock");
    }
    if (sha256(approval.rebuild.raw) !== lock.approval.rebuild_evidence_sha256) {
      throw new Error("rebuild verification SHA-256 does not match the lock");
    }
    validateApprovalStatement(approval.statement.value, lock);
    const candidateInventory = JSON.parse(
      readFileSync(join(candidateDirectory, "dist-inventory.json"), "utf8")
    );
    validateRebuildVerification(
      approval.rebuild.value,
      lock,
      verified.candidate,
      candidateInventory.length
    );

    const report = currentBundleReport();
    verifyCurrentBuild(report, lock);

    const candidateDigest = lock.candidate.artifact_ref.split("@")[1];
    const approvalDigest = lock.approval.artifact_ref.split("@")[1];
    const evidence = {
      schema_version: "auris.frontend-bundle-evidence.v1",
      kind: "auris-flow-frontend-bundle-evidence",
      status: "ok",
      source_commit: sourceCommit,
      candidate_source_commit: lock.candidate.source_commit,
      candidate_repository_tree: lock.candidate.repository_tree,
      frontend_tree: lock.candidate.frontend_tree,
      lock_sha256: createHash("sha256").update(lockRaw).digest("hex"),
      artifact_ref: lock.candidate.artifact_ref,
      artifact_digest: candidateDigest,
      approval_artifact_ref: lock.approval.artifact_ref,
      approval_artifact_digest: approvalDigest,
      approval_reference: lock.approval.approval_reference,
      signature_identity: lock.candidate.signature_identity,
      signature_issuer: lock.candidate.signature_issuer,
      approval_signature_identity: lock.approval.signature_identity,
      approval_signature_issuer: lock.approval.signature_issuer,
      build_workflow_sha: lock.candidate.build_workflow_sha,
      promotion_workflow_sha: lock.approval.promotion_workflow_sha,
      candidate_sha256: lock.candidate.candidate_sha256,
      bundle_report_sha256: lock.candidate.bundle_report_sha256,
      vite_manifest_sha256: lock.candidate.vite_manifest_sha256,
      brotli_manifest_sha256: lock.candidate.brotli_manifest_sha256,
      dist_inventory_sha256: lock.candidate.dist_inventory_sha256,
      package_lock_sha256: lock.candidate.package_lock_sha256,
      approval_statement_sha256: lock.approval.statement_sha256,
      rebuild_evidence_sha256: lock.approval.rebuild_evidence_sha256,
      totals: lock.candidate.totals,
      verified_checks: [...frontendBundleVerifiedChecks]
    };
    atomicWrite(evidencePath, canonicalJson(evidence));
    return evidence;
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

function parseArguments(argv) {
  if (argv[0] !== "verify-release") {
    throw new Error(
      "usage: verify_frontend_bundle.mjs verify-release --source-commit <sha> --output <path>"
    );
  }
  const values = {};
  for (let index = 1; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined || value.startsWith("--")) {
      throw new Error("frontend bundle verifier options must use --name value pairs");
    }
    const key = name.slice(2);
    if (!["source-commit", "output"].includes(key) || key in values) {
      throw new Error(`unknown or duplicate frontend bundle verifier option: ${name}`);
    }
    values[key] = value;
  }
  if (!values["source-commit"] || !values.output) {
    throw new Error("frontend bundle verifier requires source-commit and output");
  }
  return values;
}

async function main(argv) {
  const options = parseArguments(argv);
  const evidence = await verifyReleaseFrontendBundle({
    sourceCommit: options["source-commit"],
    output: options.output
  });
  process.stdout.write(canonicalJson(evidence));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    try {
      if (lstatSync(evidencePath).isFile()) rmSync(evidencePath);
    } catch (cleanupError) {
      if (cleanupError?.code !== "ENOENT") {
        process.stderr.write("frontend bundle evidence cleanup failed\n");
      }
    }
    process.stderr.write(`frontend bundle release verification: ${error.message}\n`);
    process.exitCode = 1;
  });
}
