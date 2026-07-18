# Repository Layout Review

This review records the current repository layout decision for the open-source development baseline.

## Decision

No repository-wide directory migration is required before the first public development release candidate.

The current layout is reasonable for review and onboarding:

- `backend/` owns the FastAPI BFF, domain services, SQLAlchemy models, Alembic migrations, backend tests, and backend-only helper scripts.
- `prototype/auris-flow-ui/` owns the React/Vite interaction baseline, UI audit scripts, Playwright smoke paths, and frontend build tooling.
- `doc/backend-spec/` owns the backend contract package: OpenAPI, DB schema, state machines, RBAC, seed fixture, migration plan, and spec validation.
- `doc/reports/` owns readiness and release review reports.
- `docker/local/` owns local dependency compose files only.
- `scripts/` owns repository-level verification, release, audit, dev-up, and real-stack orchestration entrypoints.
- `.github/` owns CI, dependency/security monitoring, issue templates, PR template, and release note categorization.
- Root documents own open-source governance, security, support, contribution, changelog, release checklist, and license discovery.

## Non-Goals

- Do not split the frontend prototype into a separate repository before the BFF contracts stabilize.
- Do not move backend migrations, tests, or app modules out of `backend/`.
- Do not move UI audit or E2E harnesses out of `prototype/auris-flow-ui/` while they exercise the local Vite app and UI fixtures directly.
- Do not add a monorepo package manager layer just to organize the current two application surfaces.

## Commit Boundary

Source and release candidate commits may include:

- Backend application code, migrations, schemas, services, workers, and tests.
- Frontend source, UI modules, API client, audit scripts, E2E harnesses, and package metadata.
- Contract documents, release documents, governance files, workflows, and verification scripts.
- Lockfiles that correspond to committed dependency manifests.

Source and release candidate commits must not include:

- `node_modules/`, `.venv/`, `.next/`, `.vite/`, `dist/`, `build/`, cache folders, or `__pycache__/`.
- Local SQLite databases, coverage files, Playwright/E2E artifacts, audit screenshots, generated PNG evidence, or `*.tsbuildinfo`.
- `.env` files, real secrets, customer data, raw audio, transcripts, or undeidentified recordings.

## Future Migration Triggers

Revisit the layout only if one of these becomes true:

- The frontend and backend need independent release cadences or package registries.
- More than one frontend app shares the same backend contract package.
- `prototype/auris-flow-ui/src/App.tsx` decomposition completes enough that modules can be promoted to package-level ownership.
- CI time or dependency installation cost requires workspace-level caching and package orchestration.
- A production deployment path needs separate app containers with independent source contexts.

Until then, the next improvement is not a directory migration; it is keeping release boundaries clean and making every new path visible in the relevant verification gate.
