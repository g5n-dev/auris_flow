export const frontendBundleBudgetPolicy = Object.freeze({
  schemaVersion: 1,
  auditedAt: "2026-07-18",
  rationale:
    "Listening、Labels 与 Insights 的生产 BFF 闭环属于已验收功能增量；预算以完整功能树实测值为基线，并保留约 1% 回归缓冲。",
  referenceAudit: Object.freeze({
    id: "frontend-decomposition/final-2026-07-18",
    totals: Object.freeze({
      jsRawBytes: 1_075_695,
      jsBrotliBytes: 286_674,
      allRawBytes: 2_183_671,
      allBrotliBytes: 449_423
    })
  }),
  auditedBaseline: Object.freeze({
    sourceCommit: "8e009d9",
    totals: Object.freeze({
      jsRawBytes: 1_101_183,
      jsBrotliBytes: 293_469,
      allRawBytes: 2_209_159,
      allBrotliBytes: 456_212
    }),
    manualLabelSplitDelta: Object.freeze({
      jsRawBytes: 697,
      jsBrotliBytes: 256,
      allRawBytes: 697,
      allBrotliBytes: 257
    })
  }),
  limits: Object.freeze({
    totalJsRawBytes: 1_112_064,
    totalJsBrotliBytes: 296_960,
    initialClosureBrotliBytes: 276_480,
    routeJsClosureBytes: 307_200,
    maxJsAssetBytes: 512_000,
    totalAssetsRawBytes: 2_232_320,
    totalAssetsBrotliBytes: 460_800,
    maxKnowledgeBytes: 80 * 1024,
    maxCatalogBytes: 180 * 1024,
    maxCssAssetBytes: 830 * 1024
  })
});

export const frontendBundleLimits = frontendBundleBudgetPolicy.limits;
