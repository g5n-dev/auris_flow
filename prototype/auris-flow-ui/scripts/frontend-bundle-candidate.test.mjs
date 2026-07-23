import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  canonicalJson,
  createApprovedBundleLock,
  createCandidateDocument,
  sha256 as hashValue,
  validateCandidateBundleReport,
  verifyCandidateDirectory,
  writeCandidateDirectory
} from "./frontend-bundle-candidate.mjs";

const digest = (character) => character.repeat(64);
const gitObject = (character) => character.repeat(40);

function driftReport(overrides = {}) {
  return {
    status: "failed",
    limits: {
      totalJsRawBytes: 1_134_592,
      totalJsBrotliBytes: 301_568,
      totalAssetsRawBytes: 2_254_080,
      totalAssetsBrotliBytes: 466_944
    },
    totals: {
      js: { rawBytes: 1_124_550, brotliBytes: 298_925 },
      all: { rawBytes: 2_233_037, brotliBytes: 461_776 }
    },
    failures: [{
      code: "APPROVED_BUNDLE_LOCK_PENDING",
      detail: {
        actual: {
          jsRawBytes: 1_124_550,
          jsBrotliBytes: 298_925,
          allRawBytes: 2_233_037,
          allBrotliBytes: 461_776
        },
        expected: {
          jsRawBytes: 1_123_292,
          jsBrotliBytes: 298_628,
          allRawBytes: 2_231_779,
          allBrotliBytes: 461_472
        }
      }
    }],
    ...overrides
  };
}

function candidateInput(overrides = {}) {
  return {
    source: {
      commit: gitObject("1"),
      repositoryTree: gitObject("2"),
      frontendTree: gitObject("3")
    },
    report: driftReport(),
    bundleLock: {
      schema_version: 1,
      kind: "auris-flow-frontend-bundle-lock",
      status: "PENDING",
      reason: "No cryptographically verifiable approved bundle exists.",
      artifact: null
    },
    comparisonSnapshot: {
      status: "LEGACY_REFERENCE",
      sourceCommit: gitObject("4"),
      comparisonBaselineCommit: gitObject("5"),
      scope: "legacy-audited-candidate",
      totals: {
        jsRawBytes: 1_123_292,
        jsBrotliBytes: 298_628,
        allRawBytes: 2_231_779,
        allBrotliBytes: 461_472
      }
    },
    buildContract: {
      node_version: "v22.17.0",
      npm_version: "10.9.2",
      vite_version: "8.1.3",
      package_lock_sha256: digest("a"),
      vite_config_sha256: digest("b"),
      checker_sha256: digest("c"),
      route_scenarios_sha256: digest("d"),
      precompression_sha256: digest("e"),
      policy_sha256: digest("f")
    },
    artifacts: {
      bundle_report_sha256: digest("6"),
      vite_manifest_sha256: digest("7"),
      brotli_manifest_sha256: digest("8"),
      dist_inventory_sha256: digest("9")
    },
    ...overrides
  };
}

function approvalOptions(candidate, overrides = {}) {
  return {
    candidateArtifactRef:
      "ghcr.io/g5n-dev/auris_flow/frontend-bundle-candidate@sha256:" +
      digest("a"),
    approvalArtifactRef:
      "ghcr.io/g5n-dev/auris_flow/frontend-bundle-approval@sha256:" +
      digest("b"),
    candidateSha256: digest("5"),
    approvalStatementSha256: digest("c"),
    rebuildEvidenceSha256: digest("d"),
    approvalReference: "CAB-2026-0718",
    candidateSignatureIdentity:
      "https://github.com/g5n-dev/auris_flow/.github/workflows/" +
      "frontend-bundle-candidate.yml@refs/heads/main",
    approvalSignatureIdentity:
      "https://github.com/g5n-dev/auris_flow/.github/workflows/" +
      "frontend-bundle-promotion.yml@refs/heads/main",
    signatureIssuer: "https://token.actions.githubusercontent.com",
    buildWorkflowSha: candidate.source.commit,
    promotionWorkflowSha: candidate.source.commit,
    approvalEnvironment: "frontend-bundle-production",
    runId: "123456789",
    runAttempt: 1,
    ...overrides
  };
}

