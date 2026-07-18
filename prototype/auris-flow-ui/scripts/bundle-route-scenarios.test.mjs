import assert from "node:assert/strict";
import test from "node:test";
import {
  APP_ENTRY,
  buildRouteScenarioDefinitions,
  compareDynamicEdgeCoverage,
  dynamicEdge,
  initialDynamicEdges,
  routeManifestEntries
} from "./bundle-route-scenarios.mjs";

test("场景集合覆盖 128 个 Canvas 组合与 21 个 Listening 可达状态", () => {
  const definitions = buildRouteScenarioDefinitions();
  assert.equal(definitions.canvas.scenarios.length, 128);
  assert.equal(new Set(definitions.canvas.scenarios.map((scenario) => scenario.id)).size, 128);
  assert.equal(definitions.listening.scenarios.length, 21);
  assert.equal(new Set(definitions.listening.scenarios.map((scenario) => scenario.id)).size, 21);
  assert.equal(definitions.data.scenarios.length, 2);
  assert.equal(definitions.evaluation.scenarios.length, 2);
});

test("Canvas 与 Listening 状态组合保持 feature 内单一资源入口", () => {
  const definitions = buildRouteScenarioDefinitions();
  for (const scenario of definitions.canvas.scenarios) {
    assert.deepEqual(scenario.entryKeys, [routeManifestEntries.canvas], scenario.id);
    assert.deepEqual(scenario.dynamicEdges, [dynamicEdge(APP_ENTRY, routeManifestEntries.canvas)], scenario.id);
  }
  for (const scenario of definitions.listening.scenarios) {
    assert.deepEqual(scenario.entryKeys, [routeManifestEntries.listening], scenario.id);
    assert.deepEqual(scenario.dynamicEdges, [dynamicEdge(APP_ENTRY, routeManifestEntries.listening)], scenario.id);
  }
});

test("动态边核对同时报告漏登记和陈旧登记", () => {
  const definitions = buildRouteScenarioDefinitions();
  const fakeManifest = {
    "index.html": { dynamicImports: [APP_ENTRY] },
    [APP_ENTRY]: {
      dynamicImports: Object.values(routeManifestEntries).filter((entry) =>
        entry !== routeManifestEntries.calibration && entry !== routeManifestEntries.settings
      )
    },
    [routeManifestEntries.evaluation]: { dynamicImports: [routeManifestEntries.calibration] },
    unexpected: { dynamicImports: ["unexpected-child"] }
  };
  const result = compareDynamicEdgeCoverage(fakeManifest, definitions, initialDynamicEdges);
  assert.deepEqual(result.unregistered, [dynamicEdge("unexpected", "unexpected-child")]);
  assert.ok(result.stale.length > 0);
});
