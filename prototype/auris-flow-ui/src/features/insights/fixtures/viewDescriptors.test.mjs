import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const fixtureUrl = new URL("./data/insights-fixtures.json", import.meta.url);
const metricsSourceUrl = new URL("../controller/buildInsightMetrics.ts", import.meta.url);
const viewSourceUrl = new URL("../controller/buildInsightView.ts", import.meta.url);
const businessChartsSourceUrl = new URL("../controller/buildInsightBusinessCharts.ts", import.meta.url);
const storeSalesChartsSourceUrl = new URL("../controller/buildInsightStoreSalesCharts.ts", import.meta.url);
const governanceChartsSourceUrl = new URL("../controller/buildInsightGovernanceCharts.ts", import.meta.url);
const emptyProjectionSourceUrl = new URL("../components/InsightsEmptyProjection.tsx", import.meta.url);

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

const cloneFixtureDescriptor = (value) => {
  if (Array.isArray(value)) return value.map(cloneFixtureDescriptor);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, cloneFixtureDescriptor(item)]));
  }
  return value;
};

const loadBuilder = async (sourceUrl, exportName, injectedNames) => {
  let source = await readFile(sourceUrl, "utf8");
  source = source.replace(
    /import \{[^\n]+\} from "\.\.\/fixtures\/viewDescriptors";\n/,
    `const { ${injectedNames.join(", ")} } = globalThis.__insightDescriptors;\n`
  );
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext
    }
  }).outputText;
  const module = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
  return module[exportName];
};

const fact = (id, flags = {}) => ({ id, crosstalk: false, lowConfidence: false, ...flags });

const createScope = () => ({
  buildRangeValues: (key, base, min = -999, max = 999) => ({
    today: base - 4,
    "7d": base - 3,
    "30d": base - 2,
    "90d": base - 1,
    custom: Math.min(max, Math.max(min, base))
  }),
  clampScore: (value) => Math.round(value * 10) / 10,
  conversionProgress: 61.2,
  crosstalkRisk: 7.4,
  dataset: {
    facts: [fact("f-a", { crosstalk: true }), fact("f-b"), fact("f-c", { lowConfidence: true })]
  },
  driveFacts: [fact("drive-a"), fact("shared")],
  effectiveReceptionRate: 82.3,
  evidenceComplete: [fact("evidence-a"), fact("evidence-b")],
  formatPercent: (value) => `${value.toFixed(1)}%`,
  modelScore: 91.8,
  northStarScore: 79.6,
  objectionResolution: 68.4,
  quoteConsistency: 77.7,
  quoteFacts: [fact("quote-a")],
  rangeDeltaText: (key) => `delta:${key}`,
  rangeValue: (key, value) => Number((value + key.length / 100).toFixed(2)),
  rangedConversionProgress: 64.1,
  rangedEffectiveReceptionRate: 83.2,
  rangedQuoteConsistency: 78.5,
  rangedRiskReverseScore: 70.4,
  resolvedFacts: [fact("resolved-a"), fact("shared")],
  riskFacts: [fact("risk-a"), fact("risk-b")],
  riskReverseScore: 72.6,
  tagAssetQuality: 88.1,
  testDriveIntent: 55.2,
  unique: (values) => [...new Set(values)],
  validReceptionFacts: [fact("valid-a"), fact("valid-b"), fact("valid-c")]
});

const serialize = (value) => JSON.stringify(
  value,
  (_key, item) => item instanceof Map ? [...item.entries()] : item
);