test("candidate is deterministic, commit-bound and permanently pending", () => {
  const candidate = createCandidateDocument(candidateInput());

  assert.equal(candidate.schema_version, "auris.frontend-bundle-candidate.v1");
  assert.equal(candidate.kind, "auris-flow-frontend-bundle-candidate");
  assert.equal(candidate.status, "PENDING_REVIEW");
  assert.deepEqual(candidate.source, {
    commit: gitObject("1"),
    repository_tree: gitObject("2"),
    frontend_tree: gitObject("3")
  });
  assert.deepEqual(candidate.delta, {
    js_raw_bytes: 1_258,
    js_brotli_bytes: 297,
    all_raw_bytes: 1_258,
    all_brotli_bytes: 304
  });
  assert.deepEqual(candidate.gate, {
    hard_budget: "PASS",
    allowed_failure_codes: [
      "APPROVED_BUNDLE_ARTIFACT_DRIFT",
      "APPROVED_BUNDLE_BASELINE_DRIFT",
      "APPROVED_BUNDLE_LOCK_PENDING",
      "APPROVED_BUNDLE_SOURCE_DRIFT"
    ],
    observed_failure_codes: ["APPROVED_BUNDLE_LOCK_PENDING"],
    other_failures: []
  });
  assert.equal("approval_reference" in candidate, false);
  assert.equal("generated_at" in candidate, false);

  const reordered = JSON.parse(JSON.stringify(candidate));
  reordered.source = {
    frontend_tree: candidate.source.frontend_tree,
    commit: candidate.source.commit,
    repository_tree: candidate.source.repository_tree
  };
  assert.equal(canonicalJson(candidate), canonicalJson(reordered));
});

test("candidate compares against the approved lock after first promotion", () => {
  const approvedTotals = {
    jsRawBytes: 1_120_000,
    jsBrotliBytes: 297_000,
    allRawBytes: 2_220_000,
    allBrotliBytes: 459_000
  };
  const previousCandidate = createCandidateDocument(candidateInput());
  const bundleLock = createApprovedBundleLock(
    previousCandidate,
    approvalOptions(previousCandidate)
  );
  bundleLock.candidate.totals = approvedTotals;
  const actual = driftReport().failures[0].detail.actual;
  const report = driftReport({
    failures: [{
      code: "APPROVED_BUNDLE_BASELINE_DRIFT",
      detail: { actual, expected: approvedTotals }
    }]
  });

  const candidate = createCandidateDocument(
    candidateInput({ bundleLock, report })
  );

  assert.equal(candidate.comparison.reference_status, "APPROVED_LOCK");
  assert.equal(
    candidate.comparison.reference_source_commit,
    bundleLock.candidate.source_commit
  );
  assert.deepEqual(candidate.comparison.snapshot_totals, approvedTotals);
  assert.deepEqual(candidate.delta, {
    js_raw_bytes: 4_550,
    js_brotli_bytes: 1_925,
    all_raw_bytes: 13_037,
    all_brotli_bytes: 2_776
  });
});

test("candidate generation accepts only approval-state failures", () => {
  assert.deepEqual(validateCandidateBundleReport(driftReport()), []);
  for (const failures of [
    [],
    [{ code: "TOTAL_JS_RAW", detail: {} }],
    [
      { code: "APPROVED_BUNDLE_LOCK_PENDING", detail: {} },
      { code: "UNREGISTERED_DYNAMIC_IMPORT", detail: {} }
    ]
  ]) {
    const errors = validateCandidateBundleReport(driftReport({ failures }));
    assert.ok(errors.length > 0, JSON.stringify(failures));
  }
});

test("candidate rejects malformed provenance and artifact digests", () => {
  assert.throws(
    () => createCandidateDocument(candidateInput({
      source: { ...candidateInput().source, commit: "HEAD" }
    })),
    /source commit/
  );
  assert.throws(
    () => createCandidateDocument(candidateInput({
      artifacts: {
        ...candidateInput().artifacts,
        vite_manifest_sha256: "not-a-digest"
      }
    })),
    /vite_manifest_sha256/
  );
});

test("promotion renders an approved lock without changing budgets", () => {
  const candidate = createCandidateDocument(candidateInput());
  const lock = createApprovedBundleLock(candidate, approvalOptions(candidate));

  assert.equal(lock.schema_version, 3);
  assert.equal(lock.status, "APPROVED");
  assert.deepEqual(lock.candidate.totals, candidate.totals);
  assert.equal("limits" in lock, false);
  assert.equal(lock.approval.approval_reference, "CAB-2026-0718");
  assert.match(lock.approval.artifact_ref, /frontend-bundle-approval@sha256:/);
});

