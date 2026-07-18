from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

SCENE_KEY_PATTERN = r"^[a-z][a-z0-9_.-]{1,95}$"
RESOURCE_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"


class StrictSceneModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SceneRoleDefinition(StrictSceneModel):
    role_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    display_name: StrictStr = Field(min_length=1, max_length=128)
    description: StrictStr = Field(min_length=1, max_length=1000)


class SceneObjectDefinition(StrictSceneModel):
    object_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    display_name: StrictStr = Field(min_length=1, max_length=128)
    schema_ref: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        pattern=RESOURCE_REF_PATTERN,
    )
    required: bool = False


class SceneDimensionDefinition(StrictSceneModel):
    dimension_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    display_name: StrictStr = Field(min_length=1, max_length=128)
    value_type: Literal["string", "date", "datetime", "number", "boolean", "id"] = "string"
    scope_level: Literal["tenant", "project", "organization", "location", "object", "run"]
    required: bool = False


class SceneActionBinding(StrictSceneModel):
    action_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    display_name: StrictStr = Field(min_length=1, max_length=128)
    capability: StrictStr = Field(min_length=1, max_length=128)
    task_type_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    data_contract_refs: list[StrictStr] = Field(default_factory=list, max_length=32)
    connector_refs: list[StrictStr] = Field(default_factory=list, max_length=32)
    model_service_refs: list[StrictStr] = Field(default_factory=list, max_length=32)
    label_version_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    prompt_version_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    knowledge_index_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    eval_dataset_version_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    hotword_pack_version_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    rubric_ref: StrictStr | None = Field(
        default=None, min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN
    )
    gold_set_key: StrictStr | None = Field(
        default=None, min_length=1, max_length=128, pattern=RESOURCE_REF_PATTERN
    )
    output_sink_refs: list[StrictStr] = Field(default_factory=list, max_length=32)
    human_review_required: bool = False

    @field_validator(
        "data_contract_refs", "connector_refs", "model_service_refs", "output_sink_refs"
    )
    @classmethod
    def action_reference_lists_are_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("scene action references must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("scene action references must be unique")
        return normalized


class SceneMetricDefinition(StrictSceneModel):
    metric_key: StrictStr = Field(
        min_length=2,
        max_length=96,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,95}$",
    )
    display_name: StrictStr = Field(min_length=1, max_length=128)
    unit: Literal[
        "count",
        "ratio",
        "percent",
        "score",
        "duration_ms",
        "currency_minor",
    ]
    calculator_ref: StrictStr = Field(
        min_length=1,
        max_length=256,
        pattern=RESOURCE_REF_PATTERN,
    )
    metric_family: StrictStr = Field(default="general", min_length=1, max_length=96)
    label_version_applicability: Literal["none", "required"] = "none"
    evidence_refs: list[StrictStr] = Field(default_factory=list, max_length=32)
    formula: StrictStr | None = Field(default=None, min_length=1, max_length=1000)
    owner: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    human_review_required: bool = False

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("metric evidence_refs must be unique")
        return value


class SceneEvalRequirement(StrictSceneModel):
    requirement_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    gate_kind: Literal[
        "core_capability",
        "scene_eval",
        "project_holdout",
        "human_agreement",
        "privacy",
        "security",
    ]
    metric_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    operator: Literal["gte", "lte", "eq"]
    threshold_ppm: StrictInt = Field(ge=0, le=1_000_000)


class SceneGovernancePolicy(StrictSceneModel):
    human_review_required: bool = True
    model_may_publish: Literal[False] = False
    retention_policy_ref: StrictStr = Field(
        min_length=1,
        max_length=256,
        pattern=RESOURCE_REF_PATTERN,
    )
    privacy_policy_ref: StrictStr = Field(
        min_length=1,
        max_length=256,
        pattern=RESOURCE_REF_PATTERN,
    )


