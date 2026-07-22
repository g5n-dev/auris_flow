import assert from "node:assert/strict";
import test from "node:test";

import {
  hasForbiddenPublicDispatchEvidence,
  isValidTerminalBusinessState,
  isPublicDispatchBoundary,
  publicDispatchIdentityMatches,
  readTrustedE2eCompletionEvidence,
  readTrustedE2eDispatchEvidence
} from "./e2e-dispatch-evidence.mjs";

const dispatch = {
  adapter: "dagster",
  operation: "run_request",
  status: "success",
  details: { external_run_id: "fake_dagster_run_001" }
};

test("public dispatch boundary uses business state without requiring internal fields", () => {
  assert.equal(
    isPublicDispatchBoundary({
      status: "submitted",
      business_status: "awaiting_completion",
      business_completion_required: true
    }),
    true
  );
  assert.equal(
    isPublicDispatchBoundary({
      status: "submitted",
      business_status: "awaiting_completion",
      business_completion_required: false
    }),
    false
  );
});

test("public dispatch identity is bound to run and request scope", () => {
  const data = {
    run_id: "label_opt_001",
    run_type: "label_optimization",
    tenant_id: "aurora_auto",
    project_id: "sales_qa"
  };
  const expected = {
    runId: "label_opt_001",
    tenantId: "aurora_auto",
    projectId: "sales_qa"
  };

  assert.equal(publicDispatchIdentityMatches(data, expected), true);
  assert.equal(
    publicDispatchIdentityMatches({ ...data, project_id: "other_project" }, expected),
    false
  );
  assert.equal(
    publicDispatchIdentityMatches({ ...data, run_id: "other_run" }, expected),
    false
  );
});

test("public dispatch boundary rejects leaked adapter protocol evidence", () => {
  assert.equal(hasForbiddenPublicDispatchEvidence({ status: "submitted" }), false);
  assert.equal(
    hasForbiddenPublicDispatchEvidence({
      status: "submitted",
      object_uri_template_name: "domain-artifact-template",
      sourceUrlTemplateName: "public-source-template"
    }),
    false
  );
  assert.equal(
    hasForbiddenPublicDispatchEvidence({ status: "submitted", dispatch }),
    true
  );
  assert.equal(
    hasForbiddenPublicDispatchEvidence({ status: "submitted", external_run_id: "run-1" }),
    true
  );
  for (const key of [
    "url",
    "uri",
    "downloadUrl",
    "adapterDispatch",
    "artifact_url",
    "artifactUrl",
    "manifest_uri",
    "processed_event_id",
    "failed_event_id",
    "dead_letter_event_id",
    "provider_artifact_ref",
    "provider",
    "provider_evidence",
    "providerEvidence",
    "access_key_id",
    "accessKeyId",
    "AWS_ACCESS_KEY_ID",
    "result_storage_object_ids",
    "result_storage_object_sha256",
    "storage-object-id",
    "callback_receipt_id",
    "objectUri",
    "graphql_url",
    "signatureNonce"
  ]) {
    assert.equal(
      hasForbiddenPublicDispatchEvidence({ status: "submitted", [key]: "internal" }),
      true,
      `expected ${key} to be rejected`
    );
  }
});

test("public dispatch boundary rejects sensitive evidence hidden in scalar values", () => {
  for (const value of [
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
    "authorization=Bearer abc123",
    "s3://auris-internal/tenant-a/private-result.json",
    "https://minio.internal:9000/tenant-a/private-result.json",
    "10.0.0.8",
    "127.0.0.1:9000",
    "2001:db8::1",
    "[::1]:9000",
    "localhost",
    "callback target localhost:9000",
    "backend endpoint 10.0.0.8:8080",
    "minio.internal:9000",
    "dagster run dg-private-001",
    "provider_evidence=openai:request-private-001"
  ]) {
    assert.equal(
      hasForbiddenPublicDispatchEvidence({ status: "submitted", display_value: value }),
      true,
      `expected scalar evidence to be rejected: ${value}`
    );
  }
});

test("public dispatch boundary keeps canonical business identifiers and same-origin routes", () => {
  assert.equal(
    hasForbiddenPublicDispatchEvidence({
      status: "submitted",
      business_status: "awaiting_completion",
      run_id: "label_optimization_001",
      label_version_id: "label_version_v1_9_0",
      store_id: "BJ-AURORA-001",
      model_version: "model.v1",
      error_code: "RUN_DEPENDENCY_BLOCKED",
      asset_key: "auris/label/event_tags",
      content_type: "application/json",
      route: "/settings/providers?tab=active",
      href: "/runs/task_run_001"
    }),
    false
  );
});

