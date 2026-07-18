# Release supply-chain runbook

The supported production artifact is a commit-bound release bundle produced by
`.github/workflows/release-images.yml`. A source checkout or a mutable image tag
alone is not a production release.

## Maintainer prerequisites

- Protect the GitHub `release` environment with required reviewers.
- Require the normal `Verify` and CodeQL checks before creating the tag.
- Enable private vulnerability reporting, secret scanning, and push protection.
- Allow the release workflow to write packages and request an OIDC identity.
- Treat GHCR release tags as immutable. Never retarget or delete a published
  release tag to repair a release; publish the next release candidate instead.

## Candidate procedure

1. Run the complete release verification on the candidate commit.
2. Push an annotated SemVer tag such as `v1.0.0-rc.1`, or manually dispatch the
   workflow with an `-rc.N` tag while validating the release process.
3. Approve the protected `release` environment only after checking the source
   commit shown by the workflow.
4. Download the final `auris-flow-<tag>-<commit>` artifact. Verify
   `SHA256SUMS`, then verify each image with Cosign against the repository's
   `release-images.yml` workflow identity and GitHub's token issuer.
5. Confirm `release-manifest.json`, `images.lock.json`, image SBOMs, dependency
   SBOMs, vulnerability reports, and the release notes name the same tag and
   source commit.

The workflow builds BFF, edge, and Dagster images for `linux/amd64` and
`linux/arm64`. It blocks HIGH/CRITICAL image findings, audits both locked Python
environments and npm, signs exact manifest digests with keyless Cosign, resolves
every third-party Compose image to a digest, and renders a release Compose file
with no `build:` directives. No vulnerability ignore file is accepted by this
workflow; a future exception process must be reviewed, scoped, and expiring
before it can be introduced.

Images are first pushed under a commit staging tag. The workflow promotes their
already scanned and signed digests to the SemVer release tag only after every
image job succeeds and every unique final Compose digest has passed Trivy on
both `linux/amd64` and `linux/arm64`. This second scan includes MySQL, Redis,
MinIO, Qdrant, Keycloak, OpenTelemetry, Tempo, Prometheus, Grafana, and the node
exporter—not only the three project-built images. An existing release tag is
accepted only when it already points to the identical digest, and is otherwise
a hard failure.

## Deploying the generated Compose document

Keep `compose.release.json` in its downloaded bundle layout or in the repository
layout used by the workflow, because its config and secret file references are
relative. Populate `production/.env` from `production/.env.example`, initialize
the Docker secret files, install the TLS certificate, and then run:

```bash
docker compose \
  --env-file build/release/production/.env \
  --file build/release/production/compose.release.json \
  up -d
```

Before deployment, the same document can be checked locally:

```bash
python3 scripts/verify_production_compose.py --release \
  --compose-file build/release/production/compose.release.json \
  --env-file build/release/production/.env
```

The release validator rejects missing digests, `latest`, unresolved image
variables, and any release service that still contains `build:`. Rollback means
deploying the previous signed bundle and following the database compatibility
window in `doc/runbooks/upgrade-rollback.md`; never reconstruct a rollback from
mutable tags.
