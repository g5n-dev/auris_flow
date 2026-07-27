import { existsSync, readFileSync } from "node:fs";

import {
  demoUiContentSourceMarkerPaths,
  findInvalidScopedDemoUiEvidence
} from "./e2e-dispatch-evidence.mjs";
import { validateExpectedAssetReadCancellations } from "./platform-bff-request-failure-policy.mjs";

const resultPath =
  process.argv[2] ||
  new URL("../e2e/artifacts/platform-bff-result.json", import.meta.url).pathname;
const outboxResultPath = process.argv[3];
const requireRealStack = process.env.AURIS_REAL_STACK_E2E === "1";

function fail(message, detail = undefined) {
  const error = { status: "failed", message, detail };
  console.error(JSON.stringify(error, null, 2));
  process.exit(1);
}

if (!existsSync(resultPath)) {
  fail("platform BFF E2E result artifact does not exist", { resultPath });
}

const result = JSON.parse(readFileSync(resultPath, "utf8"));
const outboxResult =
  outboxResultPath && existsSync(outboxResultPath)
    ? JSON.parse(readFileSync(outboxResultPath, "utf8"))
    : null;

function collectForbiddenRuntimeMarkers(value, path = "$", findings = [], allowedPaths = new Set()) {
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      collectForbiddenRuntimeMarkers(item, `${path}[${index}]`, findings, allowedPaths)
    );
    return findings;
  }
  if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      collectForbiddenRuntimeMarkers(item, `${path}.${key}`, findings, allowedPaths);
    }
    return findings;
  }
  if (typeof value !== "string") return findings;
  const normalized = value.trim().toLowerCase();
  const exactForbidden = new Set([
    "local",
    "mock",
    "mock_success",
    "local_dispatch_receipts",
    "project_admin_manual",
    "legacy_local_single_key",
    "descriptor-contract-only",
    "development-manual"
  ]);
  const unsafeUri = /^(mock|local|memory):\/\//.test(normalized);
  const manualCompletionRoute =
    /\/api\/v1\/runs\/[^/]+\/completion-receipts(?:\?|$)/.test(normalized) &&
    !normalized.includes("/external-completion-receipts");
  if (!allowedPaths.has(path) && (exactForbidden.has(normalized) || unsafeUri || manualCompletionRoute)) {
    findings.push({ path, value });
  }
  return findings;
}

