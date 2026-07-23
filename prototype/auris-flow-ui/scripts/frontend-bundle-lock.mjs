import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const FRONTEND_BUNDLE_LOCK_KIND = "auris-flow-frontend-bundle-lock";
export const FRONTEND_BUNDLE_CANDIDATE_KIND = "auris-flow-frontend-bundle-candidate";
export const FRONTEND_BUNDLE_APPROVAL_KIND = "auris-flow-frontend-bundle-approval";
export const FRONTEND_BUNDLE_SIGNATURE_ISSUER =
  "https://token.actions.githubusercontent.com";
export const frontendBundleLockPath = resolve(
  fileURLToPath(
    new URL(
      "../../../production/frontend/frontend-bundle.lock.json",
      import.meta.url
    )
  )
);

const gitObjectPattern = /^[0-9a-f]{40}$/;
const sha256Pattern = /^[0-9a-f]{64}$/;
const officialCandidateArtifactPattern =
  /^ghcr\.io\/g5n-dev\/auris_flow\/frontend-bundle-candidate@sha256:[0-9a-f]{64}$/;
const officialApprovalArtifactPattern =
  /^ghcr\.io\/g5n-dev\/auris_flow\/frontend-bundle-approval@sha256:[0-9a-f]{64}$/;
const officialCandidateIdentityPattern =
  /^https:\/\/github\.com\/g5n-dev\/auris_flow\/\.github\/workflows\/frontend-bundle-candidate\.yml@refs\/heads\/(?<branch>[A-Za-z0-9._\/-]+)$/;
const officialApprovalIdentityPattern =
  /^https:\/\/github\.com\/g5n-dev\/auris_flow\/\.github\/workflows\/frontend-bundle-promotion\.yml@refs\/heads\/(?<branch>[A-Za-z0-9._\/-]+)$/;

const exactKeys = (value, expected) => {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...expected].sort());
};

const normalizedValue = (value) => {
  if (Array.isArray(value)) return value.map(normalizedValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, normalizedValue(value[key])])
    );
  }
  return value;
};

const equalValues = (left, right) =>
  JSON.stringify(normalizedValue(left)) === JSON.stringify(normalizedValue(right));

const isNonPlaceholder = (value) =>
  typeof value === "string" &&
  value.trim().length >= 8 &&
  !/(?:^|[-_\s])(?:pending|todo|tbd|placeholder|example|replace-me)(?:$|[-_\s])/i.test(
    value.trim()
  );

export function requiresApprovedFrontendBundle(environment = process.env) {
  const values = [
    ["AURIS_RELEASE_CHECK", environment.AURIS_RELEASE_CHECK ?? "0"],
    [
      "AURIS_REQUIRE_APPROVED_BUNDLE",
      environment.AURIS_REQUIRE_APPROVED_BUNDLE ?? "0"
    ]
  ];
  for (const [name, value] of values) {
    if (!["0", "1"].includes(value)) {
      throw new Error(`${name} must be exactly 0 or 1`);
    }
  }
  return values.some(([, value]) => value === "1");
}

