export const frontendBundleBudgetPolicy = Object.freeze({
  schemaVersion: 2,
  auditedAt: "2026-07-20",
  rationale:
    "以 8e009d9 已审计构建为量化参照，登记当前 647fb5c 基础工作树上 Scene 锁、Data/Assets 权威投影与质量重跑闭环后的确定性生产构建总量；检查器要求产物与候选快照精确一致，预算只保留约 1% 缓冲。",
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
    sourceCommit: "8e009d971b3ca91f72287ee3c2529399740e1147",
    totals: Object.freeze({
      jsRawBytes: 1_101_183,
      jsBrotliBytes: 293_469,
      allRawBytes: 2_209_159,
      allBrotliBytes: 456_212
    })
  }),
  auditedCandidate: Object.freeze({
    sourceBaseCommit: "647fb5c7e7fb1cc600917be38385166004b9b552",
    comparisonBaselineCommit: "8e009d971b3ca91f72287ee3c2529399740e1147",
    scope: "scene-data-assets-authoritative-bff-closure",
    totals: Object.freeze({
      jsRawBytes: 1_123_292,
      jsBrotliBytes: 298_628,
      allRawBytes: 2_231_779,
      allBrotliBytes: 461_472
    })
  }),
  limits: Object.freeze({
    totalJsRawBytes: 1_134_592,
    totalJsBrotliBytes: 301_568,
    initialClosureBrotliBytes: 276_480,
    routeJsClosureBytes: 307_200,
    maxJsAssetBytes: 512_000,
    totalAssetsRawBytes: 2_254_080,
    totalAssetsBrotliBytes: 466_944,
    maxKnowledgeBytes: 80 * 1024,
    maxCatalogBytes: 180 * 1024,
    maxCssAssetBytes: 830 * 1024
  })
});

export const frontendBundleLimits = frontendBundleBudgetPolicy.limits;
