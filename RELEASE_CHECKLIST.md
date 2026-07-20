# Release Checklist

Use this checklist before creating an Auris Flow release candidate tag. Passing the
repository checks creates a **candidate**; it does not replace the human authority,
external repository controls, clean-host installation, or recovery drill below.

## P0 — Trusted Release Tree

- [ ] Run `python3 scripts/check_platform_readiness.py --release` and confirm
  `open_source_release_readiness: 12/12 passed` against the exact clean, committed
  candidate HEAD; staged-only content is not release evidence.
- [ ] Run `bash scripts/verify_release.sh`; do not use any skip flag for the real-stack,
  real-Dagster, product-Dagster, browser, visual, migration, audit, or security gates.
- [ ] Confirm `build/release-evidence/release-gate-manifest.json` is produced only after
  clean-clone, visual 76/76, real-stack, real-Dagster Compose, product-path Dagster and
  supply-chain evidence all report `status=ok` for the exact same clean HEAD. Recompute
  every recorded SHA-256 and reject stale, extra, symlinked or local-path-bearing files.
- [ ] Confirm `production/visual/visual-baseline.lock.json` is `APPROVED` and points to
  an immutable `ghcr.io/...@sha256:...` artifact. The gate must download it with ORAS,
  first verify its keyless signature against the exact current-repository
  `visual-baseline-build.yml@refs/heads/<default>` identity and GitHub token issuer,
  then verify package/manifest/76 PNG hashes plus source/runner/scenario/seed bindings and
  write `build/release-evidence/visual-regression.json` with `status=ok` and
  `scenario_count=passed=76`. `PENDING`, Darwin diagnostics, goal/seed overrides, host
  runtime, and update mode are not release evidence.
- [ ] Protect both `visual-baseline-build` and `visual-baseline-production` environments
  with required reviewers. Build the candidate from an exact clean commit, retain the
  workflow-produced GHCR digest and signer metadata, and reject manually written digest,
  identity, issuer, or approval fields.
- [ ] Confirm `git status --short` is empty and `git diff --check` is clean after the
  candidate commit is created.
- [ ] Confirm a fresh clone of that commit can install locked dependencies, migrate,
  test, and build the frontend bundle without untracked files or developer caches.
  Treat this as functional locked-source reproducibility; the separate release-image
  workflow must build and scan all three first-party multi-architecture images.
- [ ] Confirm runtime OpenAPI drift is zero, including every `/api/v1/*` operation and
  the `evaluation-lock` contract.
- [ ] Confirm `python3 scripts/scan_secrets.py` reports `secret scan ok` and no generated
  build output, screenshot, geometry, visual manifest, local database, cache, audio, or
  test artifact is tracked. Git retains only visual scenes/contracts/seed and the small
  OCI lock pointer.
- [ ] Confirm the intended changes follow the boundaries in
  `doc/reports/change-submission-plan.md` and the layout decision in
  `doc/reports/repository-layout-review.md`.

## P1 — Identity, Isolation, and Rights

- [ ] Exercise OIDC Authorization Code + PKCE login, cookie restore, CSRF-protected
  mutation, logout/re-authentication, user disable, role reduction, and JWKS
  overlap/retirement against a real IdP.
- [ ] Run the tenant/project/role negative matrix and confirm stable 401/403/404
  responses do not disclose resource existence or internal exception details.
- [ ] Confirm the production configuration rejects demo credentials, weak signing
  material, wildcard CORS/TrustedHost, development auth, unknown identity auto-provision,
  local/fake adapters, deterministic embeddings, and missing strong dependencies.
- [ ] Confirm every production secret is supplied through a Docker secret file/reference
  or an approved external secret manager and is absent from bundle, environment output,
  logs, SBOM, attestations, and release metadata.
- [ ] Exercise callback/completion HMAC key overlap, retirement, nonce replay rejection,
  body-bound idempotency, timeout reconciliation, dead-letter, and governed replay.
- [ ] Confirm the project owner signed
  `open-source-rights-authorization.md`, replaced every blocked placeholder
  in `NOTICE`, and reviewed `THIRD_PARTY_NOTICES.md` plus public dataset licenses.

## P2 — Real Production Runtime

- [ ] Render and validate `production/compose.yaml`; all images must use explicit tags,
  first-party containers must be non-root, and services must retain read-only filesystem,
  dropped capabilities, health checks, internal networking, and persistent volumes where
  applicable.
- [ ] Start the single-host Linux candidate with the phased command sequence in
  `production/README.md`: long-lived dependencies use bounded detached wait, while
  `db-bootstrap`, `minio-bootstrap`, `migrate`, and `identity-bootstrap` run as foreground
  one-shots and must each exit zero. Then confirm public `/healthz` reports liveness while
  public `/readyz` returns 200 only when OIDC, MySQL, Redis, MinIO, Qdrant, and real
  Dagster are ready.