test("promotion rejects placeholders, mutable artifacts and wrong signer identity", () => {
  const candidate = createCandidateDocument(candidateInput());
  const valid = approvalOptions(candidate);

  assert.throws(
    () => createApprovedBundleLock(candidate, { ...valid, approvalReference: "TODO" }),
    /approval reference/
  );
  assert.throws(
    () => createApprovedBundleLock(candidate, {
      ...valid,
      candidateArtifactRef:
        "ghcr.io/g5n-dev/auris_flow/frontend-bundle-candidate:latest"
    }),
    /immutable official GHCR digest/
  );
  assert.throws(
    () => createApprovedBundleLock(candidate, {
      ...valid,
      candidateSignatureIdentity:
        "https://github.com/attacker/repo/.github/workflows/" +
        "frontend-bundle-candidate.yml@refs/heads/main"
    }),
    /signature identity/
  );
  assert.throws(
    () => createApprovedBundleLock(candidate, {
      ...valid,
      approvalArtifactRef:
        "ghcr.io/g5n-dev/auris_flow/frontend-bundle-approval:latest"
    }),
    /approval artifact reference/
  );
});

test("candidate directory is exact, deterministic and tamper-evident", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "auris-bundle-candidate-test-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const report = driftReport();
  const viteManifest = { "index.html": { file: "assets/index-fixture.js" } };
  const brotliManifest = {
    schemaVersion: 1,
    entries: {
      "assets/index-fixture.js": {
        rawBytes: 10,
        brotliBytes: 8,
        sourceSha256: hashValue("source"),
        brotliSha256: hashValue("brotli")
      }
    }
  };
  const distInventory = [
    { path: "assets/index-fixture.js", bytes: 10, sha256: hashValue("source") }
  ];
  const artifacts = {
    bundle_report_sha256: hashValue(canonicalJson(report)),
    vite_manifest_sha256: hashValue(canonicalJson(viteManifest)),
    brotli_manifest_sha256: hashValue(canonicalJson(brotliManifest)),
    dist_inventory_sha256: hashValue(canonicalJson(distInventory))
  };
  const candidate = createCandidateDocument(candidateInput({ artifacts }));

  const first = join(root, "first");
  const second = join(root, "second");
  for (const outputDir of [first, second]) {
    await writeCandidateDirectory(outputDir, {
      candidate,
      report,
      viteManifest,
      brotliManifest,
      distInventory
    });
    const verified = await verifyCandidateDirectory(outputDir);
    assert.deepEqual(verified.candidate, candidate);
    assert.equal(verified.file_count, 5);
  }
  assert.equal(
    await readFile(join(first, "SHA256SUMS"), "utf8"),
    await readFile(join(second, "SHA256SUMS"), "utf8")
  );

  await writeFile(join(first, "unexpected.txt"), "not allowed\n", "utf8");
  await assert.rejects(
    verifyCandidateDirectory(first),
    /unexpected candidate directory entries/
  );
  await rm(join(first, "unexpected.txt"));
  await writeFile(join(first, "bundle-report.json"), "{}\n", "utf8");
  await assert.rejects(verifyCandidateDirectory(first), /checksum mismatch/);
});

test("candidate verifier rejects symlink substitution", async (context) => {
  const root = await mkdtemp(join(tmpdir(), "auris-bundle-symlink-test-"));
  context.after(() => rm(root, { recursive: true, force: true }));
  const report = driftReport();
  const viteManifest = {};
  const brotliManifest = {};
  const distInventory = [];
  const artifacts = {
    bundle_report_sha256: hashValue(canonicalJson(report)),
    vite_manifest_sha256: hashValue(canonicalJson(viteManifest)),
    brotli_manifest_sha256: hashValue(canonicalJson(brotliManifest)),
    dist_inventory_sha256: hashValue(canonicalJson(distInventory))
  };
  const candidate = createCandidateDocument(candidateInput({ artifacts }));
  const outputDir = join(root, "candidate");
  await writeCandidateDirectory(outputDir, {
    candidate,
    report,
    viteManifest,
    brotliManifest,
    distInventory
  });
  const target = join(root, "outside.json");
  await writeFile(target, "{}\n", "utf8");
  await rm(join(outputDir, "vite-manifest.json"));
  await symlink(target, join(outputDir, "vite-manifest.json"));
  await assert.rejects(verifyCandidateDirectory(outputDir), /regular non-linked file/);
});

