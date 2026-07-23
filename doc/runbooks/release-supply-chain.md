# Release supply-chain runbook

The supported production artifact is a commit-bound release bundle produced by
`.github/workflows/release-images.yml`. A source checkout or a mutable image tag
alone is not a production release.

## Maintainer prerequisites

- Protect the GitHub `release`, `release-publish`, `visual-baseline-build`, and
  `visual-baseline-production` environments with required reviewers. The latter
  three guard permanent publication and the two-stage visual baseline process.
- Require the normal `Verify` and CodeQL checks before creating the tag.
- Enable private vulnerability reporting, secret scanning, and push protection.
- Allow the release workflow to write packages and request an OIDC identity.
- Treat GHCR release tags as immutable. Never retarget or delete a published
  release tag to repair a release; publish the next release candidate instead.

## Candidate procedure

1. Run the complete release verification on the candidate commit.
2. Push an annotated SemVer tag such as `v1.0.0-rc.1`. A manual dispatch must select
   that exact tag as the workflow ref and submit the same tag input; branch-ref
   dispatches and forks fail closed because their signing identity is not canonical.
3. Approve the protected `release` environment only after checking the source
   commit shown by the workflow.
4. For a tag-push run, approve `release-publish` only after every build and
   evidence job is green. The workflow refuses to overwrite an existing GitHub
   Release. It first creates a draft and uploads the isolated
   deployment/source/evidence archives, release metadata Sigstore bundle, manifest,
   checksum set, checksum Sigstore bundle, and toolchain record. It publishes that
   draft only after repeating the remote annotated-tag check, resolving all three
   SemVer GHCR tags to their approved digests, and repeating Cosign verification.
   A failed pre-publication check leaves the draft unpublished. Manual
   dispatch validates the pipeline but deliberately does not publish.
5. Download the final `auris-flow-<tag>-<commit>` workflow artifact or the
   matching GitHub Release assets. Verify the checksum Sigstore bundle against the
   exact official tag workflow identity, then verify every file listed by
   `SHA256SUMS` and each image with Cosign against the repository's
   `release-images.yml` workflow identity and GitHub's token issuer.
6. Confirm `release-manifest.json`, `images.lock.json`, image SBOMs, dependency
   SBOMs and vulnerability reports inside the deterministic evidence archive, the
   deterministic `auris-flow-<tag>-source.tar.gz` archive,
   `auris-flow-<tag>-deployment.tar.gz`, and the release notes name the
   same tag and source commit.

The workflow builds BFF, edge, and Dagster images for `linux/amd64` and
`linux/arm64`. It blocks HIGH/CRITICAL image findings, audits both locked Python
environments and npm, signs exact manifest digests with keyless Cosign, resolves
every third-party Compose image to a digest, and renders a release Compose file
with no `build:` directives. No vulnerability ignore file is accepted by this
workflow; a future exception process must be reviewed, scoped, and expiring
before it can be introduced.

Each first-party manifest also receives two GitHub-OIDC signatures from the exact
tag-bound `release-images.yml` workflow: a SLSA v1 provenance attestation and a
CycloneDX SBOM attestation. The workflow uses the SHA-pinned
`actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6` action, publishes both
attestations beside the digest in GHCR, and preserves their separate local Sigstore
bundles. It deliberately sets `create-storage-record: false`; the release does not rely
on an organization-only artifact metadata record.

Both local bundles are verified immediately and again in `assemble-release`. After
authenticating to GHCR, `assemble-release` also performs independent SLSA and CycloneDX
lookups with `gh attestation verify --bundle-from-oci`, before the image lock is created
or any SemVer image tag is promoted. Every `gh attestation verify`
invocation is bound to the exact GitHub repository `g5n-dev/auris_flow`, OCI image
repository and digest, `.github/workflows/release-images.yml`, signer and source
commit, tag ref, expected predicate URI, and `--deny-self-hosted-runners`. OCI lookup
may return more than one
compliant attestation; the gate requires at least one, validates every returned subject
and predicate type, and requires every returned CycloneDX predicate to equal the
downloadable `.cdx.json` document. The retained verification JSON excludes
the user-controlled predicate body and duplicate bundle; it keeps the exact signed
subject, predicate type, parsed signing certificates, and verified timestamps. Before
discarding the predicate body, the verifier also requires the signed CycloneDX predicate
to equal the separately downloadable SBOM document.

