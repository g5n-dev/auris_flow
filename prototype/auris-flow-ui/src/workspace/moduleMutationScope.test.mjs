import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./moduleMutationScope.ts", import.meta.url);

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

test("mutation ledger 只暴露当前 tenant/project scope 的记录", async () => {
  const { recordsForMutationScope } = await loadModel();
  const records = [{ id: "record-a", scopeKey: "tenant-a/project-a" }];

  assert.strictEqual(recordsForMutationScope(records, "tenant-a/project-a"), records);
  assert.deepEqual(recordsForMutationScope(records, "tenant-b/project-b"), []);
});

test("同一 mutation record 的首次提交与 retry 复用原 user intent idempotency key", async () => {
  const { mutationWriteOptions } = await loadModel();
  const record = {
    id: "record-a",
    scopeKey: "tenant-a/project-a",
    idempotencyKey: "module_write:intent:stable-a"
  };

  assert.deepEqual(mutationWriteOptions(record), { idempotencyKey: "module_write:intent:stable-a" });
  assert.deepEqual(mutationWriteOptions(record), { idempotencyKey: "module_write:intent:stable-a" });
});
