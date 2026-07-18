# Third-Party Notices

This file records the direct dependency and public-dataset license boundary for
Auris Flow. It is not a substitute for the complete CycloneDX SBOM generated
from the locked dependency graph for each release candidate.

## Runtime and build dependencies

| Ecosystem | Direct dependency | Declared upstream license |
| --- | --- | --- |
| Python (BFF) | Alembic | MIT |
| Python (BFF) | Authlib | BSD-3-Clause |
| Python (BFF) | FastAPI | MIT |
| Python (BFF) | HTTPX | BSD-3-Clause |
| Python (BFF) | OpenTelemetry API / SDK / OTLP exporter | Apache-2.0 |
| Python (BFF) | OpenTelemetry FastAPI / HTTPX / urllib / Redis / SQLAlchemy instrumentation | Apache-2.0 |
| Python (BFF) | Pydantic | MIT |
| Python (BFF) | pydantic-settings | MIT |
| Python (BFF) | Prometheus Python client | Apache-2.0 |
| Python (BFF) | PyMySQL | MIT |
| Python (BFF) | python-dotenv | BSD-3-Clause |
| Python (BFF) | redis-py | MIT |
| Python (BFF) | SQLAlchemy | MIT |
| Python (BFF) | Uvicorn | BSD-3-Clause |
| Python (Dagster runtime) | Dagster | Apache-2.0 |
| Python (Dagster runtime) | Click | BSD-3-Clause |
| Python (Dagster runtime) | dagster-mysql | Apache-2.0 |
| Python (Dagster runtime) | dagster-webserver | Apache-2.0 |
| Python (Dagster runtime) | OpenTelemetry API / SDK / OTLP exporter / urllib instrumentation | Apache-2.0 |
| Python (Dagster runtime) | PyMySQL | MIT |
| npm | @vitejs/plugin-react | MIT |
| npm | lucide-react | ISC |
| npm | React | MIT |
| npm | React DOM | MIT |
| npm | Vite | MIT |
| npm (development) | @types/react | MIT |
| npm (development) | @types/react-dom | MIT |
| npm (development) | Playwright | Apache-2.0 |
| npm (development) | PostCSS | MIT |
| npm (development) | TypeScript | Apache-2.0 |

The locked transitive graph is authoritative for the actual release. CI must
generate separate BFF Python, Dagster Python, and npm CycloneDX documents from
`backend/uv.lock`, `production/dagster/uv.lock`, and
`prototype/auris-flow-ui/package-lock.json`, then retain them with the release
artifacts. Any dependency with missing, ambiguous, or non-allowlisted license
metadata in any of the three graphs is a release blocker until a maintainer
records a reviewed, expiring exception for that exact ecosystem, package, and
version.

The automated policy approves only expressions composed from the repository's
explicit permissive/weak-copyleft allowlist (`0BSD`, `Apache-2.0`,
`BSD-2-Clause`, `BSD-3-Clause`, `ISC`, `MIT`, `MIT-0`, `MPL-2.0`, and
`PSF-2.0`) with SPDX `AND`/`OR`. Generic or ambiguous labels, SPDX `WITH`
exceptions, and identifiers outside that allowlist fail closed unless an
unexpired review exception exists for the exact ecosystem, package, and locked
version. An exception is evidence of a scoped human review; it is not a
repository-wide approval of that license family.

As of the current Dagster lock, the following upstream metadata remains outside
the automatic allowlist and therefore blocks a release until separately
reviewed: `antlr4-python3-runtime@4.13.2` (`BSD`),
`python-dateutil@2.9.0.post0` (`Dual License`), and
`mysql-connector-python@9.7.0` (`GNU GPLv2 (with FOSS License Exception)`). No
review exception is asserted here for those packages. The existing Jinja2
exception remains limited to the exact package/version recorded in
`config/release/license-review-exceptions.json`.

## Public datasets

The repository does not contain public audio, transcripts, RTTM files, or
dataset archives. The current registry references AliMeeting/SLR119 and records
its data license as Creative Commons Attribution-ShareAlike 4.0 International;
associated source code is separately recorded as Apache-2.0. Download and use
require explicit license acceptance, attribution, provenance, and integrity
verification. See
`doc/backend-spec/public-audio-datasets-v0.1.json` for the machine-readable
record.

## Release rule

The source commit, images, SBOMs, license inventory, checksums, signatures,
NOTICE, and release notes must be produced from the same immutable release
candidate commit.
