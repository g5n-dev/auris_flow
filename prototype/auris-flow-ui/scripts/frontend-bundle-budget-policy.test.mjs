import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { frontendBundleBudgetPolicy } from "./frontend-bundle-budget-policy.mjs";
import { validateFrontendBundleLock } from "./frontend-bundle-lock.mjs";

const lock = JSON.parse(await readFile(
  new URL("../../../production/frontend/frontend-bundle.lock.json", import.meta.url),
  "utf8"
));
const bundleCheckerSource = await readFile(
  new URL("./check-bundle-budget.mjs", import.meta.url),
  "utf8"
);
const { auditedBaseline, limits, referenceAudit } = frontendBundleBudgetPolicy;
const approvedCandidate = frontendBundleBudgetPolicy.comparisonSnapshot;

function marginRatio(limit, actual) {
  return (limit - actual) / actual;
}

test("预算基线与比较参照都绑定可审计总量，生产锁允许受治理的状态迁移", () => {
  const reference = referenceAudit.totals;
  const baseline = auditedBaseline.totals;
  const candidate = approvedCandidate.totals;
  assert.match(auditedBaseline.sourceCommit, /^[0-9a-f]{40}$/);
  assert.deepEqual(validateFrontendBundleLock(lock), []);
  assert.ok(["PENDING", "APPROVED"].includes(lock.status));
  assert.equal(lock.status === "PENDING", lock.artifact === null);
  assert.equal(approvedCandidate.status, "LEGACY_REFERENCE");
  assert.match(approvedCandidate.sourceCommit, /^[0-9a-f]{40}$/);
  assert.equal(
    approvedCandidate.comparisonBaselineCommit,
    auditedBaseline.sourceCommit
  );
  assert.equal(baseline.jsRawBytes - reference.jsRawBytes, 25_488);
  assert.equal(baseline.jsBrotliBytes - reference.jsBrotliBytes, 6_795);
  assert.equal(candidate.jsRawBytes - baseline.jsRawBytes, 22_109);
  assert.equal(candidate.jsBrotliBytes - baseline.jsBrotliBytes, 5_159);
  assert.equal(candidate.allRawBytes - baseline.allRawBytes, 22_620);
  assert.equal(candidate.allBrotliBytes - baseline.allBrotliBytes, 5_260);
});

test("严格 bundle 检查按 APPROVED 状态解析前端 Git tree，不依赖已废弃锁版本", () => {
  assert.match(
    bundleCheckerSource,
    /approvedBundleLock\.status === ["']APPROVED["']/
  );
  assert.doesNotMatch(
    bundleCheckerSource,
    /approvedBundleLock\.schema_version === 2/
  );
});

test("全量预算只保留约 1% 小缓冲，防止用宽松阈值掩盖后续回归", () => {
  const current = approvedCandidate.totals;
  const guardedTotals = [
    ["js raw", limits.totalJsRawBytes, current.jsRawBytes],
    ["js brotli", limits.totalJsBrotliBytes, current.jsBrotliBytes],
    ["all raw", limits.totalAssetsRawBytes, current.allRawBytes],
    ["all brotli", limits.totalAssetsBrotliBytes, current.allBrotliBytes]
  ];
  for (const [name, limit, actual] of guardedTotals) {
    const ratio = marginRatio(limit, actual);
    assert.ok(ratio >= 0.005, `${name} 缓冲低于 0.5%`);
    assert.ok(ratio <= 0.015, `${name} 缓冲高于 1.5%`);
  }
});
