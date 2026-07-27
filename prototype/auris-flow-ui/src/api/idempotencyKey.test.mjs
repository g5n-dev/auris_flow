import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./idempotencyKey.ts", import.meta.url);
const BACKEND_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

async function loadIdempotencyKeyModule() {
  const source = await readFile(sourceUrl, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString("base64")}`);
}

test("后端已接受的合法 key 保持原值，包括 128 字符边界", async () => {
  const { normalizeIdempotencyKey, normalizeCallerIdempotencyKey } =
    await loadIdempotencyKeyModule();
  const readable = "label_version:intent:018f7d48-0fa1-7a50-9123-0123456789ab";
  const boundary = `a${"b".repeat(127)}`;

  assert.equal(normalizeIdempotencyKey(readable), readable);
  assert.equal(normalizeCallerIdempotencyKey(`  ${readable}  `), readable);
  assert.equal(normalizeIdempotencyKey(boundary), boundary);
});

test("Unicode、斜杠、百分号、空格和超长 key 均收敛到后端约束", async () => {
  const { normalizeIdempotencyKey } = await loadIdempotencyKeyModule();
  const cases = [
    "知识库/来源:同步",
    "asset/source",
    "asset%source",
    "asset source",
    `asset-${"x".repeat(200)}`
  ];

  for (const value of cases) {
    const normalized = normalizeIdempotencyKey(value);
    assert.match(normalized, BACKEND_KEY_PATTERN);
    assert.ok(normalized.length <= 128);
    assert.match(normalized, /-[a-f0-9]{64}$/);
    assert.ok(!normalized.includes("%"));
  }
});

test("相同输入稳定，不同原值即使可读前缀相同也由 SHA-256 指纹区分", async () => {
  const { normalizeIdempotencyKey } = await loadIdempotencyKeyModule();
  const variants = ["asset/source", "asset source", "asset%source"];
  const normalized = variants.map(normalizeIdempotencyKey);

  assert.equal(normalizeIdempotencyKey(variants[0]), normalized[0]);
  assert.equal(new Set(normalized).size, variants.length);
  assert.ok(normalized.every((value) => value.startsWith("asset-source-")));
  assert.ok(
    normalized[0].endsWith(
      createHash("sha256").update(variants[0], "utf8").digest("hex")
    )
  );
});

test("caller-supplied key 与动态 scope 使用同一规范化边界", async () => {
  const {
    composeIdempotencyKey,
    normalizeCallerIdempotencyKey,
    resolveWriteIdempotencyKey
  } = await loadIdempotencyKeyModule();
  const callerKey = normalizeCallerIdempotencyKey("  重试/键 %  ");
  const scopedKey = composeIdempotencyKey(
    "label_evaluation_lock_标签/版本 1",
    "intent",
    "stable-intent-id"
  );
  let intentFactoryCalls = 0;
  const resolvedCallerKey = resolveWriteIdempotencyKey(
    "ignored/scope",
    " legal-retry:key ",
    () => {
      intentFactoryCalls += 1;
      return "unused";
    }
  );
  const resolvedScopedKey = resolveWriteIdempotencyKey(
    "audio_playback_grant_会话/1",
    undefined,
    () => "stable-intent-id"
  );

  assert.match(callerKey, BACKEND_KEY_PATTERN);
  assert.match(scopedKey, BACKEND_KEY_PATTERN);
  assert.ok(scopedKey.startsWith("label_evaluation_lock_-1:intent:stable-intent-id-"));
  assert.equal(resolvedCallerKey, "legal-retry:key");
  assert.equal(intentFactoryCalls, 0);
  assert.match(resolvedScopedKey, BACKEND_KEY_PATTERN);
  assert.ok(resolvedScopedKey.startsWith("audio_playback_grant_-1:intent:stable-intent-id-"));
});
