from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.context import RequestContext
from app.core.errors import ApiError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import RunRecord

AUDIO_INTELLIGENCE_EXECUTION_CONTRACT = "auris-flow-audio-intelligence-v1"
AUDIO_INTELLIGENCE_INPUT_SCHEMA = "auris-flow-execution-envelope-v1"
AUDIO_INTELLIGENCE_COMPLETION_SCHEMA = "auris-flow-audio-result-receipt-v1"
AUDIO_INTELLIGENCE_JOB_NAME = "auris_flow_audio_intelligence_v1"

AUDIO_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
AUDIO_IMPORT_INPUT_SCHEMA = "auris-flow-execution-envelope-v1"
AUDIO_IMPORT_COMPLETION_SCHEMA = "auris-flow-audio-import-result-v1"
AUDIO_IMPORT_JOB_NAME = "auris_flow_audio_import_v1"

DAGSTER_RUN_REQUEST_EVENT_TYPES = frozenset(
    {
        "task_run.requested",
        "audio_intelligence.requested",
        "backfill.requested",
        "asset_check.retry_requested",
        "conversation_boundary.sync_requested",
        "platform_sync.requested",
        "eval_run.requested",
        "agent_run.requested",
        "insight_metric_aggregation.requested",
        "hotword_analysis.requested",
        "hotword_pack_version.build-requested",
        "hotword_pack_version.eval-requested",
        "hotword_pack_version.publish-requested",
        "release_deployment.command-requested",
        "provider_test.requested",
    }
)

_GENERIC_EXECUTION_MODES = frozenset({"diagnostic"})

InputValidator = Callable[[dict[str, Any]], dict[str, Any]]
CompletionValidator = Callable[["RunRecord", dict[str, Any]], Any]
CompletionMaterializer = Callable[
    ["Session", RequestContext, "RunRecord", dict[str, Any], Any],
    "ExecutionMaterialization",
]


class ExecutionContractNotConfiguredError(ValueError):
    code = "EXECUTION_CONTRACT_NOT_CONFIGURED"

    def __init__(
        self,
        *,
        event_type: str,
        run_type: str,
        requested_contract: str | None,
    ) -> None:
        super().__init__(
            f"production execution contract is not configured for {event_type}/{run_type}"
        )
        self.event_type = event_type
        self.run_type = run_type
        self.requested_contract = requested_contract


@dataclass(frozen=True)
class ExecutionMaterialization:
    status: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ExecutionContract:
    contract_id: str
    event_type: str
    run_type: str
    dagster_job_name: str
    input_schema: str
    completion_schema: str
    materializer_name: str
    _input_validator: InputValidator
    _completion_validator: CompletionValidator
    _completion_materializer: CompletionMaterializer

    def validate_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._input_validator(payload)

    def validate_completion(self, record: RunRecord, payload: dict[str, Any]) -> Any:
        return self._completion_validator(record, payload)

    def materialize(
        self,
        session: Session,
        ctx: RequestContext,
        record: RunRecord,
        completion_receipt: dict[str, Any],
        validated_completion: Any,
    ) -> ExecutionMaterialization:
        return self._completion_materializer(
            session,
            ctx,
            record,
            completion_receipt,
            validated_completion,
        )


