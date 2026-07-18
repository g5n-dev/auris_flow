import assert from "node:assert/strict";
import test from "node:test";

import { frontendBundleBudgetPolicy } from "./frontend-bundle-budget-policy.mjs";

const { auditedBaseline, limits, referenceAudit } = frontendBundleBudgetPolicy;

function marginRatio(limit, actual) {
  return (limit - actual) / actual;
}

test("预算基线记录已验收功能增量，并证明手工标签拆分不是主要增长来源", () => {
  const reference = referenceAudit.totals;
  const current = auditedBaseline.totals;
  assert.equal(current.jsRawBytes - reference.jsRawBytes, 25_488);
  assert.equal(current.jsBrotliBytes - reference.jsBrotliBytes, 6_795);
  assert.equal(current.allRawBytes - reference.allRawBytes, 25_488);
  assert.equal(current.allBrotliBytes - reference.allBrotliBytes, 6_789);
  assert.deepEqual(auditedBaseline.manualLabelSplitDelta, {
    jsRawBytes: 697,
    jsBrotliBytes: 256,
    allRawBytes: 697,
    allBrotliBytes: 257
  });
  assert.ok(auditedBaseline.manualLabelSplitDelta.jsBrotliBytes < 512);
});

test("全量预算只保留约 1% 小缓冲，防止用宽松阈值掩盖后续回归", () => {
  const current = auditedBaseline.totals;
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
