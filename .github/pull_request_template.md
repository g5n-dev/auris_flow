## Summary

- What changed:
- Why it matters:

## Scope Boundary

- [ ] `governance-release`: governance docs, release checklist, security/support, GitHub templates, CI, readiness gates.
- [ ] `backend-runtime`: auth/session handling, request context, adapters, object storage/audio playback, outbox runtime, local dependency stack.
- [ ] `backend-contracts-and-migrations`: OpenAPI, seed, migrations, domain services, backend contract/integration tests.
- [ ] `frontend-bff-ux`: React UI, BFF API client, project context, audit/E2E harnesses, bundle budget.
- [ ] This PR intentionally combines multiple groups and explains why:
- [ ] I checked `doc/reports/change-submission-plan.md` and excluded generated/local artifacts.

## Harness Gate

- [ ] I ran `bash scripts/verify_release.sh`.
- [ ] `python3 scripts/check_platform_readiness.py --release` reports `11/11 passed`.
- [ ] Backend tests pass with coverage at or above the configured threshold.
- [ ] UI/BFF E2E reports no `consoleErrors`, `pageErrors`, `requestFailures`, or `failedResponses`.
- [ ] `npm audit --prefix prototype/auris-flow-ui --audit-level=high --omit=dev` reports no high vulnerabilities.
- [ ] `python3 -m pip_audit backend --progress-spinner off` reports no known vulnerabilities.
- [ ] `python3 scripts/scan_secrets.py` reports `secret scan ok`.

## Data And Interaction Chain

- [ ] Tenant/project context is preserved through BFF calls.
- [ ] Writes use idempotency keys and expose `trace_id`.
- [ ] Outbox success, retry, dead-letter, and blocked gate behavior are covered by tests.
- [ ] UI actions show pending/success/failure feedback or a clear disabled reason.
- [ ] BFF responses used by the UI include stable IDs and trace metadata.

## Critic Review

- [ ] A critic/reviewer has checked for overclaiming, mock behavior marketed as production, broken data lineage, and hidden UI dead ends.
- [ ] Any remaining risk is documented as a known gap rather than presented as implemented.

## Open-Source Release Checks

- [ ] README and `SECURITY.md` clearly state this is an open-source development baseline, not a production SaaS release.
- [ ] Apache-2.0 license and any required notice/copyright ownership have been confirmed by the project owner.
- [ ] GitHub branch protection requires the Verify workflow before merge.
- [ ] GitHub Security Advisory or another private vulnerability reporting channel is enabled.
- [ ] Dependabot and CodeQL repository configs are present and still match the changed package ecosystems.
- [ ] GitHub secret scanning and push protection are enabled or tracked as repository administration follow-ups.
