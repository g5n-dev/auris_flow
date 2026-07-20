import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./sceneBindingScope.ts", import.meta.url);

async function loadScopeModule() {
  const source = await readFile(sourceUrl, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

test("a binding resolved for the previous project is never exposed to a new scope", async () => {
  const { resolveWorkspaceSceneResolution } = await loadScopeModule();
  const previousBinding = { project_id: "project-old" };

  const resolved = resolveWorkspaceSceneResolution({
    currentScopeKey: '["tenant-1","project-new"]',
    hasProject: true,
    resolution: {
      scopeKey: '["tenant-1","project-old"]',
      binding: previousBinding,
      state: "bound"
    }
  });

  assert.deepEqual(resolved, { binding: null, state: "pending" });
});

test("a binding is exposed only for its exact tenant/project scope", async () => {
  const { resolveWorkspaceSceneResolution } = await loadScopeModule();
  const binding = { project_id: "project-current" };
  const scopeKey = '["tenant-1","project-current"]';

  assert.deepEqual(
    resolveWorkspaceSceneResolution({
      currentScopeKey: scopeKey,
      hasProject: true,
      resolution: { scopeKey, binding, state: "bound" }
    }),
    { binding, state: "bound" }
  );
});

test("an empty project scope is unbound rather than indefinitely pending", async () => {
  const { resolveWorkspaceSceneResolution } = await loadScopeModule();

  assert.deepEqual(
    resolveWorkspaceSceneResolution({
      currentScopeKey: '["tenant-1",""]',
      hasProject: false,
      resolution: {
        scopeKey: '["tenant-1","project-old"]',
        binding: { project_id: "project-old" },
        state: "bound"
      }
    }),
    { binding: null, state: "unbound" }
  );
});
