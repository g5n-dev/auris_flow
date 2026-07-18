import assert from "node:assert/strict";
import test from "node:test";

import { parseLabelLifecycleSummary } from "./labelLifecycleSummary.ts";

test("reads an authoritative published lifecycle and production generation", () => {
  const summary = parseLabelLifecycleSummary({
    label_version_id: "lv_quote_v19",
    artifact_lifecycle: {
      status: "published",
      published_at: "2026-07-18T08:00:00+00:00",
      deprecated_at: null,
      archived_at: null,
      deprecation_reason: null
    },
    replacement: null,
    environment_activations: [{
      environment: "production",
      status: "active",
      generation: 7,
      active_deployment_id: "rd_quote_v19"
    }]
  });

  assert.equal(summary.status, "published");
  assert.deepEqual(summary.productionActivation, {
    state: "active",
    generation: 7,
    deploymentId: "rd_quote_v19"
  });
  assert.equal(summary.replacement.state, "none");
  assert.deepEqual(summary.issues, []);
});

test("keeps deprecated replacement, mapping and reason bound together", () => {
  const summary = parseLabelLifecycleSummary({
    id: "lv_quote_v18",
    artifact_lifecycle: {
      status: "deprecated",
      published_at: "2026-05-01T08:00:00Z",
      deprecated_at: "2026-07-18T09:30:00Z",
      archived_at: null,
      deprecation_reason: "报价口径升级"
    },
    replacement: {
      label_version_id: "lv_quote_v19",
      mapping_bundle_id: "lmb_quote_v18_to_v19"
    },
    environment_activations: []
  });

  assert.equal(summary.labelVersionId, "lv_quote_v18");
  assert.equal(summary.deprecationReason, "报价口径升级");
  assert.equal(summary.deprecatedAt, "2026-07-18T09:30:00Z");
  assert.deepEqual(summary.replacement, {
    state: "mapped",
    labelVersionId: "lv_quote_v19",
    mappingBundleId: "lmb_quote_v18_to_v19"
  });
  assert.equal(summary.productionActivation.state, "inactive");
});

test("fails closed for ambiguous heads and partial replacement bindings", () => {
  const summary = parseLabelLifecycleSummary({
    label_version_id: "lv_broken",
    artifact_lifecycle: { status: "published" },
    replacement: { label_version_id: "lv_next" },
    environment_activations: [
      { environment: "production", status: "active", generation: 3 },
      { environment: "prod", status: "active", generation: 4 }
    ]
  });

  assert.equal(summary.productionActivation.state, "ambiguous");
  assert.equal(summary.productionActivation.generation, null);
  assert.equal(summary.replacement.state, "incomplete");
  assert.equal(summary.replacement.labelVersionId, null);
  assert.ok(summary.issues.includes("production 激活指针不唯一"));
  assert.ok(summary.issues.includes("替代版本与映射包绑定不完整"));
});

test("does not invent lifecycle fields when the server omits them", () => {
  const summary = parseLabelLifecycleSummary({ id: "lv_legacy" });

  assert.equal(summary.status, null);
  assert.equal(summary.productionActivation.state, "unavailable");
  assert.equal(summary.replacement.state, "unavailable");
  assert.deepEqual(summary.issues, [
    "生命周期字段未返回",
    "production 激活信息未返回",
    "替代关系字段未返回"
  ]);
});