- [ ] Run the production E2E using the real Dagster code server/webserver/daemon, semantic
  embedding provider, Qdrant, object storage, Worker, Outbox, and signed callback. A
  protocol fake or deterministic test vector is not production evidence.
- [ ] Exercise Dagster submission, status synchronization, cancellation, timeout,
  completion write-back, bounded retry, restart recovery, and stale fencing rejection.
- [ ] Run `bash scripts/verify_product_dagster_path.sh` and confirm its evidence proves
  BFF submission, tenant/project isolation, confirmed Outbox dispatch, real adapter
  binding, engine status synchronization, signed completion and `SAFE_TERMINATE`
  cancellation through the Compose BFF/Worker/real-Dagster deployment.
- [ ] Exercise database restart, Worker crash, duplicate dispatch, callback timeout,
  Redis/Qdrant transient outage, Outbox lease expiry, dead-letter, and governed replay;
  confirm there is no unexplained duplicate business result.
- [ ] Run `production/scripts/backup.sh`, restore into a new empty Compose project with
  `production/scripts/restore.sh`, and pass `production/scripts/verify-backup.sh` plus
  MySQL/object/Qdrant consistency checks.

## P3 — Observability and Operations

- [ ] Trace one critical business flow from edge/BFF through database, Worker, Dagster,
  Outbox, and callback; verify the business `trace_id` is correlated with OTel trace/span
  identifiers and no secret, token, cookie, SQL value, or sensitive URL query is exported.
- [ ] Validate Prometheus recording/alert rules and Grafana dashboards for request
  latency/errors, auth failures, dependency readiness, task outcomes, Outbox backlog and
  dead-letter, callback failures, storage capacity, and backup freshness.
- [ ] Route Alertmanager (or an approved external notification service) to a private
  on-call channel and test dependency-offline, dead-letter growth, disk pressure,
  authentication-failure surge, and backup-failure notifications.
- [ ] Have a maintainer who did not author the implementation follow the installation,
  upgrade/rollback, backup/restore, key rotation, troubleshooting, and security-incident
  runbooks without undocumented local knowledge.
- [ ] Record measured SLO/RPO/RTO evidence and explicitly retain the limitation that the
  single-host release has no node-level HA or automatic host-failure recovery.

## P4 — Immutable Supply Chain

- [ ] Confirm `.github/workflows/release-images.yml` runs from the exact candidate commit,
  builds `linux/amd64` and `linux/arm64`, emits provenance and CycloneDX SBOMs, blocks
  HIGH/CRITICAL vulnerabilities, and signs every image digest with keyless Cosign.
- [ ] Audit both locked Python graphs and the npm graph with no unexpired high-severity
  exception: backend `uv.lock`, production Dagster `uv.lock`, and frontend
  `package-lock.json`.
- [ ] Verify every signature/attestation, then render the installable Compose from the
  complete service image lock. The release Compose must contain no `build`, mutable tag,
  `latest`, or unresolved image reference.
- [ ] Confirm source archive, digest-pinned Compose, image lock, SBOMs, signatures,
  provenance, `CHANGELOG`, `NOTICE`, migration notes, artifact manifest, and
  `SHA256SUMS` all name the same tag and source commit.
- [ ] Require the protected `release-publish` approval and confirm the tag-push
  workflow creates a new immutable GitHub Release; it must never overwrite an
  existing release or publish from a manual-dispatch-only validation.
- [ ] Install the signed artifact on a clean external Linux host using only the published
  README/configuration, complete OIDC and the core business flow, then feed every issue
  back into a new candidate commit.

## Human Release Authority

- [ ] Project owner confirms the release date, `v1.0.0-rc.1` tag, personal Apache-2.0
  rights holder/copyright text, and final `NOTICE` content.
- [ ] Release approver reviews all automated evidence, external clean-install evidence,
  backup/restore report, unresolved risk register, and known limitations.
- [ ] Maintainers confirm the release is described as a single-host production baseline,
  not a highly available SaaS, and that no incomplete gate is hidden by documentation.
- [ ] After at least one external RC installation and fixes, repeat every gate from the
  final commit before approving `v1.0.0`.

## Repository Administration

- [ ] Branch protection requires Verify, CodeQL, dependency evidence, image scanning,
  signed-artifact verification, and an independent release approval.
- [ ] GitHub Private Vulnerability Reporting, secret scanning, push protection, CodeQL,
  Dependabot, immutable releases, and tag protection are enabled and tested in the hosted
  repository; local workflow files alone are not evidence.
- [ ] Release notes generated from `.github/release.yml` match `CHANGELOG.md`, compatibility
  policy, migration/rollback notes, known limitations, and the security disclosure address.
