from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import gettempdir
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

TEST_DB = Path(gettempdir()) / f"auris_flow_pytest_{os.getpid()}.sqlite"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["APP_ENV"] = "test"
os.environ["ALLOW_DEV_AUTH"] = "true"
os.environ["COMPLETION_RECEIPT_SECRET"] = "auris-test-completion-secret-32chars-minimum"
os.environ["COMPLETION_RECEIPT_SIGNATURE_ID"] = "auris-test-completion"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.secrets import SecretFileSettingsSource  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.execution_contract_registry import (  # noqa: E402
    ExecutionContract,
    ExecutionContractNotConfiguredError,
    ExecutionMaterialization,
    execution_contract_registry,
)
from app.services.resource_service import load_seed_file, seed_database  # noqa: E402


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_database_file():
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()
    yield
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture(autouse=True)
def reset_database():
    # A hotword candidate points at the published baseline through a
    # self-referential FK. SQLite implements ``DROP TABLE`` as an implicit
    # row delete, so a populated self-reference can otherwise make fixture
    # teardown fail before SQLAlchemy can rebuild the schema.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(bind=connection)
        Base.metadata.create_all(bind=connection)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with SessionLocal() as session:
        seed_database(session, load_seed_file())
    yield
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(bind=connection)
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer dev-token",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "pytest-request",
    }


@pytest.fixture
def configured_test_business_execution_contracts():
    """Opt-in dedicated executors for downstream materializer integration tests.

    Production deliberately has no contracts for these actions. Tests that need
    to exercise the legacy materializers must request this fixture explicitly;
    fail-closed endpoint tests therefore still use the production registry.
    """

    def validate_input(payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)

    def validate_completion(_record: Any, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload)

    def materialize(
        session: Any,
        ctx: Any,
        record: Any,
        completion_receipt: dict[str, Any],
        _validated_completion: Any,
    ) -> ExecutionMaterialization:
        status = str(completion_receipt.get("status") or "failed")
        if status == "success":
            payload_key: str | None = None
            result: Any = None
            if record.run_type == "hotword_build":
                from app.services.hotword_service import materialize_hotword_build_completion

                payload_key = "hotword_build"
                result = materialize_hotword_build_completion(
                    session,
                    ctx,
                    record,
                    completion_receipt,
                )
            elif record.run_type == "hotword_eval":
                from app.services.hotword_service import materialize_hotword_eval_completion

                payload_key = "hotword_eval"
                result = materialize_hotword_eval_completion(
                    session,
                    ctx,
                    record,
                    completion_receipt,
                )
            elif record.run_type == "hotword_publish":
                from app.services.hotword_service import materialize_hotword_publish_completion

                payload_key = "hotword_publish"
                result = materialize_hotword_publish_completion(
                    session,
                    ctx,
                    record,
                    completion_receipt,
                )
            elif record.run_type == "hotword_analysis":
                from app.services.hotword_service import materialize_hotword_analysis_completion

                payload_key = "hotword_metrics"
                result = materialize_hotword_analysis_completion(
                    session,
                    ctx,
                    record,
                    completion_receipt,
                )
            elif record.run_type == "label_optimization":
                from app.services.prompt_candidate_service import (
                    materialize_optimization_prompt_candidates,
                )

                prior_status = record.status
                record.status = "success"
                try:
                    candidates = materialize_optimization_prompt_candidates(
                        session,
                        ctx,
                        record,
                        completion_receipt,
                    )
                finally:
                    record.status = prior_status
                if candidates:
                    result_ref = dict(record.payload.get("result_ref") or {})
                    result_ref["prompt_candidate_ids"] = [
                        candidate.candidate_id for candidate in candidates
                    ]
                    record.payload = {**record.payload, "result_ref": result_ref}
            elif record.run_type == "eval_run":
                from app.models import LabelEvalResult
                from app.services.label_eval_result_service import label_eval_result_data

                eval_result = (
                    session.query(LabelEvalResult)
                    .filter_by(
                        tenant_id=record.tenant_id,
                        project_id=record.project_id,
                        eval_run_id=record.run_id,
                    )
                    .one_or_none()
                )
                if eval_result is not None:
                    record.payload = {
                        **record.payload,
                        "label_eval_result": label_eval_result_data(eval_result),
                    }
            if payload_key is not None:
                record.payload = {**record.payload, payload_key: result}
        return ExecutionMaterialization(
            status=status if status in {"success", "failed"} else "failed",
            details={},
        )

    identities = (
        (
            "hotword_pack_version.build-requested",
            "hotword_build",
            "auris_test_hotword_build_v1",
        ),
        (
            "hotword_pack_version.eval-requested",
            "hotword_eval",
            "auris_test_hotword_eval_v1",
        ),
        (
            "hotword_pack_version.publish-requested",
            "hotword_publish",
            "auris_test_hotword_publish_v1",
        ),
        (
            "hotword_analysis.requested",
            "hotword_analysis",
            "auris_test_hotword_analysis_v1",
        ),
        (
            "agent_run.requested",
            "label_optimization",
            "auris_test_label_optimization_v1",
        ),
        (
            "eval_run.requested",
            "eval_run",
            "auris_test_label_eval_v1",
        ),
        (
            "release_deployment.command-requested",
            "release_command",
            "auris_test_release_command_v1",
        ),
        (
            "conversation_boundary.sync_requested",
            "boundary_sync",
            "auris_test_boundary_sync_v1",
        ),
        (
            "task_run.requested",
            "task_run",
            "auris_test_task_run_v1",
        ),
    )
    contracts = tuple(
        ExecutionContract(
            # Legacy test payloads predate explicit contract ids. This None
            # identity exists only while this fixture is active.
            contract_id=cast(str, None),
            event_type=event_type,
            run_type=run_type,
            dagster_job_name=job_name,
            input_schema="auris-test-execution-envelope-v1",
            completion_schema="auris-test-completion-v1",
            materializer_name=f"test_{run_type}",
            _input_validator=validate_input,
            _completion_validator=validate_completion,
            _completion_materializer=materialize,
        )
        for event_type, run_type, job_name in identities
    )
    original_contracts = execution_contract_registry._contracts
    execution_contract_registry._contracts = (*original_contracts, *contracts)
    try:
        yield
    finally:
        execution_contract_registry._contracts = original_contracts


