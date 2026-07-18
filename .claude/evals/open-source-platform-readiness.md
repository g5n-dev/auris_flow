# EVAL: open-source-platform-readiness

## Objective

Auris Flow should be usable as an open-source evaluation, labeling, and insights platform baseline. The repository must prove the product surface, backend contract, runtime foundation, and verification gate are aligned.

## Capability Evals

- [ ] A new contributor can identify the platform purpose, repository layout, local startup path, and license boundary from root documents.
- [ ] Backend developers can map evaluation, labeling, and insights screens to `/api/v1/*` contracts, seed data, state machines, and tests.
- [ ] The runtime foundation exposes structured logs, request/trace IDs, tenant/project context, idempotency, audit, outbox, and local Docker dependencies.
- [ ] The frontend keeps distinct modules for labeling, evaluation, and insights while preserving cross-module drilldown routes.
- [ ] The one-command gate can verify specs, backend tests, backend smoke, platform readiness, and frontend build.
- [ ] Public release readiness is explicitly separated from development baseline readiness and requires license, security disclosure, supply-chain audit, and UI/BFF E2E gates.

## Regression Evals

- [ ] `python3 scripts/check_platform_readiness.py` passes.
- [ ] `python3 scripts/check_platform_readiness.py --release` passes when the committed Apache-2.0 license and release hygiene files are present.
- [ ] `bash scripts/verify_fast.sh` passes.
- [ ] `AURIS_RELEASE_CHECK=1 bash scripts/verify_all.sh` fails unless `AURIS_RUN_E2E=1` is also set.
- [ ] `bash scripts/verify_release.sh` passes and enforces supply-chain audit plus UI/BFF E2E failed-response gates.
- [ ] OpenAPI still includes `/labels`, `/label-versions`, `/eval-datasets`, `/eval-runs`, `/insights/metrics`, `/insights/reports`, and `/insights/actions`.
- [ ] Contract tests still cover tenant/project scoping, idempotency replay/conflict, trace lookup, and outbox processing.
- [ ] Frontend build still succeeds after readiness checks are added.

## Graders

- Code grader: `python3 scripts/check_platform_readiness.py`
- Release grader: `python3 scripts/check_platform_readiness.py --release`
- Regression grader: `bash scripts/verify_fast.sh`
- Browser integration grader: `bash scripts/verify_release.sh`
- Human grader: release owner confirms repository settings such as branch protection, GitHub Security Advisory, CodeQL, Dependabot, and secret scanning before public release.

## Current Known Non-Goals

- This eval does not claim production-grade auth, distributed outbox locking, real ASR/VAD/LLM inference, or final license approval.
- This eval proves a coherent development baseline, not a finished production service.