const createChartScope = () => {
  const facts = [
    { id: "f1", store: "S1", tags: ["报价金额", "冲突"], person: "P1", route: "listening", time: "09:10", eventType: "报价", assetKey: "a1", status: "ok", evidenceRefs: ["e1"] },
    { id: "f2", store: "S2", tags: ["试驾", "接待"], person: "P2", route: "data", time: "10:20", eventType: "试驾", assetKey: "a2", status: "review", evidenceRefs: ["e2"] },
    { id: "f3", store: "S3", tags: ["低置信", "报价金额"], person: "P3", route: "labels", time: "11:30", eventType: "异议", assetKey: "a3", status: "pending", evidenceRefs: ["e3"] },
    { id: "f4", store: "S1", tags: ["成交意向"], person: "P1", route: "assets", time: "12:40", eventType: "跟进", assetKey: "a4", status: "done", evidenceRefs: ["e4"] },
    { id: "f5", store: "S2", tags: ["串音"], person: "P2", route: "evaluation", time: "13:50", eventType: "风险", assetKey: "a5", status: "risk", evidenceRefs: ["e5"] },
    { id: "f6", store: "S3", tags: ["接待"], person: "P3", route: "insights", time: "14:00", eventType: "接待", assetKey: "a6", status: "ready", evidenceRefs: ["e6"] },
    { id: "f7", store: "S4", tags: ["其他"], person: "P4", route: "data", time: "15:10", eventType: "其他", assetKey: "a7", status: "ready", evidenceRefs: ["e7"] }
  ];
  const topStores = [
    { store: "S1", total: 10, risk: 3, quote: 2, testDrive: 1, confidence: 91 },
    { store: "S2", total: 8, risk: 2, quote: 1, testDrive: 3, confidence: 82 },
    { store: "S3", total: 6, risk: 1, quote: 3, testDrive: 2, confidence: 77 }
  ];
  const topTags = [
    { label: "报价金额", count: 7, confidence: 90, assetKey: "tag-a" },
    { label: "低置信", count: 5, confidence: 72, assetKey: "tag-b" },
    { label: "试驾", count: 4, confidence: 88, assetKey: "tag-c" },
    { label: "冲突", count: 2, confidence: 61, assetKey: "tag-d" }
  ];
  return {
    buildTrend: (base, delta) => [base - delta, base, base + delta],
    dataset: {
      context: { label: "label-v5" },
      facts,
      qualityRows: [
        { ability: "ASR 转写", candidate: 92, baseline: 90, delta: 2, blocker: "否", route: "evaluation" },
        { ability: "边界", candidate: 78, baseline: 82, delta: -4, blocker: "是", route: "evaluation" }
      ],
      storeRows: topStores
    },
    driveFacts: [facts[1]],
    evidenceComplete: [facts[0], facts[1], facts[3]],
    firstRiskFact: facts[2],
    northStar: { delta: "+1.2" },
    northStarScore: 81.4,
    objectionResolution: 67.2,
    quoteFacts: [facts[0], facts[2]],
    rangeConfig: { reportScope: "2026-W28", labels: ["Mon", "Tue", "Wed", "Thu"] },
    rangedConversionProgress: 64.3,
    rangedEffectiveReceptionRate: 84.5,
    rangedQuoteConsistency: 78.4,
    rangedRiskReverseScore: 71.6,
    resolvedFacts: [facts[3]],
    riskFacts: [facts[2], facts[4]],
    tagAssetQuality: 89.2,
    topStores,
    topTags,
    validReceptionFacts: [facts[0], facts[1]],
    topSales: [
      { person: "P1", quote: 3, resolved: 2, risk: 0 },
      { person: "P2", quote: 2, resolved: 1, risk: 2 }
    ],
    evidenceForMetric: () => [facts[0], facts[2]],
    insightMetrics: [{ key: "m1" }],
    metricByKey: new Map([["objectionResolution", { key: "objectionResolution" }]]),
    quoteConsistency: 80.1,
    testDriveIntent: 44.4,
    compactNumber: (value) => `#${value}`,
    qualitySeries: [{ key: "q", label: "Q", tone: "blue", values: [1, 2, 3] }],
    view: { chartIds: ["a", "b", "c"] },
    visibleMetrics: [{ key: "v1" }, { key: "v2" }]
  };
};

