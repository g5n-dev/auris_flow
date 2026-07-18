# Open Source Release Readiness

This document separates the current development baseline from production readiness.

## Current Development Baseline

Auris Flow currently proves a coherent evaluation, labeling, and insights platform baseline:

- Product surface: React prototype with labeling, evaluation, insights, assets, data, listening, and task configuration modules.
- Backend surface: FastAPI BFF with `/api/v1/*` contracts for labels, evaluation runs, insight metrics, reports, actions, data assets, human review, decisions, task runs, traces, and lineage.
- Runtime foundation: request context, structured logging, trace IDs, idempotency, audit records, outbox worker, seed data, Alembic migration smoke, and Docker local dependencies.
- Verification: `bash scripts/verify_fast.sh` runs spec validation, platform readiness, Python lint/type/tests, migration smoke, backend smoke, frontend build, and frontend UI smoke.
- Browser integration: release verification uses `bash scripts/verify_release.sh`, which forces `AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1`, autostarts a temporary SQLite-backed BFF and Vite UI, and exercises browser-to-BFF write paths for labels, eval runs, feedback tasks, insight actions, reports, task configuration, knowledge, assets, settings, trace lookup, and tenant/project request context. Browser observations must stay clean: no console errors, request failures, or 4xx/5xx page responses. Negative 403/400 contracts are verified from the Node harness outside the page context.
- Real dependency services: the same release script then runs `AURIS_REAL_STACK_E2E=1 bash scripts/verify_ui_bff_e2e.sh` and refuses `AURIS_SKIP_REAL_STACK_E2E=1`. It starts real MySQL, Redis, MinIO, and Qdrant services, plus a Dagster-compatible GraphQL protocol fake and a fake platform callback receiver. It runs migrations/seed against MySQL and requires strict `/readyz` checks for `auth/database/redis/object_storage/qdrant/dagster`.
- Adapter boundary: `AURIS_DAGSTER_ADAPTER=real` means the HTTP GraphQL adapter path is exercised against `scripts/fake_dagster_graphql_server.py`; it does not start or prove a real Dagster control plane, scheduler, or executor. `AURIS_QDRANT_ADAPTER=real` writes and reads points from a real Qdrant service, but both indexed and query vectors are deterministic test vectors derived from payload data, not production embeddings. MinIO objects are verified by `HEAD`/`GET`, length, and SHA-256. External HMAC callbacks are sent to a fake receiver.
- Partial infrastructure baseline: Redis coverage is limited to readiness and selected baseline behavior such as fixed-window rate limiting; it is not a production-complete cache, lock, runtime-state, high-availability, or recovery implementation. Observability currently consists of internal `trace_id` propagation, structured logs, and trace projections; a complete OpenTelemetry SDK/exporter/Collector path is not implemented or exercised by real-stack E2E.
- Supply-chain checks: release verification runs `pip-audit backend` and `npm audit --prefix prototype/auris-flow-ui --audit-level=high`, covering both runtime and development dependencies used by the build and test toolchain.
- Visual layout gate: release verification runs `bash scripts/audit_ui.sh` across 12 primary modules and high-risk views. It blocks large empty overlays, horizontal page overflow, clipped button/tab/heading text, missing action feedback, and exposed engineering-only terms.

## Public Release Gate

Run:

```bash
python3 scripts/check_platform_readiness.py --release
```

This mode intentionally adds stricter checks than the development baseline. A formal public release must satisfy:

- Recorded project-owner confirmation of the Apache-2.0 licensing rights holder and copyright ownership.
- A committed `LICENSE`, `LICENSE.md`, `COPYING`, or `COPYING.txt`; the automated check proves only that license text is present, not that the repository has authority to grant those rights.
- Root documents for contribution, conduct, security, and verification.
- `.gitignore` coverage for local dependencies, build outputs, caches, local databases, env files, and E2E artifacts.
- Security documentation that states vulnerability reporting, demo credential boundaries, and known production gaps.
- Dependabot and CodeQL configuration for GitHub Actions, frontend, and backend dependency/security monitoring.
- CI and local release verification through `scripts/verify_release.sh`, including real MySQL/Redis/MinIO/Qdrant service I/O, Dagster protocol-fake submission, deterministic Qdrant test vectors, and fake HMAC callback receipt verification.

The Verify workflow runs the strict gate with `AURIS_RELEASE_CHECK=1` and `AURIS_RUN_E2E=1` for pull requests, pushes to `main`, `master`, and `release/**`, `v*` tags, published GitHub Releases, and manual `workflow_dispatch` runs.

## Current Status

The repository contains the standard Apache License 2.0 text, but the Apache-2.0 licensing rights holder and copyright ownership still require project-owner confirmation. Therefore the repository is only a proposed open-source development-baseline candidate; it must not be described as a completed formal open-source release until that human gate and every automated gate have passed against a frozen Git candidate. It must also not be described as production-ready because several adapters remain protocol fakes or test implementations, authentication still needs formal JWT/OIDC/SSO beyond the transitional signed-token provider, Redis and observability are partial baselines, and projection compatibility paths are still present. Governed `storage_objects` metadata, short-lived audio playback grants, and MinIO-backed HTTP Range verification are implemented for the recording path; a real Dagster runtime and completion callbacks, production embeddings, complete OpenTelemetry export, generic multipart binary workflows, production callback endpoint allowlists, secret rotation, and CRM/work-order platform integration still need production side-effect adapters.

Before a public release candidate:

1. Obtain and record project-owner confirmation of the Apache-2.0 licensing rights holder and copyright ownership.
2. Complete `RELEASE_CHECKLIST.md`.
3. Run `python3 scripts/check_platform_readiness.py --release`.
4. Run `bash scripts/verify_release.sh`.
5. Confirm the strict Verify workflow passed for the release branch/tag and, when publishing, the `release.published` run.
6. Confirm `git status --short` contains only intended source, document, lockfile, and workflow changes.
7. Confirm production gaps in `SECURITY.md` remain visible and are not marketed as implemented.
8. Confirm GitHub repository settings enable branch protection, Security Advisory, secret scanning, and push protection.