def _audio_import_input(payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.adapters import _audio_import_execution_envelope

    return _audio_import_execution_envelope(payload)


def _audio_intelligence_input(payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.adapters import _audio_execution_envelope

    return _audio_execution_envelope(payload)


def _audio_import_completion(record: RunRecord, payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.audio_import_completion_service import (
        validate_audio_import_completion_contract,
    )

    validate_audio_import_completion_contract(record, payload)
    result_ref = payload.get("result_ref")
    return dict(result_ref) if isinstance(result_ref, dict) else {}


def _audio_intelligence_completion(
    record: RunRecord,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if str(payload.get("status") or "success") != "success":
        return None
    from app.services.audio_intelligence_service import resolve_audio_intelligence_result

    return resolve_audio_intelligence_result(record, payload.get("result_ref"))


def _materialize_audio_import(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
    validated_completion: Any,
) -> ExecutionMaterialization:
    del validated_completion
    from app.services.audio_import_completion_service import (
        materialize_audio_import_completion,
    )

    import_batch = materialize_audio_import_completion(
        session,
        ctx,
        record,
        completion_receipt,
    )
    status = (
        "failed"
        if import_batch is not None and import_batch.get("status") == "failed"
        else str(completion_receipt.get("status") or "failed")
    )
    return ExecutionMaterialization(
        status=status if status in {"success", "failed"} else "failed",
        details={"import_batch": import_batch},
    )


def _materialize_audio_intelligence(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
    validated_completion: Any,
) -> ExecutionMaterialization:
    if str(completion_receipt.get("status") or "failed") != "success":
        return ExecutionMaterialization(status="failed", details={})
    if not isinstance(validated_completion, dict):
        raise RuntimeError("audio-intelligence completion validation result is missing")
    from app.services.audio_intelligence_service import (
        materialize_audio_intelligence_completion,
    )

    materialized_outputs = materialize_audio_intelligence_completion(
        session,
        ctx,
        record,
        completion_receipt,
        validated_result_ref=validated_completion,
    )
    return ExecutionMaterialization(
        status="success",
        details={"materialized_outputs": materialized_outputs},
    )


class ExecutionContractRegistry:
    def __init__(self, contracts: tuple[ExecutionContract, ...]) -> None:
        identities = {
            (contract.event_type, contract.run_type, contract.contract_id) for contract in contracts
        }
        if len(identities) != len(contracts):
            raise ValueError("execution contract registry contains duplicate identities")
        self._contracts = contracts

    @property
    def contracts(self) -> tuple[ExecutionContract, ...]:
        return self._contracts

    def resolve(
        self,
        *,
        event_type: str,
        run_type: str,
        payload: dict[str, Any],
    ) -> ExecutionContract | None:
        if event_type not in DAGSTER_RUN_REQUEST_EVENT_TYPES:
            return None
        raw_contract = payload.get("execution_contract")
        requested_contract = (
            str(raw_contract).strip()
            if isinstance(raw_contract, str) and str(raw_contract).strip()
            else None
        )
        for contract in self._contracts:
            if (
                contract.event_type == event_type
                and contract.run_type == run_type
                and contract.contract_id == requested_contract
            ):
                return contract

        execution_mode = str(payload.get("execution_mode") or "production").strip().lower()
        if requested_contract is None and execution_mode in _GENERIC_EXECUTION_MODES:
            return None
        raise ExecutionContractNotConfiguredError(
            event_type=event_type,
            run_type=run_type,
            requested_contract=requested_contract,
        )

    def require(
        self,
        *,
        event_type: str,
        run_type: str,
        payload: dict[str, Any],
    ) -> ExecutionContract:
        contract = self.resolve(
            event_type=event_type,
            run_type=run_type,
            payload=payload,
        )
        if contract is None:
            raise ExecutionContractNotConfiguredError(
                event_type=event_type,
                run_type=run_type,
                requested_contract=(str(payload.get("execution_contract") or "").strip() or None),
            )
        return contract


execution_contract_registry = ExecutionContractRegistry(
    (
        ExecutionContract(
            contract_id=AUDIO_IMPORT_EXECUTION_CONTRACT,
            event_type="task_run.requested",
            run_type="task_run",
            dagster_job_name=AUDIO_IMPORT_JOB_NAME,
            input_schema=AUDIO_IMPORT_INPUT_SCHEMA,
            completion_schema=AUDIO_IMPORT_COMPLETION_SCHEMA,
            materializer_name="audio_import",
            _input_validator=_audio_import_input,
            _completion_validator=_audio_import_completion,
            _completion_materializer=_materialize_audio_import,
        ),
        ExecutionContract(
            contract_id=AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
            event_type="audio_intelligence.requested",
            run_type="audio_intelligence",
            dagster_job_name=AUDIO_INTELLIGENCE_JOB_NAME,
            input_schema=AUDIO_INTELLIGENCE_INPUT_SCHEMA,
            completion_schema=AUDIO_INTELLIGENCE_COMPLETION_SCHEMA,
            materializer_name="audio_intelligence",
            _input_validator=_audio_intelligence_input,
            _completion_validator=_audio_intelligence_completion,
            _completion_materializer=_materialize_audio_intelligence,
        ),
    )
)


def preflight_production_execution_contract(
    *,
    event_type: str,
    run_type: str,
    requested_contract: str | None = None,
) -> ExecutionContract:
    """Fail closed before a production business action mutates persistent state.

    Business services that create runs directly must use the same registry gate as
    the generic run dispatcher.  Keeping this conversion here also guarantees a
    stable public 409 error instead of leaking the registry's internal exception.
    Diagnostic-only paths must remain separate and must not call this helper to
    advance business state.
    """

    payload: dict[str, Any] = {"execution_mode": "production"}
    if requested_contract:
        payload["execution_contract"] = requested_contract
    try:
        return execution_contract_registry.require(
            event_type=event_type,
            run_type=run_type,
            payload=payload,
        )
    except ExecutionContractNotConfiguredError as exc:
        raise ApiError(
            exc.code,
            "当前业务动作尚未配置生产执行契约，未保存任何变更",
            409,
            details=[
                {
                    "event_type": exc.event_type,
                    "run_type": exc.run_type,
                    "requested_contract": exc.requested_contract,
                }
            ],
            retryable=False,
        ) from exc