const assertFreshTree = (first, second, shared = new Set()) => {
  if (first === null || typeof first !== "object") {
    assert.equal(first, second);
    return;
  }
  if (shared.has(first)) {
    assert.strictEqual(first, second);
    return;
  }
  assert.notStrictEqual(first, second);
  assert.deepEqual(Object.keys(first), Object.keys(second));
  for (const key of Object.keys(first)) assertFreshTree(first[key], second[key], shared);
};

const loadEmptyProjection = async (descriptors) => {
  const names = ["PanelHeader", "Activity", "Building2", "Database", "FileText", "Gauge", "LayoutDashboard", "LockKeyhole", "ShieldCheck", "Tags", "UserCheck"];
  globalThis.__emptyMocks = Object.fromEntries(names.map((name) => [name, name]));
  globalThis.__insightDescriptors = { emptyProjectionViews: descriptors.emptyProjectionViews, cloneFixtureDescriptor };
  let source = await readFile(emptyProjectionSourceUrl, "utf8");
  source = source
    .replace(/import \{ PanelHeader \} from "[^\n]+";\n/, "const { PanelHeader } = globalThis.__emptyMocks;\n")
    .replace(/import \{ Activity[^\n]+\} from "lucide-react";\n/, "const { Activity, Building2, Database, FileText, Gauge, LayoutDashboard, LockKeyhole, ShieldCheck, Tags, UserCheck } = globalThis.__emptyMocks;\n")
    .replace(/import \{ cloneFixtureDescriptor, emptyProjectionViews \} from "\.\.\/fixtures\/viewDescriptors";\n/, "const { cloneFixtureDescriptor, emptyProjectionViews } = globalThis.__insightDescriptors;\n");
  source = `const React = globalThis.__react;\n${source}`;
  globalThis.__react = {
    createElement: (type, props, ...children) => ({ type, props: props ?? {}, children })
  };
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
      jsx: ts.JsxEmit.React
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
};

test("descriptor 迁移保持指标、北极星和视图完整语义", async () => {
  const descriptors = JSON.parse(await readFile(fixtureUrl, "utf8"));
  globalThis.__insightDescriptors = {
    metricDescriptors: descriptors.metricDescriptors,
    northStarDescriptor: descriptors.northStarDescriptor,
    viewDescriptors: descriptors.views
  };

  const buildInsightMetrics = await loadBuilder(
    metricsSourceUrl,
    "buildInsightMetrics",
    ["metricDescriptors", "northStarDescriptor"]
  );
  const buildInsightView = await loadBuilder(
    viewSourceUrl,
    "buildInsightView",
    ["viewDescriptors"]
  );

  const scope = createScope();
  const metrics = buildInsightMetrics(scope);
  const view = buildInsightView({ ...scope, ...metrics, currentTab: "sales" });

  assert.equal(sha256(serialize(metrics)), "d9ec833f8ff840b78c987ea934f115cc72e0a04dcef843712cbfc450b3cdf128");
  assert.equal(sha256(serialize(view)), "31350c360794a3b4d14e8c742fc07b80c834534d6c56a5db42c716daaccc64bd");
  assert.deepEqual(metrics.insightMetrics.map(({ key }) => key), [
    "effectiveReceptionRate",
    "conversionProgress",
    "objectionResolution",
    "quoteConsistency",
    "riskReverseScore",
    "testDriveIntent",
    "crosstalkRisk",
    "tagAssetQuality",
    "modelQuality"
  ]);
  assert.deepEqual(Object.keys(view.views), ["business", "store", "sales", "tags", "quality", "reports"]);
});