test("trusted reader invokes the scoped read-only helper without a shell", async () => {
  let invocation;
  const evidence = {
    run_id: "label_opt_001",
    run_type: "label_optimization",
    run_status: "submitted",
    business_status: "awaiting_completion",
    business_completion_required: true,
    event_id: 8,
    event_status: "processed",
    adapter: "dagster",
    external_id: "fake_dagster_run_001",
    dispatch
  };
  const execute = async (file, args, options) => {
    invocation = { file, args, options };
    return { stdout: JSON.stringify(evidence), stderr: "" };
  };

  const result = await readTrustedE2eDispatchEvidence(
    {
      runId: "label_opt_001",
      tenantId: "aurora_auto",
      projectId: "sales_qa",
      databaseUrl: "mysql+pymysql://user:secret@127.0.0.1/auris_test",
      pythonPath: "/opt/auris/backend/.venv/bin/python",
      helperPath: "/opt/auris/scripts/process_e2e_outbox_run.py",
      timeoutMs: 5000
    },
    execute
  );

  assert.deepEqual(result, evidence);
  assert.deepEqual(invocation.args, [
    "/opt/auris/scripts/process_e2e_outbox_run.py",
    "--read-only",
    "--tenant-id",
    "aurora_auto",
    "--project-id",
    "sales_qa",
    "label_opt_001"
  ]);
  assert.equal(invocation.file, "/opt/auris/backend/.venv/bin/python");
  assert.equal(invocation.options.shell, false);
  assert.equal(
    invocation.options.env.DATABASE_URL,
    "mysql+pymysql://user:secret@127.0.0.1/auris_test"
  );
});

test("trusted reader fails closed on run or dispatch identity drift", async () => {
  const execute = async () => ({
    stdout: JSON.stringify({
      run_id: "other_run",
      run_type: "label_optimization",
      run_status: "submitted",
      business_status: "awaiting_completion",
      business_completion_required: true,
      event_id: 8,
      event_status: "processed",
      adapter: "dagster",
      external_id: "fake_dagster_run_001",
      dispatch
    }),
    stderr: ""
  });

  await assert.rejects(
    readTrustedE2eDispatchEvidence(
      {
        runId: "label_opt_001",
        tenantId: "aurora_auto",
        projectId: "sales_qa",
        databaseUrl: "sqlite:////tmp/auris-test.sqlite",
        pythonPath: "/usr/bin/python3",
        helperPath: "/opt/auris/scripts/process_e2e_outbox_run.py"
      },
      execute
    ),
    /identity does not match/
  );
});

test("trusted reader requires an explicit successful dispatch status", async () => {
  const execute = async () => ({
    stdout: JSON.stringify({
      run_id: "label_opt_001",
      run_type: "label_optimization",
      run_status: "submitted",
      business_status: "awaiting_completion",
      business_completion_required: true,
      event_id: 8,
      event_status: "processed",
      adapter: "dagster",
      external_id: "fake_dagster_run_001",
      dispatch: { ...dispatch, status: undefined }
    }),
    stderr: ""
  });

  await assert.rejects(
    readTrustedE2eDispatchEvidence(
      {
        runId: "label_opt_001",
        tenantId: "aurora_auto",
        projectId: "sales_qa",
        databaseUrl: "sqlite:////tmp/auris-test.sqlite",
        pythonPath: "/usr/bin/python3",
        helperPath: "/opt/auris/scripts/process_e2e_outbox_run.py"
      },
      execute
    ),
    /bound external identity/
  );
});

test("trusted completion reader invokes scoped helper and returns only minimal evidence", async () => {
  let invocation;
  const rawEvidence = {
    verified: true,
    run_id: "hotword_build_001",
    run_type: "hotword_build",
    run_status: "success",
    business_status: "completed",
    business_completion_required: false,
    completion_receipt_id: "e2e_complete_hotword_build_001",
    completion_status: "success",
    receipt_state: "completed",
    status_code: 200,
    auth: {
      auth_mode: "signed_external_completion",
      binding_mode: "scoped_key_map",
      signature_mode: "hmac-sha256",
      key_id: "auris-e2e-completion",
      source: "dagster",
      tenant_id: "aurora_auto",
      project_id: "sales_qa",
      body_sha256: "b".repeat(64)
    },
    storage_objects: [
      {
        ordinal: 0,
        role: "manifest",
        content_sha256: "e".repeat(64),
        source_type: "hotword_build",
        source_id: "hotword_build_001",
        status: "verified",
        trace_id: "trace_hotword_build_001"
      }
    ]
  };
  const execute = async (file, args, options) => {
    invocation = { file, args, options };
    return { stdout: JSON.stringify(rawEvidence), stderr: "" };
  };

  const result = await readTrustedE2eCompletionEvidence(
    {
      runId: "hotword_build_001",
      tenantId: "aurora_auto",
      projectId: "sales_qa",
      completionReceiptId: "e2e_complete_hotword_build_001",
      adapter: "dagster",
      externalId: "fake_dagster_run_001",
      signatureKeyId: "auris-e2e-completion",
      source: "dagster",
      bodySha256: "b".repeat(64),
      nonce: "e2e-completion-nonce",
      databaseUrl: "mysql+pymysql://user:secret@127.0.0.1/auris_test",
      pythonPath: "/opt/auris/backend/.venv/bin/python",
      helperPath: "/opt/auris/scripts/process_e2e_outbox_run.py"
    },
    execute
  );

  assert.equal(invocation.file, "/opt/auris/backend/.venv/bin/python");
  assert.equal(invocation.options.shell, false);
  assert.deepEqual(invocation.args, [
    "/opt/auris/scripts/process_e2e_outbox_run.py",
    "--read-completion",
    "--tenant-id",
    "aurora_auto",
    "--project-id",
    "sales_qa",
    "--completion-receipt-id",
    "e2e_complete_hotword_build_001",
    "--expected-adapter",
    "dagster",
    "--expected-external-id",
    "fake_dagster_run_001",
    "--expected-signature-key-id",
    "auris-e2e-completion",
    "--expected-source",
    "dagster",
    "--expected-body-sha256",
    "b".repeat(64),
    "--expected-nonce",
    "e2e-completion-nonce",
    "hotword_build_001"
  ]);
  assert.equal(result.completionAuth.verified, true);
  assert.equal(result.storageObjects[0].sourceType, "hotword_build");
  const encoded = JSON.stringify(result);
  assert.doesNotMatch(encoded, /fake_dagster_run_001|e2e-completion-nonce/);
});

