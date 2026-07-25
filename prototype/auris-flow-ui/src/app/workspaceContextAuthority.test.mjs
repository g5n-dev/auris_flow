import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = async (relativePath) =>
  readFile(new URL(relativePath, import.meta.url), "utf8");

test("project changes use the server scope transition and never mutate tenant locally", async () => {
  const [hookSource, authClientSource, workspaceClientSource, topbarSource] = await Promise.all([
    source("./useWorkspaceContext.ts"),
    source("../api/authClient.ts"),
    source("../api/workspaceClient.ts"),
    source("../shell/TopBar.tsx")
  ]);

  assert.match(authClientSource, /\/v1\/auth\/session\/scope-transitions/);
  assert.match(authClientSource, /"X-CSRF-Token"/);
  assert.match(hookSource, /transitionBrowserAuthSession/);
  assert.match(workspaceClientSource, /workspace-context-options/);
  assert.doesNotMatch(hookSource, /tenant\s*:\s*value/);
  assert.match(topbarSource, /租户为身份隔离边界/);
});

test("scope changes cancel stale requests and broadcast notification without credentials", async () => {
  const [apiClientSource, requestScopeSource, authHookSource] = await Promise.all([
    source("../api/client.ts"),
    source("../api/requestScope.ts"),
    source("./useAuthSession.ts")
  ]);

  assert.match(requestScopeSource, /epoch/);
  assert.match(requestScopeSource, /AbortController/);
  assert.match(apiClientSource, /CSRF_TOKEN_REQUIRED/);
  assert.match(apiClientSource, /apiAuthProvider === "oidc_session"/);
  assert.match(authHookSource, /BroadcastChannel/);
  assert.doesNotMatch(authHookSource, /postMessage\([^)]*(?:csrf|token|cookie)/i);
});

test("401 is reauthentication, while 403 and ordinary 404 do not destroy the session", async () => {
  const [authClientSource, apiClientSource] = await Promise.all([
    source("../api/authClient.ts"),
    source("../api/client.ts")
  ]);

  assert.match(authClientSource, /error\.status === 401/);
  assert.doesNotMatch(
    authClientSource,
    /error\.status === 401\s*\|\|\s*error\.status === 403/
  );
  assert.match(apiClientSource, /AUTH_SCOPE_REJECTED/);
});

test("topbar context options come from the authoritative workspace response", async () => {
  const [hookSource, topbarSource, runtimeSource] = await Promise.all([
    source("./useWorkspaceContext.ts"),
    source("../shell/TopBar.tsx"),
    source("../shared/runtime/workspaceApiContext.ts")
  ]);

  assert.match(hookSource, /getWorkspaceContextOptions/);
  assert.match(topbarSource, /contextOptions/);
  assert.match(topbarSource, /"date"/);
  assert.match(runtimeSource, /LABEL_DEMO_MODE\s*\?/);
});