test("每次构建重建返回对象和所有原有嵌套数组", async () => {
  const descriptors = JSON.parse(await readFile(fixtureUrl, "utf8"));
  globalThis.__insightDescriptors = {
    metricDescriptors: descriptors.metricDescriptors,
    northStarDescriptor: descriptors.northStarDescriptor,
    viewDescriptors: descriptors.views
  };
  const buildInsightMetrics = await loadBuilder(
    metricsSourceUrl,
    "buildInsightMetrics",
    ["metricDescriptors", "northStarDescriptor"]
  );
  const buildInsightView = await loadBuilder(
    viewSourceUrl,
    "buildInsightView",
    ["viewDescriptors"]
  );
  const scope = createScope();
  const firstMetrics = buildInsightMetrics(scope);
  const secondMetrics = buildInsightMetrics(scope);
  const firstView = buildInsightView({ ...scope, ...firstMetrics, currentTab: "business" });
  const secondView = buildInsightView({ ...scope, ...secondMetrics, currentTab: "business" });

  assert.notStrictEqual(firstMetrics.insightMetrics, secondMetrics.insightMetrics);
  assert.notStrictEqual(firstMetrics.insightMetrics[0], secondMetrics.insightMetrics[0]);
  assert.notStrictEqual(firstMetrics.insightMetrics[0].tags, secondMetrics.insightMetrics[0].tags);
  assert.notStrictEqual(firstMetrics.insightMetrics[0].rangeValues, secondMetrics.insightMetrics[0].rangeValues);
  assert.notStrictEqual(firstMetrics.insightMetrics[0].evidenceIds, secondMetrics.insightMetrics[0].evidenceIds);
  assert.notStrictEqual(firstMetrics.northStar, secondMetrics.northStar);
  assert.notStrictEqual(firstMetrics.northStar.components, secondMetrics.northStar.components);
  assert.notStrictEqual(firstMetrics.northStar.components[0], secondMetrics.northStar.components[0]);
  assert.notStrictEqual(firstView.views, secondView.views);
  assert.notStrictEqual(firstView.views.business, secondView.views.business);
  assert.notStrictEqual(firstView.views.business.metricKeys, secondView.views.business.metricKeys);
  assert.notStrictEqual(firstView.views.business.chartIds, secondView.views.business.chartIds);
});

test("chart descriptor 迁移保持 19 个图表的 map key、顺序和完整语义", async () => {
  const descriptors = JSON.parse(await readFile(fixtureUrl, "utf8"));
  assert.ok(descriptors.chartDescriptors, "缺少 chartDescriptors");
  globalThis.__insightDescriptors = { chartDescriptors: descriptors.chartDescriptors, cloneFixtureDescriptor };
  const buildInsightBusinessChartFactory = await loadBuilder(
    businessChartsSourceUrl,
    "buildInsightBusinessChartFactory",
    ["chartDescriptors", "cloneFixtureDescriptor"]
  );
  const buildInsightStoreSalesChartFactory = await loadBuilder(
    storeSalesChartsSourceUrl,
    "buildInsightStoreSalesChartFactory",
    ["chartDescriptors", "cloneFixtureDescriptor"]
  );
  const buildInsightGovernanceChartFactory = await loadBuilder(
    governanceChartsSourceUrl,
    "buildInsightGovernanceChartFactory",
    ["chartDescriptors", "cloneFixtureDescriptor"]
  );
  const scope = createChartScope();
  const business = buildInsightBusinessChartFactory(scope).buildInsightBusinessCharts();
  const storeSales = buildInsightStoreSalesChartFactory(scope).buildInsightStoreSalesCharts();
  const governance = buildInsightGovernanceChartFactory(scope).buildInsightGovernanceCharts();

  assert.equal(sha256(serialize(business)), "48ddbc924dd1b6a7e691453db0a88ea4ea451116fa380041cfc03a606dfd8b11");
  assert.equal(sha256(serialize(storeSales)), "356dbc1b66930faf3a5a3f4d1390a52d95c584bb7020e873f55042f249417e74");
  assert.equal(sha256(serialize(governance)), "2a8c8f75ec9afc1ec78e7a0719e88d14284b34411b465480e73a94911ff10bf6");
  assert.deepEqual(Object.keys(business), ["business-trend", "business-sankey", "business-radar", "business-table"]);
  assert.deepEqual(Object.keys(storeSales), ["store-heatmap", "store-risk-bar", "store-table", "sales-bar", "sales-radar", "sales-table"]);
  assert.deepEqual(Object.keys(governance), ["tag-bar", "tag-sankey", "tag-table", "quality-line", "quality-radar", "quality-heatmap", "quality-table", "report-coverage", "report-table"]);
});

