import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./authoritativeAssetLineage.ts", import.meta.url);

async function loadModel() {
  const source = await readFile(sourceUrl, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

const assetKey = "auris/model/asr_transcripts";

test("只接受与当前 asset_key 一致且 nodes/edges/materializations 可验证的 lineage envelope", async () => {
  const { parseAuthoritativeAssetLineage } = await loadModel();
  const raw = {
    asset: { asset_key: assetKey, display_name: "ASR 转写资产", trace_id: "trace-asset" },
    nodes: [
      { asset_key: "auris/audio/raw_recordings", label: "原始音频", node_type: "asset", direction: "upstream", trace_id: "trace-up" },
      { asset_key: assetKey, label: "ASR 转写资产", node_type: "asset", direction: "current", trace_id: "trace-asset" },
      { asset_key: "mat-asr-1", label: "mat-asr-1", node_type: "materialization", direction: "runtime", run_id: "run-asr-1", trace_id: "trace-mat" }
    ],
    edges: [
      { edge_id: "edge-1", from: "auris/audio/raw_recordings", to: assetKey, direction: "upstream", lineage_source: "data_asset_projection", trace_id: "trace-edge" },
      { edge_id: "edge-2", from: "mat-asr-1", to: assetKey, direction: "materialized", lineage_source: "asset_materialization", trace_id: "trace-mat" }
    ],
    materializations: [
      { materialization_id: "mat-asr-1", asset_key: assetKey, run_id: "run-asr-1", status: "success", trace_id: "trace-mat" }
    ]
  };

  const result = parseAuthoritativeAssetLineage(raw, assetKey);
  assert.equal(result.ok, true);
  assert.equal(result.value.asset.assetKey, assetKey);
  assert.equal(result.value.nodes.length, 3);
  assert.equal(result.value.edges[1].source, "asset_materialization");
  assert.equal(result.value.materializations[0].id, "mat-asr-1");
});

test("响应 scope 错配、缺结构或边端点不存在时 fail closed", async () => {
  const { parseAuthoritativeAssetLineage } = await loadModel();
  const base = {
    asset: { asset_key: assetKey },
    nodes: [{ asset_key: assetKey, label: "ASR", node_type: "asset" }],
    edges: [],
    materializations: []
  };

  assert.equal(parseAuthoritativeAssetLineage({ ...base, asset: { asset_key: "foreign/asset" } }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetLineage({ ...base, nodes: "invalid" }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetLineage({ ...base, edges: [{ from: "missing", to: assetKey }] }, assetKey).ok, false);
});

test("同一 asset_key 的旧请求代次也不能覆盖当前状态", async () => {
  const { assetLineageStateReducer, initialAssetLineageState } = await loadModel();
  const first = assetLineageStateReducer(initialAssetLineageState, {
    type: "begin",
    assetKey: "asset-a",
    scopeKey: "tenant-a/project-a",
    requestKey: "tenant-a/project-a:asset-a#1"
  });
  const second = assetLineageStateReducer(first, {
    type: "begin",
    assetKey: "asset-a",
    scopeKey: "tenant-a/project-a",
    requestKey: "tenant-a/project-a:asset-a#2"
  });
  const staleReady = assetLineageStateReducer(second, {
    type: "ready",
    requestKey: "tenant-a/project-a:asset-a#1",
    value: { asset: { assetKey: "asset-a", label: "A", traceId: null }, nodes: [], edges: [], materializations: [] }
  });
  const staleError = assetLineageStateReducer(second, {
    type: "error",
    requestKey: "tenant-a/project-a:asset-a#1",
    reason: "late"
  });

  assert.strictEqual(staleReady, second);
  assert.strictEqual(staleError, second);
  assert.equal(second.status, "loading");
  assert.equal(second.requestKey, "tenant-a/project-a:asset-a#2");
});

test("切换 asset_key 后 effect 执行前也不得渲染上一资产的 ready 内容", async () => {
  const { readStateForSelectedAsset } = await loadModel();
  const priorReady = {
    assetKey: "asset-a",
    requestKey: "asset-a#1",
    status: "ready",
    value: { asset: { assetKey: "asset-a", label: "A", traceId: null }, nodes: [], edges: [], materializations: [] },
    reason: ""
  };

  priorReady.scopeKey = "tenant-a/project-a";
  assert.strictEqual(readStateForSelectedAsset(priorReady, "asset-a", "tenant-a/project-a"), priorReady);
  assert.deepEqual(readStateForSelectedAsset(priorReady, "asset-b", "tenant-a/project-a"), {
    assetKey: "asset-b",
    scopeKey: "tenant-a/project-a",
    requestKey: "",
    status: "loading",
    value: null,
    reason: ""
  });
});

test("同一 asset_key 切换 tenant/project scope 后首帧也不得渲染上一范围 lineage", async () => {
  const { readStateForSelectedAsset } = await loadModel();
  const priorReady = {
    assetKey: "asset-a",
    scopeKey: "tenant-a/project-a",
    requestKey: "tenant-a/project-a:asset-a#1",
    status: "ready",
    value: { asset: { assetKey: "asset-a", label: "A", traceId: null }, nodes: [], edges: [], materializations: [] },
    reason: ""
  };

  assert.deepEqual(readStateForSelectedAsset(priorReady, "asset-a", "tenant-b/project-b"), {
    assetKey: "asset-a",
    scopeKey: "tenant-b/project-b",
    requestKey: "",
    status: "loading",
    value: null,
    reason: ""
  });
});

test("只有当前资产节点而没有边和物化时进入权威 empty 状态", async () => {
  const { assetLineageIsEmpty } = await loadModel();
  const currentOnly = {
    asset: { assetKey, label: "ASR", traceId: null },
    nodes: [{ id: assetKey }],
    edges: [],
    materializations: []
  };

  assert.equal(assetLineageIsEmpty(currentOnly), true);
  assert.equal(assetLineageIsEmpty({ ...currentOnly, edges: [{ id: "edge" }] }), false);
  assert.equal(assetLineageIsEmpty({ ...currentOnly, materializations: [{ id: "mat" }] }), false);
});
