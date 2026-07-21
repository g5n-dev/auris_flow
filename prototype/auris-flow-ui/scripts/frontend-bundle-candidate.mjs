import { createHash, randomUUID } from "node:crypto";
import {
  lstatSync,
  mkdirSync,
  openSync,
  closeSync,
  fsyncSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  writeFileSync
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";

import {
  FRONTEND_BUNDLE_CANDIDATE_KIND,
  FRONTEND_BUNDLE_LOCK_KIND,
  FRONTEND_BUNDLE_SIGNATURE_ISSUER,
  frontendBundleLockPatterns,
  validateBundleTotals,
  validateFrontendBundleLock
} from "./frontend-bundle-lock.mjs";

const {
  gitObjectPattern,
  sha256Pattern,
  officialArtifactPattern,
  officialIdentityPattern,
  officialApprovalArtifactPattern,
  officialApprovalIdentityPattern,
  isNonPlaceholder
} = frontendBundleLockPatterns;

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalValue(value[key])])
    );
  }
  return value;
}

export function canonicalJson(value) {
  return `${JSON.stringify(canonicalValue(value), null, 2)}\n`;
}

export function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function reportTotals(report) {
  return {
    jsRawBytes: report?.totals?.js?.rawBytes,
    jsBrotliBytes: report?.totals?.js?.brotliBytes,
    allRawBytes: report?.totals?.all?.rawBytes,
    allBrotliBytes: report?.totals?.all?.brotliBytes
  };
}

export function validateCandidateBundleReport(report) {
  const failures = [];
  if (!report || typeof report !== "object" || Array.isArray(report)) {
    return ["bundle report must be an object"];
  }
  if (report.status !== "failed") {
    failures.push("bundle candidate requires a failed snapshot comparison report");
  }
  if (!Array.isArray(report.failures)) {
    failures.push("bundle report failures must be an array");
    return failures;
  }
  const codes = report.failures.map((failure) => failure?.code);
  const allowedCodes = [
    "APPROVED_BUNDLE_ARTIFACT_DRIFT",
    "APPROVED_BUNDLE_BASELINE_DRIFT",
    "APPROVED_BUNDLE_LOCK_PENDING",
    "APPROVED_BUNDLE_SOURCE_DRIFT"
  ];
  if (!codes.length || codes.some((code) => !allowedCodes.includes(code))) {
    failures.push(
      "bundle candidate permits only approved-baseline state failures"
    );
  }
  failures.push(...validateBundleTotals(reportTotals(report), "bundle report totals"));
  return failures;
}

export function requireGitObject(value, label) {
  if (!gitObjectPattern.test(value)) throw new Error(`${label} must be an exact Git object`);
}

function requireSha256(value, label) {
  if (!sha256Pattern.test(value)) throw new Error(`${label} must be a SHA-256 digest`);
}

