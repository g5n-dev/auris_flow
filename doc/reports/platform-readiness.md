# Auris Flow Platform Readiness

This document defines the current readiness bar for turning Auris Flow into an open-source evaluation, labeling, and insights platform.

## Current Bar

- Open-source surface exists: README, contribution guide, conduct guide, security guide, CI workflow, and ignore rules.
- Product contract exists: backend spec package covers API, DB, state machines, RBAC, events, seed data, migration plan, and mock-to-api mapping.
- Runtime baseline exists: FastAPI BFF, SQLAlchemy/Alembic, request context, structured logs, idempotency, audit, outbox worker, Docker local stack.
- Product modules exist: frontend and backend both expose labeling, evaluation, and insights domains.
- Verification exists: `bash scripts/verify_all.sh` runs spec validation, backend tests, backend smoke, platform readiness, and frontend build.

## Machine Check

Run:

```bash
python3 scripts/check_platform_readiness.py
```

The check intentionally verifies repository structure and cross-layer contracts. It is not a replacement for unit tests, Playwright, security scans, or production deployment checks.

For browser-level UI/BFF integration, run:

```bash
AURIS_RUN_E2E=1 bash scripts/verify_all.sh
```

The script autostarts a temporary SQLite-backed FastAPI BFF and Vite UI when no target is already available, then exercises browser-to-BFF write paths through the Vite `/api` proxy. It currently covers labeling, evaluation, insights, task configuration, knowledge, assets, settings, trace lookup, and tenant/project request context. Browser observations must stay clean: no console errors, request failures, or 4xx/5xx page responses; negative contracts are checked from the Node harness outside the page context.

For dependency-stack verification, run:

```bash
AURIS_REAL_STACK_E2E=1 bash scripts/verify_ui_bff_e2e.sh
```

That path starts MySQL, Redis, MinIO and Qdrant, runs migrations and seed against MySQL, and requires strict `/readyz` checks for `auth/database/redis/object_storage/qdrant`. It is the evidence for local real-stack connectivity; the SQLite path remains a fast development and browser-contract gate.

## Release Boundary

The repository includes an Apache-2.0 `LICENSE` and can be treated as an open-source development baseline. It is still not production-ready until formal authentication, complete RBAC, real external adapters, dependency security scanning, and production deployment checks are completed.

Strict release mode is available:

```bash
python3 scripts/check_platform_readiness.py --release
```

It must pass before publishing a public release candidate.
