# Auris Flow Dagster runtime

This image is the production code location and control-plane runtime behind Auris Flow. Dagster
remains an internal execution engine: product clients submit and inspect Auris Flow runs through
the BFF rather than through Dagster APIs.

## Runtime contract

`RealDagsterClient` uses a server-owned allowlist rather than caller-provided `job_name` or
`run_config`. Known control-plane/CI events continue to launch `auris_flow_generic_job` with
top-level `runConfigData.auris_context`. `audio_intelligence.requested` plus the exact
`auris-flow-audio-intelligence-v1` contract launches `auris_flow_audio_intelligence_v1`; an
unknown/missing event mapping or incomplete contract fails before any GraphQL request.

Both jobs reject the run before work unless these authoritative scope fields are valid:

- `tenant_id`
- `project_id`
- `trace_id`
- `run_id`
- `dispatch_idempotency_key`
- `outbox_fencing_token` in `<lease-epoch>:<attempt>` form

The audio v1 job additionally requires a strict, extra-field-free immutable execution envelope.
It binds tenant/project/run/trace, dispatch idempotency and Outbox fencing, a server deadline,
provider/model/capabilities, and the input object's bucket, key, provider version ID, byte length,
content type and SHA-256. Only the envelope SHA-256 and non-sensitive routing identifiers are put
in Dagster tags; raw object locations and hashes stay in run config and the internal evidence
ledger.

The code location signs an exact `GET ?versionId=...` against the allowlisted MinIO/S3 bucket,
streams the response, checks the returned version ID/length/type, and recomputes SHA-256. It then
calls the configured HTTPS inference provider with a closed request bound to scope, model, exact
input, deadline, idempotency and fencing. The strict response validator rejects unknown fields,
binding drift, non-finite scores and bounded-result violations. There is no local or deterministic
semantic fallback.

Validated results are written as canonical JSON to a versioned MinIO/S3 derived object. The signed
completion contains only contract and manifest/result hashes; provider endpoint/token, input or
result object locators, and transcript content never enter the callback. Object-store and provider
credentials are mounted only into `dagster-code` as secret files. In prod/release, importing the
code location constructs and validates the HTTPS provider, explicit provider/model allowlist,
credential files and result store, so bad configuration prevents the gRPC code server from
becoming healthy. OBS/OSS exact-version readers remain unimplemented.

The generic job remains an explicit control-plane acknowledgement for known non-domain and CI
flows. It does not claim to perform ASR, embedding, evaluation, or other semantic work.

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

Completion delivery still occurs inline with compute. The result manifest is durable, but an
independently retryable callback dispatcher remains required for a fully recoverable inference
path.

The provider request contains only the immutable object identity (`storage_provider`, bucket,
key, version ID, content length/type and SHA-256). It never contains storage credentials, a
presigned URL, or the audio bytes. Operations must place the real provider on a trusted network
path to the object store and give it a separate least-privilege, read-only identity for the input
prefix. The provider must fetch the exact version and verify its length and SHA-256. Dagster
storage credentials must never be forwarded or reused by the provider. Both provider POST and
result-manifest PUT reject every HTTP redirect before credentials can be replayed.

The production-path HTTPS audio endpoint is a protocol fixture only
(`reference_protocol_only=true`, `model_quality_certified=false`). It verifies the wire contract
and TLS configuration but is not a model-quality benchmark. A release candidate still requires an
external Linux E2E against the selected real provider.

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
- `OTEL_TRACE_SAMPLE_RATIO` from `0` through `1` outside production; `prod`/`production`/
  `release` requires at least `0.01`;
- `OTEL_EXPORT_TIMEOUT_SECONDS` from greater than `0` through `30`;
- optional `OTEL_EXPORTER_OTLP_HEADERS` bindings.

Plaintext OTLP is limited to the internal `otel-collector` or loopback. Export attributes are
redacted again at the exporter boundary: credentials, tokens, cookies, SQL, dynamic URL paths,
queries, exception messages, and user information in URLs are not exported. In production, disabled
telemetry or invalid initialization fails code-location startup with a stable non-secret error;
transient export failure does not fail an already-running domain execution, but the independent
pipeline monitor and BFF strict `/readyz` turn readiness unhealthy until the exact forced BFF marker
can be queried back from Tempo with the expected service name, span name, and trace ID.

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
