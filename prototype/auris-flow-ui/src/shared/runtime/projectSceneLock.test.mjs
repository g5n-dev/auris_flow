import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./projectSceneLock.ts", import.meta.url);

async function loadLockModule() {
  const source = await readFile(sourceUrl, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

const validBinding = () => ({
  environment: "production",
  status: "active",
  scene_profile_id: "scene-a",
  scene_profile_version_id: "scenev-a-1",
  manifest_sha256: "a".repeat(64),
  version: {
    status: "published",
    scene_profile_id: "scene-a",
    scene_profile_version_id: "scenev-a-1",
    manifest_sha256: "a".repeat(64)
  }
});

test("only an exact published production binding yields a project scene lock", async () => {
  const { resolveProjectSceneLock } = await loadLockModule();

  assert.deepEqual(resolveProjectSceneLock(validBinding(), "bound").lock, {
    scene_profile_id: "scene-a",
    scene_profile_version_id: "scenev-a-1",
    scene_profile_snapshot_sha256: "a".repeat(64)
  });
});

test("pending, missing and drifted bindings fail closed", async () => {
  const { resolveProjectSceneLock } = await loadLockModule();
  const drifted = validBinding();
  drifted.version.scene_profile_version_id = "scenev-other";

  assert.equal(resolveProjectSceneLock(validBinding(), "pending").lock, null);
  assert.match(resolveProjectSceneLock(validBinding(), "pending").blockedReason, /正在读取/);
  assert.equal(resolveProjectSceneLock(null, "unbound").lock, null);
  assert.match(resolveProjectSceneLock(null, "unbound").blockedReason, /未绑定/);
  assert.equal(resolveProjectSceneLock(drifted, "bound").lock, null);
  assert.match(resolveProjectSceneLock(drifted, "bound").blockedReason, /漂移|不完整/);
});
