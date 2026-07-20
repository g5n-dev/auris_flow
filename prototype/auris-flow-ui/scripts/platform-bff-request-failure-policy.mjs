export const EXPECTED_ASSET_READ_ABORT_REASON =
  "authoritative asset-check AbortController discarded a stale scope or unmounted read";

export const GOVERNED_ASSET_READ_ACTION_BY_PATH = Object.freeze({
  "/api/v1/data-assets/auris%2fmodel%2fasr_transcripts":
    "hotword-governance-authoritative-asset-detail",
  "/api/v1/data-assets/auris%2flabel%2fevent_tags":
    "asset-quality-authoritative-checks"
});

export const MAX_EXPECTED_ASSET_READ_CANCELLATIONS = 4;
export const MAX_EXPECTED_ASSET_READ_CANCELLATIONS_PER_TARGET = 2;
export const GOVERNED_E2E_ASSET_READ_SCOPE = Object.freeze({
  tenantId: "aurora_auto",
  projectId: "sales_qa"
});

function parseOrigin(rawUrl) {
  try {
    return new URL(rawUrl).origin;
  } catch {
    return null;
  }
}

export function governedFrontendOrigins({ baseUrl, demoBaseUrl }) {
  return new Set([parseOrigin(baseUrl), parseOrigin(demoBaseUrl)].filter(Boolean));
}

export function governedAssetReadTarget(rawUrl, runtimeUrls) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  const normalizedPath = parsed.pathname.toLowerCase();
  const action = GOVERNED_ASSET_READ_ACTION_BY_PATH[normalizedPath];
  if (!action || parsed.search || !governedFrontendOrigins(runtimeUrls).has(parsed.origin)) {
    return null;
  }
  return {
    action,
    origin: parsed.origin,
    path: parsed.pathname,
    normalizedPath
  };
}

function sameScope(left, right) {
  return (
    left?.tenantId === right?.tenantId &&
    left?.projectId === right?.projectId &&
    left?.tenantId === GOVERNED_E2E_ASSET_READ_SCOPE.tenantId &&
    left?.projectId === GOVERNED_E2E_ASSET_READ_SCOPE.projectId
  );
}

export function validateExpectedAssetReadCancellations(result) {
  const hasExpectedRequestFailures = Array.isArray(result?.expectedRequestFailures);
  const expectedRequestFailures = hasExpectedRequestFailures ? result.expectedRequestFailures : [];
  const runtimeUrls = { baseUrl: result?.baseUrl, demoBaseUrl: result?.demoBaseUrl };
  const invalidExpectedRequestFailures = [];
  const countsByTarget = new Map();
  const governedNetworkEventSequences = new Set();
  const governedActionEpochs = new Set();

  expectedRequestFailures.forEach((item, index) => {
    const target = governedAssetReadTarget(item?.url, runtimeUrls);
    const recovery = item?.recovery;
    const recoveryTarget = governedAssetReadTarget(recovery?.url, runtimeUrls);
    const validSequence = Number.isInteger(item?.eventSequence) && item.eventSequence > 0;
    const validRecoverySequence =
      Number.isInteger(recovery?.eventSequence) &&
      recovery.eventSequence > 0 &&
      validSequence &&
      recovery.eventSequence > item.eventSequence;
    const actionEpochPrefix = `${result?.runId}:${target?.action}:`;
    const actionEpochSequence =
      typeof item?.actionEpoch === "string" && item.actionEpoch.startsWith(actionEpochPrefix)
        ? item.actionEpoch.slice(actionEpochPrefix.length)
        : "";
    const validActionEpoch =
      typeof result?.runId === "string" &&
      result.runId.length > 0 &&
      typeof item?.actionEpoch === "string" &&
      item.actionEpoch === recovery?.actionEpoch &&
      /^[1-9][0-9]*$/.test(actionEpochSequence) &&
      !governedActionEpochs.has(item.actionEpoch);
    const uniqueEventSequences =
      validRecoverySequence &&
      !governedNetworkEventSequences.has(item.eventSequence) &&
      !governedNetworkEventSequences.has(recovery.eventSequence);
    const valid =
      target !== null &&
      recoveryTarget !== null &&
      item?.method === "GET" &&
      item?.resourceType === "fetch" &&
      item?.failure === "net::ERR_ABORTED" &&
      item?.reason === EXPECTED_ASSET_READ_ABORT_REASON &&
      item?.action === target?.action &&
      item?.origin === target?.origin &&
      item?.path === target?.path &&
      (item?.actor ?? null) === (recovery?.actor ?? null) &&
      validActionEpoch &&
      sameScope(item?.scope, recovery?.scope) &&
      uniqueEventSequences &&
      recovery?.method === "GET" &&
      recovery?.resourceType === "fetch" &&
      recovery?.status === 200 &&
      recovery?.completed === true &&
      recovery?.action === target?.action &&
      recoveryTarget?.origin === target?.origin &&
      recoveryTarget?.normalizedPath === target?.normalizedPath &&
      recovery?.origin === recoveryTarget?.origin &&
      recovery?.path === recoveryTarget?.path;

    if (!valid) {
      invalidExpectedRequestFailures.push({ index, item });
      return;
    }
    governedNetworkEventSequences.add(item.eventSequence);
    governedNetworkEventSequences.add(recovery.eventSequence);
    governedActionEpochs.add(item.actionEpoch);
    const targetKey = `${target.origin}${target.normalizedPath}`;
    countsByTarget.set(targetKey, (countsByTarget.get(targetKey) ?? 0) + 1);
  });

  const policyViolations = [];
  if (!hasExpectedRequestFailures) {
    policyViolations.push({ code: "EXPECTED_CANCELLATIONS_FIELD_REQUIRED" });
  }
  if (expectedRequestFailures.length > MAX_EXPECTED_ASSET_READ_CANCELLATIONS) {
    policyViolations.push({
      code: "EXPECTED_CANCELLATION_TOTAL_BUDGET_EXCEEDED",
      actual: expectedRequestFailures.length,
      maximum: MAX_EXPECTED_ASSET_READ_CANCELLATIONS
    });
  }
  for (const [target, count] of countsByTarget) {
    if (count > MAX_EXPECTED_ASSET_READ_CANCELLATIONS_PER_TARGET) {
      policyViolations.push({
        code: "EXPECTED_CANCELLATION_TARGET_BUDGET_EXCEEDED",
        target,
        actual: count,
        maximum: MAX_EXPECTED_ASSET_READ_CANCELLATIONS_PER_TARGET
      });
    }
  }

  return { invalidExpectedRequestFailures, policyViolations };
}
