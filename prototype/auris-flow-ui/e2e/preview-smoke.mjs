import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { createServer as createHttpServer, request as requestHttp } from "node:http";
import { fileURLToPath } from "node:url";
import { brotliDecompressSync } from "node:zlib";
import { chromium } from "playwright";
import { preview } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const distIndex = fileURLToPath(new URL("../dist/index.html", import.meta.url));
const distManifest = fileURLToPath(new URL("../dist/.vite/manifest.json", import.meta.url));
const distBrotliManifest = fileURLToPath(new URL("../dist/.vite/brotli-manifest.json", import.meta.url));
const smokeSessionToken = "auris.v1.preview-smoke.server-issued";
const previewOidcCookie = "preview-oidc-cookie-session";
let oidcLoginRequests = 0;
let expectedInitialAuthConsoleErrors = 0;

function assert(condition, message, detail = undefined) {
  if (!condition) {
    const error = new Error(message);
    if (detail !== undefined) error.detail = detail;
    throw error;
  }
}

async function waitForSignalQuietPeriod(
  signals,
  cursor,
  { quietMs = 750, timeoutMs = 5000, pollMs = 25 } = {}
) {
  const deadline = Date.now() + timeoutMs;
  let observedLength = signals.length;
  let quietSince = Date.now();
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    if (signals.length !== observedLength) {
      observedLength = signals.length;
      quietSince = Date.now();
    }
    if (Date.now() - quietSince >= quietMs) return signals.slice(cursor);
  }
  throw new Error(`浏览器信号在 ${timeoutMs}ms 内未进入 ${quietMs}ms 静默期`);
}

function assertNoUnexpectedRecoverySignals(signals, detail = undefined) {
  assert(
    !signals.some((signal) =>
      ["requestfailed", "pageerror", "console", "failed-response"].includes(signal.kind)
    ),
    "知识库恢复后仍产生浏览器错误或失败请求",
    detail ?? { signals }
  );
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function responseHeader(response, name) {
  const value = response.headers[name.toLowerCase()];
  return Array.isArray(value) ? value.join(", ") : value;
}

function assertVaryAcceptEncoding(response, detail) {
  const vary = responseHeader(response, "vary") ?? "";
  assert(
    vary.split(",").some((value) => value.trim().toLowerCase() === "accept-encoding"),
    "预览资源缺少 Vary: Accept-Encoding",
    { ...detail, vary }
  );
}

function resourceRequestPath(resourcePath) {
  if (resourcePath === "index.html") return "/";
  return `/${resourcePath.split("/").map(encodeURIComponent).join("/")}`;
}

function requestRaw(baseUrl, resourcePath, acceptEncoding) {
  const url = new URL(resourceRequestPath(resourcePath), baseUrl);
  return new Promise((resolve, reject) => {
    const request = requestHttp(url, {
      method: "GET",
      headers: { "Accept-Encoding": acceptEncoding }
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        statusCode: response.statusCode,
        headers: response.headers,
        body: Buffer.concat(chunks)
      }));
      response.on("error", reject);
    });
    request.setTimeout(10000, () => request.destroy(new Error(`请求预览资源超时：${url.pathname}`)));
    request.on("error", reject);
    request.end();
  });
}

