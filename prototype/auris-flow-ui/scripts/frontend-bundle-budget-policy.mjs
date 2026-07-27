export const frontendBundleBudgetPolicy = Object.freeze({
  schemaVersion: 4,
  auditedAt: "2026-07-27",
  rationale:
    "保留 8e009d9 与既有数据资产闭环作为提交绑定参照；P0 平台音频导入闭环按锁定工具链记录本地验收增量。正式发行仍必须由 production/frontend/frontend-bundle.lock.json 独立审批，预算只保留约 1% 缓冲。",
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
  comparisonSnapshot: Object.freeze({
    status: "LEGACY_REFERENCE",
    sourceCommit: "647fb5c7e7fb1cc600917be38385166004b9b552",
    comparisonBaselineCommit: "8e009d971b3ca91f72287ee3c2529399740e1147",
    scope: "scene-data-assets-authoritative-bff-closure",
    totals: Object.freeze({
      jsRawBytes: 1_123_292,
      jsBrotliBytes: 298_628,
      allRawBytes: 2_231_779,
      allBrotliBytes: 461_472
    })
  }),
  workingAcceptance: Object.freeze({
    status: "LOCAL_ACCEPTANCE",
    id: "p0-platform-audio-import-closure/2026-07-27",
    comparedToSourceCommit: "647fb5c7e7fb1cc600917be38385166004b9b552",
    scope:
      "data-assets connector configuration, production TaskRun, Dagster import, ImportBatch readback, AudioSession playback",
    releaseEligible: false,
    totals: Object.freeze({
      jsRawBytes: 1_139_003,
      jsBrotliBytes: 312_480,
      allRawBytes: 2_255_017,
      allBrotliBytes: 476_175
    })
  }),
  limits: Object.freeze({
    totalJsRawBytes: 1_150_976,
    totalJsBrotliBytes: 315_392,
    initialClosureBrotliBytes: 276_480,
    routeJsClosureBytes: 307_200,
    maxJsAssetBytes: 512_000,
    totalAssetsRawBytes: 2_277_376,
    totalAssetsBrotliBytes: 481_280,
    maxKnowledgeBytes: 80 * 1024,
    maxCatalogBytes: 180 * 1024,
    maxCssAssetBytes: 830 * 1024
  })
});

export const frontendBundleLimits = frontendBundleBudgetPolicy.limits;
