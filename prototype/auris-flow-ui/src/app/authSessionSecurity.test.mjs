import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = async (relativePath) =>
  readFile(new URL(relativePath, import.meta.url), "utf8");

test("browser auth never persists bearer material", async () => {
  const [sessionSource, hookSource, authClientSource] = await Promise.all([
    source("./authSession.ts"),
    source("./useAuthSession.ts"),
    source("../api/authClient.ts")
  ]);
  const browserAuthSource = `${sessionSource}\n${hookSource}\n${authClientSource}`;

  assert.doesNotMatch(browserAuthSource, /(?:localStorage|sessionStorage)\.setItem\s*\(/);
  assert.doesNotMatch(browserAuthSource, /persistAuthSession|loadStoredAuthSession/);
  assert.match(sessionSource, /removeItem\(LEGACY_AUTH_SESSION_STORAGE_KEY\)/);
  assert.match(authClientSource, /credentials:\s*"include"/);
});

test("cookie restore, OIDC redirect, CSRF and credential rules are explicit", async () => {
  const [authClientSource, apiClientSource, authPageSource] = await Promise.all([
    source("../api/authClient.ts"),
    source("../api/client.ts"),
    source("../shell/AuthPage.tsx")
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
});