test("每次图表构建重建所有原有嵌套结构，仅保留旧实现的 scope 数组引用", async () => {
  const descriptors = JSON.parse(await readFile(fixtureUrl, "utf8"));
  assert.ok(descriptors.chartDescriptors, "缺少 chartDescriptors");
  globalThis.__insightDescriptors = { chartDescriptors: descriptors.chartDescriptors, cloneFixtureDescriptor };
  const factories = [
    [await loadBuilder(businessChartsSourceUrl, "buildInsightBusinessChartFactory", ["chartDescriptors", "cloneFixtureDescriptor"]), "buildInsightBusinessCharts"],
    [await loadBuilder(storeSalesChartsSourceUrl, "buildInsightStoreSalesChartFactory", ["chartDescriptors", "cloneFixtureDescriptor"]), "buildInsightStoreSalesCharts"],
    [await loadBuilder(governanceChartsSourceUrl, "buildInsightGovernanceChartFactory", ["chartDescriptors", "cloneFixtureDescriptor"]), "buildInsightGovernanceCharts"]
  ];
  const scope = createChartScope();
  const shared = new Set([scope.rangeConfig.labels, scope.qualitySeries]);
  for (const [buildFactory, buildKey] of factories) {
    const first = buildFactory(scope)[buildKey]();
    const second = buildFactory(scope)[buildKey]();
    assertFreshTree(first, second, shared);
  }
});

test("空态 descriptor 迁移保持六个 Tab 与未知 Tab 回退渲染语义", async () => {
  const descriptors = JSON.parse(await readFile(fixtureUrl, "utf8"));
  assert.ok(descriptors.emptyProjectionViews, "缺少 emptyProjectionViews");
  const { InsightsEmptyProjection } = await loadEmptyProjection(descriptors);
  const expected = {
    business: "b27a47e1aaf8364ab784ad44bd81dd93d983f250ba314982abcdbaa7d01c232e",
    store: "c7bc8a2e52a1d7693df435fae39bec8f503ff0ef95f01e4c274619c94ecd29af",
    sales: "8f5db6413053baba1508fcc4d8fdf8dad0557b452a0a974ca5779197286059b8",
    tags: "c0602ee206c798e33b78a29c8ce19c27e98e5e5338a3e780b7ab51646a5cc3ad",
    quality: "a5d27d6301c1a4920999d7faeadb854b091aea228fba8592a66c2c86b0ea2599",
    reports: "7630f6d5e3759958eee4a6d797b9e54a28858074f294edd3da3c54a78fe06629",
    unknown: "b27a47e1aaf8364ab784ad44bd81dd93d983f250ba314982abcdbaa7d01c232e"
  };
  for (const [tab, digest] of Object.entries(expected)) {
    assert.equal(sha256(serialize(InsightsEmptyProjection({ activeTab: tab }))), digest);
  }
});

test("空态每次渲染重建 JSX、指标、检查项和血缘节点", async () => {
  const descriptors = JSON.parse(await readFile(fixtureUrl, "utf8"));
  assert.ok(descriptors.emptyProjectionViews, "缺少 emptyProjectionViews");
  const { InsightsEmptyProjection } = await loadEmptyProjection(descriptors);
  const first = InsightsEmptyProjection({ activeTab: "business" });
  const second = InsightsEmptyProjection({ activeTab: "business" });
  assertFreshTree(first, second);
});