class SceneProfileManifest(StrictSceneModel):
    schema_version: Literal["scene-profile/1"] = "scene-profile/1"
    scene_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    display_name: StrictStr = Field(min_length=1, max_length=255)
    description: StrictStr = Field(min_length=1, max_length=2000)
    locales: list[StrictStr] = Field(min_length=1, max_length=16)
    capabilities: list[StrictStr] = Field(min_length=1, max_length=32)
    roles: list[SceneRoleDefinition] = Field(min_length=1, max_length=64)
    entities: list[SceneObjectDefinition] = Field(default_factory=list, max_length=128)
    events: list[SceneObjectDefinition] = Field(default_factory=list, max_length=128)
    document_types: list[SceneObjectDefinition] = Field(default_factory=list, max_length=128)
    data_contract_refs: list[StrictStr] = Field(min_length=1, max_length=64)
    task_type_refs: list[StrictStr] = Field(min_length=1, max_length=64)
    label_version_refs: list[StrictStr] = Field(min_length=1, max_length=64)
    prompt_version_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    knowledge_index_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    eval_dataset_version_refs: list[StrictStr] = Field(min_length=1, max_length=64)
    connector_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    model_service_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    hotword_pack_version_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    rubric_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    output_sink_refs: list[StrictStr] = Field(default_factory=list, max_length=64)
    dimensions: list[SceneDimensionDefinition] = Field(default_factory=list, max_length=64)
    action_bindings: list[SceneActionBinding] = Field(default_factory=list, max_length=64)
    metrics: list[SceneMetricDefinition] = Field(min_length=1, max_length=128)
    release_requirements: list[SceneEvalRequirement] = Field(min_length=1, max_length=64)
    governance: SceneGovernancePolicy

    @field_validator(
        "locales",
        "capabilities",
        "data_contract_refs",
        "task_type_refs",
        "label_version_refs",
        "prompt_version_refs",
        "knowledge_index_refs",
        "eval_dataset_version_refs",
        "connector_refs",
        "model_service_refs",
        "hotword_pack_version_refs",
        "rubric_refs",
        "output_sink_refs",
    )
    @classmethod
    def string_lists_are_unique_and_non_blank(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("scene manifest references must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("scene manifest references must be unique")
        return normalized

    @model_validator(mode="after")
    def definition_keys_are_unique_and_metrics_are_bound(self) -> SceneProfileManifest:
        for field_name in (
            "roles",
            "entities",
            "events",
            "document_types",
            "dimensions",
            "action_bindings",
            "metrics",
        ):
            values = getattr(self, field_name)
            key_name = (
                "role_key"
                if field_name == "roles"
                else "metric_key"
                if field_name == "metrics"
                else "dimension_key"
                if field_name == "dimensions"
                else "action_key"
                if field_name == "action_bindings"
                else "object_key"
            )
            keys = [getattr(item, key_name) for item in values]
            if len(keys) != len(set(keys)):
                raise ValueError(f"{field_name} keys must be unique")
        metric_keys = {metric.metric_key for metric in self.metrics}
        unknown_metrics = {
            requirement.metric_key
            for requirement in self.release_requirements
            if requirement.metric_key not in metric_keys
        }
        if unknown_metrics:
            raise ValueError(
                "release requirements reference unknown metrics: "
                + ", ".join(sorted(unknown_metrics))
            )
        membership_fields = {
            "task_type_ref": set(self.task_type_refs),
            "label_version_ref": set(self.label_version_refs),
            "prompt_version_ref": set(self.prompt_version_refs),
            "knowledge_index_ref": set(self.knowledge_index_refs),
            "eval_dataset_version_ref": set(self.eval_dataset_version_refs),
            "hotword_pack_version_ref": set(self.hotword_pack_version_refs),
            "rubric_ref": set(self.rubric_refs),
        }
        list_membership_fields = {
            "data_contract_refs": set(self.data_contract_refs),
            "connector_refs": set(self.connector_refs),
            "model_service_refs": set(self.model_service_refs),
            "output_sink_refs": set(self.output_sink_refs),
        }
        membership_errors: list[str] = []
        for action in self.action_bindings:
            for field_name, allowed in membership_fields.items():
                ref = getattr(action, field_name)
                if ref is not None and ref not in allowed:
                    membership_errors.append(f"{action.action_key}.{field_name}={ref}")
            for field_name, allowed in list_membership_fields.items():
                unknown = sorted(set(getattr(action, field_name)) - allowed)
                membership_errors.extend(
                    f"{action.action_key}.{field_name}={ref}" for ref in unknown
                )
        if membership_errors:
            raise ValueError(
                "action bindings reference resources outside the scene manifest: "
                + ", ".join(membership_errors)
            )
        gate_kinds = {requirement.gate_kind for requirement in self.release_requirements}
        required_gate_kinds = {"core_capability", "scene_eval", "project_holdout"}
        if not required_gate_kinds.issubset(gate_kinds):
            missing = ", ".join(sorted(required_gate_kinds - gate_kinds))
            raise ValueError(f"release requirements missing mandatory gate kinds: {missing}")
        return self


class SceneProfileCreateRequest(StrictSceneModel):
    scene_profile_id: StrictStr | None = Field(default=None, min_length=3, max_length=128)
    scene_profile_version_id: StrictStr | None = Field(default=None, min_length=3, max_length=128)
    scene_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=255)
    description: StrictStr = Field(min_length=1, max_length=2000)
    version: StrictStr = Field(min_length=1, max_length=64)
    source_type: Literal["human", "import"] = "human"
    parent_version_id: StrictStr | None = Field(default=None, min_length=3, max_length=128)
    manifest: SceneProfileManifest

    @model_validator(mode="after")
    def manifest_identity_matches_request(self) -> SceneProfileCreateRequest:
        if self.manifest.scene_key != self.scene_key:
            raise ValueError("manifest.scene_key must match scene_key")
        return self


class SceneProfileGenerationRequest(StrictSceneModel):
    scene_profile_id: StrictStr | None = Field(default=None, min_length=3, max_length=128)
    scene_key: StrictStr = Field(min_length=2, max_length=96, pattern=SCENE_KEY_PATTERN)
    name: StrictStr = Field(min_length=1, max_length=255)
    description: StrictStr = Field(min_length=1, max_length=2000)
    version: StrictStr = Field(min_length=1, max_length=64)
    objective: StrictStr = Field(min_length=10, max_length=4000)
    model_ref: StrictStr = Field(min_length=1, max_length=256, pattern=RESOURCE_REF_PATTERN)
    input_refs: list[StrictStr] = Field(min_length=1, max_length=64)
    parent_version_id: StrictStr | None = Field(default=None, min_length=3, max_length=128)

    @field_validator("input_refs")
    @classmethod
    def input_refs_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("input_refs must be unique")
        return value


class SceneProfilePatchRequest(StrictSceneModel):
    expected_resource_version: StrictInt = Field(ge=1)
    manifest: SceneProfileManifest


class SceneProfileReviewRequest(StrictSceneModel):
    decision: Literal["approved", "rejected"]
    reason: StrictStr = Field(min_length=3, max_length=1000)


class SceneProfilePublishRequest(StrictSceneModel):
    reason: StrictStr = Field(min_length=3, max_length=1000)


class ProjectSceneProfileBindingRequest(StrictSceneModel):
    scene_profile_version_id: StrictStr = Field(min_length=3, max_length=128)
    environment: Literal["development", "staging", "production"] = "production"
    expected_resource_version: StrictInt | None = Field(default=None, ge=1)
