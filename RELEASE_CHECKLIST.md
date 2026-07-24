# Release Checklist

Use this checklist before creating an Auris Flow release candidate tag. Passing the
repository checks creates a **candidate**; it does not replace the human authority,
external repository controls, clean-host installation, or recovery drill below.

## P0 — Trusted Release Tree

- [ ] Run `python3 scripts/check_platform_readiness.py --release` and confirm
  `open_source_release_readiness: 12/12 passed` against the exact clean, committed
  candidate HEAD; staged-only content is not release evidence.
- [ ] Before signed images/deployment exist, run
  `bash scripts/verify_release.sh --pre-image`; do not use any skip flag for the
  real-stack, real-Dagster, product-Dagster, browser, visual, migration, audit, or
  security gates. This phase intentionally does **not** create
  `release-gate-manifest.json`.
- [ ] Confirm `build/release-evidence/release-gate-manifest.json` is produced only after
  clean-clone, dual-signed frontend bundle, visual 76/76, real-stack, real-Dagster
  Compose, product-path Dagster, supply-chain evidence, and
  `backup-restore-gate.json` and its tag-bound Sigstore sidecar all report
  `status=ok` for the exact same clean HEAD.
  The backup/restore artifact must come from the signed deployment on native Linux;
  Docker Desktop, a missing artifact, failed cleanup, or an empty-data drill fails
  closed. Recompute
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
- [ ] Confirm `production/frontend/frontend-bundle.lock.json` is `APPROVED` only after
  the protected candidate workflow built the exact default-branch tip in an unprivileged
  job, uploaded the complete dist inventory as an immutable GHCR digest, and Cosign
  verified its exact workflow SHA/repository/ref/event. The independent
  `frontend-bundle-production` promotion must rebuild byte-identically, sign a second
  approval OCI under the promotion workflow identity, and output a schema-3 pointer to
  both immutable digests. `verify_release.sh` must reverify both signatures and emit
  `frontend-bundle.json`; a `PENDING` lock, local candidate, single candidate signature,
  legacy totals reference, manually written digest or simultaneous budget increase is
  not release evidence.
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
  `minio-volume-init`, `db-bootstrap`, `minio-bootstrap`, `migrate`, and `identity-bootstrap` run as foreground
  one-shots and must each exit zero. Then confirm public `/healthz` reports liveness while
  public `/readyz` returns 200 only when OIDC, MySQL, Redis, MinIO, Qdrant, and real
  Dagster are ready.
- [ ] Run the production E2E using the real Dagster code server/webserver/daemon, selected
  HTTPS audio inference provider, semantic embedding provider, Qdrant, versioned object
  storage/result manifest, Worker, Outbox, and signed callback.
  A protocol fake or deterministic test vector is not production evidence; neither can certify model
  quality or the selected production provider.
- [ ] Exercise Dagster submission, status synchronization, cancellation, timeout,
  completion write-back, bounded retry, restart recovery, and stale fencing rejection.
- [ ] Force signed `task_run` and `audio_intelligence` completion callbacks to arrive
  before the dispatch binding commit. Both must reconcile after the authoritative
  binding appears without exposing or persisting raw result locators in the public Run,
  completion receipt, or receipt inbox. Audio staging must use a scoped pending storage
  object rather than copying bucket/key/version into the receipt.
- [ ] Run `bash scripts/verify_product_dagster_path.sh` and confirm its evidence proves
  BFF submission, tenant/project isolation, confirmed Outbox dispatch, real adapter
  binding, engine status synchronization, signed completion and `SAFE_TERMINATE`
  cancellation through the Compose BFF/Worker/real-Dagster deployment.
- [ ] Exercise database restart, Worker crash, duplicate dispatch, callback timeout,
  Redis/Qdrant transient outage, Outbox lease expiry, dead-letter, and governed replay;
  confirm there is no unexplained duplicate business result.
