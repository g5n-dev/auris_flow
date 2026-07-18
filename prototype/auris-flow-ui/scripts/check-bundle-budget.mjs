import { readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { brotliCompressSync, constants as zlibConstants } from "node:zlib";
import {
  APP_ENTRY,
  HTML_ENTRY,
  buildRouteScenarioDefinitions,
  compareDynamicEdgeCoverage,
  initialDynamicEdges
} from "./bundle-route-scenarios.mjs";
import { buildProductionFixturePayload, productionFixtureSpecs } from "./production-fixture-policy.mjs";
import { auditPrecompressedAssets, listProductionResources } from "./precompressed-assets.mjs";

const root = resolve(new URL("..", import.meta.url).pathname);
const distDir = process.env.AURIS_DIST_DIR ? resolve(root, process.env.AURIS_DIST_DIR) : join(root, "dist");
const assetsDir = join(distDir, "assets");
const manifestPath = join(distDir, ".vite", "manifest.json");
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));

const limits = {
  totalJsRawBytes: 1_121_280,
  totalJsBrotliBytes: 286_720,
  initialClosureBrotliBytes: 276_480,
  routeJsClosureBytes: 307_200,
  maxJsAssetBytes: 512_000,
  totalAssetsRawBytes: 2_194_125,
  totalAssetsBrotliBytes: 454_963,
  maxKnowledgeBytes: 80 * 1024,
  maxCatalogBytes: 180 * 1024,
  maxCssAssetBytes: 830 * 1024
};

const routeDefinitions = buildRouteScenarioDefinitions();

const failures = [];
const diagnosticBrotliCache = new Map();
const precompression = auditPrecompressedAssets(distDir);

function assertBudget(condition, code, detail) {
  if (!condition) failures.push({ code, detail });
}

function assetForPath(path) {
  const rawBytes = statSync(path).size;
  let diagnosticBrotliQ5Bytes = diagnosticBrotliCache.get(path);
  if (diagnosticBrotliQ5Bytes === undefined) {
    diagnosticBrotliQ5Bytes = brotliCompressSync(readFileSync(path), {
      params: { [zlibConstants.BROTLI_PARAM_QUALITY]: 5 }
    }).length;
    diagnosticBrotliCache.set(path, diagnosticBrotliQ5Bytes);
  }
  const name = relative(distDir, path).replaceAll("\\", "/");
  const extension = name.split(".").at(-1);
  const precompressedEntry = precompression.manifest?.entries?.[name];
  const brotliBytes = precompressedEntry?.brotliBytes ?? rawBytes;
  return {
    name,
    type: extension,
    rawBytes,
    brotliBytes,
    diagnosticBrotliQ5Bytes,
    contentEncoding: precompressedEntry ? "br" : "identity"
  };
}

function sumAssets(assets) {
  return {
    rawBytes: assets.reduce((sum, asset) => sum + asset.rawBytes, 0),
    brotliBytes: assets.reduce((sum, asset) => sum + asset.brotliBytes, 0),
    diagnosticBrotliQ5Bytes: assets.reduce((sum, asset) => sum + asset.diagnosticBrotliQ5Bytes, 0)
  };
}

function collectStaticManifestKeys(entryKeys) {
  const visited = new Set();
  const visit = (key) => {
    if (visited.has(key)) return;
    const chunk = manifest[key];
    assertBudget(Boolean(chunk), "MISSING_MANIFEST_ENTRY", { key });
    if (!chunk) return;
    visited.add(key);
    for (const imported of chunk.imports ?? []) visit(imported);
  };
  for (const entry of entryKeys) visit(entry);
  return visited;
}

function assetsForManifestKeys(keys) {
  const names = new Set();
  for (const key of keys) {
    const chunk = manifest[key];
    if (!chunk) continue;
    if (chunk.file) names.add(chunk.file);
    for (const name of chunk.css ?? []) names.add(name);
    for (const name of chunk.assets ?? []) names.add(name);
  }
  return [...names].map((name) => assetForPath(join(distDir, name))).sort((a, b) => a.name.localeCompare(b.name));
}

assertBudget(precompression.ok, "PRECOMPRESSION_INVALID", { errors: precompression.errors });

const productionAssets = listProductionResources(distDir)
  .map(assetForPath)
  .sort((a, b) => b.rawBytes - a.rawBytes);
const jsAssets = productionAssets.filter((asset) => asset.type === "js");
const cssAssets = productionAssets.filter((asset) => asset.type === "css");
const jsonAssets = productionAssets.filter((asset) => asset.type === "json");
const totalJs = sumAssets(jsAssets);
const totalAssets = sumAssets(productionAssets);

