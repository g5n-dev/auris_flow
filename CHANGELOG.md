# Changelog

All notable changes to Auris Flow are tracked here.

## Unreleased

### Added

- Added the Linux single-host production Compose candidate with FastAPI BFF, asynchronous Worker, MySQL,
  Redis, MinIO, Qdrant, real Dagster, a reference Keycloak IdP, TLS edge, OpenTelemetry Collector, Tempo,
  Prometheus, Grafana and node-exporter.
- Added generic OIDC Authorization Code + PKCE, strict issuer/audience/JWKS validation, pre-provisioned
  identity mapping, opaque HttpOnly browser sessions and CSRF/Origin protection.
- Added an HTTPS semantic embedding provider boundary with exact model dimension validation; deterministic
  vectors remain test-only and are rejected in `prod/release`.
- Added request-bound callback HMAC v2 key rotation/replay protection and real Dagster completion callbacks.
- Added quiesced MySQL/MinIO/Qdrant backup, offline manifest verification and empty-environment restore/drill
  tooling for the single-host baseline.
- Added production installation, SLO/alert, upgrade/rollback, key rotation, security incident and compatibility
  runbooks.

### Security and release engineering

- Hardened production configuration to fail closed on development authentication, weak/demo credentials,
  wildcard origins/hosts, fake/local adapters, non-strict readiness and missing real dependency settings.
- Added structured redacted logging, OTel trace correlation and a network-restricted Prometheus endpoint.
- Added governance, support, maintainer, issue and release templates plus runtime/OpenAPI drift and
  supply-chain evidence gates.
- Upgraded the isolated production Dagster runtime to `1.13.1`, `dagster-mysql` to `0.29.1`, and Click to
  `8.3.3`; the locked runtime graph now clears the strict vulnerability audit without an advisory waiver.
- Added immutable multi-architecture image provenance, CycloneDX SBOM, HIGH/CRITICAL image scanning,
  keyless signature verification, digest-pinned release Compose rendering, and artifact checksums.

### Release status

- This is still a `v1.0.0` candidate, not a published or supported production release.
- The project owner rights-holder authorization/signature, final NOTICE identity, real `v1.0.0-rc.1`
  release drill, external clean installation, notification routing and formal release approval remain open
  human/external gates.
