# Auris Flow Dagster runtime

This image is the production code location and control-plane runtime behind Auris Flow. Dagster
remains an internal execution engine: product clients submit and inspect Auris Flow runs through
the BFF rather than through Dagster APIs.

## Runtime contract

`RealDagsterClient` uses a server-owned allowlist rather than caller-provided `job_name` or
`run_config`. Known control-plane/CI events continue to launch `auris_flow_generic_job` with
top-level `runConfigData.auris_context`. `audio_intelligence.requested` plus the exact
`auris-flow-audio-intelligence-v1` contract launches `auris_flow_audio_intelligence_v1`; an
audio-import TaskRun (`task_run.requested` plus `auris-flow-audio-import-v1`) launches the fixed
`auris_flow_audio_import_v1` job. Caller-provided `job_name`, Dagster canvas state and arbitrary
run config are never forwarded. An unknown/missing event mapping or incomplete contract fails
before any GraphQL request.

All three jobs reject the run before work unless these authoritative scope fields are valid:

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

The audio-import v1 envelope freezes the import batch/root trace, connector/version/platform
binding, platform scope, HTTPS origin/path, opaque credential reference, pagination and field
mapping, cursor policy, target asset, dedupe policy, and the exact tenant/project/run object
prefix. The code location rejects scope drift, unknown fields, plaintext or credential-bearing
URLs, non-public DNS results, redirects, oversized listing/audio responses and malformed WAV
content before any object is registered. Each platform request resolves the logical host once,
rejects the request if any answer is non-public, and opens the TCP connection directly to one of
those verified addresses while retaining the frozen hostname for TLS certificate verification,
SNI and the HTTP `Host` header. The HTTP stack cannot perform a second logical-host resolution.

Platform discovery uses bounded cursor pagination. Download URLs may contain an expiring query
signature, but are never returned in the result, written to the manifest, logged, or sent the
connector authorization header. CDN hosts must match the platform origin or the exact
`AURIS_PLATFORM_AUDIO_ALLOWED_HOSTS` allowlist. Audio is streamed into a bounded spooled file,
length/type/RIFF-WAVE structure and SHA-256 are verified, then conditionally written to a
content-addressed key under the frozen run prefix. A successful write must return a non-null exact
MinIO/S3 version ID and a canonical strong ETag; retries reuse the exact current version only when
its stored length/type/hash metadata and strong ETag are present. Missing, malformed or weak ETags
fail closed.

The platform tenant is enforced by a dedicated credential binding rather than by a caller-selected
query/header. The binding must exactly match tenant, project, platform connection, external tenant
and HTTPS origin before its headers can be loaded. If the frozen platform scope contains stores,
`field_mapping.store_ref` is mandatory and every source record is checked against that exact
allowlist before its audio URL is fetched. The frozen `cursor_policy.field` is also mandatory on
every record. In the v1 contract it is a persistent incremental cursor, not an opaque page-only
token: values must be bounded, comparable, unique and strictly increasing across the frozen
window. Every non-terminal `next_cursor_path` value must equal that page's final normalized record
cursor; an empty page cannot carry a next cursor. Type changes, equal/regressed values, a mismatched
next cursor, or a duplicate `external_record_id` fail the whole batch before any record on that
page is downloaded. This makes a page-boundary cursor safe to reuse in the next run without
silently advancing past an unverified record. Item failures still prevent the BFF from advancing
the candidate.

The execution deadline is enforced before network calls and around every listing, download and
object-upload chunk; socket timeout alone is not treated as a wall-clock deadline. A run also has
a hard total audio-byte budget. Each declared audio `Content-Length` is reserved before the first
body byte is read and remains charged even if download, MIME or WAV validation later fails, so
invalid items cannot bypass the budget.

After all pages finish, the job writes a canonical, content-addressed
`auris-flow-audio-import-manifest-v1`. The signed completion contains the manifest descriptor,
per-item source metadata and exact version/ETag object binding, all storage descriptors (including
the manifest ETag), batch status and
`total/succeeded/skipped/failed/next_cursor_candidate` metrics. A completed empty window and a
completed batch whose individual downloads all failed are both operationally successful
completions: `batch_status` remains the business result so the BFF can materialize zero items or
the concrete failures. Listing, pagination, credential, manifest or storage-control failures send
a failed completion instead.

Platform credentials are resolved only inside the code location. Prefer a secret-mounted JSON
file configured by `AURIS_PLATFORM_CREDENTIAL_BINDINGS_FILE`:

```json
{
  "secret://platform/recordings-reader": {
    "tenant_id": "aurora_auto",
    "project_id": "sales_qa",
    "platform_connection_id": "platform_connection_001",
    "platform_tenant_ref": "external_tenant_001",
    "base_url": "https://recordings.example.com",
    "headers": {
      "Authorization": "Bearer <secret>"
    }
  }
}
```

Allowed header names are `Authorization`, `X-API-Key`, and `X-Auth-Token`. As a simpler bearer-only
fallback is intentionally unsupported: a header-only token cannot prove its tenant, project,
platform-connection and origin binding. The opaque reference is used only as an exact JSON key and
is never interpolated into a filesystem path.

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

Audio import also reports executor-owned stages to
`/api/v1/runs/{run_id}/external-progress-receipts`: `downloading` immediately before the first
audio fetch and `verifying` after item processing but before the manifest write. Progress uses the
same scoped HMAC/keyring/replay protection as completion, with a stable
`dagster-progress:{dagster_run_id}:{stage}` idempotency key. Dagster cannot set the BFF-owned
`queued`, `listing`, `materializing`, `completed`, or `failed` stages.

`downloading` is a registration barrier: source download cannot begin until the BFF has verified,
persisted and acknowledged that signed stage receipt. Only the explicit transient conflict codes
`AUDIO_IMPORT_PROGRESS_DISPATCH_BINDING_MISSING`,
`AUDIO_IMPORT_PROGRESS_BATCH_NOT_RUNNING`, and `IDEMPOTENCY_REQUEST_IN_PROGRESS` are retryable.
They use exponential `0.5/1/2/4s` (4-second capped) backoff through the immutable execution
deadline; every retry keeps the body and idempotency key stable but uses a fresh timestamp and
nonce. All other HTTP 409 conflicts fail immediately. `verifying` retains the bounded five-attempt
delivery policy.

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

- `storage-bootstrap`: one-shot MySQL schema initialization and migration;
- `grpc`: code location on port 4000;
- `webserver`: internal GraphQL/web process on port 3000;
- `daemon`: run coordinator and heartbeat writer;
- `health`: daemon heartbeat liveness check.

`dagster_database_url` is loaded from its Docker secret file into `DAGSTER_MYSQL_URL` by the
entrypoint. Inline and file sources cannot be configured simultaneously, and the value is never
printed. Run, event-log, and schedule storage all use the same MySQL URL via `dagster.yaml`.
Compose runs `storage-bootstrap` with only that secret and a read-only root filesystem. The code
location, webserver, and daemon all require its successful completion, so no long-running Dagster
process races another process to create the initial schema.

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