test("candidate and promotion workflows remain protected and fail closed", async () => {
  const [candidateWorkflow, promotionWorkflow, candidateCli] = await Promise.all([
    readFile(
      new URL("../../../.github/workflows/frontend-bundle-candidate.yml", import.meta.url),
      "utf8"
    ),
    readFile(
      new URL("../../../.github/workflows/frontend-bundle-promotion.yml", import.meta.url),
      "utf8"
    ),
    readFile(
      new URL("./frontend-bundle-candidate-cli.mjs", import.meta.url),
      "utf8"
    )
  ]);

  const candidateBuild = candidateWorkflow.match(
    /  build-candidate:[\s\S]*?(?=\n  publish-signed-candidate:)/
  )?.[0];
  const candidatePublisher = candidateWorkflow.match(
    /  publish-signed-candidate:[\s\S]*$/
  )?.[0];
  assert.ok(candidateBuild);
  assert.ok(candidatePublisher);
  assert.doesNotMatch(candidateBuild, /environment:|id-token:\s*write|packages:\s*write/);
  assert.match(candidateBuild, /npm ci[\s\S]*npm run build/);
  assert.match(candidatePublisher, /environment:\s*frontend-bundle-build/);
  assert.match(candidatePublisher, /id-token:\s*write/);
  assert.match(candidatePublisher, /packages:\s*write/);
  assert.doesNotMatch(candidatePublisher, /actions\/checkout|npm ci|npm run|\bnode\s/);
  assert.match(candidateWorkflow, /source_commit must equal the reviewed default-branch tip/);
  assert.match(candidateWorkflow, /WORKFLOW_SOURCE_COMMIT: \$\{\{ github\.workflow_sha \}\}/);
  assert.match(candidateWorkflow, /cosign sign --yes/);
  assert.match(candidateWorkflow, /oras push[\s\S]*--format json/);
  assert.match(candidateWorkflow, /--certificate-github-workflow-sha/);
  assert.match(candidateWorkflow, /frontend-bundle-candidate\.yml@refs\/heads/);
  assert.doesNotMatch(candidateWorkflow, /build\/release-evidence/);
  assert.match(
    candidateCli,
    /AURIS_REQUIRE_APPROVED_BUNDLE:\s*"1"/
  );

  const rebuildVerifier = promotionWorkflow.match(
    /  rebuild-verify:[\s\S]*?(?=\n  approve-sign:)/
  )?.[0];
  const approvalSigner = promotionWorkflow.match(/  approve-sign:[\s\S]*$/)?.[0];
  assert.ok(rebuildVerifier);
  assert.ok(approvalSigner);
  assert.match(rebuildVerifier, /packages:\s*read/);
  assert.doesNotMatch(rebuildVerifier, /packages:\s*write|id-token:\s*write/);
  assert.match(rebuildVerifier, /frontend-bundle-candidate-cli\.mjs verify/);
  assert.match(approvalSigner, /environment:\s*frontend-bundle-production/);
  assert.match(approvalSigner, /packages:\s*write/);
  assert.match(approvalSigner, /id-token:\s*write/);
  assert.doesNotMatch(
    approvalSigner,
    /actions\/checkout|npm ci|npm run|frontend-bundle-candidate-cli/
  );
  assert.match(promotionWorkflow, /cosign verify/);
  assert.match(promotionWorkflow, /cosign sign --yes/);
  assert.match(promotionWorkflow, /frontend-bundle-approval@sha256|frontend-bundle-approval"/);
  assert.match(promotionWorkflow, /schema_version:\s*3/);
  assert.match(promotionWorkflow, /--certificate-github-workflow-sha/);
  assert.match(
    promotionWorkflow,
    /production\/frontend\/frontend-bundle\.lock\.json/
  );
  for (const source of [candidateWorkflow, promotionWorkflow]) {
    assert.doesNotMatch(source, /uses:\s+[^\s]+@v\d/);
  }
});
