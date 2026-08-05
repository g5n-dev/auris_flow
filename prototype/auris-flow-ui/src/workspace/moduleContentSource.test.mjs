import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./moduleContentSource.ts", import.meta.url);

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

test("静态或混合实现模块在 truth mode 即使摘要同步也不得声明 BFF 内容权威", async () => {
  const { resolveModuleContentSource } = await loadModel();

  for (const moduleKey of ["settings", "evaluation", "insights"]) {
    assert.equal(resolveModuleContentSource({ moduleKey, projectionStatus: "synced", demoMode: false }), "none");
    assert.equal(resolveModuleContentSource({ moduleKey, projectionStatus: "empty", demoMode: false }), "none");
  }
});

test("只有显式绑定当前 projection items 的模块在同步后可声明 BFF 内容权威", async () => {
  const { resolveModuleContentSource } = await loadModel();

  assert.equal(resolveModuleContentSource({ moduleKey: "assets", projectionStatus: "synced", demoMode: false }), "bff");
  assert.equal(resolveModuleContentSource({ moduleKey: "data", projectionStatus: "synced", demoMode: false }), "bff");
  assert.equal(resolveModuleContentSource({ moduleKey: "home", projectionStatus: "synced", demoMode: false }), "bff");
  assert.equal(resolveModuleContentSource({ moduleKey: "assets", projectionStatus: "empty", demoMode: false }), "none");
});

test("demo mode 的降级或同步投影明确标记 mock", async () => {
  const { resolveModuleContentSource } = await loadModel();

  assert.equal(resolveModuleContentSource({ moduleKey: "settings", projectionStatus: "synced", demoMode: true }), "mock");
  assert.equal(resolveModuleContentSource({ moduleKey: "home", projectionStatus: "synced", demoMode: true }), "mock");
  assert.equal(resolveModuleContentSource({ moduleKey: "home", projectionStatus: "degraded", demoMode: true }), "mock");
  assert.equal(resolveModuleContentSource({ moduleKey: "home", projectionStatus: "pending", demoMode: true }), "none");
});

test("production truth 的 none 内容源必须 fail closed，不得挂载非权威模块详情", async () => {
  const { resolveModuleDetailVisibility } = await loadModel();

  for (const moduleKey of ["settings", "evaluation", "insights"]) {
    const contentSource = "none";
    assert.deepEqual(
      resolveModuleDetailVisibility({
        moduleKey,
        projectionStatus: "synced",
        contentSource,
        demoMode: false
      }),
      { renderDetails: false, detailsUnavailable: true }
    );
  }
});
