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
   Release and publishes the isolated deployment/source/evidence archives, release
   metadata Sigstore bundle, manifest, checksum set, and checksum Sigstore bundle as
   permanent release assets. Manual
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
The deployment tar has one top-level `auris-flow-<tag>-deployment/` directory, a stable
`production/compose.yaml` containing the rendered digest-only document, and a
`production/release-metadata.json` binding its schema/tag/source commit, Compose
SHA-256, image-lock SHA-256, restore-policy SHA-256, and exact service-image map. The
metadata is signed separately; its Sigstore bundle is a published checksum-covered
asset installed as `production/release-metadata.sigstore.json`. It has no `.git` and
never contains a runner workspace path.

Images are first pushed under a commit staging tag. The workflow promotes their
already scanned and signed digests to the SemVer release tag only after every
image job succeeds and every unique final Compose digest has passed Trivy on
both `linux/amd64` and `linux/arm64`. This second scan includes MySQL, Redis,
MinIO, Qdrant, Keycloak, OpenTelemetry, Tempo, Prometheus, Grafana, and the node
exporter—not only the three project-built images. An existing release tag is
accepted only when it already points to the identical digest, and is otherwise
a hard failure.

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
  --file production/compose.yaml up -d --wait
```

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