async function verifyPrecompressedTransport(baseUrl, brotliManifest, identityResourcePaths) {
  assert(brotliManifest?.schemaVersion === 1, "Brotli manifest schemaVersion 非法", {
    distBrotliManifest,
    schemaVersion: brotliManifest?.schemaVersion
  });
  assert(brotliManifest?.algorithm === "br" && brotliManifest?.quality === 11, "Brotli manifest 算法或质量非法", {
    distBrotliManifest,
    algorithm: brotliManifest?.algorithm,
    quality: brotliManifest?.quality
  });
  const entries = Object.entries(brotliManifest?.entries ?? {});
  assert(entries.length > 0, "Brotli manifest 没有生产资源", { distBrotliManifest });

  await Promise.all(entries.map(async ([resourcePath, entry]) => {
    const detail = { resourcePath, acceptEncoding: "br" };
    const response = await requestRaw(baseUrl, resourcePath, "br");
    assert(response.statusCode === 200, "Brotli 资源响应状态异常", { ...detail, statusCode: response.statusCode });
    assert(responseHeader(response, "content-encoding")?.toLowerCase() === "br", "预压缩资源未以 Brotli 传输", {
      ...detail,
      contentEncoding: responseHeader(response, "content-encoding")
    });
    assertVaryAcceptEncoding(response, detail);
    assert(Number(responseHeader(response, "content-length")) === entry.brotliBytes, "Brotli Content-Length 与 manifest 不一致", {
      ...detail,
      expected: entry.brotliBytes,
      actual: responseHeader(response, "content-length")
    });
    assert(response.body.length === entry.brotliBytes, "Brotli 响应体长度与 manifest 不一致", {
      ...detail,
      expected: entry.brotliBytes,
      actual: response.body.length
    });
    assert(sha256(response.body) === entry.brotliSha256, "Brotli 响应 hash 与 manifest 不一致", {
      ...detail,
      expected: entry.brotliSha256,
      actual: sha256(response.body)
    });

    let decoded;
    try {
      decoded = brotliDecompressSync(response.body);
    } catch (error) {
      assert(false, "Brotli HTTP 响应无法解压", { ...detail, error: String(error) });
    }
    const source = readFileSync(fileURLToPath(new URL(resourcePath, new URL("../dist/", import.meta.url))));
    assert(decoded.equals(source), "Brotli HTTP 响应解压后与生产源文件不一致", { ...detail });
    assert(sha256(decoded) === entry.sourceSha256, "Brotli HTTP 响应解压后的 hash 与 manifest 不一致", {
      ...detail,
      expected: entry.sourceSha256,
      actual: sha256(decoded)
    });
  }));

  const identityCases = [...identityResourcePaths].flatMap((resourcePath) => [
    { resourcePath, acceptEncoding: "identity" },
    { resourcePath, acceptEncoding: "br;q=0" }
  ]);
  await Promise.all(identityCases.map(async ({ resourcePath, acceptEncoding }) => {
    const entry = brotliManifest.entries[resourcePath];
    assert(entry, "identity 门禁资源不在 Brotli manifest 中", { resourcePath, distBrotliManifest });
    const detail = { resourcePath, acceptEncoding };
    const response = await requestRaw(baseUrl, resourcePath, acceptEncoding);
    assert(response.statusCode === 200, "identity 资源响应状态异常", { ...detail, statusCode: response.statusCode });
    assert(responseHeader(response, "content-encoding") === undefined, "禁用 Brotli 后仍返回压缩响应", {
      ...detail,
      contentEncoding: responseHeader(response, "content-encoding")
    });
    assertVaryAcceptEncoding(response, detail);
    assert(Number(responseHeader(response, "content-length")) === entry.rawBytes, "identity Content-Length 与 manifest 不一致", {
      ...detail,
      expected: entry.rawBytes,
      actual: responseHeader(response, "content-length")
    });
    assert(response.body.length === entry.rawBytes, "identity 响应体长度与 manifest 不一致", {
      ...detail,
      expected: entry.rawBytes,
      actual: response.body.length
    });
    assert(sha256(response.body) === entry.sourceSha256, "identity 响应 hash 与 manifest 不一致", {
      ...detail,
      expected: entry.sourceSha256,
      actual: sha256(response.body)
    });
  }));

  return {
    quality: brotliManifest.quality,
    compressedResources: entries.length,
    identityCases: identityCases.length
  };
}

async function visibleLazyTestIds(page, suffix) {
  return page.locator(`[data-testid$="${suffix}"]:visible`).evaluateAll((elements) =>
    elements.map((element) => element.getAttribute("data-testid"))
  );
}

async function assertNoVisibleLazyFailure(page, phase) {
  const visibleErrors = await visibleLazyTestIds(page, "-module-load-error");
  assert(visibleErrors.length === 0, `${phase} 仍显示 lazy 模块错误边界`, { phase, visibleErrors });
}