const jsonEntries = Object.entries(manifest).filter(([key, value]) => key.endsWith(".json") && value.file?.endsWith(".json"));
const catalogEntries = jsonEntries.filter(([key]) => /^src\/catalogs\/production\/(?:module|static)-catalog\.json$/.test(key));
const featureFixtureEntries = jsonEntries.filter(([key]) => /^src\/features\/[^/]+\/fixtures\/.*\.json$/.test(key));
const sharedFixtureEntries = jsonEntries.filter(([key]) => /^src\/shared\/fixtures\/data\/.*\.json$/.test(key));
const fixtureEntries = [...featureFixtureEntries, ...sharedFixtureEntries];
const classifiedJsonEntries = new Set([...catalogEntries, ...fixtureEntries]);
const unclassifiedJson = jsonEntries.filter((entry) => !classifiedJsonEntries.has(entry));
const catalogAssets = catalogEntries.map(([, entry]) => assetForPath(join(distDir, entry.file)));

const initialKeys = collectStaticManifestKeys([HTML_ENTRY, APP_ENTRY, routeDefinitions.home.entry]);
const initialAssets = assetsForManifestKeys(initialKeys);
const initialClosure = sumAssets(initialAssets);
const routes = {};
for (const [route, definition] of Object.entries(routeDefinitions)) {
  const routeKeys = new Set();
  const scenarioReports = definition.scenarios.map((scenario) => {
    const keys = collectStaticManifestKeys(scenario.entryKeys);
    for (const key of keys) routeKeys.add(key);
    const assets = assetsForManifestKeys(keys);
    const js = assets.filter((asset) => asset.type === "js");
    return {
      id: scenario.id,
      entryKeys: scenario.entryKeys,
      dynamicEdges: scenario.dynamicEdges,
      keys: [...keys].sort(),
      ...sumAssets(js),
      assets: js.map((asset) => asset.name),
      allAssets: assets.map((asset) => asset.name)
    };
  });
  const maxScenario = scenarioReports.reduce((largest, scenario) =>
    scenario.rawBytes > largest.rawBytes ? scenario : largest
  );
  const routeAssets = assetsForManifestKeys(routeKeys);
  routes[route] = {
    entries: [definition.entry],
    keys: [...routeKeys].sort(),
    rawBytes: maxScenario.rawBytes,
    brotliBytes: maxScenario.brotliBytes,
    assets: maxScenario.assets,
    allAssets: routeAssets.map((asset) => asset.name),
    maxScenario: maxScenario.id,
    scenarios: scenarioReports
  };
}

const dynamicCoverage = compareDynamicEdgeCoverage(manifest, routeDefinitions, initialDynamicEdges);
assertBudget(dynamicCoverage.unregistered.length === 0, "UNREGISTERED_DYNAMIC_IMPORT", {
  edges: dynamicCoverage.unregistered
});
assertBudget(dynamicCoverage.stale.length === 0, "STALE_DYNAMIC_SCENARIO_EDGE", {
  edges: dynamicCoverage.stale
});

const indexChunk = manifest["index.html"];
const appChunk = manifest["src/App.tsx"];
assertBudget(indexChunk?.dynamicImports?.includes("src/App.tsx"), "APP_NOT_LAZY_FROM_ENTRY", indexChunk);
assertBudget(appChunk?.dynamicImports?.includes("src/features/home/index.ts"), "HOME_NOT_LAZY_FROM_APP", appChunk);
assertBudget(catalogEntries.length === 2, "CATALOG_COUNT", { catalogEntries });
for (const [source] of catalogEntries) {
  const name = source.split("/").at(-1);
  const canonical = JSON.parse(readFileSync(join(root, "src/catalogs", name), "utf8"));
  const production = JSON.parse(readFileSync(join(root, source), "utf8"));
  assertBudget(JSON.stringify(production) === JSON.stringify(canonical), "CATALOG_SEMANTIC_DRIFT", { source, canonical: `src/catalogs/${name}` });
}
assertBudget(unclassifiedJson.length === 0, "UNCLASSIFIED_JSON", { unclassifiedJson });
assertBudget(jsonEntries.length === catalogEntries.length + fixtureEntries.length, "JSON_CLASSIFICATION", { jsonEntries });

for (const spec of productionFixtureSpecs) {
  const source = JSON.parse(readFileSync(join(root, spec.source), "utf8"));
  const expected = buildProductionFixturePayload(source, spec);
  const actual = JSON.parse(readFileSync(join(root, spec.output), "utf8"));
  const manifestEntry = jsonEntries.find(([key]) => key === spec.output);
  assertBudget(JSON.stringify(actual) === JSON.stringify(expected), "PRODUCTION_FIXTURE_DRIFT", {
    source: spec.source,
    output: spec.output
  });
  assertBudget(Boolean(manifestEntry), "PRODUCTION_FIXTURE_NOT_EMITTED", { output: spec.output });
  assertBudget(!jsonEntries.some(([key]) => key === spec.source), "CANONICAL_FIXTURE_EMITTED", { source: spec.source });
}

