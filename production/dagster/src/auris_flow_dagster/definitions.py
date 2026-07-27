from typing import Any

from dagster import ConfigMapping, Definitions, Field, OpExecutionContext, Permissive, job, op

from auris_flow_dagster.audio_import import execute_audio_import_and_report
from auris_flow_dagster.callback import CompletionCallbackClient
from auris_flow_dagster.contracts import (
    validate_audio_execution_envelope,
    validate_audio_import_envelope,
    validate_auris_context,
)
from auris_flow_dagster.observability import configure_observability, domain_span
from auris_flow_dagster.runtime import (
    configured_audio_runtime_dependencies,
    execute_and_report,
    execute_audio_intelligence_and_report,
)

_OBSERVABILITY = configure_observability()
_AUDIO_PROVIDER, _AUDIO_RESULT_STORE = configured_audio_runtime_dependencies()


def map_auris_run_config(config: dict[str, Any]) -> dict[str, Any]:
    """Map RealDagsterClient's top-level runConfigData into the single domain op."""

    # Validate before planning so missing scope/fencing never reaches domain work.
    validate_auris_context(config.get("auris_context"))
    return {
        "ops": {
            "execute_auris_flow_domain_work": {
                "config": {
                    "auris_context": config["auris_context"],
                    "execution": config.get("execution", {}),
                }
            }
        }
    }


auris_run_config = ConfigMapping(
    config_schema=Permissive(  # type: ignore[no-untyped-call]
        {
            "auris_context": Field(Permissive(), is_required=True),  # type: ignore[no-untyped-call]
            "execution": Field(
                Permissive(),  # type: ignore[no-untyped-call]
                is_required=False,
                default_value={},
            ),
        }
    ),
    config_fn=map_auris_run_config,
)


def map_audio_intelligence_run_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate both scope and immutable input binding before Dagster plans the op."""

    scope = validate_auris_context(config.get("auris_context"))
    validate_audio_execution_envelope(
        config.get("execution_envelope"),
        auris_context=scope,
    )
    return {
        "ops": {
            "execute_auris_flow_audio_intelligence_v1": {
                "config": {
                    "auris_context": config["auris_context"],
                    "execution_envelope": config["execution_envelope"],
                }
            }
        }
    }


audio_intelligence_run_config = ConfigMapping(
    config_schema={
        "auris_context": Field(Permissive(), is_required=True),  # type: ignore[no-untyped-call]
        "execution_envelope": Field(
            Permissive(),  # type: ignore[no-untyped-call]
            is_required=True,
        ),
    },
    config_fn=map_audio_intelligence_run_config,
)


def map_audio_import_run_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the server-owned audio import envelope before planning any network work."""

    scope = validate_auris_context(config.get("auris_context"))
    validate_audio_import_envelope(
        config.get("execution_envelope"),
        auris_context=scope,
    )
    return {
        "ops": {
            "execute_auris_flow_audio_import_v1": {
                "config": {
                    "auris_context": config["auris_context"],
                    "execution_envelope": config["execution_envelope"],
                }
            }
        }
    }


audio_import_run_config = ConfigMapping(
    config_schema={
        "auris_context": Field(Permissive(), is_required=True),  # type: ignore[no-untyped-call]
        "execution_envelope": Field(
            Permissive(),  # type: ignore[no-untyped-call]
            is_required=True,
        ),
    },
    config_fn=map_audio_import_run_config,
)


@op(
    name="execute_auris_flow_domain_work",
    description="执行 Auris Flow 领域任务并以受签名回执闭环运行状态。",
    config_schema={
        "auris_context": Field(Permissive(), is_required=True),  # type: ignore[no-untyped-call]
        "execution": Field(
            Permissive(),  # type: ignore[no-untyped-call]
            is_required=False,
            default_value={},
        ),
    },
)
def execute_auris_flow_domain_work(context: OpExecutionContext) -> dict[str, Any]:
    scope = validate_auris_context(context.op_config["auris_context"])
    execution = context.op_config.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("execution configuration must be an object")
    with domain_span(scope):
        context.log.info(
            "Auris Flow domain execution started",
            extra={"auris": scope.public_metadata()},
        )
        result = execute_and_report(
            scope=scope,
            dagster_run_id=context.run_id,
            execution=execution,
            callback=CompletionCallbackClient(),
        )
        context.log.info(
            "Auris Flow domain execution completed",
            extra={"auris": scope.public_metadata()},
        )
    return dict(result)