export function sha256File(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

export function sha256CanonicalJsonFile(path) {
  const payload = JSON.parse(readFileSync(path, "utf8"));
  const canonical = `${JSON.stringify(normalizedValue(payload), null, 2)}\n`;
  return createHash("sha256").update(canonical).digest("hex");
}

export function validateBundleTotals(totals, label = "bundle totals") {
  const failures = [];
  const keys = ["jsRawBytes", "jsBrotliBytes", "allRawBytes", "allBrotliBytes"];
  if (!exactKeys(totals, keys)) {
    failures.push(`${label} must contain exactly ${keys.join(", ")}`);
    return failures;
  }
  for (const key of keys) {
    if (!Number.isSafeInteger(totals[key]) || totals[key] < 0) {
      failures.push(`${label}.${key} must be a non-negative safe integer`);
    }
  }
  return failures;
}

function validWorkflowIdentity(value, pattern) {
  return (
    typeof value === "string" &&
    pattern.test(value) &&
    !value.includes("..") &&
    !value.replace(/^https:\/\//, "").includes("//") &&
    !value.endsWith("/")
  );
}

function validateCandidateProvenance(candidate) {
  const failures = [];
  const keys = [
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
  ];
  if (!exactKeys(candidate, keys)) {
    return [
      `immutable frontend bundle candidate must contain exactly ${keys.join(", ")}`
    ];
  }
  if (!officialCandidateArtifactPattern.test(candidate.artifact_ref)) {
    failures.push(
      "frontend bundle candidate artifact_ref must be an immutable official GHCR digest"
    );
  }
  if (!validWorkflowIdentity(candidate.signature_identity, officialCandidateIdentityPattern)) {
    failures.push("frontend bundle candidate signature identity is invalid");
  }
  if (candidate.signature_issuer !== FRONTEND_BUNDLE_SIGNATURE_ISSUER) {
    failures.push("frontend bundle candidate signature issuer is invalid");
  }
  for (const field of [
    "source_commit",
    "repository_tree",
    "frontend_tree",
    "build_workflow_sha"
  ]) {
    if (!gitObjectPattern.test(candidate[field])) {
      failures.push(`frontend bundle ${field} is invalid`);
    }
  }
  if (candidate.build_workflow_sha !== candidate.source_commit) {
    failures.push("frontend bundle build workflow is not bound to source commit");
  }
  for (const field of [
    "candidate_sha256",
    "bundle_report_sha256",
    "vite_manifest_sha256",
    "brotli_manifest_sha256",
    "dist_inventory_sha256",
    "package_lock_sha256"
  ]) {
    if (!sha256Pattern.test(candidate[field])) {
      failures.push(`frontend bundle ${field} is invalid`);
    }
  }
  failures.push(...validateBundleTotals(candidate.totals));
  return failures;
}

function validateApprovalProvenance(approval) {
  const failures = [];
  const keys = [
    "approval_reference",
    "artifact_ref",
    "environment",
    "promotion_workflow_sha",
    "rebuild_evidence_sha256",
    "run_attempt",
    "run_id",
    "signature_identity",
    "signature_issuer",
    "statement_sha256"
  ];
  if (!exactKeys(approval, keys)) {
    return [
      `immutable frontend bundle approval must contain exactly ${keys.join(", ")}`
    ];
  }
  if (!officialApprovalArtifactPattern.test(approval.artifact_ref)) {
    failures.push(
      "frontend bundle approval artifact_ref must be an immutable official GHCR digest"
    );
  }
  if (!isNonPlaceholder(approval.approval_reference)) {
    failures.push("frontend bundle approval reference is invalid");
  }
  if (approval.environment !== "frontend-bundle-production") {
    failures.push("frontend bundle approval environment is invalid");
  }
  if (!validWorkflowIdentity(approval.signature_identity, officialApprovalIdentityPattern)) {
    failures.push("frontend bundle approval signature identity is invalid");
  }
  if (approval.signature_issuer !== FRONTEND_BUNDLE_SIGNATURE_ISSUER) {
    failures.push("frontend bundle approval signature issuer is invalid");
  }
  if (!gitObjectPattern.test(approval.promotion_workflow_sha)) {
    failures.push("frontend bundle promotion_workflow_sha is invalid");
  }
  for (const field of ["statement_sha256", "rebuild_evidence_sha256"]) {
    if (!sha256Pattern.test(approval[field])) {
      failures.push(`frontend bundle approval ${field} is invalid`);
    }
  }
  if (typeof approval.run_id !== "string" || !/^[1-9][0-9]*$/.test(approval.run_id)) {
    failures.push("frontend bundle approval run_id is invalid");
  }
  if (!Number.isSafeInteger(approval.run_attempt) || approval.run_attempt < 1) {
    failures.push("frontend bundle approval run_attempt is invalid");
  }
  return failures;
}

export function validateFrontendBundleLock(lock, { requireApproved = false } = {}) {
  const failures = [];
  if (!lock || typeof lock !== "object" || Array.isArray(lock)) {
    return ["frontend bundle lock must be an object"];
  }
  if (lock.kind !== FRONTEND_BUNDLE_LOCK_KIND) {
    failures.push("frontend bundle lock kind is invalid");
  }
  if (typeof lock.reason !== "string" || !lock.reason.trim()) {
    failures.push("frontend bundle lock reason must be non-empty");
  }
  if (lock.schema_version === 1) {
    const keys = ["artifact", "kind", "reason", "schema_version", "status"];
    if (!exactKeys(lock, keys)) {
      failures.push(`schema-1 frontend bundle lock must contain exactly ${keys.join(", ")}`);
    }
    if (lock.status !== "PENDING") {
      failures.push("schema-1 frontend bundle lock must be PENDING");
    }
    if (lock.artifact !== null) {
      failures.push("PENDING frontend bundle lock must not contain an artifact");
    }
  } else if (lock.schema_version === 3) {
    const keys = ["approval", "candidate", "kind", "reason", "schema_version", "status"];
    if (!exactKeys(lock, keys)) {
      failures.push(`schema-3 frontend bundle lock must contain exactly ${keys.join(", ")}`);
    }
    if (lock.status !== "APPROVED") {
      failures.push("schema-3 frontend bundle lock must be APPROVED");
    }
    failures.push(...validateCandidateProvenance(lock.candidate));
    failures.push(...validateApprovalProvenance(lock.approval));
    if (
      lock.candidate?.source_commit !== lock.approval?.promotion_workflow_sha
    ) {
      failures.push(
        "frontend bundle promotion workflow is not bound to candidate source commit"
      );
    }
    const candidateIdentity = officialCandidateIdentityPattern.exec(
      lock.candidate?.signature_identity ?? ""
    );
    const approvalIdentity = officialApprovalIdentityPattern.exec(
      lock.approval?.signature_identity ?? ""
    );
    if (
      candidateIdentity?.groups?.branch &&
      approvalIdentity?.groups?.branch &&
      candidateIdentity.groups.branch !== approvalIdentity.groups.branch
    ) {
      failures.push("frontend bundle candidate and approval branches differ");
    }
  } else {
    failures.push("frontend bundle lock schema_version must be 1 or 3");
  }
  if (requireApproved && lock.status !== "APPROVED") {
    failures.push("frontend bundle lock is PENDING; no approved artifact exists");
  }
  return failures;
}

export function loadFrontendBundleLock(path = frontendBundleLockPath) {
  const lock = JSON.parse(readFileSync(path, "utf8"));
  const failures = validateFrontendBundleLock(lock);
  if (failures.length) {
    throw new Error(`invalid frontend bundle lock: ${failures.join("; ")}`);
  }
  return lock;
}

export function validateApprovedBundleAgainstBuild(lock, build) {
  const lockFailures = validateFrontendBundleLock(lock);
  if (lockFailures.length) {
    return [{ code: "APPROVED_BUNDLE_LOCK_INVALID", detail: { failures: lockFailures } }];
  }
  if (lock.status !== "APPROVED") {
    return [{
      code: "APPROVED_BUNDLE_LOCK_PENDING",
      detail: { reason: lock.reason }
    }];
  }
  const failures = [];
  if (!equalValues(build.totals, lock.candidate.totals)) {
    failures.push({
      code: "APPROVED_BUNDLE_BASELINE_DRIFT",
      detail: { actual: build.totals, expected: lock.candidate.totals }
    });
  }
  const sourceDrift = {};
  if (build.frontendTree !== lock.candidate.frontend_tree) {
    sourceDrift.frontend_tree = {
      actual: build.frontendTree,
      expected: lock.candidate.frontend_tree
    };
  }
  if (Object.keys(sourceDrift).length) {
    failures.push({ code: "APPROVED_BUNDLE_SOURCE_DRIFT", detail: sourceDrift });
  }
  const artifactDrift = {};
  for (const [buildKey, lockKey] of [
    ["packageLockSha256", "package_lock_sha256"],
    ["viteManifestSha256", "vite_manifest_sha256"],
    ["brotliManifestSha256", "brotli_manifest_sha256"],
    ["distInventorySha256", "dist_inventory_sha256"]
  ]) {
    if (build[buildKey] !== lock.candidate[lockKey]) {
      artifactDrift[lockKey] = {
        actual: build[buildKey],
        expected: lock.candidate[lockKey]
      };
    }
  }
  if (Object.keys(artifactDrift).length) {
    failures.push({ code: "APPROVED_BUNDLE_ARTIFACT_DRIFT", detail: artifactDrift });
  }
  return failures;
}

export function buildDistInventory(distDir) {
  const root = resolve(distDir);
  const rootStat = lstatSync(root);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error("frontend dist root must be a real directory");
  }
  const files = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      const stat = lstatSync(path);
      if (stat.isSymbolicLink()) {
        throw new Error(`frontend dist symlink is forbidden: ${relative(root, path)}`);
      }
      if (stat.isDirectory()) {
        visit(path);
      } else if (stat.isFile()) {
        if (stat.nlink !== 1) {
          throw new Error(
            `frontend dist hardlink is forbidden: ${relative(root, path)}`
          );
        }
        files.push({
          path: relative(root, path).replaceAll("\\", "/"),
          bytes: stat.size,
          sha256: sha256File(path)
        });
      } else {
        throw new Error(`frontend dist entry must be regular: ${relative(root, path)}`);
      }
    }
  };
  visit(root);
  return files.sort((left, right) =>
    left.path < right.path ? -1 : left.path > right.path ? 1 : 0
  );
}

export function distInventorySha256(inventory) {
  const payload = `${JSON.stringify(normalizedValue(inventory), null, 2)}\n`;
  return createHash("sha256").update(payload).digest("hex");
}

export const frontendBundleLockPatterns = Object.freeze({
  gitObjectPattern,
  sha256Pattern,
  officialArtifactPattern: officialCandidateArtifactPattern,
  officialIdentityPattern: officialCandidateIdentityPattern,
  officialApprovalArtifactPattern,
  officialApprovalIdentityPattern,
  isNonPlaceholder
});
