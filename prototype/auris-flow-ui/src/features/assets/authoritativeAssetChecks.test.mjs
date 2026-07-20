import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./authoritativeAssetChecks.ts", import.meta.url);

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

const assetKey = "auris/label/event_tags";

const validDetail = {
  asset_key: assetKey,
  display_name: "事件标签资产",
  checks: [
    {
      check_id: "check-event-schema",
      name: "Schema 稳定性",
      status: "failed",
      failed_partitions: ["2026-07-20/store-a", "2026-07-20/store-b"]
    },
    {
      check_id: "check-event-freshness",
      name: "新鲜度",
      status: "passed",
      failed_partitions: []
    },
    {
      check_id: "check-event-lineage",
      name: "血缘完整性",
      status: "passed",
      failed_partitions: ["2026-07-20/store-c"]
    }
  ]
};

test("严格读取当前资产 checks，并只选择明确失败 ID 与分区", async () => {
  const { parseAuthoritativeAssetChecks, failedAssetCheckSelection } = await loadModel();
  const result = parseAuthoritativeAssetChecks(validDetail, assetKey);

  assert.equal(result.ok, true);
  assert.equal(result.value.assetKey, assetKey);
  assert.deepEqual(result.value.checks.map((check) => check.id), [
    "check-event-schema",
    "check-event-freshness",
    "check-event-lineage"
  ]);
  assert.deepEqual(failedAssetCheckSelection(result.value), {
    failedCheckIds: ["check-event-schema", "check-event-lineage"],
    failedPartitions: ["2026-07-20/store-a", "2026-07-20/store-b", "2026-07-20/store-c"]
  });
});

test("scope 错配、弱 check、未知状态、非法或重复分区全部 fail closed", async () => {
  const { parseAuthoritativeAssetChecks } = await loadModel();

  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, asset_key: "foreign/asset" }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, checks: "failed" }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, checks: [{ name: "无 ID", status: "failed", failed_partitions: [] }] }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, checks: [{ check_id: "c1", name: "未知", status: "mystery", failed_partitions: [] }] }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, checks: [{ check_id: "c1", name: "非法分区", status: "failed", failed_partitions: ["", 42] }] }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, checks: [{ check_id: "c1", name: "重复分区", status: "failed", failed_partitions: ["p1", "p1"] }] }, assetKey).ok, false);
  assert.equal(parseAuthoritativeAssetChecks({ ...validDetail, checks: [validDetail.checks[0], { ...validDetail.checks[0] }] }, assetKey).ok, false);
});

test("重跑门禁同时依赖权威失败事实和 Scene lock", async () => {
  const {
    assetCheckRetryDecision,
    assetChecksStateReducer,
    initialAssetChecksState,
    parseAuthoritativeAssetChecks
  } = await loadModel();
  const parsed = parseAuthoritativeAssetChecks(validDetail, assetKey);
  assert.equal(parsed.ok, true);
  const ready = assetChecksStateReducer(
    assetChecksStateReducer(initialAssetChecksState, { type: "begin", assetKey, scopeKey: "tenant-a/project-a", requestKey: `${assetKey}#1` }),
    { type: "ready", requestKey: `${assetKey}#1`, value: parsed.value }
  );

  assert.deepEqual(assetCheckRetryDecision(ready, false, "Scene 未绑定"), {
    enabled: false,
    blockedReason: "Scene 未绑定",
    selection: null
  });
  assert.deepEqual(assetCheckRetryDecision(ready, true, ""), {
    enabled: true,
    blockedReason: "",
    selection: {
      failedCheckIds: ["check-event-schema", "check-event-lineage"],
      failedPartitions: ["2026-07-20/store-a", "2026-07-20/store-b", "2026-07-20/store-c"]
    }
  });

  const noFailure = {
    ...ready,
    value: {
      ...ready.value,
      checks: [{ id: "check-ok", name: "通过", status: "passed", failedPartitions: [] }]
    }
  };
  assert.match(assetCheckRetryDecision(noFailure, true, "").blockedReason, /没有失败/);
  assert.match(assetCheckRetryDecision({ ...ready, status: "loading", value: null }, true, "").blockedReason, /正在读取/);
  assert.match(assetCheckRetryDecision({ ...ready, status: "error", value: null, reason: "invalid" }, true, "").blockedReason, /invalid/);
  assert.match(assetCheckRetryDecision({ ...ready, status: "empty", value: { ...ready.value, checks: [] } }, true, "").blockedReason, /未返回 checks/);
});

test("切换资产首帧与旧请求完成都不能泄漏旧 checks", async () => {
  const {
    assetChecksStateReducer,
    initialAssetChecksState,
    readChecksStateForSelectedAsset
  } = await loadModel();
  const first = assetChecksStateReducer(initialAssetChecksState, { type: "begin", assetKey: "asset-a", scopeKey: "tenant-a/project-a", requestKey: "asset-a#1" });
  const second = assetChecksStateReducer(first, { type: "begin", assetKey: "asset-b", scopeKey: "tenant-a/project-a", requestKey: "asset-b#2" });
  const stale = assetChecksStateReducer(second, {
    type: "ready",
    requestKey: "asset-a#1",
    value: { assetKey: "asset-a", label: "A", traceId: null, checks: [] }
  });

  assert.strictEqual(stale, second);
  assert.deepEqual(readChecksStateForSelectedAsset(second, "asset-c", "tenant-a/project-a"), {
    assetKey: "asset-c",
    scopeKey: "tenant-a/project-a",
    requestKey: "",
    status: "loading",
    value: null,
    reason: ""
  });
  assert.deepEqual(readChecksStateForSelectedAsset(second, "asset-b", "tenant-b/project-b"), {
    assetKey: "asset-b",
    scopeKey: "tenant-b/project-b",
    requestKey: "",
    status: "loading",
    value: null,
    reason: ""
  });
});
