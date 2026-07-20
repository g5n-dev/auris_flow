import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./dataTruthModel.ts", import.meta.url);

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

test("缺失或越界 session confidence 保持未提供，不被归零或猜测", async () => {
  const { normalizeSessionConfidence, formatSessionConfidence } = await loadModel();

  for (const value of [undefined, null, "0.82", Number.NaN, -0.1, 1.1]) {
    assert.equal(normalizeSessionConfidence(value), null);
  }
  assert.equal(normalizeSessionConfidence(0), 0);
  assert.equal(normalizeSessionConfidence(0.82), 0.82);
  assert.equal(formatSessionConfidence(null), "未提供");
  assert.equal(formatSessionConfidence(0.82), "82%");
});

test("session confidence 不得转换为数据资产质量", async () => {
  const { projectTruthAssetCatalog } = await loadModel();

  assert.deepEqual(
    projectTruthAssetCatalog({ assetKey: "auris/audio/raw_recordings", confidence: 0.99 }),
    {
      name: "auris/audio/raw_recordings",
      assetKey: "auris/audio/raw_recordings",
      quality: null
    }
  );
});

test("处理产物只接受 BFF 明确返回的白名单，不根据会话存在推断 VAD/ASR/raw wav", async () => {
  const { authoritativeProcessingProducts } = await loadModel();

  assert.deepEqual(authoritativeProcessingProducts({ confidence: 0.9 }), []);
  assert.deepEqual(
    authoritativeProcessingProducts({
      processing_products: ["vad", "asr_transcript", "raw_wav", "voice_segments", "vad", "unknown"]
    }),
    ["VAD", "ASR transcript", "raw wav", "voice_segments"]
  );
  assert.deepEqual(authoritativeProcessingProducts({ processing_products: "vad" }), []);
});
