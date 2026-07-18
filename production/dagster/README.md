# Auris Flow Dagster runtime

This image is the production code location and control-plane runtime behind Auris Flow. Dagster
remains an internal execution engine: product clients submit and inspect Auris Flow runs through
the BFF rather than through Dagster APIs.

## Runtime contract

`RealDagsterClient` launches `auris_flow_generic_job` with top-level `runConfigData.auris_context`.
The code location rejects the run before domain work unless all of the following authoritative
fields are present and valid:

- `tenant_id`
- `project_id`
- `trace_id`
- `run_id`
- `dispatch_idempotency_key`
- `outbox_fencing_token` in `<lease-epoch>:<attempt>` form

The generic job currently performs an explicit control-plane acknowledgement. It does not claim
to perform ASR, embedding, evaluation, or other semantic work. Product-specific workflows can
replace `acknowledge_domain_workflow` while keeping the same scope and completion contract.

## Signed completion

The job reports either success or failure to
`/api/v1/runs/{run_id}/external-completion-receipts`. Its canonical HMAC message is kept under an
independent contract test against the BFF verifier.

- The completion receipt ID and business idempotency key are stable for a Dagster run.
- Every network attempt uses a fresh timestamp and nonce, so transport retries remain idempotent
  without weakening replay protection.
- The keyring is reloaded from `/run/secrets/completion_receipt_key_bindings` on each attempt.
- A single eligible Dagster key is selected automatically. During an overlapping key rotation,
  set `AURIS_COMPLETION_RECEIPT_ACTIVE_KEY_ID` explicitly; ambiguity fails closed.
- Key bindings must include `dagster` in `allowed_sources` and an exact tenant/project scope.
- Failure receipts and Dagster exceptions are sanitized and never include the original exception
  message or keyring contents.

`AURIS_BFF_INTERNAL_URL` defaults to the internal Compose address `http://bff:8000`. Production
plaintext HTTP is accepted only for the internal `bff`, loopback, or localhost host names.

## Trace propagation and telemetry

The BFF projects its current W3C trace parent into the authoritative `auris_context` as
`otel_trace_id`, `otel_parent_span_id`, and `otel_trace_flags`. The code location accepts those
fields only as an all-or-nothing set, rejects zero or malformed identifiers, reconstructs a remote
parent, and creates `auris_flow.domain.execute` as its child. `urllib` calls, including completion
callbacks, use the same tracer provider.

Telemetry is enabled with `OTEL_ENABLED=true`. Configure the code location with:

- `OTEL_EXPORTER_OTLP_ENDPOINT` (the Compose default is
  `http://otel-collector:4318/v1/traces`);
- `OTEL_SERVICE_NAME`;
- `OTEL_TRACE_SAMPLE_RATIO` from `0` through `1`;
- `OTEL_EXPORT_TIMEOUT_SECONDS` from greater than `0` through `30`;
- optional `OTEL_EXPORTER_OTLP_HEADERS` bindings.

Plaintext OTLP is limited to the internal `otel-collector` or loopback. Export attributes are
redacted again at the exporter boundary: credentials, tokens, cookies, SQL, URL queries, exception
messages, and user information in URLs are not exported. Invalid telemetry configuration or a
collector/exporter failure disables or drops telemetry and does not prevent code-location startup
or fail domain execution.

## Process roles

The same pinned, non-root image supports:

- `grpc`: code location on port 4000;
- `webserver`: internal GraphQL/web process on port 3000;
- `daemon`: run coordinator and heartbeat writer;
- `health`: daemon heartbeat liveness check.

`dagster_database_url` is loaded from its Docker secret file into `DAGSTER_MYSQL_URL` by the
entrypoint. Inline and file sources cannot be configured simultaneously, and the value is never
printed. Run, event-log, and schedule storage all use the same MySQL URL via `dagster.yaml`.

## Verification

From this directory:

```bash
uv sync --frozen --all-extras
uv run --frozen ruff format --check src tests
uv run --frozen ruff check src tests
uv run --frozen mypy src
uv run --frozen pytest
```

From the repository root:

```bash
docker build -f production/dagster/Dockerfile -t auris-flow-dagster:verify .
```
