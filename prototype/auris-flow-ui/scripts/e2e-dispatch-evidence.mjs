import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const forbiddenPublicKeyFingerprints = new Set(
  [
    "access_key",
    "access_token",
    "adapter",
    "adapter_dispatch",
    "adapter_mode",
    "authenticated_source",
    "auth",
    "authorization",
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
    "execution_contract",
    "execution_deadline_at",
    "execution_envelope",
    "external_id",
    "external_run_id",
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
    "token"
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
  "protocol",
  "remote",
  "secret",
  "signature",
  "token"
]);
const forbiddenPublicKeySuffixes = [
  ["storage", "object", "id"],
  ["object", "key"],
  ["object", "uri"],
  ["bucket"]
];
const externalIdFieldByAdapter = {
  dagster: "external_run_id",
  external_callback: "callback_receipt_id",
  object_storage: "storage_object_id"
};

function requiredText(value, label) {
  if (typeof value !== "string" || !value || value !== value.trim()) {
    throw new Error(`${label} must be a non-empty canonical string`);
  }
  return value;
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

export function hasForbiddenPublicDispatchEvidence(value) {
  if (Array.isArray(value)) {
    return value.some((item) => hasForbiddenPublicDispatchEvidence(item));
  }
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(
    ([key, child]) => keyIsForbidden(key) || hasForbiddenPublicDispatchEvidence(child)
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
