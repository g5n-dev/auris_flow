import assert from "node:assert/strict";
import test from "node:test";

import {
  EXPECTED_ASSET_READ_ABORT_REASON,
  validateExpectedAssetReadCancellations
} from "./platform-bff-request-failure-policy.mjs";

const baseUrl = "http://127.0.0.1:5173/";
const demoBaseUrl = "http://127.0.0.1:5174/";
const runId = "e2e-policy-test";
const scope = { tenantId: "aurora_auto", projectId: "sales_qa" };

function cancellation({
  origin = "http://127.0.0.1:5174",
  path = "/api/v1/data-assets/auris%2Fmodel%2Fasr_transcripts",
  action = "hotword-governance-authoritative-asset-detail",
  eventSequence = 1,
  actionEpoch = `${runId}:${action}:${eventSequence}`
} = {}) {
  const url = `${origin}${path}`;
  return {
    method: "GET",
    resourceType: "fetch",
    url,
    origin,
    path,
    action,
    actionEpoch,
    scope,
    failure: "net::ERR_ABORTED",
    reason: EXPECTED_ASSET_READ_ABORT_REASON,
    eventSequence,
    recovery: {
      method: "GET",
      resourceType: "fetch",
      url,
      origin,
      path,
      action,
      actionEpoch,
      scope,
      status: 200,
      completed: true,
      eventSequence: eventSequence + 1
    }
  };
}

test("只接受已知源和资产、固定 scope 且有后继完整 200 的取消", () => {
  const validation = validateExpectedAssetReadCancellations({
    baseUrl,
    demoBaseUrl,
    runId,
    expectedRequestFailures: [cancellation()]
  });
  assert.deepEqual(validation, {
    invalidExpectedRequestFailures: [],
    policyViolations: []
  });
});

test("未知源、缺失恢复和伪造 scope 均 fail closed", () => {
  const unknownOrigin = cancellation({ origin: "https://untrusted.example" });
  const missingRecovery = cancellation();
  delete missingRecovery.recovery;
  const wrongScope = cancellation();
  wrongScope.recovery = {
    ...wrongScope.recovery,
    scope: { tenantId: "other", projectId: "other" }
  };
  const consistentlyWrongScope = cancellation({ eventSequence: 3 });
  consistentlyWrongScope.scope = { tenantId: "other", projectId: "other" };
  consistentlyWrongScope.recovery = {
    ...consistentlyWrongScope.recovery,
    scope: { tenantId: "other", projectId: "other" }
  };
  const validation = validateExpectedAssetReadCancellations({
    baseUrl,
    demoBaseUrl,
    runId,
    expectedRequestFailures: [unknownOrigin, missingRecovery, wrongScope, consistentlyWrongScope]
  });
  assert.equal(validation.invalidExpectedRequestFailures.length, 4);
});

test("同一网络事件不能被复制为两份取消或恢复证明", () => {
  const duplicated = cancellation();
  const validation = validateExpectedAssetReadCancellations({
    baseUrl,
    demoBaseUrl,
    runId,
    expectedRequestFailures: [cancellation(), duplicated]
  });
  assert.equal(validation.invalidExpectedRequestFailures.length, 1);
});

test("每个取消必须使用唯一且格式精确的动作 epoch", () => {
  const reusedEpoch = cancellation({ eventSequence: 3, actionEpoch: `${runId}:hotword-governance-authoritative-asset-detail:1` });
  const malformedEpoch = cancellation({
    eventSequence: 5,
    actionEpoch: `${runId}:hotword-governance-authoritative-asset-detail:extra:5`
  });
  const validation = validateExpectedAssetReadCancellations({
    baseUrl,
    demoBaseUrl,
    runId,
    expectedRequestFailures: [cancellation(), reusedEpoch, malformedEpoch]
  });
  assert.equal(validation.invalidExpectedRequestFailures.length, 2);
});

test("复制大量白名单取消会同时触发总量和单目标预算", () => {
  const expectedRequestFailures = Array.from({ length: 1000 }, (_, index) =>
    cancellation({ eventSequence: index * 2 + 1 })
  );
  const validation = validateExpectedAssetReadCancellations({
    baseUrl,
    demoBaseUrl,
    runId,
    expectedRequestFailures
  });
  assert.deepEqual(
    validation.policyViolations.map((item) => item.code).sort(),
    [
      "EXPECTED_CANCELLATION_TARGET_BUDGET_EXCEEDED",
      "EXPECTED_CANCELLATION_TOTAL_BUDGET_EXCEEDED"
    ]
  );
});

test("缺失 expectedRequestFailures 字段必须 fail closed", () => {
  const validation = validateExpectedAssetReadCancellations({ baseUrl, demoBaseUrl, runId });
  assert.deepEqual(validation.policyViolations, [
    { code: "EXPECTED_CANCELLATIONS_FIELD_REQUIRED" }
  ]);
});
