import assert from "node:assert/strict";
import test from "node:test";

import { frontendBundleBudgetPolicy } from "./frontend-bundle-budget-policy.mjs";

const { auditedBaseline, auditedCandidate, limits, referenceAudit } = frontendBundleBudgetPolicy;

function marginRatio(limit, actual) {
  return (limit - actual) / actual;
}

test("预算基线与当前候选都绑定可审计总量，不再依赖手填的局部 bundle 增量", () => {
  const reference = referenceAudit.totals;
  const baseline = auditedBaseline.totals;
  const candidate = auditedCandidate.totals;
  assert.match(auditedBaseline.sourceCommit, /^[0-9a-f]{40}$/);
  assert.match(auditedCandidate.sourceBaseCommit, /^[0-9a-f]{40}$/);
  assert.equal(auditedCandidate.comparisonBaselineCommit, auditedBaseline.sourceCommit);
  assert.equal(baseline.jsRawBytes - reference.jsRawBytes, 25_488);
  assert.equal(baseline.jsBrotliBytes - reference.jsBrotliBytes, 6_795);
  assert.equal(candidate.jsRawBytes - baseline.jsRawBytes, 22_109);
  assert.equal(candidate.jsBrotliBytes - baseline.jsBrotliBytes, 5_159);
  assert.equal(candidate.allRawBytes - baseline.allRawBytes, 22_620);
  assert.equal(candidate.allBrotliBytes - baseline.allBrotliBytes, 5_260);
});

test("全量预算只保留约 1% 小缓冲，防止用宽松阈值掩盖后续回归", () => {
  const current = auditedCandidate.totals;
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
