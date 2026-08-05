import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

const checkerPath = new URL("./check-platform-bff-result.mjs", import.meta.url).pathname;

function focusedArtifact() {
  const completedAt = new Date();
  const startedAt = new Date(completedAt.getTime() - 1_000);
  return {
    schema_version: "auris.audio-import-browser-e2e.v2",
    status: "ok",
    stage: "completed",
    mode: "audio-import-only",
    runId: "focused-audio-import-checker",
    baseUrl: "http://127.0.0.1:5173/",
    startedAt: startedAt.toISOString(),
    completedAt: completedAt.toISOString(),
    executionProfile: {
      realStack: true,
      platformSource: "https",
      inferenceProvider: "https",
      dagster: "real",
      objectStorage: "real",
      uiEvidencePolicy: "browser-clicks-and-bff-readback"
    },
    audioImportClosedLoop: {
      connectorId: "audio_import_connector_001",
      connectorTraceId: "trace_connector_001",
      platformConnectionId: "conn_platform_auth",
      taskVersionId: "task_version_audio_import_001",
      taskRunId: "task_run_audio_import_001",
      importBatchId: "import_batch_audio_import_001",
      audioSessionId: "audio_session_import_001",
      rootTraceId: "trace_audio_import_root_001",
      status: "succeeded",
      executionMode: "production",
      previewCount: 3,
      total: 3,
      succeeded: 3,
      duplicates: 0,
      failed: 0,
      playbackGrantStatus: 201,
      playbackStatus: 206,
      playbackUiBound: true,
      playbackRangeVerified: true,
      connectorWriteCount: 2,
      pageRefreshRecovered: true,
      coldContextRecovered: true,
      coldRecovery: {
        sessionStorageLength: 0,
        batchQuery:
          "?target_asset_key=auris%2Faudio%2Fraw_recordings&connector_id=connector_001&task_version_id=task_version_audio_import_001",
        status: "succeeded"
      },
      rootTraceReadable: true,
      legacyPlatformSyncRequests: 0,
      targetAssetKey: "auris/audio/raw_recordings",
      sceneProfileId: "scene_auto_sales_quality",
      sceneProfileVersionId: "scenev_auto_sales_quality_v1",
      sceneProfileSnapshotSha256: "a".repeat(64)
    },
    audioIntelligenceReviewClosedLoop: {
      audioSessionId: "audio_session_import_001",
      rootTraceId: "trace_audio_import_root_001",
      intelligenceRunId: "audio_intelligence_001",
      intelligenceRunStatus: "success",
      evidencePackId: "evidence_audio_001",
      evidenceStatus: "ready",
      evidenceSha256: "b".repeat(64),
      audioSha256: "c".repeat(64),
      storageObjectVersion: "minio-version-001",
      asrResultId: "audio_session_import_001:asr:audio_intelligence_001",
      reviewTaskId: "hrt_audio_001",
      reviewQueue: "audio_evidence_review",
      reviewDecisionId: "hrd_audio_001",
      reviewDecision: "modified",
      reviewStatus: "success",
      decisionCurrentTraceId: "trace_review_action_001",
      taskReadbackMatched: true,
      evidenceReadbackMatched: true,
      affectedObjectsReadBack: true,
      affectedObjectReadbackCount: 6,
      outputSinkBindings: 0,
      callbackReadbacks: 0,
      nextReviewTaskId: null,
      queueEmpty: true,
      traceRootMatched: true,
      traceNodeKinds: [
        "run",
        "import_batch",
        "import_item",
        "storage_object",
        "audio_recording",
        "audio_session",
        "asr_result",
        "evidence_pack",
        "human_review_task",
        "human_review_decision"
      ],
      traceEdgeCount: 8,
      noSeedSwitch: true
    },
    tenantAudioImportPull: {
      taskRunId: "task_run_audio_import_tenant_002",
      importBatchId: "import_batch_audio_import_tenant_002",
      taskVersionId: "task_version_audio_import_001",
      traceId: "trace_audio_import_tenant_002",
      status: "pending",
      executionMode: "production",
      legacyPlatformSyncRequests: 0
    },
    consoleErrors: [],
    pageErrors: [],
    requestFailures: [],
    failedResponses: []
  };
}

function runChecker(artifact) {
  const directory = mkdtempSync(join(tmpdir(), "auris-focused-audio-checker-"));
  const artifactPath = join(directory, "result.json");
  writeFileSync(artifactPath, `${JSON.stringify(artifact)}\n`, "utf8");
  try {
    return spawnSync(process.execPath, [checkerPath, artifactPath], {
      encoding: "utf8",
      env: {
        ...process.env,
        AURIS_REAL_STACK_E2E: "1",
        AURIS_E2E_RUN_ID: artifact.runId
      }
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("focused audio import checker accepts only the complete real browser chain", () => {
  const result = runChecker(focusedArtifact());
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /audio-import-browser-real-stack/);
});

for (const [name, mutate] of [
  [
    "governed skip",
    (artifact) => {
      artifact.audioImportClosedLoop.status = "skipped";
      artifact.audioImportClosedLoop.reasonCode = "REAL_AUDIO_IMPORT_FIXTURE_REQUIRED";
    }
  ],
  [
    "legacy platform sync fallback",
    (artifact) => {
      artifact.tenantAudioImportPull.legacyPlatformSyncRequests = 1;
    }
  ],
  [
    "non-real Dagster profile",
    (artifact) => {
      artifact.executionProfile.dagster = "local";
    }
  ],
  [
    "missing post-import review readback",
    (artifact) => {
      artifact.audioIntelligenceReviewClosedLoop.evidenceReadbackMatched = false;
    }
  ],
  [
    "incomplete affected-object readback",
    (artifact) => {
      artifact.audioIntelligenceReviewClosedLoop.affectedObjectReadbackCount = 2;
    }
  ],
  [
    "missing strong import trace kind",
    (artifact) => {
      artifact.audioIntelligenceReviewClosedLoop.traceNodeKinds =
        artifact.audioIntelligenceReviewClosedLoop.traceNodeKinds.filter(
          (kind) => kind !== "storage_object"
        );
    }
  ],
  [
    "missing bound callback receipt",
    (artifact) => {
      artifact.audioIntelligenceReviewClosedLoop.outputSinkBindings = 1;
      artifact.audioIntelligenceReviewClosedLoop.callbackReadbacks = 1;
    }
  ],
  [
    "missing cold browser recovery",
    (artifact) => {
      artifact.audioImportClosedLoop.coldContextRecovered = false;
    }
  ]
]) {
  test(`focused audio import checker rejects ${name}`, () => {
    const artifact = focusedArtifact();
    mutate(artifact);
    const result = runChecker(artifact);
    assert.equal(result.status, 1, {
      stdout: result.stdout,
      stderr: result.stderr
    });
    assert.match(result.stderr, /"status": "failed"/);
  });
}