@op(
    name="execute_auris_flow_audio_intelligence_v1",
    description="验证精确版本音频，调用受策略约束的 HTTPS 推理服务并持久化结果证据。",
    config_schema={
        "auris_context": Field(Permissive(), is_required=True),  # type: ignore[no-untyped-call]
        "execution_envelope": Field(
            Permissive(),  # type: ignore[no-untyped-call]
            is_required=True,
        ),
    },
)
def execute_auris_flow_audio_intelligence_v1(context: OpExecutionContext) -> dict[str, Any]:
    scope = validate_auris_context(context.op_config["auris_context"])
    envelope = validate_audio_execution_envelope(
        context.op_config["execution_envelope"],
        auris_context=scope,
    )
    with domain_span(scope):
        context.log.info(
            "Auris Flow audio input verification started",
            extra={
                "auris": {
                    **scope.public_metadata(),
                    "execution_contract": envelope.execution_contract,
                    "execution_envelope_sha256": envelope.sha256,
                }
            },
        )
        result = execute_audio_intelligence_and_report(
            scope=scope,
            dagster_run_id=context.run_id,
            envelope=envelope,
            callback=CompletionCallbackClient(),
            provider=_AUDIO_PROVIDER,
            manifest_store=_AUDIO_RESULT_STORE,
        )
    return dict(result)


@op(
    name="execute_auris_flow_audio_import_v1",
    description="分页读取平台音频清单，安全复制精确版本对象并持久化不可变导入清单。",
    config_schema={
        "auris_context": Field(Permissive(), is_required=True),  # type: ignore[no-untyped-call]
        "execution_envelope": Field(
            Permissive(),  # type: ignore[no-untyped-call]
            is_required=True,
        ),
    },
)
def execute_auris_flow_audio_import_v1(context: OpExecutionContext) -> dict[str, Any]:
    scope = validate_auris_context(context.op_config["auris_context"])
    envelope = validate_audio_import_envelope(
        context.op_config["execution_envelope"],
        auris_context=scope,
    )
    with domain_span(scope):
        context.log.info(
            "Auris Flow platform audio import started",
            extra={
                "auris": {
                    **scope.public_metadata(),
                    "execution_contract": envelope.execution_contract,
                    "execution_envelope_sha256": envelope.sha256,
                    "import_batch_id": envelope.import_batch_id,
                    "root_trace_id": envelope.root_trace_id,
                }
            },
        )
        result = execute_audio_import_and_report(
            scope=scope,
            dagster_run_id=context.run_id,
            envelope=envelope,
            callback=CompletionCallbackClient(),
        )
    return dict(result)


@job(
    name="auris_flow_generic_job",
    description="Auris Flow 通用领域执行入口；保持租户、项目与 trace 全链路关联。",
    config=auris_run_config,
)
def auris_flow_generic_job() -> None:
    execute_auris_flow_domain_work()


@job(
    name="auris_flow_audio_intelligence_v1",
    description="Auris Flow 音频智能 v1 严格输入完整性执行入口。",
    config=audio_intelligence_run_config,
)
def auris_flow_audio_intelligence_v1() -> None:
    execute_auris_flow_audio_intelligence_v1()


@job(
    name="auris_flow_audio_import_v1",
    description="Auris Flow 平台音频导入 v1 严格来源与精确版本对象执行入口。",
    config=audio_import_run_config,
)
def auris_flow_audio_import_v1() -> None:
    execute_auris_flow_audio_import_v1()


defs = Definitions(
    jobs=[
        auris_flow_generic_job,
        auris_flow_audio_intelligence_v1,
        auris_flow_audio_import_v1,
    ]
)
