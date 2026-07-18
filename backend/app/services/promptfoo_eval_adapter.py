from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.models import RunRecord, StorageObject
from app.schemas.evaluation import LabelingEvalCompletionResult
from app.schemas.requests import RunCompletionReceiptRequest
from app.services.audio_intelligence_service import validate_scoped_storage_object_reference

PROMPTFOO_EVAL_SUITES = (
    "golden",
    "boundary",
    "adversarial",
    "fresh",
    "canary",
    "regression",
)
PROMPTFOO_RESULT_SCHEMA_VERSION = "auris.promptfoo-eval-result.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/yaml",
        "application/x-yaml",
        "text/yaml",
        "text/x-yaml",
    }
)
_RESULT_CONTENT_TYPES = frozenset({"application/json"})
_DISPATCH_EXTERNAL_ID_KEYS = {
    "dagster": "external_run_id",
    "object_storage": "storage_object_id",
    "external_callback": "callback_receipt_id",
}
PromptfooCompletionAdapter = Literal["dagster", "object_storage", "external_callback"]


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


class PromptfooLockedVersions(BaseModel):
    """Exact strong-version manifest already frozen by an internal labeling EvalRun."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scene_profile_id: str = Field(min_length=1, max_length=128)
    scene_profile_version_id: str = Field(min_length=1, max_length=128)
    scene_profile_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eval_dataset_version_id: str = Field(min_length=1, max_length=128)
    eval_dataset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eval_dataset_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eval_dataset_manifest_storage_object_id: str = Field(min_length=1, max_length=128)
    eval_dataset_resource_version: int = Field(ge=1)
    label_version_id: str = Field(min_length=1, max_length=128)
    label_resource_version: int = Field(ge=1)
    prompt_version_id: str = Field(min_length=1, max_length=128)
    prompt_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: str = Field(min_length=1, max_length=256)
    aggregation_policy_version_id: str = Field(min_length=1, max_length=128)
    aggregation_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    optimization_run_id: str = Field(min_length=1, max_length=128)
    optimization_run_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_suites: list[
        Literal["golden", "boundary", "adversarial", "fresh", "canary", "regression"]
    ] = Field(min_length=6, max_length=6)

    @field_validator("evaluation_suites")
    @classmethod
    def suites_are_locked_in_release_order(cls, value: list[str]) -> list[str]:
        if tuple(value) != PROMPTFOO_EVAL_SUITES:
            raise ValueError("all six locked evaluation suites are required in canonical order")
        return value


class PromptfooLockedBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locked_versions: PromptfooLockedVersions

    @model_validator(mode="after")
    def binding_matches_locked_versions(self) -> PromptfooLockedBundle:
        expected = _canonical_sha256(self.locked_versions.model_dump(mode="json"))
        if self.binding_sha256 != expected:
            raise ValueError("binding_sha256 does not match the locked EvalRun bundle")
        return self


class PromptfooArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    storage_object_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromptfooEvalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    eval_run_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    dispatch_adapter: PromptfooCompletionAdapter
    dispatch_external_id: str = Field(min_length=1, max_length=256)
    bundle: PromptfooLockedBundle
    config_artifact: PromptfooArtifactReference

    @field_validator("dispatch_external_id")
    @classmethod
    def dispatch_external_id_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dispatch_external_id must not be blank")
        return value


class PromptfooResultArtifactDocument(BaseModel):
    """Canonical JSON stored in object storage by the optional Promptfoo executor."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["auris.promptfoo-eval-result.v1"]
    eval_run_id: str = Field(min_length=1, max_length=128)
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_run_id: str = Field(min_length=1, max_length=256)
    labeling_eval_result: LabelingEvalCompletionResult
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider_metadata")
    @classmethod
    def provider_metadata_is_non_authoritative(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("provider") not in {None, "promptfoo"}:
            raise ValueError("provider_metadata.provider must be promptfoo")
        if value.get("authoritative") is True or value.get("fact_source") is not None:
            raise ValueError("Promptfoo provider metadata cannot claim business authority")
        try:
            canonical_json_bytes(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider_metadata must be finite canonical JSON") from exc
        return value


def serialize_promptfoo_result_artifact(document: PromptfooResultArtifactDocument) -> bytes:
    """Serialize exactly the bytes whose SHA-256 must be registered in StorageObject."""

    return canonical_json_bytes(document.model_dump(mode="json"))


@dataclass(frozen=True)
class PromptfooAdapterConfig:
    mode: Literal["disabled", "optional"] = "disabled"
    executable: str = "promptfoo"
    timeout_seconds: int = 7200

    def __post_init__(self) -> None:
        executable = self.executable.strip()
        if not executable or len(executable) > 1024 or any(ord(char) < 32 for char in executable):
            raise ValueError("PROMPTFOO_EXECUTABLE must be a non-blank executable name or path")
        if executable.startswith("-"):
            raise ValueError("PROMPTFOO_EXECUTABLE must not be an option")
        if not 1 <= self.timeout_seconds <= 7200:
            raise ValueError("PROMPTFOO_TIMEOUT_SECONDS must be between 1 and 7200")
        object.__setattr__(self, "executable", executable)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PromptfooAdapterConfig:
        source = os.environ if env is None else env
        mode = source.get("AURIS_PROMPTFOO_ADAPTER", "disabled").strip().lower()
        if mode not in {"disabled", "optional"}:
            raise ValueError("AURIS_PROMPTFOO_ADAPTER must be disabled or optional")
        raw_timeout = source.get("PROMPTFOO_TIMEOUT_SECONDS", "7200").strip()
        try:
            timeout_seconds = int(raw_timeout)
        except ValueError as exc:
            raise ValueError("PROMPTFOO_TIMEOUT_SECONDS must be an integer") from exc
        return cls(
            mode=mode,  # type: ignore[arg-type]
            executable=source.get("PROMPTFOO_EXECUTABLE", "promptfoo"),
            timeout_seconds=timeout_seconds,
        )


@dataclass(frozen=True)
class PromptfooMaterializedPaths:
    """Trusted paths produced after fetching the verified config artifact."""

    sandbox_root: Path
    config_path: Path
    result_path: Path


@dataclass(frozen=True)
class PromptfooCommandPlan:
    status: Literal["disabled", "unavailable", "ready"]
    required: bool
    argv: tuple[str, ...]
    shell: Literal[False]
    timeout_seconds: int
    reason: str | None
    config_artifact: PromptfooArtifactReference
    result_content_type: Literal["application/json"] = "application/json"


ExecutableResolver = Callable[[str], str | None]


class PromptfooCliAdapter:
    """Build a safe CLI plan; process execution remains in the controlled worker."""

    def __init__(
        self,
        config: PromptfooAdapterConfig | None = None,
        *,
        executable_resolver: ExecutableResolver = shutil.which,
    ) -> None:
        self.config = config or PromptfooAdapterConfig.from_env()
        self._resolve_executable = executable_resolver

    def plan(
        self,
        request: PromptfooEvalRequest,
        paths: PromptfooMaterializedPaths,
    ) -> PromptfooCommandPlan:
        config_path, result_path = _validated_materialized_paths(paths)
        if self.config.mode == "disabled":
            return PromptfooCommandPlan(
                status="disabled",
                required=False,
                argv=(),
                shell=False,
                timeout_seconds=self.config.timeout_seconds,
                reason="adapter-disabled",
                config_artifact=request.config_artifact,
            )

        executable = self._resolve_executable(self.config.executable)
        if executable is None:
            return PromptfooCommandPlan(
                status="unavailable",
                required=False,
                argv=(),
                shell=False,
                timeout_seconds=self.config.timeout_seconds,
                reason="optional-executable-unavailable",
                config_artifact=request.config_artifact,
            )
        normalized_executable = _validated_executable(executable)
        return PromptfooCommandPlan(
            status="ready",
            required=False,
            argv=(
                normalized_executable,
                "eval",
                "--config",
                str(config_path),
                "--output",
                str(result_path),
                "--no-progress-bar",
            ),
            shell=False,
            timeout_seconds=self.config.timeout_seconds,
            reason=None,
            config_artifact=request.config_artifact,
        )


def _validated_executable(value: str) -> str:
    executable = value.strip()
    if (
        not executable
        or not Path(executable).is_absolute()
        or any(ord(character) < 32 for character in executable)
    ):
        raise ValueError("resolved Promptfoo executable must be an absolute path")
    return executable


def _validated_materialized_paths(paths: PromptfooMaterializedPaths) -> tuple[Path, Path]:
    root = paths.sandbox_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("sandbox_root must be a directory")
    try:
        config_path = paths.config_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("materialized Promptfoo config does not exist") from exc
    result_path = paths.result_path.resolve(strict=False)
    if not config_path.is_relative_to(root) or not result_path.is_relative_to(root):
        raise ValueError("config_path and result_path must remain inside sandbox_root")
    if not config_path.is_file() or config_path.suffix.lower() not in {".json", ".yaml", ".yml"}:
        raise ValueError("materialized Promptfoo config must be a JSON or YAML file")
    if result_path.suffix.lower() != ".json":
        raise ValueError("Promptfoo result_path must use the .json suffix")
    if not result_path.parent.exists() or not result_path.parent.is_dir():
        raise ValueError("Promptfoo result_path parent must already exist")
    if result_path.exists():
        raise ValueError("Promptfoo result_path must not reuse a stale artifact")
    if config_path == result_path:
        raise ValueError("Promptfoo config_path and result_path must differ")
    return config_path, result_path


def _validate_artifact_reference(
    session: Session,
    *,
    request: PromptfooEvalRequest,
    artifact: PromptfooArtifactReference,
    purpose: str,
    allowed_content_types: frozenset[str],
) -> StorageObject:
    storage_object = validate_scoped_storage_object_reference(
        session,
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        storage_object_id=artifact.storage_object_id,
        purpose=purpose,
        expected_content_sha256=artifact.content_sha256,
    )
    if storage_object.content_type.lower() not in allowed_content_types:
        raise ApiError(
            "PROMPTFOO_ARTIFACT_CONTENT_TYPE_INVALID",
            f"{purpose} 的对象类型不受支持",
            422,
            details=[
                {
                    "storage_object_id": storage_object.storage_object_id,
                    "content_type": storage_object.content_type,
                    "allowed_content_types": sorted(allowed_content_types),
                }
            ],
        )
    return storage_object


def validate_promptfoo_config_artifact(
    session: Session,
    *,
    request: PromptfooEvalRequest,
) -> StorageObject:
    """Validate the config before a worker fetches it into the execution sandbox."""

    storage_object = _validate_artifact_reference(
        session,
        request=request,
        artifact=request.config_artifact,
        purpose="Promptfoo 评测配置",
        allowed_content_types=_CONFIG_CONTENT_TYPES,
    )
    payload = storage_object.payload if isinstance(storage_object.payload, dict) else {}
    if (
        storage_object.source_type != "promptfoo_eval_config"
        or storage_object.source_id != request.eval_run_id
        or payload.get("binding_sha256") != request.bundle.binding_sha256
    ):
        raise ApiError(
            "PROMPTFOO_CONFIG_ARTIFACT_BINDING_MISMATCH",
            "Promptfoo 配置 Artifact 未绑定当前 EvalRun 锁定 Bundle",
            409,
            details=[
                {
                    "storage_object_id": storage_object.storage_object_id,
                    "expected_source_type": "promptfoo_eval_config",
                    "expected_source_id": request.eval_run_id,
                    "expected_binding_sha256": request.bundle.binding_sha256,
                    "actual_source_type": storage_object.source_type,
                    "actual_source_id": storage_object.source_id,
                    "actual_binding_sha256": payload.get("binding_sha256"),
                }
            ],
        )
    return storage_object


def _suite_manifest_sha256(result: LabelingEvalCompletionResult) -> str:
    manifest = [
        {
            "suite": suite.suite,
            "sample_count": suite.sample_count,
            "sample_manifest_sha256": suite.sample_manifest_sha256,
        }
        for suite in sorted(result.suites, key=lambda item: item.suite)
    ]
    return _canonical_sha256(manifest)


def _parse_result_document(raw: dict[str, Any]) -> PromptfooResultArtifactDocument:
    try:
        return PromptfooResultArtifactDocument.model_validate(raw)
    except ValidationError as exc:
        raise ApiError(
            "PROMPTFOO_RESULT_DOCUMENT_INVALID",
            "Promptfoo 结果 Artifact 不满足内部强 Schema",
            422,
            details=[{"errors": exc.errors(include_url=False)}],
        ) from exc


def _validate_result_bundle(
    request: PromptfooEvalRequest,
    document: PromptfooResultArtifactDocument,
) -> None:
    result = document.labeling_eval_result
    locked = request.bundle.locked_versions
    expected = {
        "eval_run_id": (request.eval_run_id, document.eval_run_id),
        "artifact.binding_sha256": (request.bundle.binding_sha256, document.binding_sha256),
        "result.binding_sha256": (request.bundle.binding_sha256, result.binding_sha256),
        "dataset_manifest_sha256": (
            locked.eval_dataset_manifest_sha256,
            result.dataset_manifest_sha256,
        ),
        "dataset_snapshot_sha256": (
            locked.eval_dataset_snapshot_sha256,
            result.dataset_snapshot_sha256,
        ),
        "sample_manifest_sha256": (
            _suite_manifest_sha256(result),
            result.sample_manifest_sha256,
        ),
    }
    mismatches = [
        {"field": field, "expected": values[0], "actual": values[1]}
        for field, values in expected.items()
        if values[0] != values[1]
    ]
    if mismatches:
        raise ApiError(
            "PROMPTFOO_RESULT_BUNDLE_MISMATCH",
            "Promptfoo 结果与内部 EvalRun 锁定 Bundle 不一致",
            409,
            details=mismatches,
        )


def _validate_eval_run_dispatch_binding(
    session: Session,
    *,
    request: PromptfooEvalRequest,
    completion_adapter: PromptfooCompletionAdapter | None,
) -> tuple[PromptfooCompletionAdapter, str]:
    """Bind the optional executor receipt back to the authoritative dispatch.

    Promptfoo has its own provider run id, but that identifier is only evidence
    about the external evaluator.  RunService accepts a completion only when the
    adapter and external id equal the protocol dispatch already persisted on the
    EvalRun, so those values must never be derived from Promptfoo output.
    """

    record = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == request.eval_run_id,
            RunRecord.tenant_id == request.tenant_id,
            RunRecord.project_id == request.project_id,
        )
    )
    if record is None:
        raise ApiError(
            "PROMPTFOO_EVAL_RUN_NOT_FOUND",
            "Promptfoo 请求绑定的 EvalRun 不存在或不属于当前租户项目",
            404,
            details=[{"eval_run_id": request.eval_run_id}],
        )

    run_payload = record.payload if isinstance(record.payload, dict) else {}
    dispatch = run_payload.get("dispatch")
    dispatch_details = dispatch.get("details") if isinstance(dispatch, dict) else None
    actual_adapter = str(dispatch.get("adapter") or "") if isinstance(dispatch, dict) else ""
    external_id_key = _DISPATCH_EXTERNAL_ID_KEYS.get(actual_adapter)
    actual_external_id = (
        str(dispatch_details.get(external_id_key) or "")
        if isinstance(dispatch_details, dict) and external_id_key
        else ""
    )

    requested_locked = request.bundle.locked_versions.model_dump(mode="json")
    mismatches: list[dict[str, Any]] = []
    expected_values: dict[str, tuple[Any, Any]] = {
        "run_type": ("eval_run", record.run_type),
        "run_status": ("submitted", record.status),
        "capability": ("labeling", run_payload.get("capability")),
        "business_completion_required": (
            True,
            run_payload.get("business_completion_required"),
        ),
        "binding_sha256": (
            request.bundle.binding_sha256,
            run_payload.get("binding_sha256"),
        ),
        "locked_versions": (requested_locked, run_payload.get("locked_versions")),
        "dispatch.adapter": (request.dispatch_adapter, actual_adapter),
        "dispatch.external_id": (request.dispatch_external_id, actual_external_id),
    }
    if completion_adapter is not None:
        expected_values["completion_adapter"] = (actual_adapter, completion_adapter)
    for field, (expected, actual) in expected_values.items():
        if expected != actual:
            mismatches.append({"field": field, "expected": expected, "actual": actual})

    if actual_adapter not in _DISPATCH_EXTERNAL_ID_KEYS or not actual_external_id:
        mismatches.append(
            {
                "field": "dispatch.receipt_binding",
                "expected": "supported adapter with a persisted external id",
                "actual": {
                    "adapter": actual_adapter or None,
                    "external_id": actual_external_id or None,
                },
            }
        )
    if mismatches:
        raise ApiError(
            "PROMPTFOO_EVAL_DISPATCH_BINDING_MISMATCH",
            "Promptfoo 请求与 EvalRun 的锁定 Bundle 或协议分发回执不一致",
            409,
            details=mismatches,
        )
    return cast(PromptfooCompletionAdapter, actual_adapter), actual_external_id


