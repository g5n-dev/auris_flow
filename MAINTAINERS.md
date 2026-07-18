# Maintainers

This file defines the maintainer responsibilities for the public repository surface. Named maintainers and release authority must be confirmed by the project owner before a formal open-source launch.

## Responsibilities

- Keep `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, and backend contracts aligned with the implemented behavior.
- Require the release verification gate before merging release candidates.
- Triage security reports privately and avoid exposing exploit details in public issues.
- Label known production gaps clearly instead of presenting local/demo paths as production-ready.
- Preserve tenant/project isolation, idempotency, audit, trace, and outbox guarantees in backend changes.

## Release Authority

Until ownership is confirmed, releases should be treated as development snapshots, not official production-ready distributions.
