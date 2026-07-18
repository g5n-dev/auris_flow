import assert from "node:assert/strict";
import test from "node:test";

import { replaceProductionFixtureUrl } from "./select-production-fixtures.mjs";

test("只替换 new URL 中的生产 fixture，不改变类型契约 import", () => {
  const source = [
    'import type schema from "./data/canvas-fixtures.json";',
    'const url = new URL("./data/canvas-fixtures.json", import.meta.url);'
  ].join("\n");
  const output = replaceProductionFixtureUrl(
    source,
    "./data/canvas-fixtures.json",
    "./production/canvas-fixtures.json"
  );
  assert.match(output, /import type schema from "\.\/data\/canvas-fixtures\.json"/);
  assert.match(output, /new URL\("\.\/production\/canvas-fixtures\.json"/);
});

test("普通字符串和相似路径保持不变", () => {
  const source = [
    'const path = "./data/listening-fixtures.json";',
    'const url = new URL("./data/listening-fixtures.json.bak", import.meta.url);'
  ].join("\n");
  assert.equal(
    replaceProductionFixtureUrl(source, "./data/listening-fixtures.json", "./production/listening-fixtures.json"),
    source
  );
});
