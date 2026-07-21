import assert from "node:assert/strict";
import { link, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  buildDistInventory,
  requiresApprovedFrontendBundle,
  sha256CanonicalJsonFile,
  validateApprovedBundleAgainstBuild,
  validateFrontendBundleLock
} from "./frontend-bundle-lock.mjs";

const sha256 = (character) => character.repeat(64);
const gitObject = (character) => character.repeat(40);
const totals = {
  jsRawBytes: 1_124_550,
  jsBrotliBytes: 298_925,
  allRawBytes: 2_233_037,
  allBrotliBytes: 461_776
};

function approvedV3Lock() {
  return {
    schema_version: 3,
    kind: "auris-flow-frontend-bundle-lock",
    status: "APPROVED",
    reason: "Dual-signed protected promotion.",
    candidate: {
      artifact_ref:
        "ghcr.io/auris-flow/auris-flow/frontend-bundle-candidate@sha256:" + sha256("1"),
      signature_identity:
        "https://github.com/auris-flow/auris-flow/.github/workflows/" +
        "frontend-bundle-candidate.yml@refs/heads/main",
      signature_issuer: "https://token.actions.githubusercontent.com",
      source_commit: gitObject("2"),
      repository_tree: gitObject("3"),
      frontend_tree: gitObject("4"),
      build_workflow_sha: gitObject("2"),
      candidate_sha256: sha256("5"),
      bundle_report_sha256: sha256("6"),
      vite_manifest_sha256: sha256("7"),
      brotli_manifest_sha256: sha256("8"),
      dist_inventory_sha256: sha256("a"),
      package_lock_sha256: sha256("9"),
      totals
    },
    approval: {
      artifact_ref:
        "ghcr.io/auris-flow/auris-flow/frontend-bundle-approval@sha256:" +
        sha256("b"),
      statement_sha256: sha256("c"),
      rebuild_evidence_sha256: sha256("d"),
      approval_reference: "CAB-2026-0718",
      environment: "frontend-bundle-production",
      promotion_workflow_sha: gitObject("2"),
      signature_identity:
        "https://github.com/auris-flow/auris-flow/.github/workflows/" +
        "frontend-bundle-promotion.yml@refs/heads/main",
      signature_issuer: "https://token.actions.githubusercontent.com",
      run_id: "123456789",
      run_attempt: 1
    }
  };
}

test("v3 lock validates dual-signed immutable official provenance", () => {
  assert.deepEqual(validateFrontendBundleLock(approvedV3Lock()), []);
});

test("v3 lock detects same-size content substitution and frontend tree drift", () => {
  const lock = approvedV3Lock();
  const build = {
    totals,
    frontendTree: lock.candidate.frontend_tree,
    packageLockSha256: lock.candidate.package_lock_sha256,
    viteManifestSha256: lock.candidate.vite_manifest_sha256,
    brotliManifestSha256: lock.candidate.brotli_manifest_sha256,
    distInventorySha256: lock.candidate.dist_inventory_sha256
  };
  assert.deepEqual(validateApprovedBundleAgainstBuild(lock, build), []);
  assert.ok(
    validateApprovedBundleAgainstBuild(lock, {
      ...build,
      brotliManifestSha256: sha256("a")
    }).some((failure) => failure.code === "APPROVED_BUNDLE_ARTIFACT_DRIFT")
  );
  assert.ok(
    validateApprovedBundleAgainstBuild(lock, {
      ...build,
      distInventorySha256: sha256("b")
    }).some((failure) => failure.code === "APPROVED_BUNDLE_ARTIFACT_DRIFT")
  );
  assert.ok(
    validateApprovedBundleAgainstBuild(lock, {
      ...build,
      frontendTree: gitObject("b")
    }).some((failure) => failure.code === "APPROVED_BUNDLE_SOURCE_DRIFT")
  );
});

test("pending lock is structurally valid but approval checks fail closed", () => {
  const pending = {
    schema_version: 1,
    kind: "auris-flow-frontend-bundle-lock",
    status: "PENDING",
    reason: "No cryptographically verifiable approved bundle exists.",
    artifact: null
  };
  assert.deepEqual(validateFrontendBundleLock(pending), []);
  assert.ok(validateFrontendBundleLock(pending, { requireApproved: true }).length > 0);
  assert.ok(
    validateApprovedBundleAgainstBuild(pending, { totals }).some(
      (failure) => failure.code === "APPROVED_BUNDLE_LOCK_PENDING"
    )
  );
});

test("approved bundle is required only by explicit or release gates", () => {
  assert.equal(requiresApprovedFrontendBundle({}), false);
  assert.equal(
    requiresApprovedFrontendBundle({ AURIS_RELEASE_CHECK: "0" }),
    false
  );
  assert.equal(
    requiresApprovedFrontendBundle({ AURIS_RELEASE_CHECK: "1" }),
    true
  );
  assert.equal(
    requiresApprovedFrontendBundle({ AURIS_REQUIRE_APPROVED_BUNDLE: "1" }),
    true
  );
  assert.throws(
    () => requiresApprovedFrontendBundle({ AURIS_RELEASE_CHECK: "yes" }),
    /must be exactly 0 or 1/
  );
});

test("mutable and cross-repository approved locks fail closed", () => {

  const mutable = approvedV3Lock();
  mutable.candidate.artifact_ref =
    "ghcr.io/auris-flow/auris-flow/frontend-bundle-candidate:latest";
  assert.ok(validateFrontendBundleLock(mutable).length > 0);

  const attacker = approvedV3Lock();
  attacker.candidate.signature_identity =
    "https://github.com/attacker/repo/.github/workflows/" +
    "frontend-bundle-candidate.yml@refs/heads/main";
  assert.ok(validateFrontendBundleLock(attacker).length > 0);

  const unsignedApproval = approvedV3Lock();
  unsignedApproval.approval.artifact_ref =
    "ghcr.io/auris-flow/auris-flow/frontend-bundle-approval:latest";
  assert.ok(validateFrontendBundleLock(unsignedApproval).length > 0);
});

test("dist inventory rejects hard-linked build outputs", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "auris-bundle-hardlink-test-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const original = join(root, "index.js");
  await writeFile(original, "export const value = 1;\n", "utf8");
  await link(original, join(root, "index-copy.js"));

  assert.throws(() => buildDistInventory(root), /hardlink/i);
});

test("manifest hashes use canonical JSON while dist inventory preserves bytes", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "auris-bundle-canonical-json-test-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const first = join(root, "first.json");
  const second = join(root, "second.json");
  await writeFile(first, '{"b":2,"a":{"y":2,"x":1}}\n', "utf8");
  await writeFile(
    second,
    '{\n  "a": { "x": 1, "y": 2 },\n  "b": 2\n}\n',
    "utf8"
  );

  assert.equal(
    sha256CanonicalJsonFile(first),
    sha256CanonicalJsonFile(second)
  );
  const inventory = buildDistInventory(root);
  assert.notEqual(inventory[0].sha256, inventory[1].sha256);
});