export function createCandidateDocument({
  source,
  report,
  bundleLock,
  comparisonSnapshot,
  buildContract,
  artifacts
}) {
  requireGitObject(source?.commit, "source commit");
  requireGitObject(source?.repositoryTree, "repository tree");
  requireGitObject(source?.frontendTree, "frontend tree");
  const lockFailures = validateFrontendBundleLock(bundleLock);
  if (lockFailures.length) {
    throw new Error(`bundle lock is invalid: ${lockFailures.join("; ")}`);
  }
  const reportFailures = validateCandidateBundleReport(report);
  if (reportFailures.length) {
    throw new Error(`bundle report is not candidate-eligible: ${reportFailures.join("; ")}`);
  }
  for (const field of [
    "package_lock_sha256",
    "vite_config_sha256",
    "checker_sha256",
    "route_scenarios_sha256",
    "precompression_sha256",
    "policy_sha256"
  ]) {
    requireSha256(buildContract?.[field], `build_contract.${field}`);
  }
  for (const field of [
    "bundle_report_sha256",
    "vite_manifest_sha256",
    "brotli_manifest_sha256",
    "dist_inventory_sha256"
  ]) {
    requireSha256(artifacts?.[field], `artifacts.${field}`);
  }
  for (const field of ["node_version", "npm_version", "vite_version"]) {
    if (typeof buildContract?.[field] !== "string" || !buildContract[field].trim()) {
      throw new Error(`build_contract.${field} must be non-empty`);
    }
  }
  const totals = reportTotals(report);
  let referenceStatus;
  let referenceSourceCommit;
  let expected;
  if (bundleLock.status === "APPROVED") {
    referenceStatus = "APPROVED_LOCK";
    referenceSourceCommit = bundleLock.candidate.source_commit;
    expected = bundleLock.candidate.totals;
  } else {
    if (
      comparisonSnapshot?.status !== "LEGACY_REFERENCE" ||
      !gitObjectPattern.test(comparisonSnapshot?.sourceCommit ?? "") ||
      !gitObjectPattern.test(comparisonSnapshot?.comparisonBaselineCommit ?? "")
    ) {
      throw new Error("comparison snapshot must be a commit-bound LEGACY_REFERENCE");
    }
    referenceStatus = comparisonSnapshot.status;
    referenceSourceCommit = comparisonSnapshot.sourceCommit;
    expected = comparisonSnapshot.totals;
  }
  const comparisonFailures = validateBundleTotals(
    expected,
    "comparison snapshot totals"
  );
  if (comparisonFailures.length) throw new Error(comparisonFailures.join("; "));
  const drift = report.failures.find((failure) => failure?.detail?.actual)?.detail;
  if (drift && (
    canonicalJson(drift.actual) !== canonicalJson(totals) ||
    canonicalJson(drift.expected) !== canonicalJson(expected)
  )) throw new Error("bundle drift detail is not bound to report and comparison totals");
  return {
    schema_version: "auris.frontend-bundle-candidate.v1",
    kind: FRONTEND_BUNDLE_CANDIDATE_KIND,
    status: "PENDING_REVIEW",
    source: {
      commit: source.commit,
      repository_tree: source.repositoryTree,
      frontend_tree: source.frontendTree
    },
    comparison: {
      bundle_lock_schema_version: bundleLock.schema_version,
      bundle_lock_status: bundleLock.status,
      reference_status: referenceStatus,
      reference_source_commit: referenceSourceCommit,
      snapshot_totals: expected
    },
    build_contract: buildContract,
    artifacts,
    totals,
    delta: {
      js_raw_bytes: totals.jsRawBytes - expected.jsRawBytes,
      js_brotli_bytes: totals.jsBrotliBytes - expected.jsBrotliBytes,
      all_raw_bytes: totals.allRawBytes - expected.allRawBytes,
      all_brotli_bytes: totals.allBrotliBytes - expected.allBrotliBytes
    },
    limits: report.limits,
    gate: {
      hard_budget: "PASS",
      allowed_failure_codes: [
        "APPROVED_BUNDLE_ARTIFACT_DRIFT",
        "APPROVED_BUNDLE_BASELINE_DRIFT",
        "APPROVED_BUNDLE_LOCK_PENDING",
        "APPROVED_BUNDLE_SOURCE_DRIFT"
      ],
      observed_failure_codes: report.failures.map((failure) => failure.code).sort(),
      other_failures: []
    }
  };
}