BuildKit automatic attestations are disabled because the release did not independently
verify their metadata. They are replaced—not weakened—by the signed GitHub attestations
and the separately downloadable CycloneDX document. For each image, the closed
`*.image.json` record binds the CycloneDX file, two distinct Sigstore bundles, immediate
verification records, local assembly-stage verification records, and OCI-registry
assembly-stage verification records by SHA-256. Every later
workflow read revalidates those fixed filenames, file types, semantics, and hashes. The
record and sanitized verification receipts detect internal evidence drift; a receipt is
not an independent trust anchor. Release authenticity comes from the signed final
checksum set that covers the deterministic evidence archive, together with the original
Sigstore verification and governed workflow identity.

`scripts/verify_release.sh` is the single producer of dependency release evidence.
After the runtime gates and SBOM/license inventory, it exports both locked Python
runtime graphs, writes the backend and Dagster `pip-audit` JSON reports, writes the
frontend `npm audit` JSON report, and invokes the finalizer exactly once with
`--require-audits`. Any audit failure prevents `release-gate-manifest.json` from being
created. The release workflow does not delete, regenerate, or post-process a
pre-audit success manifest; it only uploads the gate directory. On failure it uses
the separate `dependency-evidence-failed-<commit>` artifact so maintainers retain
the available diagnostic reports without mistaking them for successful evidence.

## Signed visual baseline

1. Dispatch `visual-baseline-build.yml` from the repository default branch with an
   exact source commit. The protected Linux job requires that commit to be the current
   default-branch tip and that `github.workflow_sha` equals it, generates
   76 screenshots with the digest-pinned Playwright container, creates the canonical
   tar layer, and pushes it only under the current repository's GHCR namespace.
2. Record the workflow output `ghcr.io/<owner>/<repo>/visual-baseline@sha256:<digest>`.
   The job keyless-signs that exact digest and immediately verifies the certificate
   identity `visual-baseline-build.yml@refs/heads/<default>` against GitHub's token
   issuer. It never edits or approves the repository lock.
3. Dispatch `visual-baseline-promotion.yml` from the default branch under the separate
   protected promotion environment. It recomputes the only trusted signer identity,
   verifies the signature before ORAS download, validates the package and all 76 PNGs,
   checks both OCI annotations (`image.revision` and the job workflow SHA), and emits
   an `APPROVED` lock candidate containing the identity, issuer and exact workflow SHA.
4. Review and commit only the generated lock pointer. Every strict release run installs
   pinned Cosign and re-verifies the locked digest before materializing screenshots;
   promotion-time verification alone is insufficient.

The committed lock remains `PENDING` until both protected workflows have actually run
and a human has approved the real digest. Local diagnostics and workflow YAML are never
evidence that a baseline exists.

The source archive is produced with `git archive` from the already validated
source commit, then compressed with timestamp-free gzip metadata. It is not
assembled from the runner working tree. A second deterministic deployment
archive and a third evidence archive are assembled before manifest generation with
sorted paths, the source commit timestamp, and fixed numeric ownership. The source,
deployment and evidence archives use distinct `-source`, `-deployment` and `-evidence`
top-level directories. All three archives, the production
README, security policy, release checklist, supply-chain runbook, and Compose
policy validator are covered by the final manifest and signed checksum set.
Immediately before evidence archival, every image record is semantically reverified with
all local and registry assembly receipts required. In the publication job, the signed
`SHA256SUMS` is verified first and its evidence-archive entry is required exactly once;
only then is image evidence safely extracted from that checksum-bound archive into an
isolated temporary directory and every image record, evidence hash, exact digest, and
closed semantic field reverified. Loose workflow-artifact copies of `images/` are never
used as the publication trust input.
The prebuilt final-runtime `production-path-gate.json` is also listed directly in
`SHA256SUMS`. Before archiving, and again after the release job downloads the signed
checksum set, `verify_production_path_gate.py release-evidence` validates its closed
release tag/commit/image-lock/native-Linux/source binding and reuses the full local
runtime-proof validator. A self-consistent JSON hash cannot substitute for any of the
three governed dead-letter retry proofs.
The deployment tar has one top-level `auris-flow-<tag>-deployment/` directory, a stable
`production/compose.yaml` containing the rendered digest-only document, and a
`production/release-metadata.json` binding its schema/tag/source commit, Compose
SHA-256, image-lock SHA-256, restore-policy SHA-256, exact service-image map, and a
sorted v3 inventory of every regular bundle file with its canonical path, SHA-256,
type and exact mode. The metadata file itself is the unavoidable self-hash exception;
the detached Sigstore bundle is a separately checksum-covered asset installed read-only
as `production/release-metadata.sigstore.json`. Post-extraction verification rejects
missing, extra, duplicate, traversal, symlink/special-file and mode-changing members,
including scripts and Runbooks. The deployment archive has no `.git` and never contains
a runner workspace path.

