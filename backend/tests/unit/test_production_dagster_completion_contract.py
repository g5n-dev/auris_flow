from __future__ import annotations

import sys
from pathlib import Path

PRODUCTION_DAGSTER_SRC = Path(__file__).resolve().parents[3] / "production" / "dagster" / "src"
sys.path.insert(0, str(PRODUCTION_DAGSTER_SRC))

from auris_flow_dagster.callback import canonical_signature_message  # noqa: E402
from auris_flow_dagster.contracts import validate_auris_context  # noqa: E402

from app.core.completion_signature import completion_signature_message  # noqa: E402


def test_production_dagster_signature_canonicalization_matches_bff_verifier() -> None:
    scope = validate_auris_context(
        {
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": "trace-contract-check",
            "run_id": "run-contract-check",
            "dispatch_idempotency_key": "dispatch:contract:check",
            "outbox_fencing_token": "7:3",
        }
    )
    shared = {
        "method": "POST",
        "path": "/api/v1/runs/run-contract-check/external-completion-receipts",
        "query": "",
        "idempotency_key": "dagster-completion:dg-contract-check",
        "timestamp": "2026-07-18T10:30:00+00:00",
        "nonce": "nonce-contract-check",
        "key_id": "dagster-2026-01",
        "body_sha256": "0" * 64,
    }
    assert canonical_signature_message(scope=scope, **shared) == completion_signature_message(
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        source="dagster",
        **shared,
    )