- [ ] Run `production/scripts/backup.sh`, restore into a new random empty Compose
  project with `production/scripts/restore.sh`, and pass `production/scripts/verify-backup.sh`
  with `--drill`, `--cleanup-on-success`, and
  `--evidence-output /absolute/release-evidence/backup-restore-gate.json`, plus
  MySQL/object/Qdrant consistency checks. Retain the schema
  `auris.backup-restore-gate.v1` evidence only after exact-project containers,
  volumes, and networks are removed. The official release workflow must sign it with
  the exact tag-bound GitHub OIDC identity; the evidence must bind source commit,
  release tag, actual signed metadata/Compose/image-lock digests, non-empty authority
  counts (including a real `json_resources` business row, not only migration/Dagster
  metadata), tool hashes, measured durations, and the native-Linux host observation.

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
- [ ] From outside the Compose host, run an independent watchdog that detects complete
  Prometheus or Alertmanager loss and proves a real notification receipt; in-Compose
  self-monitoring and `observability-health` startup checks are not sufficient evidence.
- [ ] Have a maintainer who did not author the implementation follow the installation,
  upgrade/rollback, backup/restore, key rotation, troubleshooting, and security-incident
  runbooks without undocumented local knowledge.
- [ ] Record measured SLO/RPO/RTO evidence and explicitly retain the limitation that the
  single-host release has no node-level HA or automatic host-failure recovery.

## P4 — Immutable Supply Chain

- [ ] Confirm `.github/workflows/release-images.yml` runs from the exact candidate commit,
  builds `linux/amd64` and `linux/arm64`, emits a GitHub-OIDC-signed SLSA v1 provenance
  attestation and CycloneDX SBOM attestation for every exact manifest digest, blocks
  HIGH/CRITICAL vulnerabilities, and signs every image digest with keyless Cosign.
- [ ] Confirm both local attestation bundles pass `gh attestation verify` immediately after
  creation and again after artifact download, then independently fetch SLSA and CycloneDX
  attestations from the authenticated GHCR digest with `--bundle-from-oci`. Bind the exact
  repository, signer workflow, signer/source commit, tag ref, predicate type, and
  `--deny-self-hosted-runners`; require at least one registry result and exact equality
  between every signed CycloneDX predicate and the downloadable SBOM. Confirm each closed
  image record binds both bundles plus local and registry verification receipts by SHA-256.
  Treat those sanitized receipts as drift evidence, not an independent trust anchor.
- [ ] Audit both locked Python graphs and the npm graph with no unexpired high-severity
  exception: backend `uv.lock`, production Dagster `uv.lock`, and frontend
  `package-lock.json`.
- [ ] Verify every signature/attestation, then render the installable Compose from the
  complete service image lock. The release Compose must contain no `build`, mutable tag,
  `latest`, or unresolved image reference.
- [ ] Confirm source archive, digest-pinned Compose, image lock, SBOMs, signatures,
  provenance, `CHANGELOG`, `NOTICE`, migration notes, artifact manifest, and
  `SHA256SUMS` all name the same tag and source commit. The signed final checksum set,
  rather than an unsigned build-job JSON record, is the release authenticity anchor.
- [ ] Require the protected `release-publish` approval and confirm the tag-push
  workflow creates a new immutable GitHub Release; it must never overwrite an
  existing release or publish from a manual-dispatch-only validation. Confirm the signed
  evidence archive is safely extracted and image records are reverified from the archive,
  and that GitHub API checks immediately before and after publication peel the remote
  annotated tag to the approved commit. Repository tag ruleset and immutable release
  settings remain mandatory external controls.
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

- [ ] Default-branch ruleset requires pull requests, independent approval, resolved
  conversations, and the Verify and CodeQL checks; it forbids force-push and deletion.
  Required checks must be produced on every pull request.
- [ ] Release-tag ruleset and the protected `release` / `release-publish` environments
  require dependency evidence, image scanning, signed-artifact verification, and an
  independent release approval. Tag-only or manual-dispatch-only jobs must not be
  configured as default-branch required checks.
- [ ] GitHub Private Vulnerability Reporting, secret scanning, push protection, CodeQL,
  Dependabot, immutable releases, and tag protection are enabled and tested in the hosted
  repository; local workflow files alone are not evidence.
- [ ] Release notes generated from `.github/release.yml` match `CHANGELOG.md`, compatibility
  policy, migration/rollback notes, known limitations, and the security disclosure address.