async function assertNoVisibleLazyFallback(page, phase) {
  const visibleFallbacks = await visibleLazyTestIds(page, "-module-loading");
  assert(visibleFallbacks.length === 0, `${phase} 仍显示 lazy 模块加载占位`, { phase, visibleFallbacks });
}

async function verifyDelayedFailureIsObserved() {
  const syntheticSignals = [];
  setTimeout(() => syntheticSignals.push({ kind: "failed-response", status: 503 }), 500);
  const observed = await waitForSignalQuietPeriod(syntheticSignals, 0, {
    quietMs: 750,
    timeoutMs: 2500
  });
  let gateRejected = false;
  try {
    assertNoUnexpectedRecoverySignals(observed);
  } catch {
    gateRejected = true;
  }
  assert(gateRejected, "恢复门禁未拒绝 500ms 延迟到达的失败响应", { observed });
}

function sendJson(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(payload));
}

async function readJsonBody(request) {
  let body = "";
  for await (const chunk of request) body += chunk;
  return body ? JSON.parse(body) : {};
}

async function enterApp(page, baseUrl) {
  await page.goto(baseUrl, {
    waitUntil: "networkidle",
    timeout: 30000
  });
  const demoAccount = page.getByRole("button", { name: "演示账号" });
  const oidcAccount = page.getByRole("button", { name: "使用组织账号登录" });
  if (await oidcAccount.count()) await oidcAccount.click();
  else if (await demoAccount.count()) await demoAccount.click();
  await page.getByText("运营首页", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
}

async function openKnowledge(page) {
  const nav = page.locator('button[aria-label="导航：知识库"]').first();
  await nav.waitFor({ state: "visible", timeout: 8000 });
  await nav.click();
}

assert(existsSync(distIndex), "缺少生产构建产物，请先运行 npm run build", { distIndex });
assert(existsSync(distManifest), "缺少生产 manifest，请先运行 npm run build", { distManifest });
assert(existsSync(distBrotliManifest), "缺少 Brotli manifest，请先运行 npm run build", {
  distBrotliManifest
});
const manifest = JSON.parse(readFileSync(distManifest, "utf8"));
const brotliManifest = JSON.parse(readFileSync(distBrotliManifest, "utf8"));
const manifestAssetPath = (entryKey) => {
  const file = manifest[entryKey]?.file;
  assert(typeof file === "string", "生产 manifest 缺少入口", { entryKey, distManifest });
  return `/${file}`;
};
const appChunkPath = manifestAssetPath("src/App.tsx");
const knowledgeChunkPath = manifestAssetPath("src/modules/knowledge/index.ts");
const catalogAssetPaths = new Set([
  manifestAssetPath("src/catalogs/production/module-catalog.json"),
  manifestAssetPath("src/catalogs/production/static-catalog.json")
]);
const entryCssPaths = manifest["index.html"]?.css;
assert(Array.isArray(entryCssPaths) && entryCssPaths.length > 0, "生产 manifest 缺少入口 CSS", {
  distManifest,
  entry: manifest["index.html"]
});
const identityResourcePaths = new Set([
  "index.html",
  appChunkPath.replace(/^\/+/, ""),
  ...entryCssPaths,
  ...[...catalogAssetPaths].map((path) => path.replace(/^\/+/, ""))
]);
const isKnowledgeChunkUrl = (url) => new URL(url).pathname === knowledgeChunkPath;
const knowledgeRouteMatcher = (url) => url.pathname === knowledgeChunkPath;
await verifyDelayedFailureIsObserved();

const bffStub = createHttpServer(async (request, response) => {
  const path = request.url?.split("?")[0] ?? "";
  const hasOidcCookie = request.headers.cookie
    ?.split(";")
    .map((item) => item.trim())
    .includes(`auris_session=${previewOidcCookie}`) ?? false;
  if (path === "/healthz") {
    sendJson(response, 200, { status: "ok", service: "preview-smoke-stub" });
    return;
  }
  if (path === "/api/v1/auth/dev-login" && request.method === "POST") {
    const payload = await readJsonBody(request);
    if (payload.email !== "demo.operator@auris.local" || payload.password !== "auris-demo") {
      sendJson(response, 401, { error: { code: "INVALID_CREDENTIALS", message: "邮箱或密码错误" } });
      return;
    }
    sendJson(response, 200, {
      data: {
        access_token: smokeSessionToken,
        token_type: "Bearer",
        expires_at: "2099-01-01T00:00:00+00:00",
        user: {
          user_id: "u_admin_001",
          name: "Demo Operator",
          email: "demo.operator@auris.local",
          role: "平台管理员",
          roles: ["project_admin", "asset_manager"],
          initials: "D",
          tenant_id: "aurora_auto",
          tenant_name: "极光汽车",
          project_id: "sales_qa",
          project_name: "销售话术质检"
        }
      },
      meta: { trace_id: "trace_preview_login", request_id: "preview-login" }
    });
    return;
  }
  if (path === "/api/v1/auth/oidc/login" && request.method === "GET") {
    const requestedReturnPath = new URL(request.url ?? "", "http://preview.local")
      .searchParams.get("return_path");
    const returnPath = requestedReturnPath?.startsWith("/") && !requestedReturnPath.startsWith("//")
      ? requestedReturnPath
      : "/";
    oidcLoginRequests += 1;
    response.writeHead(303, {
      Location: returnPath,
      "Cache-Control": "no-store",
      "Set-Cookie": `auris_session=${previewOidcCookie}; Path=/; HttpOnly; SameSite=Lax`
    });
    response.end();
    return;
  }
  if (path === "/api/v1/auth/session" && request.method === "GET") {
    if (!hasOidcCookie && request.headers.authorization !== `Bearer ${smokeSessionToken}`) {
      sendJson(response, 401, { error: { code: "AUTH_SESSION_INVALID", message: "浏览器会话无效" } });
      return;
    }
    sendJson(response, 200, {
      data: {
        user_id: "u_admin_001",
        name: "Demo Operator",
        email: "demo.operator@auris.local",
        role: "平台管理员",
        roles: ["project_admin", "asset_manager"],
        initials: "D",
        tenant_id: "aurora_auto",
        tenant_name: "极光汽车",
        project_id: "sales_qa",
        project_name: "销售话术质检",
        provider: "oidc_session",
        csrf_token: "preview-smoke-csrf-fixture"
      },
      meta: { trace_id: "trace_preview_session", request_id: "preview-session" }
    });
    return;
  }
  if (
    path.startsWith("/api/v1/") &&
    !hasOidcCookie &&
    request.headers.authorization !== `Bearer ${smokeSessionToken}`
  ) {
    sendJson(response, 401, { error: { code: "UNAUTHORIZED", message: "缺少服务端会话" } });
    return;
  }
  if (path === "/api/v1/insights/ops-summary" && request.method === "GET") {
    sendJson(response, 200, {
      data: { audio_count: 9421, pending_count: 319, anomaly_count: 17, recent_asset_count: 23 },
      meta: { trace_id: "trace_preview_home", request_id: "preview-home" }
    });
    return;
  }
  if (path === "/api/v1/projects/sales_qa/scene-profile" && request.method === "GET") {
    const manifestSha256 = "a".repeat(64);
    const manifest = {
      schema_version: "scene-profile/1",
      scene_key: "auto-sales-quality",
      display_name: "汽车门店销售质检",
      description: "preview smoke 使用的确定性生产场景绑定。",
      locales: ["zh-CN"],
      capabilities: ["audio-intelligence", "labeling", "insight"],
      roles: [
        { role_key: "sales-agent", display_name: "销售", description: "门店销售服务人员" },
        { role_key: "customer", display_name: "客户", description: "接受接待和咨询的客户" }
      ],
      entities: [
        { object_key: "store", display_name: "门店", schema_ref: "schema:store/v1", required: true },
        { object_key: "employee", display_name: "员工", schema_ref: "schema:employee/v1", required: true }
      ],
      events: [
        { object_key: "quote-event", display_name: "报价事件", schema_ref: "schema:quote-event/v1", required: false }
      ],
      document_types: [
        { object_key: "quote-document", display_name: "报价单", schema_ref: "schema:quote-document/v1", required: false }
      ],
      data_contract_refs: ["contract:audio-session/v1", "contract:business-event/v1"],
      task_type_refs: ["task_sales_quality"],
      label_version_refs: ["label_v1_8_4"],
      prompt_version_refs: ["prompt_quote_v3"],
      knowledge_index_refs: ["ki_sales_policy_v1"],
      eval_dataset_version_refs: ["evalset_quote_risk_v12"],
      connector_refs: ["conn_platform_auth"],
      model_service_refs: ["model_asr_prod"],
      metrics: [
        {
          metric_key: "audio-transcript-quality",
          display_name: "音频转写质量",
          unit: "ratio",
          calculator_ref: "metric:audio-transcript-quality/v1",
          evidence_refs: ["audio", "transcript"]
        },
        {
          metric_key: "labeling-f1",
          display_name: "标签 F1",
          unit: "ratio",
          calculator_ref: "metric:labeling-f1/v1",
          evidence_refs: ["label", "gold"]
        }
      ],
      release_requirements: [
        {
          requirement_key: "core-audio-gate",
          gate_kind: "core_capability",
          metric_key: "audio-transcript-quality",
          operator: "gte",
          threshold_ppm: 850000
        },
        {
          requirement_key: "scene-label-gate",
          gate_kind: "scene_eval",
          metric_key: "labeling-f1",
          operator: "gte",
          threshold_ppm: 880000
        }
      ],
      governance: {
        human_review_required: true,
        model_may_publish: false,
        retention_policy_ref: "policy:retention/default-v1",
        privacy_policy_ref: "policy:privacy/audio-v1"
      }
    };
    sendJson(response, 200, {
      data: {
        binding_id: "sceneb_sales_qa_production",
        project_id: "sales_qa",
        environment: "production",
        scene_profile_id: "scene_auto_sales_quality",
        scene_profile_version_id: "scenev_auto_sales_quality_v1",
        manifest_sha256: manifestSha256,
        status: "active",
        resource_version: 1,
        trace_id: "trace_preview_scene_profile",
        version: {
          scene_profile_version_id: "scenev_auto_sales_quality_v1",
          scene_profile_id: "scene_auto_sales_quality",
          version: "v1.0.0",
          status: "published",
          source_type: "human",
          manifest,
          manifest_sha256: manifestSha256,
          resource_version: 1,
          requested_by: "u_admin_001",
          reviewed_by: "u_release_admin_001",
          published_by: "u_release_admin_001",
          trace_id: "trace_preview_scene_profile"
        }
      },
      meta: { trace_id: "trace_preview_scene_profile", request_id: "preview-scene-profile" }
    });
    return;
  }
  if (path === "/api/v1/knowledge-sources" && request.method === "GET") {
    sendJson(response, 200, {
      data: { items: [{ id: "ks_sales_sop", status: "synced" }] },
      meta: { trace_id: "trace_preview_knowledge", request_id: "preview-knowledge" }
    });
    return;
  }
  sendJson(response, 404, {
    error: {
      code: "PREVIEW_STUB_ROUTE_UNREGISTERED",
      message: `preview smoke 未注册接口：${request.method} ${path}`
    }
  });
});

await new Promise((resolve) => bffStub.listen(0, "127.0.0.1", resolve));
const bffAddress = bffStub.address();
assert(typeof bffAddress === "object" && bffAddress !== null, "无法启动 preview BFF stub");

const proxyTarget = `http://127.0.0.1:${bffAddress.port}`;
const previewServer = await preview({
  root,
  logLevel: "error",
  preview: {
    host: "127.0.0.1",
    port: 0,
    strictPort: false,
    proxy: {
      "/api": { target: proxyTarget, changeOrigin: true },
      "/healthz": { target: proxyTarget, changeOrigin: true }
    }
  }
});
const serverAddress = previewServer.httpServer.address();
assert(typeof serverAddress === "object" && serverAddress !== null, "无法解析 preview 服务地址");
const baseUrl = `http://127.0.0.1:${serverAddress.port}/`;

const browser = await chromium.launch({ headless: true });
const normalContext = await browser.newContext({ baseURL: baseUrl, viewport: { width: 1440, height: 900 } });
const normalPage = await normalContext.newPage();
const consoleErrors = [];
const pageErrors = [];
const requestFailures = [];
const failedResponses = [];
const knowledgeChunkResponses = [];
const startupAssetSignals = [];
const normalFallbackSignals = [];
let releaseNormalKnowledgeChunk = () => {};
const normalKnowledgeChunkGate = new Promise((resolve) => {
  releaseNormalKnowledgeChunk = resolve;
});

await normalPage.route(knowledgeRouteMatcher, async (route) => {
  normalFallbackSignals.push({ kind: "route-held", url: route.request().url() });
  await normalKnowledgeChunkGate;
  normalFallbackSignals.push({ kind: "route-continued", url: route.request().url() });
  await route.continue();
});

normalPage.on("console", (message) => {
  if (
    message.type() === "error" &&
    message.text().includes("status of 401 (Unauthorized)") &&
    expectedInitialAuthConsoleErrors < 2
  ) {
    expectedInitialAuthConsoleErrors += 1;
    return;
  }
  if (message.type() === "error") consoleErrors.push(message.text());
});
normalPage.on("pageerror", (error) => pageErrors.push(error.message));
normalPage.on("requestfailed", (request) => requestFailures.push({ url: request.url(), error: request.failure()?.errorText }));
normalPage.on("request", (request) => {
  const pathname = new URL(request.url()).pathname;
  if (catalogAssetPaths.has(pathname)) {
    startupAssetSignals.push({ kind: "catalog-request", pathname });
  } else if (pathname === appChunkPath) {
    startupAssetSignals.push({ kind: "app-request", pathname });
  }
});
normalPage.on("response", (response) => {
  const pathname = new URL(response.url()).pathname;
  if (pathname === appChunkPath) {
    startupAssetSignals.push({ kind: "app-response", pathname, status: response.status() });
  }
  if (isKnowledgeChunkUrl(response.url())) {
    knowledgeChunkResponses.push({ url: response.url(), status: response.status() });
  }
  if (response.status() >= 400 && !(pathname === "/api/v1/auth/session" && response.status() === 401)) {
    failedResponses.push({ url: response.url(), status: response.status(), method: response.request().method() });
  }
});

const chunkFailureSignals = [];
let precompressedTransport;
try {
  precompressedTransport = await verifyPrecompressedTransport(
    baseUrl,
    brotliManifest,
    identityResourcePaths
  );
  await enterApp(normalPage, baseUrl);
  const catalogRequests = startupAssetSignals.filter((signal) => signal.kind === "catalog-request");
  const appRequests = startupAssetSignals.filter((signal) => signal.kind === "app-request");
  const appResponseIndex = startupAssetSignals.findIndex((signal) => signal.kind === "app-response");
  assert(
    appRequests.length >= 1 && catalogRequests.length === appRequests.length * 2,
    "每次启动（含 OIDC 回跳）都必须并行预热两个 catalog",
    {
    startupAssetSignals
    }
  );
  assert(
    appResponseIndex > 0 &&
      startupAssetSignals
        .slice(0, appResponseIndex)
        .filter((signal) => signal.kind === "catalog-request").length === 2,
    "catalog 请求仍串行等待 App chunk 完成",
    { startupAssetSignals }
  );
  assert(knowledgeChunkResponses.length === 0, "知识库 chunk 在进入模块前被提前加载", {
    knowledgeChunkResponses
  });
  await openKnowledge(normalPage);
  await normalPage.getByTestId("knowledge-module-loading").waitFor({ state: "visible", timeout: 10000 });
  assert(normalFallbackSignals.some((signal) => signal.kind === "route-held"), "未通过受控 chunk 请求观察知识库 fallback", {
    normalFallbackSignals
  });
  await assertNoVisibleLazyFailure(normalPage, "知识库正常加载期间");
  releaseNormalKnowledgeChunk();
  await normalPage.getByTestId("knowledge-module-root").waitFor({ state: "visible", timeout: 10000 });
  await normalPage.unroute(knowledgeRouteMatcher);
  await assertNoVisibleLazyFailure(normalPage, "知识库正常加载完成后");
  await assertNoVisibleLazyFallback(normalPage, "知识库正常加载完成后");
  assert(knowledgeChunkResponses.length === 1, "知识库独立 chunk 未被生产页面按需加载", { knowledgeChunkResponses });
  assert(knowledgeChunkResponses[0]?.status === 200, "知识库 chunk 响应异常", { knowledgeChunkResponses });
  assert(consoleErrors.length === 0, "生产 preview 存在 console error", { consoleErrors });
  assert(pageErrors.length === 0, "生产 preview 存在 page error", { pageErrors });
  assert(requestFailures.length === 0, "生产 preview 存在 request failure", { requestFailures });
  assert(failedResponses.length === 0, "生产 preview 存在失败响应", { failedResponses });
  assert(oidcLoginRequests >= 1, "生产 preview 未通过 OIDC 重定向建立 Cookie 会话", {
    oidcLoginRequests
  });

  const failureContext = await browser.newContext({ baseURL: baseUrl, viewport: { width: 1440, height: 900 } });
  const failurePage = await failureContext.newPage();
  const recoveredChunkResponses = [];
  let releaseInjectedKnowledgeFailure = () => {};
  const injectedKnowledgeFailureGate = new Promise((resolve) => {
    releaseInjectedKnowledgeFailure = resolve;
  });
  await failurePage.route(knowledgeRouteMatcher, async (route) => {
    chunkFailureSignals.push({ kind: "route-held", url: route.request().url() });
    await injectedKnowledgeFailureGate;
    chunkFailureSignals.push({ kind: "route-abort", url: route.request().url() });
    await route.abort("failed");
  });
  failurePage.on("requestfailed", (request) => {
    chunkFailureSignals.push({ kind: "requestfailed", url: request.url(), error: request.failure()?.errorText });
  });
  failurePage.on("response", (response) => {
    const pathname = new URL(response.url()).pathname;
    if (isKnowledgeChunkUrl(response.url()) && response.status() === 200) {
      recoveredChunkResponses.push({ url: response.url(), status: response.status() });
    }
    if (response.status() >= 400 && !(pathname === "/api/v1/auth/session" && response.status() === 401)) {
      chunkFailureSignals.push({
        kind: "failed-response",
        url: response.url(),
        status: response.status(),
        method: response.request().method()
      });
    }
  });
  failurePage.on("pageerror", (error) => chunkFailureSignals.push({ kind: "pageerror", error: error.message }));
  failurePage.on("console", (message) => {
    if (
      message.type() === "error" &&
      message.text().includes("status of 401 (Unauthorized)") &&
      expectedInitialAuthConsoleErrors < 2
    ) {
      expectedInitialAuthConsoleErrors += 1;
      return;
    }
    if (message.type() === "error") chunkFailureSignals.push({ kind: "console", error: message.text() });
  });
  try {
    await enterApp(failurePage, baseUrl);
    await openKnowledge(failurePage);
    await failurePage.getByTestId("knowledge-module-loading").waitFor({ state: "visible", timeout: 10000 });
    assert(chunkFailureSignals.some((signal) => signal.kind === "route-held"), "故障注入前未观察到知识库 fallback", {
      chunkFailureSignals
    });
    await assertNoVisibleLazyFailure(failurePage, "知识库故障注入前");
    releaseInjectedKnowledgeFailure();
    await failurePage.getByTestId("knowledge-module-load-error").waitFor({ state: "visible", timeout: 10000 });
    const injectedVisibleErrors = await visibleLazyTestIds(failurePage, "-module-load-error");
    assert(
      injectedVisibleErrors.length === 1 && injectedVisibleErrors[0] === "knowledge-module-load-error",
      "故障注入后未只显示知识库 lazy 错误边界",
      { injectedVisibleErrors, chunkFailureSignals }
    );
    await assertNoVisibleLazyFallback(failurePage, "知识库故障注入后");
    const reloadButton = failurePage.getByRole("button", { name: "重新加载" });
    await reloadButton.waitFor({ state: "visible", timeout: 3000 });
    const injectedSignals = await waitForSignalQuietPeriod(chunkFailureSignals, 0, {
      quietMs: 750,
      timeoutMs: 10000
    });
    const injectedRequestFailures = injectedSignals.filter((signal) => signal.kind === "requestfailed");
    const unexpectedNetworkSignals = injectedSignals.filter(
      (signal) =>
        ["requestfailed", "failed-response"].includes(signal.kind) &&
        !isKnowledgeChunkUrl(signal.url)
    );
    assert(
      chunkFailureSignals.some((signal) => signal.kind === "route-abort") &&
        injectedRequestFailures.length >= 1 &&
        injectedRequestFailures.every((signal) => isKnowledgeChunkUrl(signal.url)),
      "未捕获模拟的知识库 chunk 加载失败",
      { injectedSignals, chunkFailureSignals }
    );
    assert(unexpectedNetworkSignals.length === 0, "故障页存在非预期的请求失败或 HTTP >= 400 响应", {
      injectedSignals,
      unexpectedNetworkSignals
    });
    const recoverySignalCursor = chunkFailureSignals.length;
    await failurePage.unroute(knowledgeRouteMatcher);
    await reloadButton.click();
    await failurePage.getByText("运营首页", { exact: true }).waitFor({ state: "visible", timeout: 10000 });
    await assertNoVisibleLazyFailure(failurePage, "知识库恢复后的首页");
    await assertNoVisibleLazyFallback(failurePage, "知识库恢复后的首页");
    const recoveredKnowledgeApi = failurePage.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/knowledge-sources" &&
        response.request().method() === "GET",
      { timeout: 10000 }
    );
    await openKnowledge(failurePage);
    await failurePage.getByTestId("knowledge-module-root").waitFor({ state: "visible", timeout: 10000 });
    const knowledgeResponse = await recoveredKnowledgeApi;
    assert(knowledgeResponse.status() === 200, "知识库恢复后数据接口未成功", {
      status: knowledgeResponse.status()
    });
    await failurePage.waitForLoadState("networkidle", { timeout: 10000 });
    const recoverySignals = await waitForSignalQuietPeriod(
      chunkFailureSignals,
      recoverySignalCursor,
      { quietMs: 750, timeoutMs: 10000 }
    );
    assert(recoveredChunkResponses.length >= 1, "知识库 chunk 失败后重新加载未恢复", {
      recoveredChunkResponses
    });
    await assertNoVisibleLazyFailure(failurePage, "知识库重新加载完成后");
    await assertNoVisibleLazyFallback(failurePage, "知识库重新加载完成后");
    assertNoUnexpectedRecoverySignals(recoverySignals, {
      recoverySignals,
      chunkFailureSignals
    });
  } finally {
    releaseInjectedKnowledgeFailure();
    await failureContext.close();
  }

  console.log(JSON.stringify({
    status: "ok",
    baseUrl,
    precompressedTransport,
    knowledgeChunk: knowledgeChunkResponses[0],
    normal: { consoleErrors, pageErrors, requestFailures, failedResponses },
    normalFallbackSignals,
    chunkFailureSignals,
    recoveredChunkResponses
  }, null, 2));
} finally {
  releaseNormalKnowledgeChunk();
  await normalContext.close();
  await browser.close();
  await previewServer.close();
  await new Promise((resolve) => bffStub.close(resolve));
}
