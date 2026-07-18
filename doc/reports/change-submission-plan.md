# Change Submission Plan

This plan defines the review boundary for the current open-source release preparation work.

## Recommended Split

Use four review groups instead of one large submission:

1. `governance-release`
   - Root governance documents, release checklist, security/support/maintainer files, GitHub issue templates, release notes config, `.gitignore`, and repository-level readiness gates.
   - Primary gates: `python3 scripts/check_platform_readiness.py --release`, `python3 scripts/scan_secrets.py`, `git diff --check`.

2. `backend-runtime`
   - FastAPI security baseline, dev auth/session handling, request context, adapters, object storage/audio playback, outbox worker lifecycle, fake external services, Docker local dependency updates, and backend runtime scripts.
   - Primary gates: backend Ruff, mypy, unit/contract/integration tests, `smoke_backend.py`, outbox and real-stack targeted scripts.

3. `backend-contracts-and-migrations`
   - OpenAPI drift updates, seed fixture changes, Alembic migrations, migration verifier, domain services, schema additions, RBAC/trace tests, idempotency/concurrency tests, calibration/appeal/label-policy contracts.
   - Primary gates: `doc/backend-spec/validate_backend_spec.py`, `backend/scripts/verify_migrations.py`, backend contract and integration test suites.

4. `frontend-bff-ux`
   - React UI state feedback, BFF API client, project context handling, lazy/static catalog extraction, UI smoke/E2E harnesses, audit scripts, bundle budget, and styles.
   - Primary gates: frontend build, bundle budget, UI smoke, UI/BFF E2E, `npm --prefix prototype/auris-flow-ui run audit:auto`.

## Submit Together Only When

Submit multiple groups together only when the same behavior cannot be reviewed or verified independently. Examples:

- A backend API contract and the frontend BFF call that proves the contract.
- A migration and the integration test that proves upgrade/downgrade or historical compatibility.
- A release gate and the document or script that the gate requires.

## Keep Out Of Source Review

Do not include local-only or generated artifacts:

- Dependency folders, virtual environments, caches, build outputs, `.next/`, `.vite/`, `dist/`, `build/`, and `*.tsbuildinfo`.
- Local SQLite databases, coverage files, Playwright/E2E artifacts, audit screenshots, generated evidence PNGs, and temporary reports.
- Real secrets, `.env` files, customer data, raw audio, transcripts, or undeidentified recordings.

## Final Aggregation Gate

After all groups are reviewed together on the release candidate branch, run:

```bash
bash scripts/verify_release.sh
```

The result must include:

- OpenAPI runtime drift at zero.
- Backend tests passing with configured coverage.
- Frontend build and bundle budget passing with at least 25 KB total JS buffer.
- UI/BFF E2E clean browser observations.
- Real-stack E2E artifact proving MySQL, Redis, MinIO, Qdrant, Dagster protocol submission/readback, HMAC callbacks, and HTTP 206 audio range playback.