function isMysqlE2eDatabaseRef(value) {
  const normalized = String(value || "").trim();
  if (/^mysql(?:\+[a-z0-9_]+)?:\/\//i.test(normalized)) return true;
  // The outbox verifier intentionally removes credentials and the SQLAlchemy
  // scheme before persisting its artifact. Keep accepting that redacted form,
  // but only for an isolated E2E database locator.
  return /^[^/\s]+(?::\d+)?\/auris_flow_e2e_[a-z0-9_]+$/i.test(normalized);
}

const maxAgeMs = Number(process.env.AURIS_E2E_RESULT_MAX_AGE_MS || 30 * 60 * 1000);
const expectedRunId = process.env.AURIS_E2E_RUN_ID;
const now = Date.now();
const startedAt = result.startedAt ? Date.parse(result.startedAt) : Number.NaN;
const completedAt = result.completedAt ? Date.parse(result.completedAt) : Number.NaN;
if (result.status !== "ok") {
  fail("platform BFF E2E result artifact is not a completed ok run", {
    status: result.status,
    runId: result.runId,
    resultPath
  });
}
if (
  !result.runId ||
  !result.startedAt ||
  !result.completedAt ||
  Number.isNaN(startedAt) ||
  Number.isNaN(completedAt)
) {
  fail("platform BFF E2E result artifact is missing run freshness metadata", {
    runId: result.runId,
    startedAt: result.startedAt,
    completedAt: result.completedAt
  });
}
if (expectedRunId && result.runId !== expectedRunId) {
  fail("platform BFF E2E result artifact does not belong to the current run", {
    expectedRunId,
    actualRunId: result.runId,
    resultPath
  });
}
if (startedAt > completedAt || completedAt > now + 5000) {
  fail("platform BFF E2E result artifact has an invalid time sequence", {
    startedAt: result.startedAt,
    completedAt: result.completedAt,
    checkedAt: new Date(now).toISOString()
  });
}
if (now - completedAt > maxAgeMs) {
  fail("platform BFF E2E result artifact is stale", {
    runId: result.runId,
    completedAt: result.completedAt,
    maxAgeMs
  });
}
if (outboxResultPath && !outboxResult) {
  fail("outbox E2E result artifact does not exist", { outboxResultPath });
}
if (result.mode === "audio-import-only") {
  const audioImport = result.audioImportClosedLoop;
  const tenantPull = result.tenantAudioImportPull;
  const invalidRuntimeMarkers = collectForbiddenRuntimeMarkers(result, "$audioImportBrowser");
  const diagnostics = [
    ...(Array.isArray(result.consoleErrors) ? result.consoleErrors : ["consoleErrors missing"]),
    ...(Array.isArray(result.pageErrors) ? result.pageErrors : ["pageErrors missing"]),
    ...(Array.isArray(result.requestFailures) ? result.requestFailures : ["requestFailures missing"]),
    ...(Array.isArray(result.failedResponses) ? result.failedResponses : ["failedResponses missing"])
  ];
  if (
    !requireRealStack ||
    result.schema_version !== "auris.audio-import-browser-e2e.v1" ||
    result.stage !== "completed" ||
    result.executionProfile?.realStack !== true ||
    result.executionProfile?.platformSource !== "https" ||
    result.executionProfile?.dagster !== "real" ||
    result.executionProfile?.objectStorage !== "real" ||
    result.executionProfile?.uiEvidencePolicy !== "browser-clicks-and-bff-readback" ||
    invalidRuntimeMarkers.length ||
    diagnostics.length
  ) {
    fail("focused audio import browser artifact is not a clean real-stack run", {
      requireRealStack,
      schema_version: result.schema_version,
      stage: result.stage,
      executionProfile: result.executionProfile,
      invalidRuntimeMarkers,
      diagnostics
    });
  }
  if (
    !audioImport?.connectorId ||
    !audioImport?.connectorTraceId ||
    !audioImport?.platformConnectionId ||
    !audioImport?.taskVersionId ||
    !audioImport?.taskRunId ||
    !audioImport?.importBatchId ||
    !audioImport?.audioSessionId ||
    !audioImport?.rootTraceId ||
    audioImport?.status !== "succeeded" ||
    audioImport?.executionMode !== "production" ||
    audioImport?.previewCount !== 3 ||
    !(audioImport?.total >= 1) ||
    !(audioImport?.succeeded >= 1) ||
    audioImport?.failed !== 0 ||
    audioImport?.playbackGrantStatus !== 201 ||
    audioImport?.playbackStatus !== 206 ||
    !Number.isInteger(audioImport?.connectorWriteCount) ||
    audioImport.connectorWriteCount < 1 ||
    audioImport.connectorWriteCount > 2 ||
    audioImport?.pageRefreshRecovered !== true ||
    audioImport?.rootTraceReadable !== true ||
    audioImport?.legacyPlatformSyncRequests !== 0 ||
    !audioImport?.targetAssetKey ||
    !audioImport?.sceneProfileId ||
    !audioImport?.sceneProfileVersionId ||
    !/^[0-9a-f]{64}$/.test(audioImport?.sceneProfileSnapshotSha256 ?? "")
  ) {
    fail("focused browser run did not close the real audio import user story", {
      audioImport
    });
  }
  if (
    !tenantPull?.taskRunId ||
    !tenantPull?.importBatchId ||
    !tenantPull?.traceId ||
    tenantPull?.taskRunId === audioImport.taskRunId ||
    tenantPull?.importBatchId === audioImport.importBatchId ||
    tenantPull?.taskVersionId !== audioImport.taskVersionId ||
    !["pending", "queued", "submitted"].includes(tenantPull?.status) ||
    tenantPull?.executionMode !== "production" ||
    tenantPull?.legacyPlatformSyncRequests !== 0
  ) {
    fail("focused browser tenant pull did not reuse the published production TaskVersion", {
      tenantPull,
      audioImport
    });
  }
  console.log(
    JSON.stringify(
      {
        status: "ok",
        checked: "audio-import-browser-real-stack",
        resultPath,
        runId: result.runId,
        taskRunId: audioImport.taskRunId,
        importBatchId: audioImport.importBatchId,
        audioSessionId: audioImport.audioSessionId,
        playbackStatus: audioImport.playbackStatus,
        tenantTaskRunId: tenantPull.taskRunId
      },
      null,
      2
    )
  );
  process.exit(0);
}
if (requireRealStack) {
  if (
      result.executionProfile?.realStack !== true ||
      result.executionProfile?.completionReceiptPolicy !== "signed-external-only" ||
      result.executionProfile?.objectStorageVerification !== "minio-sigv4-put-head-get" ||
      result.executionProfile?.uiEvidencePolicy !== "production-fail-closed-plus-demo-fixture-bff"
  ) {
    fail("real-stack platform artifact is missing its external-completion and MinIO proof profile", {
      executionProfile: result.executionProfile
    });
  }
  const completionObservations = Array.isArray(result.completionReceiptObservations)
    ? result.completionReceiptObservations
    : [];
  const invalidCompletionObservations = completionObservations.filter(
    (item) =>
      !item?.runId ||
      !item?.adapter ||
      item?.httpStatus !== 200 ||
      !item?.route?.endsWith("/external-completion-receipts") ||
      item?.authMode !== "signed_external_completion" ||
      item?.bindingMode !== "scoped_key_map" ||
      item?.tenantId !== "aurora_auto" ||
      item?.projectId !== "sales_qa" ||
      !/^[0-9a-f]{64}$/.test(item?.bodySha256 || "")
  );
  if (!completionObservations.length || invalidCompletionObservations.length) {
    fail("real-stack run completion did not exclusively use scoped signed external receipts", {
      completionObservations,
      invalidCompletionObservations
    });
  }

  const invalidDemoUiEvidence = findInvalidScopedDemoUiEvidence(result);
  if (invalidDemoUiEvidence.length) {
    fail("real-stack demo UI evidence is not paired with production fail-closed and BFF proof", {
      invalidDemoUiEvidence
    });
  }
  const markerFindings = collectForbiddenRuntimeMarkers(
    result,
    "$platform",
    [],
    demoUiContentSourceMarkerPaths
  );
  if (outboxResult) collectForbiddenRuntimeMarkers(outboxResult, "$outbox", markerFindings);
  if (markerFindings.length) {
    fail("real-stack artifacts contain mock/local/manual completion markers", { markerFindings });
  }
  if (
    outboxResult &&
    (outboxResult.status !== "ok" ||
      outboxResult.e2e_run_id !== result.runId ||
      !isMysqlE2eDatabaseRef(outboxResult.database_url))
  ) {
    fail("real-stack outbox artifact is not bound to this MySQL-backed E2E run", {
      resultRunId: result.runId,
      outbox: {
        status: outboxResult.status,
        e2e_run_id: outboxResult.e2e_run_id,
        database_url: outboxResult.database_url
      }
    });
  }
}
if (
  result.authRestore?.reloadStatus !== 200 ||
  result.authRestore?.transientStatus !== 503 ||
  result.authRestore?.retryStatus !== 200 ||
  result.authRestore?.sessionPreserved !== true
) {
  fail("platform BFF E2E did not verify reload and transient auth recovery", {
    authRestore: result.authRestore
  });
}
const expectedModules = ["labels", "evaluation", "insights", "canvas", "knowledge", "assets", "settings"];
const uiMutations = Array.isArray(result.uiMutations) ? result.uiMutations : [];
const modules = new Set(uiMutations.map((item) => item.module));
const missingModules = expectedModules.filter((module) => !modules.has(module));
if (missingModules.length) {
  fail("platform BFF E2E did not cover all required UI write modules", {
    missingModules,
    uiMutations
  });
}

const invalidMutations = uiMutations.filter((item) => !item.id || !item.traceId);
if (invalidMutations.length) {
  fail("platform BFF E2E mutation is missing backend id or traceId", { invalidMutations });
}

const expectedCoverageModules = [
  "home",
  "tenant",
  "project",
  "canvas",
  "data",
  "knowledge",
  "listening",
  "labels",
  "insights",
  "evaluation",
  "assets",
  "settings"
];
const audioImportClosedLoop = result.audioImportClosedLoop;
const audioImportFixtureSkipped =
  audioImportClosedLoop?.status === "skipped" &&
  audioImportClosedLoop?.reasonCode === "REAL_AUDIO_IMPORT_FIXTURE_REQUIRED";
const expectedWriteCoverageModules = expectedCoverageModules.filter(
  (module) =>
    module !== "home" &&
    !(audioImportFixtureSkipped && module === "tenant")
);
const allowedCoverageWriteSources = new Set(["ui-click", "browser-api", "server-api", "pytest"]);
const coverageMatrix = Array.isArray(result.coverageMatrix) ? result.coverageMatrix : [];
const coverageByModule = new Map(coverageMatrix.map((item) => [item.module, item]));
const missingCoverageModules = expectedCoverageModules.filter((module) => !coverageByModule.has(module));
if (missingCoverageModules.length) {
  fail("platform BFF E2E coverage matrix is missing modules", {
    missingCoverageModules,
    coverageMatrix
  });
}
const invalidReadCoverage = expectedCoverageModules
  .map((module) => coverageByModule.get(module))
  .filter((item) => item?.read?.status !== "verified" || !Array.isArray(item.read.endpoints) || !item.read.endpoints.length);
if (invalidReadCoverage.length) {
  fail("platform BFF E2E coverage matrix has invalid read coverage", {
    invalidReadCoverage
  });
}
const invalidWriteCoverage = expectedWriteCoverageModules
  .map((module) => coverageByModule.get(module))
  .filter((item) => !Array.isArray(item.writes) || !item.writes.length);
if (invalidWriteCoverage.length) {
  fail("platform BFF E2E coverage matrix has modules without write coverage", {
    invalidWriteCoverage
  });
}
const invalidCoverageWrites = coverageMatrix.flatMap((item) =>
  (Array.isArray(item.writes) ? item.writes : [])
    .filter(
      (write) =>
        !write?.key ||
        !write?.id ||
        !write?.traceId ||
        !write?.via ||
        !allowedCoverageWriteSources.has(write.via)
    )
    .map((write) => ({ module: item.module, write }))
);
if (invalidCoverageWrites.length) {
  fail("platform BFF E2E coverage matrix has writes without key/id/traceId/via", {
    invalidCoverageWrites
  });
}
const nonUiClickCoverageWrites = [];
for (const module of coverageMatrix) {
  for (const write of module.writes || []) {
    if (write.via !== "ui-click") nonUiClickCoverageWrites.push({ module: module.module, ...write });
  }
}
if (nonUiClickCoverageWrites.length) {
  fail("platform BFF E2E coverage matrix must only use UI-click writes", {
    nonUiClickCoverageWrites
  });
}
const missingUiClickWriteCoverage = expectedWriteCoverageModules.filter((module) => {
  const writes = coverageByModule.get(module)?.writes;
  return !Array.isArray(writes) || !writes.some((write) => write.via === "ui-click");
});
if (missingUiClickWriteCoverage.length) {
  fail("platform BFF E2E coverage matrix has write modules without UI-click coverage", {
    missingUiClickWriteCoverage
  });
}
const evaluationWrites = coverageByModule.get("evaluation")?.writes || [];
const labelWrites = coverageByModule.get("labels")?.writes || [];
const labelVersionWrite = labelWrites.find((item) => item.key === "labelVersion");
if (!labelVersionWrite || labelVersionWrite.via !== "ui-click") {
  fail("platform BFF E2E labelVersion must be covered by UI click", {
    expected: { key: "labelVersion", via: "ui-click" },
    value: labelVersionWrite,
    labelWrites
  });
}
for (const key of [
  "evalRun",
  "feedbackTask",
  "blindCalibrationGold",
  "hotwordBadcaseDecision",
  "hotwordCandidateBuild",
  "hotwordShadowEval",
  "hotwordModelApproval",
  "hotwordManualPublish"
]) {
  const write = evaluationWrites.find((item) => item.key === key);
  if (!write || write.via !== "ui-click") {
    fail(`platform BFF E2E evaluation ${key} must be covered by UI click`, {
      expected: { key, via: "ui-click" },
      value: write,
      evaluationWrites
    });
  }
}

if (
  !result.blindCalibration?.roundId ||
  !result.blindCalibration?.roundTraceId ||
  !result.blindCalibration?.adjudicationId ||
  !result.blindCalibration?.adjudicationTraceId ||
  !result.blindCalibration?.goldSetVersionId ||
  !result.blindCalibration?.goldTraceId ||
  result.blindCalibration?.observedAgreementPpm !== 750000 ||
  result.blindCalibration?.cohenKappaMicros !== 500000 ||
  result.blindCalibration?.reviewerASubmissions?.length !== 4 ||
  result.blindCalibration?.reviewerBSubmissions?.length !== 4
) {
  fail("platform BFF E2E blind calibration did not close A/B review, adjudication, metrics, and gold release", {
    value: result.blindCalibration
  });
}

const hotwordGovernance = result.hotwordGovernance;

function assertGovernedStorageProof(
  section,
  { label, expectedCount, sourceType, sourceId, rootTraceId }
) {
  const registered = Array.isArray(section?.registeredStorageObjects)
    ? section.registeredStorageObjects
    : [];
  const invalidRegistered = registered.filter(
    (item) =>
      !item?.storageObjectId ||
      !/^[0-9a-f]{64}$/.test(item?.contentSha256 || "") ||
      item?.tenantId !== "aurora_auto" ||
      item?.projectId !== "sales_qa" ||
      item?.sourceType !== sourceType ||
      item?.sourceId !== sourceId ||
      item?.status !== "verified" ||
      item?.traceId !== rootTraceId ||
      !item?.objectKey?.startsWith(
        `tenants/aurora_auto/projects/sales_qa/runs/${sourceId}/`
      )
  );
  if (registered.length !== expectedCount || invalidRegistered.length) {
    fail(`${label} storage registration proof is incomplete`, {
      registered,
      invalidRegistered,
      expectedCount,
      sourceType,
      sourceId,
      rootTraceId
    });
  }
  if (!requireRealStack) return;
  const remote = Array.isArray(section?.remoteStorageProofs) ? section.remoteStorageProofs : [];
  const invalidRemote = remote.filter((item) => {
    const registration = registered.find(
      (candidate) => candidate.storageObjectId === item?.storageObjectId
    );
    return (
      !registration ||
      item?.contentSha256 !== registration.contentSha256 ||
      item?.provider !== registration.provider ||
      item?.bucket !== registration.bucket ||
      item?.objectKey !== registration.objectKey ||
      item?.sizeBytes <= 0 ||
      item?.putStatus !== 200 ||
      item?.headStatus !== 200 ||
      item?.getStatus !== 200 ||
      item?.transport !== "aws-sigv4" ||
      item?.verified !== true ||
      !String(item?.objectUri || "").startsWith("s3://")
    );
  });
  if (remote.length !== expectedCount || invalidRemote.length) {
    fail(`${label} MinIO byte-level proof is incomplete`, {
      remote,
      invalidRemote,
      registered,
      expectedCount
    });
  }
}

function assertSignedHotwordCompletion(section, label) {
  if (!requireRealStack) return;
  if (
    !section?.completionRoute?.endsWith("/external-completion-receipts") ||
    section?.completionAuth?.authMode !== "signed_external_completion" ||
    section?.completionAuth?.bindingMode !== "scoped_key_map" ||
    section?.completionAuth?.signatureMode !== "hmac-sha256" ||
    section?.completionAuth?.tenantId !== "aurora_auto" ||
    section?.completionAuth?.projectId !== "sales_qa"
  ) {
    fail(`${label} did not use a scoped signed external completion receipt`, {
      completionRoute: section?.completionRoute,
      completionAuth: section?.completionAuth
    });
  }
}

if (
  hotwordGovernance?.coverageStatus !== "verified" ||
  hotwordGovernance?.statistics?.httpStatus !== 200 ||
  hotwordGovernance?.statistics?.badcaseListHttpStatus !== 200 ||
  !hotwordGovernance?.statistics?.traceId ||
  !hotwordGovernance?.statistics?.badcaseListTraceId ||
  hotwordGovernance?.statistics?.badcaseId !== "A-4107" ||
  hotwordGovernance?.statistics?.itemCount < 1
) {
  fail("platform BFF E2E hotword statistics projection is incomplete", {
    value: hotwordGovernance?.statistics,
    coverageStatus: hotwordGovernance?.coverageStatus
  });
}
if (
  hotwordGovernance?.badcaseReview?.badcaseId !== "A-4107" ||
  hotwordGovernance?.badcaseReview?.httpStatus !== 201 ||
  !hotwordGovernance?.badcaseReview?.traceId ||
  !hotwordGovernance?.badcaseReview?.decisionId ||
  hotwordGovernance?.badcaseReview?.finalStatus !== "pending-backflow" ||
  hotwordGovernance?.badcaseReview?.candidateState !== "confirmed" ||
  !Number.isInteger(hotwordGovernance?.badcaseReview?.resourceVersion)
) {
  fail("platform BFF E2E hotword badcase human review is not persisted", {
    value: hotwordGovernance?.badcaseReview
  });
}
if (
  !hotwordGovernance?.candidatePack?.versionId ||
  !hotwordGovernance?.candidatePack?.itemId ||
  !hotwordGovernance?.candidatePack?.itemTraceId ||
  ![200, 201].includes(hotwordGovernance?.candidatePack?.itemMutationHttpStatus) ||
  hotwordGovernance?.candidatePack?.validatingHttpStatus !== 200 ||
  !hotwordGovernance?.candidatePack?.validatingTraceId ||
  !hotwordGovernance?.candidatePack?.buildRunId ||
  hotwordGovernance?.candidatePack?.buildRunHttpStatus !== 200 ||
  hotwordGovernance?.candidatePack?.buildRunStatus !== "success" ||
  hotwordGovernance?.candidatePack?.dispatchAdapter !== "dagster" ||
  !hotwordGovernance?.candidatePack?.dispatchExternalId ||
  hotwordGovernance?.candidatePack?.completionHttpStatus !== 200 ||
  !hotwordGovernance?.candidatePack?.completionTraceId ||
  hotwordGovernance?.candidatePack?.registeredStorageObjectIds?.length !== 2 ||
  hotwordGovernance?.candidatePack?.finalReadHttpStatus !== 200 ||
  hotwordGovernance?.candidatePack?.finalStatus !== "published" ||
  !Number.isInteger(hotwordGovernance?.candidatePack?.finalResourceVersion) ||
  !hotwordGovernance?.candidatePack?.rootTraceId ||
  !hotwordGovernance?.candidatePack?.manifestStorageObjectId ||
  hotwordGovernance?.candidatePack?.currentPackStatus !== "active" ||
  hotwordGovernance?.candidatePack?.currentPackVersionId !==
    hotwordGovernance?.candidatePack?.versionId
) {
  fail("platform BFF E2E hotword candidate build and publication state is incomplete", {
    value: hotwordGovernance?.candidatePack
  });
}
assertSignedHotwordCompletion(hotwordGovernance.candidatePack, "hotword build");
assertGovernedStorageProof(hotwordGovernance.candidatePack, {
  label: "hotword build",
  expectedCount: 2,
  sourceType: "hotword_build",
  sourceId: hotwordGovernance.candidatePack.buildRunId,
  rootTraceId: hotwordGovernance.candidatePack.rootTraceId
});
if (
  hotwordGovernance?.fixedEvaluation?.requestIssued !== true ||
  hotwordGovernance?.fixedEvaluation?.httpStatus !== 202 ||
  !hotwordGovernance?.fixedEvaluation?.runId ||
  !hotwordGovernance?.fixedEvaluation?.traceId ||
  hotwordGovernance?.fixedEvaluation?.dispatchAdapter !== "dagster" ||
  hotwordGovernance?.fixedEvaluation?.completionHttpStatus !== 200 ||
  hotwordGovernance?.fixedEvaluation?.finalStatus !== "review_required" ||
  hotwordGovernance?.fixedEvaluation?.locked !== true ||
  hotwordGovernance?.fixedEvaluation?.gatePassed !== true ||
  hotwordGovernance?.fixedEvaluation?.resultStorageObjectIds?.length !== 2
) {
  fail("platform BFF E2E hotword fixed shadow evaluation is incomplete", {
    value: hotwordGovernance?.fixedEvaluation
  });
}
assertSignedHotwordCompletion(hotwordGovernance.fixedEvaluation, "hotword fixed evaluation");
assertGovernedStorageProof(hotwordGovernance.fixedEvaluation, {
  label: "hotword fixed evaluation",
  expectedCount: 2,
  sourceType: "hotword_eval",
  sourceId: hotwordGovernance.fixedEvaluation.runId,
  rootTraceId: hotwordGovernance.candidatePack.rootTraceId
});
if (
  hotwordGovernance?.modelApproval?.requestIssued !== true ||
  hotwordGovernance?.modelApproval?.httpStatus !== 200 ||
  !hotwordGovernance?.modelApproval?.traceId ||
  hotwordGovernance?.modelApproval?.actorId !== "u_model_001" ||
  hotwordGovernance?.modelApproval?.finalStatus !== "approved" ||
  !Number.isInteger(hotwordGovernance?.modelApproval?.resourceVersion)
) {
  fail("platform BFF E2E hotword model owner approval is incomplete", {
    value: hotwordGovernance?.modelApproval
  });
}
if (
  hotwordGovernance?.manualPublish?.requestIssued !== true ||
  hotwordGovernance?.manualPublish?.httpStatus !== 202 ||
  !hotwordGovernance?.manualPublish?.runId ||
  !hotwordGovernance?.manualPublish?.traceId ||
  hotwordGovernance?.manualPublish?.dispatchAdapter !== "dagster" ||
  hotwordGovernance?.manualPublish?.completionHttpStatus !== 200 ||
  !hotwordGovernance?.manualPublish?.completionTraceId ||
  hotwordGovernance?.manualPublish?.finalStatus !== "published" ||
  !hotwordGovernance?.manualPublish?.taskVersionId ||
  hotwordGovernance?.manualPublish?.packStatus !== "active" ||
  hotwordGovernance?.manualPublish?.currentPackVersionId !==
    hotwordGovernance?.candidatePack?.versionId
) {
  fail("platform BFF E2E hotword manual publication is incomplete", {
    value: hotwordGovernance?.manualPublish
  });
}
assertSignedHotwordCompletion(hotwordGovernance.manualPublish, "hotword publish");
if (
  hotwordGovernance?.asrDiffCorrection?.badcaseId !== "A-4107" ||
  !hotwordGovernance?.asrDiffCorrection?.correctionId ||
  !hotwordGovernance?.asrDiffCorrection?.traceId ||
  !hotwordGovernance?.asrDiffCorrection?.originalBadcaseTraceId ||
  hotwordGovernance?.asrDiffCorrection?.traceId ===
    hotwordGovernance?.asrDiffCorrection?.originalBadcaseTraceId ||
  hotwordGovernance?.asrDiffCorrection?.finalStatus !== "pending-backflow" ||
  hotwordGovernance?.asrDiffCorrection?.currentPublishedHotwordPackVersionId !==
    hotwordGovernance?.candidatePack?.versionId ||
  !hotwordGovernance?.asrDiffCorrection?.evidenceStorageObjectId ||
  hotwordGovernance?.asrDiffCorrection?.sourceMaterializationId !==
    "mat_asr_20250526_122300" ||
  hotwordGovernance?.asrDiffCorrection?.reusedExisting !== true ||
  hotwordGovernance?.asrDiffCorrection?.badcaseCountBefore < 1 ||
  hotwordGovernance?.asrDiffCorrection?.badcaseCountAfter !==
    hotwordGovernance?.asrDiffCorrection?.badcaseCountBefore ||
  hotwordGovernance?.asrDiffCorrection?.postRequestCount !== 0 ||
  hotwordGovernance?.asrDiffCorrection?.statEligibility !== "discovery-only" ||
  hotwordGovernance?.asrDiffCorrection?.eligibleForReleaseGate !== false ||
  hotwordGovernance?.asrDiffCorrection?.uiAction !== "annotation-correction" ||
  hotwordGovernance?.asrDiffCorrection?.deepLinkAction !== "deep-link"
) {
  fail("platform BFF E2E ASR Diff did not reuse the existing evidence-bound hotword Badcase", {
    value: hotwordGovernance?.asrDiffCorrection
  });
}
if (
  hotwordGovernance?.taskVersionRelease?.taskVersionId !==
    hotwordGovernance?.manualPublish?.taskVersionId ||
  !hotwordGovernance?.taskVersionRelease?.publishRunId ||
  hotwordGovernance?.taskVersionRelease?.publishHttpStatus !== 202 ||
  !hotwordGovernance?.taskVersionRelease?.publishTraceId ||
  hotwordGovernance?.taskVersionRelease?.decisionHttpStatus !== 200 ||
  !hotwordGovernance?.taskVersionRelease?.decisionTraceId ||
  hotwordGovernance?.taskVersionRelease?.finalStatus !== "published" ||
  hotwordGovernance?.taskVersionRelease?.hotwordPackVersionId !==
    hotwordGovernance?.candidatePack?.versionId
) {
  fail("platform BFF E2E generated TaskVersion did not pass its real release gate", {
    value: hotwordGovernance?.taskVersionRelease
  });
}
if (
  !hotwordGovernance?.controlledBackfill?.runId ||
  hotwordGovernance?.controlledBackfill?.requestHttpStatus !== 202 ||
  !hotwordGovernance?.controlledBackfill?.requestTraceId ||
  hotwordGovernance?.controlledBackfill?.dispatchAdapter !== "dagster" ||
  hotwordGovernance?.controlledBackfill?.completionHttpStatus !== 200 ||
  hotwordGovernance?.controlledBackfill?.finalStatus !== "success" ||
  hotwordGovernance?.controlledBackfill?.sourceMaterializationId !==
    "mat_asr_20250526_122300" ||
  hotwordGovernance?.controlledBackfill?.requestMaterializationId !==
    hotwordGovernance?.controlledBackfill?.sourceMaterializationId ||
  hotwordGovernance?.controlledBackfill?.requestSourceMaterializationId !==
    hotwordGovernance?.controlledBackfill?.sourceMaterializationId ||
  !hotwordGovernance?.controlledBackfill?.newMaterializationId ||
  hotwordGovernance?.controlledBackfill?.overwriteHistory !== false ||
  hotwordGovernance?.controlledBackfill?.rootTraceId !==
    hotwordGovernance?.candidatePack?.rootTraceId
) {
  fail("platform BFF E2E governed hotword backfill is incomplete or overwrote history", {
    value: hotwordGovernance?.controlledBackfill
  });
}
assertSignedHotwordCompletion(hotwordGovernance.controlledBackfill, "hotword controlled backfill");
assertGovernedStorageProof(hotwordGovernance.controlledBackfill, {
  label: "hotword controlled backfill",
  expectedCount: 2,
  sourceType: "asset_backfill",
  sourceId: hotwordGovernance.controlledBackfill.runId,
  rootTraceId: hotwordGovernance.controlledBackfill.rootTraceId
});
const backfillMaterializationRefs = Array.isArray(
  hotwordGovernance?.controlledBackfill?.materializationStorageRefs
)
  ? hotwordGovernance.controlledBackfill.materializationStorageRefs
  : [];
const backfillRegistered = hotwordGovernance.controlledBackfill.registeredStorageObjects || [];
const invalidMaterializationRefs = backfillMaterializationRefs.filter((reference) => {
  const registration = backfillRegistered.find(
    (item) => item.storageObjectId === reference?.storage_object_id
  );
  return (
    !registration ||
    reference?.status !== "verified" ||
    reference?.run_id !== hotwordGovernance.controlledBackfill.runId ||
    reference?.content_sha256 !== registration.contentSha256 ||
    reference?.provider !== registration.provider ||
    reference?.bucket !== registration.bucket ||
    reference?.object_key !== registration.objectKey
  );
});
if (
  backfillMaterializationRefs.length !== 2 ||
  invalidMaterializationRefs.length ||
  new Set(backfillMaterializationRefs.map((item) => item.storage_object_id)).size !== 2
) {
  fail("hotword materialization does not reference this backfill's newly verified JSONL and manifest", {
    backfillMaterializationRefs,
    backfillRegistered,
    invalidMaterializationRefs
  });
}

const canvasWrites = coverageByModule.get("canvas")?.writes || [];
const hotwordTaskVersionRelease = canvasWrites.find(
  (item) => item.key === "hotwordTaskVersionRelease"
);
if (!hotwordTaskVersionRelease || hotwordTaskVersionRelease.via !== "ui-click") {
  fail("platform BFF E2E hotword TaskVersion release must be covered by UI click", {
    value: hotwordTaskVersionRelease,
    canvasWrites
  });
}
const assetWrites = coverageByModule.get("assets")?.writes || [];
const hotwordControlledBackfill = assetWrites.find(
  (item) => item.key === "hotwordControlledBackfill"
);
if (!hotwordControlledBackfill || hotwordControlledBackfill.via !== "ui-click") {
  fail("platform BFF E2E hotword controlled backfill must be covered by UI click", {
    value: hotwordControlledBackfill,
    assetWrites
  });
}
const listeningCoverage = coverageByModule.get("listening");
const reusedHotwordBadcase = listeningCoverage?.read?.reusedObjects?.find(
  (item) => item.id === hotwordGovernance?.asrDiffCorrection?.badcaseId
);
if (
  !reusedHotwordBadcase ||
  reusedHotwordBadcase.via !== "deep-link" ||
  reusedHotwordBadcase.traceId !==
    hotwordGovernance?.asrDiffCorrection?.originalBadcaseTraceId
) {
  fail("platform BFF E2E ASR Diff existing Badcase reuse must be covered by UI deep link", {
    value: reusedHotwordBadcase,
    listeningCoverage
  });
}
const listeningWrites = listeningCoverage?.writes || [];
const asrAnnotationCorrection = listeningWrites.find(
  (item) => item.key === "asrAnnotationCorrection"
);
if (
  !asrAnnotationCorrection ||
  asrAnnotationCorrection.via !== "ui-click" ||
  asrAnnotationCorrection.id !== hotwordGovernance?.asrDiffCorrection?.correctionId ||
  asrAnnotationCorrection.traceId !== hotwordGovernance?.asrDiffCorrection?.traceId ||
  asrAnnotationCorrection.statEligibility !== "discovery-only"
) {
  fail("platform BFF E2E ASR annotation correction must be covered by a UI write", {
    value: asrAnnotationCorrection,
    listeningWrites
  });
}
const insightWrites = coverageByModule.get("insights")?.writes || [];
const insightActionWrite = insightWrites.find((item) => item.key === "insightAction");
if (!insightActionWrite || insightActionWrite.via !== "ui-click") {
  fail("platform BFF E2E insightAction must be covered by UI click", {
    expected: { key: "insightAction", via: "ui-click" },
    value: insightActionWrite,
    insightWrites
  });
}
const insightReportWrite = insightWrites.find((item) => item.key === "insightReport");
if (!insightReportWrite || insightReportWrite.via !== "ui-click") {
  fail("platform BFF E2E insightReport must be covered by UI click", {
    expected: { key: "insightReport", via: "ui-click" },
    value: insightReportWrite,
    insightWrites
  });
}

for (const key of ["labelVersion", "evalRun", "feedbackTask", "insightAction", "insightReport"]) {
  if (!result[key]?.id || !result[key]?.traceId) {
    fail(`platform BFF E2E ${key} is missing id or traceId`, { value: result[key] });
  }
}
if (!result.projectCreate?.id || !result.projectCreate?.name || !result.projectCreate?.traceId) {
  fail("platform BFF E2E projectCreate is missing id, name, or traceId", {
    value: result.projectCreate
  });
}
if (audioImportFixtureSkipped) {
  if (
    requireRealStack ||
    audioImportClosedLoop.connectorWriteCount !== 0 ||
    audioImportClosedLoop.legacyPlatformSyncRequests !== 0 ||
    !audioImportClosedLoop.targetAssetKey ||
    !audioImportClosedLoop.sceneProfileId ||
    !audioImportClosedLoop.sceneProfileVersionId ||
    !/^[0-9a-f]{64}$/.test(audioImportClosedLoop.sceneProfileSnapshotSha256 ?? "")
  ) {
    fail(
      "real-stack P0 audio import cannot be skipped and local prerequisite skips must remain write-free",
      { requireRealStack, value: audioImportClosedLoop }
    );
  }
} else if (
  !audioImportClosedLoop?.connectorId ||
  !audioImportClosedLoop?.connectorTraceId ||
  !audioImportClosedLoop?.platformConnectionId ||
  !audioImportClosedLoop?.taskVersionId ||
  !audioImportClosedLoop?.taskRunId ||
  !audioImportClosedLoop?.importBatchId ||
  !audioImportClosedLoop?.audioSessionId ||
  !audioImportClosedLoop?.rootTraceId ||
  audioImportClosedLoop?.status !== "succeeded" ||
  audioImportClosedLoop?.executionMode !== "production" ||
  audioImportClosedLoop?.previewCount !== 3 ||
  !(audioImportClosedLoop?.total >= 1) ||
  !(audioImportClosedLoop?.succeeded >= 1) ||
  audioImportClosedLoop?.failed !== 0 ||
  audioImportClosedLoop?.playbackGrantStatus !== 201 ||
  audioImportClosedLoop?.playbackStatus !== 206 ||
  !Number.isInteger(audioImportClosedLoop?.connectorWriteCount) ||
  audioImportClosedLoop.connectorWriteCount < 1 ||
  audioImportClosedLoop.connectorWriteCount > 2 ||
  audioImportClosedLoop?.pageRefreshRecovered !== true ||
  audioImportClosedLoop?.rootTraceReadable !== true ||
  audioImportClosedLoop?.legacyPlatformSyncRequests !== 0 ||
  !audioImportClosedLoop?.targetAssetKey ||
  !audioImportClosedLoop?.sceneProfileId ||
  !audioImportClosedLoop?.sceneProfileVersionId ||
  !/^[0-9a-f]{64}$/.test(audioImportClosedLoop?.sceneProfileSnapshotSha256 ?? "")
) {
  fail(
    "platform BFF E2E audio import did not close connector, immutable version, production run, batch, session and playback",
    { value: audioImportClosedLoop }
  );
}
if (
  !result.dataExportAction?.id ||
  !result.dataExportAction?.traceId ||
  !result.dataExportAction?.status ||
  !result.dataExportAction?.sceneProfileId ||
  !result.dataExportAction?.sceneProfileVersionId ||
  !/^[0-9a-f]{64}$/.test(result.dataExportAction?.sceneProfileSnapshotSha256 ?? "")
) {
  fail("platform BFF E2E dataExportAction is missing its run receipt or immutable SceneProfile lock", {
    value: result.dataExportAction
  });
}
if (
  result.dataSceneProfileGate?.status !== "blocked" ||
  result.dataSceneProfileGate?.reasonCode !== "SCENE_PROFILE_BINDING_REQUIRED" ||
  result.dataSceneProfileGate?.connectorPostCount !== 0 ||
  result.dataSceneProfileGate?.exportPostCount !== 0
) {
  fail("platform BFF E2E Data writes must fail closed without an active SceneProfile binding", {
    value: result.dataSceneProfileGate
  });
}
if (
  result.voiceprintEnrollmentGate?.status !== "blocked" ||
  result.voiceprintEnrollmentGate?.reasonCode !== "VOICEPRINT_CANDIDATE_READ_MODEL_UNAVAILABLE" ||
  result.voiceprintEnrollmentGate?.postCount !== 0
) {
  fail("platform BFF E2E must fail closed while the authoritative voiceprint candidate read model is unavailable", {
    value: result.voiceprintEnrollmentGate
  });
}
if (!result.insightReport?.runId) {
  fail("platform BFF E2E insightReport is missing backing runId", {
    value: result.insightReport
  });
}

for (const key of ["boundary", "eventLink", "annotation", "decision", "appeal"]) {
  if (!result.listeningActions?.[key]?.id || !result.listeningActions?.[key]?.traceId) {
    fail(`platform BFF E2E listening ${key} is missing id or traceId`, {
      value: result.listeningActions?.[key]
    });
  }
}
const listeningAppeal = result.listeningActions?.appeal;
if (
  listeningAppeal?.status !== "submitted" ||
  !listeningAppeal?.sourceDecisionId ||
  listeningAppeal.sourceDecisionId !== result.listeningActions?.decision?.decisionId
) {
  fail("platform BFF E2E quality appeal is not bound to the immutable source decision", {
    appeal: listeningAppeal,
    decision: result.listeningActions?.decision
  });
}
const listeningRecording = result.listeningActions?.recording;
if (
  listeningRecording?.audioSessionId !== "S20250526-000128" ||
  listeningRecording?.grantPath !== "/api/v1/audio-sessions/S20250526-000128/playback-grants" ||
  listeningRecording?.grantStatus !== 201 ||
  listeningRecording?.grantState !== "active" ||
  !listeningRecording?.grantTraceId ||
  Number.isNaN(Date.parse(listeningRecording?.grantExpiresAt || "")) ||
  listeningRecording?.playbackPath !== "/api/v1/audio-playback" ||
  listeningRecording?.mediaStatus !== 206 ||
  listeningRecording?.acceptRanges !== "bytes" ||
  !String(listeningRecording?.range || "").startsWith("bytes=") ||
  listeningRecording?.resourceType !== "media" ||
  listeningRecording?.mediaHasAuthorization !== false ||
  listeningRecording?.mediaHasTenantContext !== false ||
  listeningRecording?.grantRequestCount !== 1 ||
  listeningRecording?.requestOrder?.[0] !== "grant" ||
  listeningRecording?.requestOrder?.[1] !== "media"
) {
  fail("platform BFF E2E listening recording is missing grant-first headerless Range proof", {
    value: listeningRecording
  });
}
const playbackGrantWrite = (coverageByModule.get("listening")?.writes || []).find(
  (write) => write.key === "playbackGrant"
);
if (
  playbackGrantWrite?.via !== "ui-click" ||
  !playbackGrantWrite?.id ||
  playbackGrantWrite?.traceId !== listeningRecording.grantTraceId
) {
  fail("platform BFF E2E playback grant must be covered by a UI click", {
    playbackGrantWrite,
    listeningRecording
  });
}

if (
  !result.evaluationPromptUi?.evalRunId ||
  !result.evaluationPromptUi?.evalTraceId ||
  !result.evaluationPromptUi?.feedbackTaskId ||
  !result.evaluationPromptUi?.feedbackTraceId
) {
  fail("platform BFF E2E evaluationPromptUi is missing eval or feedback trace", {
    value: result.evaluationPromptUi
  });
}
if (
  !result.evaluationBadcaseUi?.evalRunId ||
  !result.evaluationBadcaseUi?.evalTraceId ||
  !result.evaluationBadcaseUi?.feedbackRunId ||
  !result.evaluationBadcaseUi?.feedbackTaskId ||
  !result.evaluationBadcaseUi?.feedbackTraceId
) {
  fail("platform BFF E2E evaluationBadcaseUi is missing eval or feedback trace", {
    value: result.evaluationBadcaseUi
  });
}

for (const key of ["saveDraft", "publishGate", "runOnce"]) {
  const receipt = result.canvasToolbarActions?.[key];
  if (!receipt?.id || !receipt?.traceId) {
    fail(`platform BFF E2E canvas toolbar ${key} is missing id or traceId`, {
      value: receipt
    });
  }
  const expectedStatus = key === "publishGate" ? "success" : key === "runOnce" ? "pending" : undefined;
  if (expectedStatus && receipt.status !== expectedStatus) {
    fail(`platform BFF E2E canvas toolbar ${key} expected status ${expectedStatus}`, {
      value: receipt
    });
  }
}

const expectedDomainPageActions = {
  knowledgeSync: "knowledge_sync",
  knowledgeIndex: "knowledge_build",
  settingsProviderTest: "provider_test"
};
for (const [key, expectedRunType] of Object.entries(expectedDomainPageActions)) {
  const receipt = result.domainPageActions?.[key];
  if (!receipt?.id || !receipt?.traceId || !receipt?.status || !receipt?.runType) {
    fail(`platform BFF E2E domain page action ${key} is missing id, traceId, status, or runType`, {
      value: receipt
    });
  }
  if (receipt.status !== "pending" || receipt.runType !== expectedRunType) {
    fail(`platform BFF E2E domain page action ${key} has unexpected status or runType`, {
      expected: { status: "pending", runType: expectedRunType },
      value: receipt
    });
  }
}
const tenantAudioImportPull = result.domainPageActions?.tenantAudioImportPull;
if (audioImportFixtureSkipped) {
  if (
    tenantAudioImportPull?.status !== "skipped" ||
    tenantAudioImportPull?.reasonCode !== "REAL_AUDIO_IMPORT_FIXTURE_REQUIRED" ||
    tenantAudioImportPull?.legacyPlatformSyncRequests !== 0
  ) {
    fail("tenant audio pull prerequisite skip must be explicit and must not call the legacy endpoint", {
      value: tenantAudioImportPull
    });
  }
} else if (
  !tenantAudioImportPull?.taskRunId ||
  !tenantAudioImportPull?.importBatchId ||
  tenantAudioImportPull?.taskVersionId !== audioImportClosedLoop.taskVersionId ||
  !tenantAudioImportPull?.traceId ||
  !["pending", "queued", "submitted"].includes(tenantAudioImportPull?.status) ||
  tenantAudioImportPull?.executionMode !== "production" ||
  tenantAudioImportPull?.legacyPlatformSyncRequests !== 0
) {
  fail("tenant pull must reuse the published audio import TaskVersion through a production TaskRun", {
    value: tenantAudioImportPull,
    audioImportClosedLoop
  });
}
if (!audioImportFixtureSkipped) {
  const dataAudioImportWrite = (coverageByModule.get("data")?.writes || []).find(
    (item) => item.key === "audioImportClosedLoop"
  );
  const tenantAudioImportWrite = (coverageByModule.get("tenant")?.writes || []).find(
    (item) => item.key === "tenantAudioImportPull"
  );
  if (
    dataAudioImportWrite?.via !== "ui-click" ||
    dataAudioImportWrite?.id !== audioImportClosedLoop.taskRunId ||
    dataAudioImportWrite?.traceId !== audioImportClosedLoop.rootTraceId ||
    dataAudioImportWrite?.importBatchId !== audioImportClosedLoop.importBatchId ||
    dataAudioImportWrite?.audioSessionId !== audioImportClosedLoop.audioSessionId ||
    dataAudioImportWrite?.executionMode !== "production" ||
    dataAudioImportWrite?.playbackStatus !== 206
  ) {
    fail("data module must cover the entire audio import closed loop through UI clicks", {
      value: dataAudioImportWrite,
      audioImportClosedLoop
    });
  }
  if (
    tenantAudioImportWrite?.via !== "ui-click" ||
    tenantAudioImportWrite?.id !== tenantAudioImportPull.taskRunId ||
    tenantAudioImportWrite?.traceId !== tenantAudioImportPull.traceId ||
    tenantAudioImportWrite?.taskVersionId !== audioImportClosedLoop.taskVersionId ||
    tenantAudioImportWrite?.importBatchId !== tenantAudioImportPull.importBatchId ||
    tenantAudioImportWrite?.executionMode !== "production"
  ) {
    fail("tenant module must reuse the published audio import configuration through UI clicks", {
      value: tenantAudioImportWrite,
      tenantAudioImportPull
    });
  }
}

const globalExportAction = result.globalExportAction;
if (
  !globalExportAction?.id ||
  !globalExportAction?.traceId ||
  !globalExportAction?.status ||
  !globalExportAction?.runType
) {
  fail("platform BFF E2E global export action is missing id, traceId, status, or runType", {
    value: globalExportAction
  });
}
if (globalExportAction.status !== "pending" || globalExportAction.runType !== "export") {
  fail("platform BFF E2E global export action has unexpected status or runType", {
    expected: { status: "pending", runType: "export" },
    value: globalExportAction
  });
}

const controlledExperiment = result.canvasToolbarActions?.experiment;
const controlledExperimentConfig = controlledExperiment?.configuration;
const controlledExperimentProvenance = controlledExperiment?.metricProvenance;
const controlledExperimentArms = Array.isArray(controlledExperiment?.arms)
  ? controlledExperiment.arms
  : [];
const controlledExperimentArmKeys = controlledExperimentArms
  .map((arm) => arm?.armKey)
  .sort();
const isSha256 = (value) => /^[0-9a-f]{64}$/.test(value || "");
if (
  !controlledExperiment?.id ||
  !controlledExperiment?.traceId ||
  controlledExperiment?.status !== "running" ||
  !controlledExperiment?.runId ||
  !controlledExperiment?.runTraceId ||
  !["control", "candidate"].includes(controlledExperiment?.arm) ||
  !controlledExperiment?.metricSnapshotId ||
  !controlledExperiment?.metricSnapshotTraceId ||
  controlledExperiment?.verdict !== "insufficient_sample" ||
  controlledExperiment?.outcomeCount !== 2 ||
  controlledExperimentConfig?.candidateAllocationPpm !== 300000 ||
  controlledExperimentConfig?.allocationUnit !== "conversation" ||
  controlledExperimentConfig?.minSampleSizePerArm !== 20 ||
  controlledExperimentConfig?.confidenceLevel !== 0.99 ||
  !controlledExperimentConfig?.controlTaskVersionId ||
  !controlledExperimentConfig?.candidateTaskVersionId ||
  controlledExperimentConfig.controlTaskVersionId === controlledExperimentConfig.candidateTaskVersionId ||
  controlledExperimentConfig?.variantDimension !== "workflow" ||
  JSON.stringify(controlledExperimentConfig?.actualChangedDimensions) !== JSON.stringify(["workflow"]) ||
  !isSha256(controlledExperimentConfig?.variantDiffSha256) ||
  JSON.stringify(controlledExperimentArmKeys) !== JSON.stringify(["candidate", "control"]) ||
  controlledExperimentArms.some(
    (arm) =>
      !arm?.taskVersionId ||
      !isSha256(arm?.taskVersionBehaviorSha256) ||
      !isSha256(arm?.taskVersionBindingSha256)
  ) ||
  !isSha256(controlledExperiment?.runExpectedBindingSha256) ||
  controlledExperiment?.runExecutedBindingSha256 !== controlledExperiment?.runExpectedBindingSha256 ||
  controlledExperiment?.sampleRatioDiagnostic?.status !== "pass" ||
  controlledExperiment?.sampleRatioDiagnostic?.detected !== false ||
  controlledExperiment?.sampleRatioDiagnostic?.assignment?.total !== 1 ||
  controlledExperimentProvenance?.factSource !== "signed_task_run_completion" ||
  controlledExperimentProvenance?.sourceRunCount !== 1 ||
  controlledExperimentProvenance?.completionReceiptCount !== 1 ||
  controlledExperimentProvenance?.calculatorEngine !== "auris.experiment.metric-engine/v2" ||
  !isSha256(controlledExperimentProvenance?.evidenceSha256)
) {
  fail("platform BFF E2E controlled experiment did not close configuration, execution, metrics, and provenance", {
    value: controlledExperiment
  });
}

const expectedCoreFlows = [
  "taskPublish",
  "labelPublish",
  "audioIngest",
  "audioIntelligence",
  "knowledgeSync",
  "assetQualityRetry",
  "externalCallback",
  "exportRun",
  "settingsPublish"
];
const expectedCoreFlowSemantics = {
  taskPublish: { status: "blocked", runType: "task_version_publish" },
  labelPublish: { status: "completed", runType: "release_deployment" },
  audioIngest: { status: "pending", runType: "audio_ingest" },
  audioIntelligence: { status: "pending", runType: "audio_intelligence" },
  knowledgeSync: { status: "pending", runType: "knowledge_sync" },
  assetQualityRetry: { status: "pending", runType: "asset_check_retry" },
  externalCallback: { status: "pending", runType: "external_callback" },
  exportRun: { status: "pending", runType: "export" },
  settingsPublish: { status: "success", runType: "settings_publish" }
};
for (const key of expectedCoreFlows) {
  const receipt = result.coreFlows?.[key];
  if (!receipt?.id || !receipt?.traceId || !receipt?.status || !receipt?.runType) {
    fail(`platform BFF E2E core flow ${key} is missing id, traceId, status, or runType`, {
      value: receipt
    });
  }
  const expected = expectedCoreFlowSemantics[key];
  if (receipt.status !== expected.status || receipt.runType !== expected.runType) {
    fail(`platform BFF E2E core flow ${key} has unexpected status or runType`, {
      expected,
      value: receipt
    });
  }
}
const audioImportCoreFlow = result.coreFlows?.audioImport;
if (audioImportFixtureSkipped) {
  if (
    audioImportCoreFlow?.status !== "skipped" ||
    audioImportCoreFlow?.reasonCode !== "REAL_AUDIO_IMPORT_FIXTURE_REQUIRED"
  ) {
    fail("core audio import prerequisite skip is missing", {
      value: audioImportCoreFlow
    });
  }
} else if (
  audioImportCoreFlow?.id !== audioImportClosedLoop.taskRunId ||
  audioImportCoreFlow?.traceId !== audioImportClosedLoop.rootTraceId ||
  audioImportCoreFlow?.status !== "succeeded" ||
  audioImportCoreFlow?.runType !== "audio_import" ||
  audioImportCoreFlow?.taskVersionId !== audioImportClosedLoop.taskVersionId ||
  audioImportCoreFlow?.importBatchId !== audioImportClosedLoop.importBatchId ||
  audioImportCoreFlow?.audioSessionId !== audioImportClosedLoop.audioSessionId ||
  audioImportCoreFlow?.executionMode !== "production" ||
  audioImportCoreFlow?.playbackStatus !== 206
) {
  fail("core audio import evidence does not match the browser-closed P0 chain", {
    value: audioImportCoreFlow,
    audioImportClosedLoop
  });
}

const labelPublish = result.coreFlows?.labelPublish;
const labelPublishTransitions = labelPublish?.transitions;
const requiredLabelReleaseTransitions = [
  ["publish", "shadowing"],
  ["approveGray", "gray-releasing"],
  ["promote", "completed"]
];
const invalidLabelReleaseTransitions = requiredLabelReleaseTransitions.filter(([key, status]) => {
  const transition = labelPublishTransitions?.[key];
  return (
    !transition?.commandId ||
    !transition?.commandRunId ||
    !transition?.commandTraceId ||
    !transition?.completionTraceId ||
    transition?.status !== status
  );
});
if (
  !/^[0-9a-f]{64}$/.test(labelPublish?.bundleSha256 || "") ||
  !labelPublish?.rollbackTargetDeploymentId ||
  labelPublishTransitions?.monitor?.status !== "monitoring" ||
  invalidLabelReleaseTransitions.length
) {
  fail("platform BFF E2E label release did not close publish, gray monitoring, and promotion", {
    value: labelPublish,
    invalidLabelReleaseTransitions
  });
}

for (const key of ["pageErrors", "requestFailures", "unexpectedConsoleErrors", "unexpectedFailedResponses"]) {
  if (Array.isArray(result[key]) && result[key].length > 0) {
    fail(`platform BFF E2E contains ${key}`, { [key]: result[key] });
  }
}

const expectedCancellationValidation = validateExpectedAssetReadCancellations(result);
if (
  expectedCancellationValidation.invalidExpectedRequestFailures.length ||
  expectedCancellationValidation.policyViolations.length
) {
  fail("platform BFF E2E contains ungoverned expected request cancellations", {
    ...expectedCancellationValidation
  });
}

const failedResponses = Array.isArray(result.failedResponses) ? result.failedResponses : [];
if (failedResponses.length) {
  fail("platform BFF E2E has browser failed responses", { failedResponses });
}

console.log(
  JSON.stringify(
    {
      status: "ok",
      resultPath,
      uiMutationModules: [...modules],
      failedResponses,
      checkedObjects: ["labelVersion", "evalRun", "feedbackTask", "insightAction", "insightReport"],
      checkedProjectCreate: "projectCreate",
      checkedAudioImportClosedLoop:
        "connector -> test/preview -> immutable TaskVersion -> production TaskRun -> ImportBatch -> AudioSession -> playback grant/range",
      checkedDataExportAction: "dataExportAction",
      checkedDataSceneProfileGate: "dataSceneProfileGate",
      checkedVoiceprintEnrollmentGate: "voiceprintEnrollmentGate",
      checkedListeningActions: ["boundary", "eventLink", "annotation", "decision", "appeal", "asrHotwordCorrection"],
      checkedListeningRecording: "playback-grant -> headerless media Range",
      checkedEvaluationPromptUi: "evaluationPromptUi",
      checkedEvaluationBadcaseUi: "evaluationBadcaseUi",
      checkedHotwordGovernance: {
        verified: [
          "statistics",
          "badcaseReview",
          "candidatePack",
          "fixedEvaluation",
          "modelApproval",
          "manualPublish",
          "asrDiffCorrection",
          "taskVersionRelease",
          "controlledBackfill"
        ]
      },
      checkedCanvasToolbarActions: ["saveDraft", "publishGate", "runOnce"],
      checkedDomainPageActions: Object.keys(expectedDomainPageActions),
      checkedTenantAudioImportPull: "published TaskVersion -> production TaskRun -> ImportBatch readback",
      checkedGlobalExportAction: "globalExportAction",
      checkedCoreFlows: expectedCoreFlows,
      checkedCoverageModules: expectedCoverageModules,
      checkedWriteCoverageModules: expectedWriteCoverageModules,
      checkedUiClickWriteCoverageModules: expectedWriteCoverageModules
    },
    null,
    2
  )
);
