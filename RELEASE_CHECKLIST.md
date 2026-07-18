# Release Checklist

Use this checklist before tagging or publishing an Auris Flow open-source release candidate.

## Required Automated Gates

- [ ] Run `python3 scripts/check_platform_readiness.py --release` and confirm `open_source_release_readiness: 12/12 passed`.
- [ ] Run `bash scripts/verify_release.sh`.
- [ ] Confirm the Verify workflow runs for `release/**` branches and `v*` tags with job-level `AURIS_RELEASE_CHECK=1` and `AURIS_RUN_E2E=1`.
- [ ] Confirm the Verify workflow is configured for a published GitHub Release and `workflow_dispatch`, and that the applicable strict run passed.
- [ ] Confirm `bash scripts/verify_release.sh` ran both the SQLite UI/BFF E2E path and `bash scripts/verify_real_stack.sh`.
- [ ] Confirm the real-stack artifact reports MySQL, `real_qdrant`, real MinIO object storage, HTTP `206` range playback, outbox fencing, and no sqlite/mock/local fallback markers.
- [ ] Confirm the real-stack claim remains scoped: Dagster is a protocol fake, Qdrant uses deterministic test vectors, Redis is a partial baseline, and no OTel production path is asserted.
- [ ] Confirm `bash scripts/verify_release.sh` ran `bash scripts/audit_ui.sh` and reported no large empty overlay, horizontal overflow, clipped critical text, or missing action feedback.
- [ ] Confirm `git diff --check` has no whitespace errors.
- [ ] Confirm `python3 scripts/scan_secrets.py` reports `secret scan ok`.

## Release Hygiene

- [ ] Confirm `git status --short` includes only intended source, migration, test, documentation, lockfile, workflow, and script changes.
- [ ] Confirm build output, screenshots, local databases, coverage files, caches, and E2E artifacts are ignored.
- [ ] Confirm `doc/reports/repository-layout-review.md` still says no directory migration is required, or update it with the explicit migration trigger and scope.
- [ ] Confirm large changes are split or justified using `doc/reports/change-submission-plan.md`.
- [ ] Confirm `README.md`, `SECURITY.md`, `SUPPORT.md`, `GOVERNANCE.md`, `MAINTAINERS.md`, and `CHANGELOG.md` reflect the release scope.
- [ ] Confirm OpenAPI runtime drift is zero and `doc/backend-spec/openapi-v0.1.yaml` matches FastAPI runtime operations.
- [ ] Confirm bundle budget still has at least 25 KB total JS buffer.
- [ ] Confirm UI/BFF E2E reports no console errors, page errors, request failures, or failed responses.

## Human Release Authority

- [ ] Project owner confirms the release date and release tag.
- [ ] Project owner confirms the Apache-2.0 licensing rights holder and copyright owner text; until recorded, block tagging/publishing and do not claim a completed formal open-source release.
- [ ] Project owner confirms whether a `NOTICE` file is required for third-party or attribution obligations.
- [ ] Maintainers confirm this release is described as an open-source development baseline, not production-ready SaaS.
- [ ] Maintainers confirm production gaps in `SECURITY.md` remain visible.

## Repository Administration

- [ ] Branch protection requires the Verify workflow before merge.
- [ ] GitHub Security Advisory or another private vulnerability reporting channel is enabled.
- [ ] GitHub secret scanning and push protection are enabled.
- [ ] Dependabot and CodeQL are enabled for GitHub Actions, frontend npm dependencies, and backend Python dependencies.
- [ ] Release notes are generated from `.github/release.yml` and checked against `CHANGELOG.md`.
