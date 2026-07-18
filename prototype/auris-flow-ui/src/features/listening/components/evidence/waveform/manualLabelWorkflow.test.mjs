import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const sourceUrl = new URL("./manualLabelWorkflow.ts", import.meta.url);

const loadModel = async () => {
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }
  }).outputText;
  const withoutTypeImport = compiled.replace(/^import[^;]+;\s*/u, "");
  return import(`data:text/javascript;base64,${Buffer.from(withoutTypeImport).toString("base64")}`);
};

test("只在标签 ID、名称或别名唯一匹配时自动选择", async () => {
  const { matchLabelVersionItem } = await loadModel();
  const items = [
    { label_id: "qa.amount_conflict", canonical_name: "金额冲突", aliases: ["报价冲突"] },
    { label_id: "qa.low_confidence", canonical_name: "低置信", aliases: [] }
  ];
  assert.equal(matchLabelVersionItem(items, "qa.amount_conflict", "任意").label_id, "qa.amount_conflict");
  assert.equal(matchLabelVersionItem(items, "", "报价冲突").label_id, "qa.amount_conflict");
  assert.equal(matchLabelVersionItem(items, "", "未知"), null);
});

test("按冻结 value_type 严格转换，不能把任意文本当布尔或数值", async () => {
  const { parseManualLabelValue } = await loadModel();
  assert.equal(parseManualLabelValue("boolean", "是", 10, 20), true);
  assert.equal(parseManualLabelValue("numeric", "281900", 10, 20), 281900);
  assert.deepEqual(parseManualLabelValue("multi", "报价, 优惠，报价", 10, 20), ["报价", "优惠"]);
  assert.deepEqual(parseManualLabelValue("temporal", "", 10, 20), { start_ms: 10, end_ms: 20 });
  assert.throws(() => parseManualLabelValue("boolean", "也许", 10, 20), /布尔标签值/);
  assert.throws(() => parseManualLabelValue("numeric", "28万", 10, 20), /有限数字/);
});

test("occurred_at 优先使用会话起点，否则只接受可验证的 session 日期与区域时钟", async () => {
  const { resolveManualLabelOccurredAt } = await loadModel();
  assert.equal(
    resolveManualLabelOccurredAt("2025-05-26T04:23:00Z", "S20250526-000128", "12:27:18", 258000),
    "2025-05-26T04:27:18.000Z"
  );
  assert.equal(
    resolveManualLabelOccurredAt(undefined, "S20250526-000128", "12:27:18", 0),
    "2025-05-26T04:27:18.000Z"
  );
  assert.equal(resolveManualLabelOccurredAt(undefined, "未选择会话", "--:--", 0), null);
});

test("证据哈希确定、映射只读 replacement 强绑定且 rebase ID 有界", async () => {
  const { mappingBundleFromLifecycle, rebasedAnnotationId, sha256Evidence } = await loadModel();
  const document = { region_id: "region-1", end_ms: 20, start_ms: 10 };
  const first = await sha256Evidence(document);
  const second = await sha256Evidence({ start_ms: 10, region_id: "region-1", end_ms: 20 });
  assert.match(first, /^[0-9a-f]{64}$/);
  assert.equal(first, second);
  assert.equal(mappingBundleFromLifecycle({ replacement: { mapping_bundle_id: "bundle-v1-v2" } }), "bundle-v1-v2");
  assert.equal(mappingBundleFromLifecycle({ replacement: { label_version_id: "v2" } }), "");
  assert.ok(rebasedAnnotationId("a".repeat(128), 42).length <= 128);
});
