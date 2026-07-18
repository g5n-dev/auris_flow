import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const sourceUrl = new URL("./buildInsightAgentAction.ts", import.meta.url);

test("N/A 或未物化指标在创建数值目标前 fail-closed", async () => {
  const source = await readFile(sourceUrl, "utf8");
  const guardIndex = source.indexOf("selectedMetric.valueNumber === null");
  const targetIndex = source.indexOf("target_value: selectedMetric.valueNumber + 2");

  assert(guardIndex >= 0, "缺少 N/A 数值目标门禁");
  assert(targetIndex > guardIndex, "数值目标必须在 null 门禁之后构造");
  assert.match(source, /当前为 N\/A 或未物化.*禁止创建数值目标/);
});
