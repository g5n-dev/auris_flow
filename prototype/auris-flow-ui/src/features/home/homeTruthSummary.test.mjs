import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const sourceUrl = new URL("./homeTruthSummary.ts", import.meta.url);

const loadModel = async () => {
  const source = await readFile(sourceUrl, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext }
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);
};

test("首页只从 ops-summary 生成运行中、待处理、失败、最新导入四类摘要", async () => {
  const { buildHomeTruthSummary } = await loadModel();
  const cards = buildHomeTruthSummary({
    running_count: 2,
    pending_count: 7,
    model_anomaly_count: 3,
    sessions: [
      {
        audio_session_id: "audio-session-001",
        import_batch_id: "batch-001",
        started_at: "2026-07-28T09:30:00+08:00"
      }
    ]
  });

  assert.deepEqual(cards.map((card) => card.key), [
    "running",
    "pending",
    "failed",
    "latest_import"
  ]);
  assert.deepEqual(cards.map((card) => card.value), ["2", "7", "3", "1"]);
  assert.match(cards[3].detail, /audio-session-001/);
  assert.ok(cards.every((card) => card.source === "bff"));
});

test("BFF 未提供字段时 fail closed，不用 fixture 补数", async () => {
  const { buildHomeTruthSummary } = await loadModel();
  const cards = buildHomeTruthSummary({});
  assert.deepEqual(cards.map((card) => card.value), ["—", "—", "—", "0"]);
  assert.match(cards[0].detail, /BFF 未提供/);
  assert.match(cards[1].detail, /BFF 未提供/);
  assert.match(cards[2].detail, /BFF 未提供/);
  assert.match(cards[3].detail, /尚无导入会话/);
});