for (const [source, entry] of featureFixtureEntries) {
  const owner = source.match(/^src\/features\/([^/]+)/)?.[1];
  const owners = Object.entries(routes)
    .filter(([, route]) => route.allAssets.includes(entry.file))
    .map(([route]) => route);
  assertBudget(owners.includes(owner), "FIXTURE_NOT_IN_OWNER_ROUTE", { source, owner, owners, file: entry.file });
  assertBudget(owners.every((route) => route === owner), "FIXTURE_CROSSES_ROUTE", { source, owner, owners, file: entry.file });
}
for (const [source, entry] of sharedFixtureEntries) {
  const owners = Object.entries(routes)
    .filter(([, route]) => route.allAssets.includes(entry.file))
    .map(([route]) => route);
  assertBudget(owners.length > 0, "SHARED_FIXTURE_WITHOUT_ROUTE", { source, owners, file: entry.file });
  assertBudget(!initialAssets.some((asset) => asset.name === entry.file), "SHARED_FIXTURE_IN_INITIAL_CLOSURE", {
    source,
    owners,
    file: entry.file
  });
}

const largestJs = jsAssets[0];
const largestCss = cssAssets[0];
const knowledgeEntry = manifest["src/modules/knowledge/index.ts"];
const knowledgeAssets = knowledgeEntry?.file
  ? [assetForPath(join(distDir, knowledgeEntry.file))]
  : [];
assertBudget(Boolean(largestJs), "NO_JS_ASSET", { assetsDir });
assertBudget(Boolean(largestCss), "NO_CSS_ASSET", { assetsDir });
assertBudget(knowledgeAssets.length === 1, "KNOWLEDGE_CHUNK_COUNT", { knowledgeAssets });
assertBudget((knowledgeAssets[0]?.rawBytes ?? Infinity) <= limits.maxKnowledgeBytes, "KNOWLEDGE_CHUNK_RAW", { knowledgeAssets, limit: limits.maxKnowledgeBytes });
assertBudget((largestJs?.rawBytes ?? Infinity) <= limits.maxJsAssetBytes, "MAX_JS_CHUNK_RAW", { largestJs, limit: limits.maxJsAssetBytes });
assertBudget((largestCss?.rawBytes ?? Infinity) <= limits.maxCssAssetBytes, "MAX_CSS_RAW", { largestCss, limit: limits.maxCssAssetBytes });
assertBudget(sumAssets(catalogAssets).rawBytes <= limits.maxCatalogBytes, "CATALOG_RAW", { catalogAssets, limit: limits.maxCatalogBytes });
assertBudget(totalJs.rawBytes <= limits.totalJsRawBytes, "TOTAL_JS_RAW", { actual: totalJs.rawBytes, limit: limits.totalJsRawBytes });
assertBudget(totalJs.brotliBytes <= limits.totalJsBrotliBytes, "TOTAL_JS_BROTLI", { actual: totalJs.brotliBytes, limit: limits.totalJsBrotliBytes });
assertBudget(initialClosure.brotliBytes <= limits.initialClosureBrotliBytes, "INITIAL_CLOSURE_BROTLI", { actual: initialClosure.brotliBytes, limit: limits.initialClosureBrotliBytes, assets: initialAssets });
assertBudget(totalAssets.rawBytes <= limits.totalAssetsRawBytes, "TOTAL_ASSETS_RAW", { actual: totalAssets.rawBytes, limit: limits.totalAssetsRawBytes });
assertBudget(totalAssets.brotliBytes <= limits.totalAssetsBrotliBytes, "TOTAL_ASSETS_BROTLI", { actual: totalAssets.brotliBytes, limit: limits.totalAssetsBrotliBytes });
for (const [route, closure] of Object.entries(routes)) {
  assertBudget(closure.rawBytes <= limits.routeJsClosureBytes, "ROUTE_JS_CLOSURE_RAW", { route, actual: closure.rawBytes, limit: limits.routeJsClosureBytes, assets: closure.assets });
}

const report = {
  status: failures.length ? "failed" : "ok",
  generatedAt: new Date().toISOString(),
  brotliQuality: 11,
  diagnosticBrotliQuality: 5,
  limits,
  totals: { js: totalJs, all: totalAssets },
  initialClosure: { ...initialClosure, assets: initialAssets.map((asset) => asset.name) },
  routes,
  dynamicCoverage,
  classifications: {
    catalogs: catalogEntries.map(([source, entry]) => ({ source, file: entry.file })),
    featureFixtures: featureFixtureEntries.map(([source, entry]) => ({ source, file: entry.file })),
    sharedFixtures: sharedFixtureEntries.map(([source, entry]) => ({ source, file: entry.file }))
  },
  precompression: {
    ok: precompression.ok,
    errors: precompression.errors,
    resources: precompression.resources.map((resource) => resource.path)
  },
  assets: productionAssets,
  failures
};

console.log(JSON.stringify(report, null, 2));
if (failures.length) process.exitCode = 1;