@pytest.fixture
def configured_test_legacy_generic_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt an old contract test into an in-process, non-production executor.

    The application registry remains fail closed.  A test requesting this
    fixture may exercise the pre-registry synchronous read/write projections
    without pretending that ``auris_flow_generic_job`` is a production
    executor.  Explicit/unknown contract ids are never swallowed.
    """

    legacy_identities = frozenset(
        {
            ("task_run.requested", "task_run"),
            ("backfill.requested", "asset_backfill"),
            ("asset_check.retry_requested", "asset_check_retry"),
            ("conversation_boundary.sync_requested", "boundary_sync"),
            ("platform_sync.requested", "platform_sync"),
            ("eval_run.requested", "eval_run"),
            ("agent_run.requested", "eval_feedback"),
            ("agent_run.requested", "label_optimization"),
            ("agent_run.requested", "label_extraction"),
            ("insight_metric_aggregation.requested", "insight_metric_aggregation"),
            ("hotword_analysis.requested", "hotword_analysis"),
            ("hotword_pack_version.build-requested", "hotword_build"),
            ("hotword_pack_version.eval-requested", "hotword_eval"),
            ("hotword_pack_version.publish-requested", "hotword_publish"),
            ("release_deployment.command-requested", "release_command"),
            ("provider_test.requested", "provider_test"),
        }
    )
    production_resolve = execution_contract_registry.resolve

    def resolve_test_executor(
        *,
        event_type: str,
        run_type: str,
        payload: dict[str, Any],
    ):
        try:
            return production_resolve(
                event_type=event_type,
                run_type=run_type,
                payload=payload,
            )
        except ExecutionContractNotConfiguredError:
            requested_contract = payload.get("execution_contract")
            if (event_type, run_type) in legacy_identities and not (
                isinstance(requested_contract, str) and requested_contract.strip()
            ):
                return None
            raise

    monkeypatch.setattr(
        execution_contract_registry,
        "resolve",
        resolve_test_executor,
    )

    production_require = execution_contract_registry.require

    def test_materialize(
        _session: Any,
        _ctx: Any,
        _record: Any,
        completion_receipt: dict[str, Any],
        _validated_completion: Any,
    ) -> ExecutionMaterialization:
        status = str(completion_receipt.get("status") or "failed")
        return ExecutionMaterialization(
            status=status if status in {"success", "failed"} else "failed",
            details={},
        )

    def require_test_executor(
        *,
        event_type: str,
        run_type: str,
        payload: dict[str, Any],
    ) -> ExecutionContract:
        try:
            return production_require(
                event_type=event_type,
                run_type=run_type,
                payload=payload,
            )
        except ExecutionContractNotConfiguredError:
            requested_contract = payload.get("execution_contract")
            if (event_type, run_type) not in legacy_identities or (
                isinstance(requested_contract, str) and requested_contract.strip()
            ):
                raise
            return ExecutionContract(
                contract_id="auris-test-only-legacy-preflight-v1",
                event_type=event_type,
                run_type=run_type,
                dagster_job_name="auris_test_only_legacy_executor",
                input_schema="auris-test-only-input-v1",
                completion_schema="auris-test-only-completion-v1",
                materializer_name="test_only_legacy_preflight",
                _input_validator=lambda candidate: dict(candidate),
                _completion_validator=lambda _record, candidate: dict(candidate),
                _completion_materializer=test_materialize,
            )

    monkeypatch.setattr(
        execution_contract_registry,
        "require",
        require_test_executor,
    )


@pytest.fixture
def allow_inline_production_settings_for_policy_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolate cross-field model checks from the separately tested source policy."""

    original = SecretFileSettingsSource._has_nonempty_inline_value

    def ignore_initializer_values(
        source: SecretFileSettingsSource,
        field_name: str,
    ) -> bool:
        init_values = source.settings_sources_data.get("InitSettingsSource", {})
        if field_name in init_values:
            return False
        return original(source, field_name)

    monkeypatch.setattr(
        SecretFileSettingsSource,
        "_has_nonempty_inline_value",
        ignore_initializer_values,
    )
