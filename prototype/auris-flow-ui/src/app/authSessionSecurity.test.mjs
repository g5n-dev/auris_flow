import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = async (relativePath) =>
  readFile(new URL(relativePath, import.meta.url), "utf8");

test("browser auth discards compatibility bearer material and remains cookie-only", async () => {
  const [sessionSource, hookSource, authClientSource, apiClientSource] = await Promise.all([
    source("./authSession.ts"),
    source("./useAuthSession.ts"),
    source("../api/authClient.ts"),
    source("../api/client.ts")
  ]);
  const browserAuthSource = `${sessionSource}\n${hookSource}\n${authClientSource}\n${apiClientSource}`;

  assert.doesNotMatch(browserAuthSource, /(?:localStorage|sessionStorage)\.setItem\s*\(/);
  assert.doesNotMatch(browserAuthSource, /persistAuthSession|loadStoredAuthSession/);
  assert.match(sessionSource, /removeItem\(LEGACY_AUTH_SESSION_STORAGE_KEY\)/);
  assert.match(authClientSource, /credentials:\s*"include"/);
  assert.match(authClientSource, /access_token:\s*_discardedCompatibilityToken/);
  assert.doesNotMatch(sessionSource, /authToken:\s*session\.access_token/);
  assert.doesNotMatch(apiClientSource, /authToken:\s*session\.access_token/);
  assert.doesNotMatch(apiClientSource, /headers\.set\("Authorization"/);
});

test("cookie restore, OIDC redirect, CSRF and credential rules are explicit", async () => {
  const [authClientSource, apiClientSource, authPageSource, platformBffSource, uiSmokeSource, visualRegressionSource, viteConfigSource, previewSmokeSource] = await Promise.all([
    source("../api/authClient.ts"),
    source("../api/client.ts"),
    source("../shell/AuthPage.tsx"),
    source("../../e2e/platform-bff.mjs"),
    source("../../e2e/ui-smoke.mjs"),
    source("../../audit/visual-regression.spec.mjs"),
    source("../../vite.config.ts"),
    source("../../e2e/preview-smoke.mjs")
  ]);

  assert.match(authClientSource, /credentials:\s*"include"/);
  assert.match(authClientSource, /"\/v1\/auth\/session"/);
  assert.match(authClientSource, /\/v1\/auth\/oidc\/login/);
  assert.match(authClientSource, /"X-CSRF-Token"/);
  assert.doesNotMatch(authClientSource, /headers[\s\S]{0,180}Authorization/);
  assert.match(apiClientSource, /getBrowserSessionCsrfToken/);
  assert.match(apiClientSource, /headers\.set\("X-CSRF-Token"/);
  assert.match(apiClientSource, /credentials:\s*"include"/);
  assert.doesNotMatch(apiClientSource, /localStorage|sessionStorage/);
  assert.match(authPageSource, /使用组织账号登录/);
  assert.match(authPageSource, /DEMO_AUTH_ENABLED = import\.meta\.env\.DEV/);
  assert.doesNotMatch(authPageSource, /VITE_DEMO_MODE/);
  assert.match(platformBffSource, /!grantRequestHeaders\.authorization/);
  assert.match(platformBffSource, /grantRequestHeaders\.cookie\?\.includes\("auris_session="\)/);
  assert.match(platformBffSource, /!requestHeaders\.authorization/);
  assert.match(platformBffSource, /requestHeaders\.cookie\?\.includes\("auris_session="\)/);
  assert.match(platformBffSource, /!submissionHeaders\.authorization/);
  assert.match(platformBffSource, /submissionHeaders\.cookie\?\.includes\("auris_session="\)/);
  assert.match(platformBffSource, /anonymousSessionProbeFailure/);
  assert.doesNotMatch(
    platformBffSource,
    /assert\(\s*(?:grantRequestHeaders|requestHeaders|submissionHeaders)\.authorization\s*&&|(?:grantRequestHeaders|requestHeaders|submissionHeaders|headers\(\))\.authorization\s*===\s*`Bearer/
  );
  assert.match(uiSmokeSource, /const hasSessionCookie\s*=/);
  assert.match(uiSmokeSource, /const isSystemWorkerBearer\s*=/);
  assert.match(uiSmokeSource, /browserAuthorizationRequests/);
  assert.match(uiSmokeSource, /request\.headers\["x-csrf-token"\]\s*!==\s*smokeCsrfToken/);
  assert.match(uiSmokeSource, /invalidBrowserCsrfRequests/);
  assert.match(uiSmokeSource, /!request\.headers\.authorization/);
  assert.doesNotMatch(
    uiSmokeSource,
    /path\.startsWith\("\/api\/v1\/"\)\s*&&\s*request\.headers\.authorization\s*!==/
  );
  assert.match(visualRegressionSource, /page\.request\.post\([\s\S]{0,180}\/api\/v1\/auth\/dev-login/);
  assert.doesNotMatch(visualRegressionSource, /getByRole\("button",\s*\{\s*name:\s*"演示账号"/);
  assert.match(viteConfigSource, /"\/readyz"\s*:\s*\{/);
  assert.match(previewSmokeSource, /path === "\/readyz"/);
  assert.match(previewSmokeSource, /"\/readyz"\s*:\s*\{\s*target:\s*proxyTarget/);
});
