# Governance

Auris Flow uses lightweight maintainer governance while the project is still a development baseline.

## Decision Rules

- Runtime behavior, API contracts, and verification gates take precedence over mock-only UI claims.
- Open-source release readiness requires passing the repository release gate and documenting known gaps.
- Production claims require explicit maintainer approval and evidence from real dependency stack verification.

## Contribution Boundaries

- Do not commit real secrets, customer data, raw audio, or transcripts.
- Do not bypass tenant/project checks, idempotency, audit, trace, or outbox behavior.
- Do not replace the current high-fidelity frontend prototype with an unrelated design direction.
- Do not introduce ClickHouse as a first-stage default component.