def build_promptfoo_completion_payload(
    session: Session,
    *,
    request: PromptfooEvalRequest,
    result_artifact: PromptfooArtifactReference,
    result_document: dict[str, Any],
    completion_adapter: PromptfooCompletionAdapter | None = None,
) -> dict[str, Any]:
    """Create a standard EvalRun completion body from verified immutable artifacts.

    This does not update EvalRun or gate state. The normal completion endpoint must
    authenticate the receipt, revalidate the locked bundle and materialize the
    internal ``LabelEvalResult``. Promptfoo metadata is explicitly non-authoritative.
    """

    document = _parse_result_document(result_document)
    _validate_result_bundle(request, document)
    dispatch_adapter, dispatch_external_id = _validate_eval_run_dispatch_binding(
        session,
        request=request,
        completion_adapter=completion_adapter,
    )
    if result_artifact.storage_object_id == request.config_artifact.storage_object_id:
        raise ApiError(
            "PROMPTFOO_ARTIFACT_ROLE_COLLISION",
            "Promptfoo 配置与结果必须使用不同 StorageObject",
            409,
        )

    serialized_document = serialize_promptfoo_result_artifact(document)
    document_sha256 = hashlib.sha256(serialized_document).hexdigest()
    if result_artifact.content_sha256 != document_sha256:
        raise ApiError(
            "PROMPTFOO_RESULT_DOCUMENT_HASH_MISMATCH",
            "Promptfoo 结果文档哈希与 Artifact 引用不一致",
            409,
            details=[
                {
                    "expected": document_sha256,
                    "actual": result_artifact.content_sha256,
                }
            ],
        )

    config_object = validate_promptfoo_config_artifact(
        session,
        request=request,
    )
    result_object = _validate_artifact_reference(
        session,
        request=request,
        artifact=result_artifact,
        purpose="Promptfoo 评测结果",
        allowed_content_types=_RESULT_CONTENT_TYPES,
    )
    if (
        result_object.source_type != "promptfoo_eval_result"
        or result_object.source_id != request.eval_run_id
    ):
        raise ApiError(
            "PROMPTFOO_RESULT_ARTIFACT_BINDING_MISMATCH",
            "Promptfoo 结果 Artifact 未绑定当前 EvalRun",
            409,
            details=[
                {
                    "storage_object_id": result_object.storage_object_id,
                    "expected_source_type": "promptfoo_eval_result",
                    "expected_source_id": request.eval_run_id,
                    "actual_source_type": result_object.source_type,
                    "actual_source_id": result_object.source_id,
                }
            ],
        )

    result = document.labeling_eval_result
    payload = RunCompletionReceiptRequest(
        status="success",
        adapter=dispatch_adapter,
        completion_receipt_id=f"promptfoo_{document_sha256[:24]}",
        source=dispatch_adapter,
        external_id=dispatch_external_id,
        result_ref={
            "labeling_eval_result": result.model_dump(mode="json"),
            "provider_evidence": {
                "schema_version": PROMPTFOO_RESULT_SCHEMA_VERSION,
                "provider": "promptfoo",
                "authoritative": False,
                "fact_source": "internal-label-eval-result",
                "provider_run_id": document.provider_run_id,
                "binding_sha256": request.bundle.binding_sha256,
                "config_artifact": {
                    "object_type": "storage_object",
                    "object_id": config_object.storage_object_id,
                    "content_sha256": request.config_artifact.content_sha256,
                },
                "result_artifact": {
                    "object_type": "storage_object",
                    "object_id": result_object.storage_object_id,
                    "content_sha256": document_sha256,
                },
            },
        },
        metrics=result.overall.model_dump(mode="json"),
        note="Promptfoo 仅提供评测 Artifact；内部 EvalResult 重算门禁并持久化事实",
        retryable=False,
    )
    return payload.model_dump(mode="json", exclude_none=True)


__all__ = [
    "PROMPTFOO_EVAL_SUITES",
    "PROMPTFOO_RESULT_SCHEMA_VERSION",
    "PromptfooAdapterConfig",
    "PromptfooArtifactReference",
    "PromptfooCliAdapter",
    "PromptfooCommandPlan",
    "PromptfooCompletionAdapter",
    "PromptfooEvalRequest",
    "PromptfooLockedBundle",
    "PromptfooLockedVersions",
    "PromptfooMaterializedPaths",
    "PromptfooResultArtifactDocument",
    "build_promptfoo_completion_payload",
    "canonical_json_bytes",
    "serialize_promptfoo_result_artifact",
    "validate_promptfoo_config_artifact",
]
