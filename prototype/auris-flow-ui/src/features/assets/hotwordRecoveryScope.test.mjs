import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const sourceUrl = new URL("./hotwordRecoveryScope.ts", import.meta.url);

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

test("hotword recovery 只对精确 tenant/project scope 暴露，切换首帧立即清空旧 binding", async () => {
  const { resolveHotwordRecoveryForScope } = await loadModel();
  const prior = {
    scopeKey: "tenant-a/project-a",
    status: "ready",
    reason: "ready",
    binding: {
      hotwordPackVersionId: "pack-a",
      evalRunId: "eval-a",
      taskVersionId: "task-a",
      rootTraceId: "trace-a",
      sourceAsset: "auris/model/asr_transcripts",
      sourceMaterializationId: "mat-a",
      sourceBadcaseIds: ["case-a"]
    }
  };

  assert.strictEqual(resolveHotwordRecoveryForScope(prior, "tenant-a/project-a"), prior);
  assert.deepEqual(resolveHotwordRecoveryForScope(prior, "tenant-b/project-b"), {
    scopeKey: "tenant-b/project-b",
    status: "loading",
    reason: "正在读取当前租户项目的已发布词包与权威 ASR 物化。"
  });
});

test("旧 scope 的 hotword/backfill UI 草稿不得在新项目成为可提交草稿", async () => {
  const { backfillDraftForScope } = await loadModel();
  const priorDraft = {
    scopeKey: "tenant-a/project-a",
    assetKey: "auris/model/asr_transcripts",
    assetName: "ASR",
    draftId: "draft-a",
    reason: "hotword",
    status: "草稿"
  };

  assert.strictEqual(backfillDraftForScope(priorDraft, "tenant-a/project-a"), priorDraft);
  assert.equal(backfillDraftForScope(priorDraft, "tenant-b/project-b"), null);
});
