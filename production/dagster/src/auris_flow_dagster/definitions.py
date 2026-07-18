from typing import Any

from dagster import ConfigMapping, Definitions, Field, OpExecutionContext, Permissive, job, op

from auris_flow_dagster.callback import CompletionCallbackClient
from auris_flow_dagster.contracts import validate_auris_context
from auris_flow_dagster.observability import configure_observability, domain_span
from auris_flow_dagster.runtime import execute_and_report

_OBSERVABILITY = configure_observability()


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


@job(
    name="auris_flow_generic_job",
    description="Auris Flow 通用领域执行入口；保持租户、项目与 trace 全链路关联。",
    config=auris_run_config,
)
def auris_flow_generic_job() -> None:
    execute_auris_flow_domain_work()


defs = Definitions(jobs=[auris_flow_generic_job])