export function createApprovedBundleLock(candidate, {
  candidateArtifactRef,
  approvalArtifactRef,
  candidateSha256,
  approvalStatementSha256,
  rebuildEvidenceSha256,
  approvalReference,
  candidateSignatureIdentity,
  approvalSignatureIdentity,
  signatureIssuer,
  buildWorkflowSha,
  promotionWorkflowSha,
  approvalEnvironment,
  runId,
  runAttempt
}) {
  if (
    candidate?.schema_version !== "auris.frontend-bundle-candidate.v1" ||
    candidate?.kind !== FRONTEND_BUNDLE_CANDIDATE_KIND ||
    candidate?.status !== "PENDING_REVIEW"
  ) {
    throw new Error("promotion requires a PENDING_REVIEW frontend bundle candidate");
  }
  if (!officialArtifactPattern.test(candidateArtifactRef ?? "")) {
    throw new Error("artifact reference must be an immutable official GHCR digest");
  }
  if (!officialApprovalArtifactPattern.test(approvalArtifactRef ?? "")) {
    throw new Error(
      "approval artifact reference must be an immutable official GHCR digest"
    );
  }
  requireSha256(candidateSha256, "candidate SHA-256");
  requireSha256(approvalStatementSha256, "approval statement SHA-256");
  requireSha256(rebuildEvidenceSha256, "rebuild evidence SHA-256");
  if (!isNonPlaceholder(approvalReference)) {
    throw new Error("approval reference must be non-placeholder change-control evidence");
  }
  if (!officialIdentityPattern.test(candidateSignatureIdentity ?? "")) {
    throw new Error(
      "candidate signature identity must be the official candidate workflow"
    );
  }
  if (!officialApprovalIdentityPattern.test(approvalSignatureIdentity ?? "")) {
    throw new Error(
      "approval signature identity must be the official promotion workflow"
    );
  }
  if (signatureIssuer !== FRONTEND_BUNDLE_SIGNATURE_ISSUER) {
    throw new Error("signature issuer must be GitHub Actions OIDC");
  }
  if (approvalEnvironment !== "frontend-bundle-production") {
    throw new Error("approval environment must be frontend-bundle-production");
  }
  if (typeof runId !== "string" || !/^[1-9][0-9]*$/.test(runId)) {
    throw new Error("approval run ID must be a positive decimal string");
  }
  if (!Number.isSafeInteger(runAttempt) || runAttempt < 1) {
    throw new Error("approval run attempt must be a positive integer");
  }
  for (const [value, label] of [
    [buildWorkflowSha, "build workflow SHA"],
    [promotionWorkflowSha, "promotion workflow SHA"]
  ]) {
    requireGitObject(value, label);
    if (value !== candidate.source.commit) {
      throw new Error(`${label} must equal candidate source commit`);
    }
  }
  const lock = {
    schema_version: 3,
    kind: FRONTEND_BUNDLE_LOCK_KIND,
    status: "APPROVED",
    reason: "Dual-signed protected promotion.",
    candidate: {
      artifact_ref: candidateArtifactRef,
      signature_identity: candidateSignatureIdentity,
      signature_issuer: signatureIssuer,
      source_commit: candidate.source.commit,
      repository_tree: candidate.source.repository_tree,
      frontend_tree: candidate.source.frontend_tree,
      build_workflow_sha: buildWorkflowSha,
      candidate_sha256: candidateSha256,
      bundle_report_sha256: candidate.artifacts.bundle_report_sha256,
      vite_manifest_sha256: candidate.artifacts.vite_manifest_sha256,
      brotli_manifest_sha256: candidate.artifacts.brotli_manifest_sha256,
      dist_inventory_sha256: candidate.artifacts.dist_inventory_sha256,
      package_lock_sha256: candidate.build_contract.package_lock_sha256,
      totals: candidate.totals
    },
    approval: {
      artifact_ref: approvalArtifactRef,
      statement_sha256: approvalStatementSha256,
      rebuild_evidence_sha256: rebuildEvidenceSha256,
      approval_reference: approvalReference.trim(),
      environment: approvalEnvironment,
      promotion_workflow_sha: promotionWorkflowSha,
      signature_identity: approvalSignatureIdentity,
      signature_issuer: signatureIssuer,
      run_id: runId,
      run_attempt: runAttempt
    }
  };
  const failures = validateFrontendBundleLock(lock);
  if (failures.length) throw new Error(`rendered lock is invalid: ${failures.join("; ")}`);
  return lock;
}

