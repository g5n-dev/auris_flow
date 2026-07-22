import { execFile } from "node:child_process";
import { isIP } from "node:net";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const forbiddenPublicKeyFingerprints = new Set(
  [
    "access_key",
    "access_key_id",
    "access_token",
    "adapter",
    "adapter_dispatch",
    "adapter_mode",
    "agent_tool_plan",
    "authenticated_source",
    "auth",
    "authorization",
    "artifact_uri",
    "api_key",
    "bearer_token",
    "bucket",
    "callback_receipt_id",
    "claim_token",
    "cookie",
    "cookies",
    "credential",
    "dagster_run_draft",
    "dagster_run_id",
    "details",
    "dispatch",
    "dispatch_idempotency_key",
    "dispatch_request",
    "dispatch_request_sha256",
    "dispatch_state",
    "download_url",
    "endpoint",
    "engine_status",
    "engine_status_observed_at",
    "error",
    "execution_contract",
    "execution_deadline_at",
    "execution_envelope",
    "external_id",
    "external_run_id",
    "failed_event_id",
    "dead_letter_event_id",
    "graphql",
    "graphql_url",
    "headers",
    "hmac",
    "id_token",
    "input_object",
    "job_name",
    "monitor_generation",
    "next_status_sync_at",
    "nonce",
    "object_key",
    "object_uri",
    "observed_engine_status",
    "password",
    "pipeline_name",
    "private_key",
    "partial_artifact_uri",
    "processed_event_id",
    "provider_artifact_ref",
    "provider",
    "provider_evidence",
    "protocol_receipt",
    "refresh_token",
    "remote_id",
    "remote_run_id",
    "repository_location_name",
    "repository_name",
    "request_headers",
    "response_typename",
    "run_config",
    "secret_ref",
    "signature",
    "signature_body_hash",
    "signature_key_id",
    "signature_mode",
    "signature_nonce",
    "signature_request_hash",
    "signed_at",
    "signed_url",
    "signing_key",
    "storage_object_id",
    "result_storage_object_ids",
    "result_storage_object_sha256",
    "token",
    "uri",
    "url"
  ].map((key) => key.replace(/[^a-z0-9]+/g, ""))
);
const forbiddenPublicKeyTokens = new Set([
  "adapter",
  "credential",
  "dagster",
  "details",
  "dispatch",
  "endpoint",
  "engine",
  "graphql",
  "headers",
  "hmac",
  "internal",
  "password",
  "provider",
  "protocol",
  "remote",
  "secret",
  "signature",
  "token"
]);
const forbiddenPublicKeySuffixes = [
  ["url"],
  ["uri"],
  ["access", "key", "id"],
  ["storage", "object", "id"],
  ["storage", "object", "ids"],
  ["storage", "object", "sha256"],
  ["object", "key"],
  ["object", "uri"],
  ["bucket"]
];
const forbiddenPublicValueTokens = new Set([
  "adapter",
  "dagster",
  "dispatch",
  "engine",
  "graphql",
  "minio",
  "mysql",
  "protocol",
  "provider",
  "qdrant",
  "redis"
]);
const bearerCredentialPattern = /\bbearer\s+[A-Za-z0-9._~+/=-]+/i;
const absoluteNetworkUriPattern = /\b[a-z][a-z0-9+.-]*:\/\/[^\s]+/i;
const hostnameCandidatePattern =
  /\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}(?::\d{1,5})?\b/gi;
