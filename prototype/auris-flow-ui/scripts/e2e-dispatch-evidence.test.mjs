import assert from "node:assert/strict";
import test from "node:test";

import {
  hasForbiddenPublicDispatchEvidence,
  isPublicDispatchBoundary,
  publicDispatchIdentityMatches,
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
    hasForbiddenPublicDispatchEvidence({ status: "submitted", dispatch }),
    true
  );
  assert.equal(
    hasForbiddenPublicDispatchEvidence({ status: "submitted", external_run_id: "run-1" }),
    true
  );
  for (const key of [
    "adapterDispatch",
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
