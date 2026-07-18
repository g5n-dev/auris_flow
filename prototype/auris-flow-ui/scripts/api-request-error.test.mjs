import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import ts from "typescript";

const clientUrl = new URL("../src/api/client.ts", import.meta.url);
const errorModuleUrl = new URL("../src/api/apiRequestError.ts", import.meta.url);

async function transpileModule(source, fileName) {
  return ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022
    },
    fileName
  }).outputText;
}

async function loadClient() {
  const errorModuleSource = await readFile(errorModuleUrl, "utf8");
  const errorModule = await transpileModule(errorModuleSource, errorModuleUrl.pathname);
  const errorModuleDataUrl = `data:text/javascript;base64,${Buffer.from(errorModule).toString("base64")}`;
  const source = (await readFile(clientUrl, "utf8"))
    .replaceAll("import.meta.env.VITE_API_BASE_URL", "undefined")
    .replaceAll("import.meta.env.VITE_DEMO_MODE", "undefined")
    .replaceAll('"./apiRequestError"', JSON.stringify(errorModuleDataUrl));
  const transpiled = await transpileModule(source, clientUrl.pathname);
  return import(`data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`);
}

async function captureRejectedRequest(client, body, status) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: false,
    status,
    json: async () => body
  });
  try {
    await client.apiRequest("/v1/test-resource");
    assert.fail("request should reject");
  } catch (error) {
    return error;
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("apiRequest 保留 Error/message 并结构化暴露后端错误字段", async () => {
  const client = await loadClient();
  client.setApiContext({ tenantId: "tenant-test", projectId: "project-test" });
  const details = { expected_generation: 3, actual_generation: 4, trace_id: "trace-details" };

  const error = await captureRejectedRequest(client, {
    meta: { trace_id: "trace-meta" },
    error: {
      code: "STALE_LABEL_VERSION",
      message: "标签版本已变化",
      details,
      retryable: true,
      status: 422,
      trace_id: "trace-error"
    }
  }, 409);

  assert.ok(error instanceof Error);
  assert.ok(error instanceof client.ApiRequestError);
  assert.equal(client.isApiRequestError(error), true);
  assert.equal(error.name, "ApiRequestError");
  assert.equal(error.message, "标签版本已变化");
  assert.equal(error.status, 409);
  assert.equal(error.code, "STALE_LABEL_VERSION");
  assert.deepEqual(error.details, details);
  assert.equal(error.retryable, true);
  assert.equal(error.traceId, "trace-meta");
});

test("apiRequest 的 trace 按 error、details 回退，畸形响应保持原有状态文案", async () => {
  const client = await loadClient();
  client.setApiContext({ tenantId: "tenant-test", projectId: "project-test" });

  const errorTrace = await captureRejectedRequest(client, {
    error: {
      message: "发布冲突",
      trace_id: "trace-error",
      details: { trace_id: "trace-details" }
    }
  }, 409);
  assert.equal(errorTrace.traceId, "trace-error");
  assert.equal(errorTrace.retryable, false);

  const detailsTrace = await captureRejectedRequest(client, {
    error: { details: { trace_id: "trace-details" } }
  }, 503);
  assert.equal(detailsTrace.message, "API 503");
  assert.equal(detailsTrace.status, 503);
  assert.equal(detailsTrace.traceId, "trace-details");
  assert.equal(detailsTrace.code, undefined);
  assert.equal(detailsTrace.retryable, false);
});
