from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.label_policy.canonical import canonicalize_policy, sha256_json
from app.domain.label_policy.registry import PATH_REGISTRY_V1, ThresholdDimension, ValueKind
from app.schemas.label_policy import (
    CompareExpression,
    Expression,
    LabelPolicyDSL,
    LogicalExpression,
    NotExpression,
    NullExpression,
    SetExpression,
)

MAX_AST_NODES = 512
MAX_AST_DEPTH = 12
MAX_EXECUTION_STEPS = 4096
COMPILER_VERSION = "label-policy-compiler/1.0.0"


class PolicyCompileError(ValueError):
    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class CompiledPolicy:
    policy: LabelPolicyDSL
    canonical_ast: dict[str, Any]
    source_sha256: str
    canonical_sha256: str
    compiler_version: str
    ast_nodes: int
    max_depth: int
    execution_step_budget: int = MAX_EXECUTION_STEPS


def _value_kind(value: object) -> ValueKind | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    return None


def _path_kind(path: str) -> ValueKind:
    spec = PATH_REGISTRY_V1.get(path)
    if spec is None:
        raise PolicyCompileError(
            "POLICY_PATH_UNKNOWN",
            f"policy path is not registered: {path}",
            path=path,
        )
    return spec.kind


def _validate_expression(
    expression: Expression,
    *,
    threshold_dimensions: dict[str, ThresholdDimension],
    depth: int,
) -> tuple[int, int]:
    if depth > MAX_AST_DEPTH:
        raise PolicyCompileError(
            "POLICY_AST_TOO_DEEP",
            f"policy AST exceeds maximum depth {MAX_AST_DEPTH}",
        )
    if isinstance(expression, LogicalExpression):
        nodes = 1
        deepest = depth
        for item in expression.items:
            child_nodes, child_depth = _validate_expression(
                item,
                threshold_dimensions=threshold_dimensions,
                depth=depth + 1,
            )
            nodes += child_nodes
            deepest = max(deepest, child_depth)
        return nodes, deepest
    if isinstance(expression, NotExpression):
        child_nodes, child_depth = _validate_expression(
            expression.item,
            threshold_dimensions=threshold_dimensions,
            depth=depth + 1,
        )
        return child_nodes + 1, max(depth, child_depth)

    path_kind = _path_kind(expression.path)
    if isinstance(expression, CompareExpression):
        if expression.threshold is not None:
            threshold_dimension = threshold_dimensions.get(expression.threshold)
            if threshold_dimension is None:
                raise PolicyCompileError(
                    "POLICY_THRESHOLD_UNKNOWN",
                    f"policy threshold is not defined: {expression.threshold}",
                )
            if path_kind != "int":
                raise PolicyCompileError(
                    "POLICY_TYPE_MISMATCH",
                    f"threshold comparison requires an integer path: {expression.path}",
                    path=expression.path,
                )
            expected_dimension = PATH_REGISTRY_V1[expression.path].threshold_dimension
            if expected_dimension is None or expected_dimension != threshold_dimension:
                raise PolicyCompileError(
                    "POLICY_THRESHOLD_DIMENSION_MISMATCH",
                    (
                        f"threshold {expression.threshold!r} has dimension "
                        f"{threshold_dimension!r}, expected {expected_dimension!r} "
                        f"for {expression.path}"
                    ),
                    path=expression.path,
                )
        else:
            value_kind = _value_kind(expression.value)
            if expression.value is None and expression.op not in {"eq", "ne"}:
                raise PolicyCompileError(
                    "POLICY_NULL_ORDERING",
                    "null can only be used with eq or ne; prefer is_null/is_not_null",
                    path=expression.path,
                )
            if value_kind is not None and value_kind != path_kind:
                raise PolicyCompileError(
                    "POLICY_TYPE_MISMATCH",
                    f"literal type does not match registered path: {expression.path}",
                    path=expression.path,
                )
        if expression.op in {"lt", "lte", "gt", "gte"} and path_kind != "int":
            raise PolicyCompileError(
                "POLICY_ORDERING_TYPE_INVALID",
                f"ordering comparison requires an integer path: {expression.path}",
                path=expression.path,
            )
    elif isinstance(expression, SetExpression):
        for value in expression.values:
            if _value_kind(value) != path_kind:
                raise PolicyCompileError(
                    "POLICY_TYPE_MISMATCH",
                    f"set member type does not match registered path: {expression.path}",
                    path=expression.path,
                )
    elif not isinstance(expression, NullExpression):
        raise PolicyCompileError("POLICY_EXPRESSION_UNKNOWN", "unsupported policy expression")
    return 1, depth


def compile_policy(policy: LabelPolicyDSL) -> CompiledPolicy:
    threshold_dimensions = {threshold.key: threshold.type for threshold in policy.thresholds}
    ast_nodes = 0
    max_depth = 1
    for rule in policy.rules:
        rule_nodes, rule_depth = _validate_expression(
            rule.when,
            threshold_dimensions=threshold_dimensions,
            depth=1,
        )
        ast_nodes += rule_nodes
        max_depth = max(max_depth, rule_depth)
    if ast_nodes > MAX_AST_NODES:
        raise PolicyCompileError(
            "POLICY_AST_TOO_LARGE",
            f"policy AST exceeds maximum node count {MAX_AST_NODES}",
        )
    source = policy.model_dump(mode="json", exclude_none=False)
    canonical = canonicalize_policy(policy)
    return CompiledPolicy(
        policy=policy,
        canonical_ast=canonical,
        source_sha256=sha256_json(source),
        canonical_sha256=sha256_json(canonical),
        compiler_version=COMPILER_VERSION,
        ast_nodes=ast_nodes,
        max_depth=max_depth,
    )