const candidatePayloadFiles = Object.freeze([
  "brotli-manifest.json",
  "bundle-report.json",
  "candidate.json",
  "dist-inventory.json",
  "vite-manifest.json"
]);
const candidateDirectoryFiles = Object.freeze([
  ...candidatePayloadFiles,
  "SHA256SUMS"
].sort());

export function requireRealDirectory(path, label) {
  const stat = lstatSync(path);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new Error(`${label} must be a real directory`);
  }
}

function requireRegularUnlinkedFile(path, label) {
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) {
    throw new Error(`${label} must be a regular non-linked file`);
  }
}

export function atomicWrite(path, content) {
  const temporary = join(
    dirname(path),
    `.${basename(path)}.${process.pid}.${randomUUID()}.tmp`
  );
  let descriptor;
  try {
    descriptor = openSync(temporary, "wx", 0o644);
    writeFileSync(descriptor, content, "utf8");
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = undefined;
    renameSync(temporary, path);
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
    try {
      rmSync(temporary);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

function candidatePayloads({
  candidate,
  report,
  viteManifest,
  brotliManifest,
  distInventory
}) {
  return {
    "brotli-manifest.json": canonicalJson(brotliManifest),
    "bundle-report.json": canonicalJson(report),
    "candidate.json": canonicalJson(candidate),
    "dist-inventory.json": canonicalJson(distInventory),
    "vite-manifest.json": canonicalJson(viteManifest)
  };
}

function validateInventory(inventory) {
  if (!Array.isArray(inventory)) return ["dist inventory must be an array"];
  const failures = [];
  const paths = [];
  for (const entry of inventory) {
    if (
      !entry ||
      typeof entry !== "object" ||
      Array.isArray(entry) ||
      JSON.stringify(Object.keys(entry).sort()) !==
        JSON.stringify(["bytes", "path", "sha256"])
    ) {
      failures.push("dist inventory entries must contain exactly bytes, path, sha256");
      continue;
    }
    if (
      typeof entry.path !== "string" ||
      !entry.path ||
      entry.path.startsWith("/") ||
      entry.path.includes("\\") ||
      entry.path.split("/").some((part) => !part || part === "." || part === "..")
    ) {
      failures.push(`dist inventory path is unsafe: ${entry.path}`);
    }
    if (!Number.isSafeInteger(entry.bytes) || entry.bytes < 0) {
      failures.push(`dist inventory bytes are invalid: ${entry.path}`);
    }
    if (!sha256Pattern.test(entry.sha256)) {
      failures.push(`dist inventory digest is invalid: ${entry.path}`);
    }
    paths.push(entry.path);
  }
  const sorted = [...paths].sort();
  if (JSON.stringify(paths) !== JSON.stringify(sorted)) {
    failures.push("dist inventory paths must be sorted");
  }
  if (new Set(paths).size !== paths.length) {
    failures.push("dist inventory paths must be unique");
  }
  return failures;
}

function validateCandidateDocument(candidate, report, distInventory) {
  const failures = [];
  if (
    candidate?.schema_version !== "auris.frontend-bundle-candidate.v1" ||
    candidate?.kind !== FRONTEND_BUNDLE_CANDIDATE_KIND ||
    candidate?.status !== "PENDING_REVIEW"
  ) {
    failures.push("candidate identity or PENDING_REVIEW status is invalid");
  }
  for (const [value, label] of [
    [candidate?.source?.commit, "candidate source commit"],
    [candidate?.source?.repository_tree, "candidate repository tree"],
    [candidate?.source?.frontend_tree, "candidate frontend tree"]
  ]) {
    if (!gitObjectPattern.test(value ?? "")) failures.push(`${label} is invalid`);
  }
  failures.push(...validateCandidateBundleReport(report));
  failures.push(...validateInventory(distInventory));
  if (canonicalJson(reportTotals(report)) !== canonicalJson(candidate?.totals)) {
    failures.push("candidate totals do not match bundle report");
  }
  if ("approval_reference" in (candidate ?? {}) || "generated_at" in (candidate ?? {})) {
    failures.push("pending candidate contains forbidden approval or time metadata");
  }
  return failures;
}

export async function writeCandidateDirectory(outputDir, documents) {
  const output = resolve(outputDir);
  requireRealDirectory(dirname(output), "candidate output parent");
  let created = false;
  try {
    mkdirSync(output, { mode: 0o755 });
    created = true;
    const payloads = candidatePayloads(documents);
    const expectedArtifactHashes = {
      bundle_report_sha256: sha256(payloads["bundle-report.json"]),
      vite_manifest_sha256: sha256(payloads["vite-manifest.json"]),
      brotli_manifest_sha256: sha256(payloads["brotli-manifest.json"]),
      dist_inventory_sha256: sha256(payloads["dist-inventory.json"])
    };
    if (
      canonicalJson(documents.candidate?.artifacts) !==
      canonicalJson(expectedArtifactHashes)
    ) {
      throw new Error("candidate artifact digests do not match rendered payloads");
    }
    for (const name of candidatePayloadFiles) atomicWrite(join(output, name), payloads[name]);
    const sums = candidatePayloadFiles
      .map((name) => `${sha256(payloads[name])}  ${name}`)
      .join("\n");
    atomicWrite(join(output, "SHA256SUMS"), `${sums}\n`);
    return await verifyCandidateDirectory(output);
  } catch (error) {
    if (created) rmSync(output, { recursive: true, force: true });
    throw error;
  }
}

export async function verifyCandidateDirectory(candidateDir) {
  const root = resolve(candidateDir);
  requireRealDirectory(root, "candidate directory");
  const entries = readdirSync(root).sort();
  if (JSON.stringify(entries) !== JSON.stringify(candidateDirectoryFiles)) {
    throw new Error(`unexpected candidate directory entries: ${entries.join(", ")}`);
  }
  for (const name of entries) {
    requireRegularUnlinkedFile(join(root, name), `candidate ${name}`);
  }
  const sumLines = readFileSync(join(root, "SHA256SUMS"), "utf8")
    .trimEnd()
    .split("\n");
  const expectedLines = candidatePayloadFiles.map((name) => {
    const digest = sha256(readFileSync(join(root, name)));
    return `${digest}  ${name}`;
  });
  if (JSON.stringify(sumLines) !== JSON.stringify(expectedLines)) {
    throw new Error("candidate SHA256SUMS checksum mismatch");
  }
  const readJson = (name) => JSON.parse(readFileSync(join(root, name), "utf8"));
  const candidate = readJson("candidate.json");
  const report = readJson("bundle-report.json");
  const viteManifest = readJson("vite-manifest.json");
  const brotliManifest = readJson("brotli-manifest.json");
  const distInventory = readJson("dist-inventory.json");
  for (const [name, value] of [
    ["candidate.json", candidate],
    ["bundle-report.json", report],
    ["vite-manifest.json", viteManifest],
    ["brotli-manifest.json", brotliManifest],
    ["dist-inventory.json", distInventory]
  ]) {
    if (readFileSync(join(root, name), "utf8") !== canonicalJson(value)) {
      throw new Error(`candidate ${name} is not canonical JSON`);
    }
  }
  const validationFailures = validateCandidateDocument(candidate, report, distInventory);
  if (validationFailures.length) {
    throw new Error(`candidate document is invalid: ${validationFailures.join("; ")}`);
  }
  const actualArtifacts = {
    bundle_report_sha256: sha256(canonicalJson(report)),
    vite_manifest_sha256: sha256(canonicalJson(viteManifest)),
    brotli_manifest_sha256: sha256(canonicalJson(brotliManifest)),
    dist_inventory_sha256: sha256(canonicalJson(distInventory))
  };
  if (canonicalJson(candidate.artifacts) !== canonicalJson(actualArtifacts)) {
    throw new Error("candidate artifact checksum mismatch");
  }
  return {
    candidate,
    candidate_sha256: sha256(canonicalJson(candidate)),
    file_count: candidatePayloadFiles.length
  };
}