const canonicalSchemaVersionPattern =
  /^auris\.[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*\.v\d+(?:[._-]\d+)*$/;
const locatorTlds = new Set([
  "ai",
  "app",
  "cloud",
  "cn",
  "com",
  "corp",
  "dev",
  "internal",
  "io",
  "lan",
  "local",
  "localhost",
  "net",
  "org",
  "test"
]);
const internalSingleLabelHosts = new Set([
  "api",
  "backend",
  "dagster",
  "grafana",
  "localhost",
  "minio",
  "mysql",
  "otel-collector",
  "prometheus",
  "qdrant",
  "redis",
  "worker"
]);
const safePublicRouteFields = new Set(["href", "route"]);
const externalIdFieldByAdapter = {
  dagster: "external_run_id",
  external_callback: "callback_receipt_id",
  object_storage: "storage_object_id"
};
const terminalCompletionRunTypes = new Set([
  "audio_ingest",
  "audio_intelligence",
  "asset_backfill",
  "asset_check_retry",
  "boundary_sync",
  "eval_feedback",
  "eval_run",
  "export",
  "external_callback",
  "hotword_analysis",
  "hotword_build",
  "hotword_eval",
  "hotword_publish",
  "hotword_rollback",
  "insight_metric_aggregation",
  "insight_report",
  "knowledge_build",
  "knowledge_sync",
  "label_extraction",
  "label_optimization",
  "label_publish",
  "platform_sync",
  "provider_test",
  "release_command",
  "scene_profile_generation",
  "settings_publish",
  "task_run",
  "task_version_publish"
]);

const scopedDemoUiEvidenceSelectors = Object.freeze([
  ["$platform.evaluationPromptUi", (result) => result?.evaluationPromptUi],
  ["$platform.evaluationBadcaseUi", (result) => result?.evaluationBadcaseUi],
  ["$platform.blindCalibration", (result) => result?.blindCalibration],
  ["$platform.hotwordGovernance", (result) => result?.hotwordGovernance],
  [
    "$platform.domainPageActions.settingsProviderTest",
    (result) => result?.domainPageActions?.settingsProviderTest
  ],
  ["$platform.coreFlows.settingsPublish", (result) => result?.coreFlows?.settingsPublish],
  ["$platform.insightAction", (result) => result?.insightAction],
  ["$platform.insightReport", (result) => result?.insightReport]
]);

export const demoUiContentSourceMarkerPaths = new Set(
  scopedDemoUiEvidenceSelectors.map(([path]) => `${path}.contentSource`)
);

export function collectScopedDemoUiEvidence(result) {
  return scopedDemoUiEvidenceSelectors.map(([path, select]) => [path, select(result)]);
}

export function findInvalidScopedDemoUiEvidence(result) {
  return collectScopedDemoUiEvidence(result).filter(
    ([, evidence]) =>
      evidence?.contentSource !== "mock" ||
      evidence?.productionTruthMode !== "fail_closed" ||
      evidence?.backendInteractionSource !== "bff"
  );
}

export function projectDemoUiEvidenceProof(evidence) {
  return {
    contentSource: evidence?.contentSource,
    productionTruthMode: evidence?.productionTruthMode,
    backendInteractionSource: evidence?.backendInteractionSource
  };
}

const businessBlockedCompletionRunTypes = new Set(["eval_run", "release_command"]);

function requiredText(value, label) {
  if (typeof value !== "string" || !value || value !== value.trim()) {
    throw new Error(`${label} must be a non-empty canonical string`);
  }
  return value;
}

export function isValidTerminalBusinessState(runType, runStatus, businessStatus) {
  if (!terminalCompletionRunTypes.has(runType)) return false;
  if (runStatus === "success") {
    return businessStatus === (runType === "label_optimization" ? "awaiting-review" : "completed");
  }
  if (runStatus === "failed") return businessStatus === "failed";
  return (
    runStatus === "blocked" &&
    businessStatus === "blocked" &&
    businessBlockedCompletionRunTypes.has(runType)
  );
}

export function isPublicDispatchBoundary(data) {
  return Boolean(
    data &&
      data.status === "submitted" &&
      data.business_status === "awaiting_completion" &&
      data.business_completion_required === true
  );
}

export function publicDispatchIdentityMatches(data, { runId, tenantId, projectId }) {
  return Boolean(
    data &&
      data.run_id === runId &&
      data.tenant_id === tenantId &&
      data.project_id === projectId
  );
}

function normalizedKeyTokens(key) {
  const canonical = String(key)
    .normalize("NFKC")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return canonical ? canonical.split("_").filter(Boolean) : [];
}

function keyIsForbidden(key) {
  const tokens = normalizedKeyTokens(key);
  const fingerprint = tokens.join("");
  if (forbiddenPublicKeyFingerprints.has(fingerprint)) return true;
  if (tokens.some((token) => forbiddenPublicKeyTokens.has(token))) return true;
  return forbiddenPublicKeySuffixes.some(
    (suffix) =>
      tokens.length >= suffix.length &&
      suffix.every((token, index) => token === tokens[tokens.length - suffix.length + index])
  );
}

function isSafeSameOriginRoute(value, fieldName) {
  const fieldFingerprint = normalizedKeyTokens(fieldName).join("");
  if (!safePublicRouteFields.has(fieldFingerprint)) return false;
  if (!value.startsWith("/") || value.startsWith("//") || /[\\#\u0000-\u001f]/.test(value)) {
    return false;
  }
  const [path] = value.split("?", 1);
  return (
    !path.includes("//") &&
    !path.split("/").some((segment) => segment === "." || segment === "..")
  );
}

function isIpLocator(value) {
  if (isIP(value)) return true;
  const bracketed = value.match(/^\[([0-9a-f:.]+)\](?::\d{1,5})?$/i);
  if (bracketed && isIP(bracketed[1])) return true;
  const hostPort = value.match(/^(.+):(\d{1,5})$/);
  return Boolean(hostPort && isIP(hostPort[1]));
}

function containsIpLocator(value) {
  const candidates = value
    .split(/[\s"'`(){}<>,;=]+/)
    .map((candidate) => candidate.replace(/^\.+|[.!?]+$/g, ""))
    .filter(Boolean);
  return candidates.some((candidate) => isIpLocator(candidate));
}

function containsHostLocator(value) {
  if (/\blocalhost(?::\d{1,5})?\b/i.test(value)) return true;
  for (const candidate of value.matchAll(hostnameCandidatePattern)) {
    const hostnameWithPort = candidate[0].toLowerCase();
    const [hostname, port] = hostnameWithPort.split(":", 2);
    const labels = hostname.split(".");
    if (port || labels.length >= 3 || locatorTlds.has(labels.at(-1))) return true;
  }
  const singleLabelHost = value.match(/\b([a-z][a-z0-9-]{0,62}):(\d{1,5})\b/i);
  return Boolean(
    singleLabelHost && internalSingleLabelHosts.has(singleLabelHost[1].toLowerCase())
  );
}

function isCanonicalSchemaVersion(value, fieldName) {
  return (
    normalizedKeyTokens(fieldName).join("") === "schemaversion" &&
    canonicalSchemaVersionPattern.test(value)
  );
}

function stringContainsForbiddenPublicEvidence(value, fieldName) {
  const visibleValue = value.normalize("NFKC").replace(/\p{Cf}/gu, "").trim();
  if (!visibleValue) return false;
  if (bearerCredentialPattern.test(visibleValue)) return true;
  if (absoluteNetworkUriPattern.test(visibleValue)) return true;
  if (containsIpLocator(visibleValue)) return true;
  if (!isCanonicalSchemaVersion(visibleValue, fieldName) && containsHostLocator(visibleValue)) {
    return true;
  }
  if (isSafeSameOriginRoute(visibleValue, fieldName)) return false;
  return normalizedKeyTokens(visibleValue).some((token) => forbiddenPublicValueTokens.has(token));
}

export function hasForbiddenPublicDispatchEvidence(value, fieldName = "") {
  if (Array.isArray(value)) {
    return value.some((item) => hasForbiddenPublicDispatchEvidence(item, fieldName));
  }
  if (typeof value === "string") return stringContainsForbiddenPublicEvidence(value, fieldName);
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(
    ([key, child]) => keyIsForbidden(key) || hasForbiddenPublicDispatchEvidence(child, key)
  );
}

export async function readTrustedE2eDispatchEvidence(
  {
    runId,
    tenantId,
    projectId,
    databaseUrl,
    pythonPath,
    helperPath,
    timeoutMs = 10000
  },
  execute = execFileAsync
) {
  const expectedRunId = requiredText(runId, "runId");
  const expectedTenantId = requiredText(tenantId, "tenantId");
  const expectedProjectId = requiredText(projectId, "projectId");
  const trustedDatabaseUrl = requiredText(databaseUrl, "databaseUrl");
  const trustedPythonPath = requiredText(pythonPath, "pythonPath");
  const trustedHelperPath = requiredText(helperPath, "helperPath");
  let stdout;
  try {
    ({ stdout } = await execute(
      trustedPythonPath,
      [
        trustedHelperPath,
        "--read-only",
        "--tenant-id",
        expectedTenantId,
        "--project-id",
        expectedProjectId,
        expectedRunId
      ],
      {
        encoding: "utf8",
        env: { ...process.env, DATABASE_URL: trustedDatabaseUrl },
        maxBuffer: 1024 * 1024,
        shell: false,
        timeout: Math.max(1000, Number(timeoutMs) || 10000),
        windowsHide: true
      }
    ));
  } catch (error) {
    const stderr = typeof error?.stderr === "string" ? error.stderr.trim().slice(-2000) : "";
    throw new Error(
      `trusted E2E dispatch evidence helper failed for ${expectedRunId}${
        stderr ? `: ${stderr}` : ""
      }`
    );
  }

  let evidence;
  try {
    evidence = JSON.parse(String(stdout || "").trim());
  } catch {
    throw new Error(`trusted E2E dispatch evidence is not valid JSON for ${expectedRunId}`);
  }
  if (!evidence || typeof evidence !== "object" || evidence.run_id !== expectedRunId) {
    throw new Error(`trusted E2E dispatch evidence identity does not match ${expectedRunId}`);
  }
  if (
    evidence.run_status !== "submitted" ||
    evidence.business_status !== "awaiting_completion" ||
    evidence.business_completion_required !== true ||
    evidence.event_status !== "processed" ||
    !Number.isInteger(evidence.event_id) ||
    evidence.event_id <= 0
  ) {
    throw new Error(`trusted E2E dispatch evidence is not processed for ${expectedRunId}`);
  }
  const dispatch = evidence.dispatch;
  const adapter = evidence.adapter;
  const externalIdField = externalIdFieldByAdapter[adapter];
  if (
    !dispatch ||
    typeof dispatch !== "object" ||
    dispatch.adapter !== adapter ||
    dispatch.status !== "success" ||
    typeof externalIdField !== "string" ||
    typeof dispatch.details?.[externalIdField] !== "string" ||
    dispatch.details[externalIdField] !== evidence.external_id
  ) {
    throw new Error(`trusted E2E dispatch evidence has no bound external identity for ${expectedRunId}`);
  }
  return evidence;
}

export async function readTrustedE2eCompletionEvidence(
  {
    runId,
    tenantId,
    projectId,
    completionReceiptId,
    adapter,
    externalId,
    signatureKeyId,
    source,
    bodySha256,
    nonce,
    databaseUrl,
    pythonPath,
    helperPath,
    timeoutMs = 10000
  },
  execute = execFileAsync
) {
  const expectedRunId = requiredText(runId, "runId");
  const expectedTenantId = requiredText(tenantId, "tenantId");
  const expectedProjectId = requiredText(projectId, "projectId");
  const expectedReceiptId = requiredText(completionReceiptId, "completionReceiptId");
  const expectedAdapter = requiredText(adapter, "adapter");
  const expectedExternalId = requiredText(externalId, "externalId");
  const expectedKeyId = requiredText(signatureKeyId, "signatureKeyId");
  const expectedSource = requiredText(source, "source");
  const expectedBodySha256 = requiredText(bodySha256, "bodySha256");
  const expectedNonce = requiredText(nonce, "nonce");
  const trustedDatabaseUrl = requiredText(databaseUrl, "databaseUrl");
  const trustedPythonPath = requiredText(pythonPath, "pythonPath");
  const trustedHelperPath = requiredText(helperPath, "helperPath");
  let stdout;
  try {
    ({ stdout } = await execute(
      trustedPythonPath,
      [
        trustedHelperPath,
        "--read-completion",
        "--tenant-id",
        expectedTenantId,
        "--project-id",
        expectedProjectId,
        "--completion-receipt-id",
        expectedReceiptId,
        "--expected-adapter",
        expectedAdapter,
        "--expected-external-id",
        expectedExternalId,
        "--expected-signature-key-id",
        expectedKeyId,
        "--expected-source",
        expectedSource,
        "--expected-body-sha256",
        expectedBodySha256,
        "--expected-nonce",
        expectedNonce,
        expectedRunId
      ],
      {
        encoding: "utf8",
        env: { ...process.env, DATABASE_URL: trustedDatabaseUrl },
        maxBuffer: 1024 * 1024,
        shell: false,
        timeout: Math.max(1000, Number(timeoutMs) || 10000),
        windowsHide: true
      }
    ));
  } catch (error) {
    const stderr = typeof error?.stderr === "string" ? error.stderr.trim().slice(-2000) : "";
    throw new Error(
      `trusted E2E completion evidence helper failed for ${expectedRunId}${
        stderr ? `: ${stderr}` : ""
      }`
    );
  }

  let evidence;
  try {
    evidence = JSON.parse(String(stdout || "").trim());
  } catch {
    throw new Error(`trusted E2E completion evidence is not valid JSON for ${expectedRunId}`);
  }
  const auth = evidence?.auth;
  if (
    evidence?.verified !== true ||
    evidence?.run_id !== expectedRunId ||
    evidence?.completion_receipt_id !== expectedReceiptId ||
    evidence?.run_status !== evidence?.completion_status ||
    !isValidTerminalBusinessState(
      evidence?.run_type,
      evidence?.run_status,
      evidence?.business_status
    ) ||
    evidence?.business_completion_required !== false ||
    evidence?.receipt_state !== "completed" ||
    evidence?.status_code !== 200 ||
    auth?.auth_mode !== "signed_external_completion" ||
    auth?.binding_mode !== "scoped_key_map" ||
    auth?.signature_mode !== "hmac-sha256" ||
    auth?.key_id !== expectedKeyId ||
    auth?.source !== expectedSource ||
    auth?.tenant_id !== expectedTenantId ||
    auth?.project_id !== expectedProjectId ||
    auth?.body_sha256 !== expectedBodySha256 ||
    !Array.isArray(evidence?.storage_objects)
  ) {
    throw new Error(`trusted E2E completion evidence drift for ${expectedRunId}`);
  }
  const storageObjects = evidence.storage_objects.map((item, index) => {
    if (
      item?.ordinal !== index ||
      typeof item?.role !== "string" ||
      !/^[0-9a-f]{64}$/.test(item?.content_sha256 || "") ||
      typeof item?.source_type !== "string" ||
      item?.source_id !== expectedRunId ||
      item?.status !== "verified" ||
      typeof item?.trace_id !== "string" ||
      !item.trace_id
    ) {
      throw new Error(`trusted E2E completion storage evidence drift for ${expectedRunId}`);
    }
    return {
      ordinal: item.ordinal,
      role: item.role,
      contentSha256: item.content_sha256,
      sourceType: item.source_type,
      sourceId: item.source_id,
      status: item.status,
      traceId: item.trace_id
    };
  });
  return {
    verified: true,
    runId: evidence.run_id,
    runType: evidence.run_type,
    runStatus: evidence.run_status,
    businessStatus: evidence.business_status,
    completionReceiptId: evidence.completion_receipt_id,
    completionAuth: {
      authMode: auth.auth_mode,
      bindingMode: auth.binding_mode,
      signatureMode: auth.signature_mode,
      keyId: auth.key_id,
      source: auth.source,
      tenantId: auth.tenant_id,
      projectId: auth.project_id,
      bodySha256: auth.body_sha256,
      verified: true
    },
    storageObjects
  };
}