Images are first pushed under a commit staging tag. The workflow promotes their
already scanned and signed digests to the SemVer release tag only after every
image job succeeds and every unique final Compose digest has passed Trivy on
both `linux/amd64` and `linux/arm64`. This second scan includes MySQL, Redis,
MinIO, Qdrant, Keycloak, OpenTelemetry, Tempo, Prometheus, Grafana, and the node
exporter—not only the three project-built images. Before creating a SemVer image
tag, the workflow obtains a scoped GHCR bearer token and performs an authenticated
Registry v2 manifest `HEAD`. Only an explicit HTTP 404 is treated as absence.
Authentication/authorization failures, rate limiting, server errors, redirects,
transport failures, malformed success headers, and every other status fail closed.
An existing release tag is accepted only when its unique
`Docker-Content-Digest` already equals the approved digest. After tag creation, the
workflow performs another authenticated lookup and requires the exact digest before
continuing.

The release workflow installs GitHub CLI `2.96.0` from the official Linux amd64
archive whose SHA-256 is fixed in the workflow. The image-build, assembly, and
publication jobs reject an archive hash or reported version mismatch. Build records
are retained with each image artifact; the assembly record
`gh-cli-toolchain.json` is included in the evidence archive and directly covered by
the signed checksum set. The publication job reproduces the record from its own
installed binary and byte-compares it with that signed assembly record before any
GitHub mutation.

Immediately before draft creation, after draft upload, immediately before draft
promotion, and after publication, the publication job reads the remote tag through
the GitHub Git Data API, requires an annotated (not lightweight) tag, safely peels
any bounded annotated-tag chain, and requires the final commit to equal the approved
source commit. Between the two pre-publication tag checks it also requires the BFF,
Dagster, and edge GHCR SemVer tags to resolve to the signed image-lock digests and
repeats keyless Cosign verification against the exact tag-bound workflow identity.
Only then does `gh release edit --draft=false` make the release visible. RC tags stay
prereleases and are never marked latest; a formal SemVer release is explicitly marked
latest. A post-publication mismatch fails the job but deliberately does not delete or
rewrite the Release; maintainers must treat it as a security incident. These checks do
not replace an externally administered release-tag ruleset, tag deletion and update
restrictions, protected release environments, or GitHub immutable release settings.
Those repository controls must be enabled and tested before publication.

## Deploying the generated Compose document

Verify the downloaded deployment tar and metadata Sigstore bundle against the signed
checksum set, extract it, install the metadata bundle at the documented production
path, enter the single deployment top-level directory, and follow its root `README.md`.
The first local preflight is:

```bash
python3 scripts/release_bundle.py verify --bundle-root . --verify-signature
```

After populating `production/.env`, secret files and TLS, validate the same stable
Compose entry that backup, restore and drills consume:

```bash
python3 scripts/verify_production_compose.py --release \
  --compose-file production/compose.yaml \
  --env-file production/.env
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml config --quiet
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml pull
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps minio-volume-init
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  mysql redis minio qdrant tempo node-exporter
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps db-bootstrap
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps minio-bootstrap
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps migrate
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  keycloak otel-collector
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps identity-bootstrap
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  dagster-code dagster-webserver dagster-daemon bff worker prometheus grafana edge
```

The volume initializer, bootstrap and migration services are foreground one-shots. Each must exit zero
before the next phase; never mix an exited one-shot into detached `up --wait` health
semantics.

The release validator rejects missing digests, `latest`, unresolved image
variables, and any release service that still contains `build:`. Rollback means
deploying the previous signed bundle and following the database compatibility
window in `doc/runbooks/upgrade-rollback.md`; never reconstruct a rollback from
mutable tags.

The packaged backup and restore scripts do not use Git. They reverify the installed
metadata Sigstore bundle and every currently running release-service image against the
signed digest map; MySQL, MinIO, Qdrant and Redis are always required. A restore from
another release is accepted only when its exact tag, full commit and metadata SHA-256
are present in the signed restore compatibility policy and the operator repeats the
full commit. The v1.0 policy is empty, so every cross-commit restore fails closed.
