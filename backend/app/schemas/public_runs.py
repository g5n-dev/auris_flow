from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, JsonValue


class _ClosedPublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicRunResponseMeta(_ClosedPublicModel):
    trace_id: str
    request_id: str


PublicRunDataT = TypeVar("PublicRunDataT")


class PublicRunEnvelope(_ClosedPublicModel, Generic[PublicRunDataT]):
    data: PublicRunDataT
    meta: PublicRunResponseMeta


class PublicRunCollectionMeta(PublicRunResponseMeta):
    total: int
    limit: int
    next_cursor: str | None


class PublicRunCollectionData(_ClosedPublicModel, Generic[PublicRunDataT]):
    items: list[PublicRunDataT]


class PublicRunCollectionEnvelope(_ClosedPublicModel, Generic[PublicRunDataT]):
    data: PublicRunCollectionData[PublicRunDataT]
    meta: PublicRunCollectionMeta


class PublicRunNextAction(_ClosedPublicModel):
    key: str
    label: str
    code: str | None = None
    type: str | None = None
    href: str | None = None
    route: str | None = None
    available_at: str | None = None


PublicFilterScalar = str | int | float | bool
PublicFilterValue = PublicFilterScalar | list[PublicFilterScalar]


class ExportScope(_ClosedPublicModel):
    target: str | None = None
    object_id: str | None = None
    module_key: str | None = None
    active_tab: str | None = None
    filter: dict[str, PublicFilterValue] | None = None


class ExportDownloadRef(_ClosedPublicModel):
    kind: Literal["bff_download"]
    status: Literal["reserved", "ready", "unavailable"]
    href: str | None
    content_type: str
    expires_at: str | None


class ExportJob(_ClosedPublicModel):
    id: str
    run_id: str
    export_job_id: str
    run_type: Literal["export"]
    status: str
    format: str
    target: str | None = None
    object_id: str | None = None
    scene_profile_id: str | None = None
    scene_profile_version_id: str | None = None
    scene_profile_snapshot_sha256: str | None = None
    scope: ExportScope
    download_ref: ExportDownloadRef | None
    trace_id: str
    next_actions: list[PublicRunNextAction]


class PublicExtractionSourceBinding(_ClosedPublicModel):
    source_family: str
    source_type: str | None = None
    correlation_group_id: str | None = None


class PublicExtractionSubjectReference(_ClosedPublicModel):
    subject_key: str | None = None
    id: str | None = None
    evidence_ref: str | None = None
    data_range: str | None = None


class PublicReleaseHeadLock(_ClosedPublicModel):
    environment: str
    generation: int
    active_deployment_id: str
    active_bundle_sha256: str


class LabelExtractionRunPublic(_ClosedPublicModel):
    id: str | None = None
    run_id: str | None = None
    extraction_run_id: str
    tenant_id: str
    project_id: str
    label_version_id: str
    prompt_version_id: str
    model_version: str
    schema_version: str
    aggregation_policy_version_id: str | None = None
    source_bindings: list[PublicExtractionSourceBinding]
    manifest_sha256: str | None = None
    aggregation_run_id: str | None = None
    aggregate_ids: list[str]
    status: str
    subject_scope: str
    subject_refs: list[PublicExtractionSubjectReference]
    input_sha256: str
    observation_count: int
    release_head_lock: PublicReleaseHeadLock | None = None
    next_actions: list[PublicRunNextAction]
    trace_id: str
    created_at: str


class PublicPromptVersionCandidate(_ClosedPublicModel):
    id: str
    candidate_id: str
    prompt_version_id: str | None = None
    prompt_asset_id: str | None = None
    parent_version_id: str | None = None
    label_version_id: str | None = None
    model_version: str | None = None
    schema_version: str | None = None
    status: str
    template: dict[str, JsonValue] | None = None
    output_schema: dict[str, JsonValue] | None = None
    generation_params: dict[str, JsonValue] | None = None
    structured_diff: dict[str, JsonValue] | None = None
    source_badcase_refs: list[JsonValue] | None = None
    content_sha256: str | None = None
    source_run_id: str | None = None
    source_run_type: str | None = None
    agent_run_id: str | None = None
    eval_run_id: str | None = None
    feedback_task_id: str | None = None
    base_prompt_version: str | None = None
    target: str | None = None
    badcase_refs: list[JsonValue] | None = None
    result_ref: dict[str, JsonValue] | None = None
    metrics: dict[str, JsonValue] | None = None
    change_set_id: str | None = None
    review_task_id: str | None = None
    review_gate: dict[str, JsonValue] | None = None
    write_policy: dict[str, JsonValue] | None = None
    affected_objects: list[JsonValue] | None = None
    summary: str | None = None
    review_status: str | None = None
    review_submission_ids: list[str] | None = None
    received_reviews: int | None = None
    review_decision_id: str | None = None
    review_resolution_source: str | None = None
    adjudication_id: str | None = None
    requested_field_diff: dict[str, JsonValue] | None = None
    reviewed_at: str | None = None
    source_trace_id: str | None = None
    action_trace_id: str | None = None
    revision_of_candidate_id: str | None = None
    source_review_decision_id: str | None = None
    child_prompt_version_id: str | None = None
    child_review_task_id: str | None = None
    trace_id: str