test("completion business state policy accepts only finite run-type and terminal-status pairs", () => {
  assert.equal(isValidTerminalBusinessState("hotword_build", "success", "completed"), true);
  assert.equal(
    isValidTerminalBusinessState("label_optimization", "success", "awaiting-review"),
    true
  );
  assert.equal(isValidTerminalBusinessState("eval_run", "blocked", "blocked"), true);
  assert.equal(isValidTerminalBusinessState("hotword_build", "success", "evaluating"), false);
  assert.equal(
    isValidTerminalBusinessState("hotword_build", "success", "future-terminal-state"),
    false
  );
  assert.equal(isValidTerminalBusinessState("hotword_build", "failed", "completed"), false);
  assert.equal(
    isValidTerminalBusinessState("hotword_build", "blocked", "awaiting-review"),
    false
  );
  assert.equal(isValidTerminalBusinessState("unknown_external_run", "success", "completed"), false);
});

test("trusted completion reader rejects internally consistent false-green business states", async () => {
  for (const businessStatus of ["evaluating", "future-terminal-state"]) {
    const execute = async () => ({
      stdout: JSON.stringify({
        verified: true,
        run_id: "run_001",
        run_type: "hotword_build",
        run_status: "success",
        business_status: businessStatus,
        business_completion_required: false,
        completion_receipt_id: "receipt_001",
        completion_status: "success",
        receipt_state: "completed",
        status_code: 200,
        auth: {
          auth_mode: "signed_external_completion",
          binding_mode: "scoped_key_map",
          signature_mode: "hmac-sha256",
          key_id: "auris-e2e-completion",
          source: "dagster",
          tenant_id: "aurora_auto",
          project_id: "sales_qa",
          body_sha256: "b".repeat(64)
        },
        storage_objects: []
      }),
      stderr: ""
    });

    await assert.rejects(
      readTrustedE2eCompletionEvidence(
        {
          runId: "run_001",
          tenantId: "aurora_auto",
          projectId: "sales_qa",
          completionReceiptId: "receipt_001",
          adapter: "dagster",
          externalId: "external_001",
          signatureKeyId: "auris-e2e-completion",
          source: "dagster",
          bodySha256: "b".repeat(64),
          nonce: "nonce_001",
          databaseUrl: "sqlite:////tmp/auris-test.sqlite",
          pythonPath: "/usr/bin/python3",
          helperPath: "/opt/auris/scripts/process_e2e_outbox_run.py"
        },
        execute
      ),
      /completion evidence drift/
    );
  }
});

test("trusted completion reader fails closed on authentication drift", async () => {
  const execute = async () => ({
    stdout: JSON.stringify({
      verified: true,
      run_id: "run_001",
      run_type: "task_run",
      run_status: "success",
      business_status: "completed",
      business_completion_required: false,
      completion_receipt_id: "receipt_001",
      completion_status: "success",
      receipt_state: "completed",
      status_code: 200,
      auth: {
        auth_mode: "signed_external_completion",
        binding_mode: "scoped_key_map",
        signature_mode: "hmac-sha256",
        key_id: "other-key",
        source: "dagster",
        tenant_id: "aurora_auto",
        project_id: "sales_qa",
        body_sha256: "b".repeat(64)
      },
      storage_objects: []
    }),
    stderr: ""
  });

  await assert.rejects(
    readTrustedE2eCompletionEvidence(
      {
        runId: "run_001",
        tenantId: "aurora_auto",
        projectId: "sales_qa",
        completionReceiptId: "receipt_001",
        adapter: "dagster",
        externalId: "external_001",
        signatureKeyId: "auris-e2e-completion",
        source: "dagster",
        bodySha256: "b".repeat(64),
        nonce: "nonce_001",
        databaseUrl: "sqlite:////tmp/auris-test.sqlite",
        pythonPath: "/usr/bin/python3",
        helperPath: "/opt/auris/scripts/process_e2e_outbox_run.py"
      },
      execute
    ),
    /completion evidence drift/
  );
});
