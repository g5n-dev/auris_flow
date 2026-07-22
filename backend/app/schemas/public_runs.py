from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue


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
PublicRunStatus = Literal[
    "queued",
    "pending",
    "running",
    "submitted",
    "completion_pending",
    "cancelling",
    "success",
    "failed",
    "blocked",
    "cancelled",
]


class RunCompletionSummary(_ClosedPublicModel):
    """Engine-neutral, recursively sanitized completion evidence."""

    completion_receipt_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,127}$",
    )
    status: PublicRunStatus
    result_ref: dict[str, JsonValue] = Field(default_factory=dict)
    metrics: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Z][A-Z0-9_]{2,127}$",
    )
    retryable: bool | None = None
    received_at: (
        Annotated[
            str,
            Field(json_schema_extra={"format": "date-time"}),
        ]
        | None
    ) = None


class PublicRunDetail(BaseModel):
    """Runtime schema for the recursively sanitized public Run projection.

    Run types add domain-specific fields, so compatibility requires allowing those
    fields here. The service projection remains the authority that removes internal
    execution and storage evidence before response-model serialization.
    """

    model_config = ConfigDict(extra="allow")

    run_id: str
    id: str | None = None
    run_type: str
    status: PublicRunStatus
    tenant_id: str
    project_id: str
    trace_id: str
    business_status: str | None = None
    business_completion_required: bool | None = None
    completion_receipt: RunCompletionSummary | None = None


class RunCompletionReceiptPendingData(_ClosedPublicModel):
    run_id: str
    status: PublicRunStatus
    completion_receipt_id: str
    receipt_state: Literal["pending_binding", "pending_cancellation_resolution"]
    trace_id: str


class RunCompletionReceiptPendingResponse(PublicRunEnvelope[RunCompletionReceiptPendingData]):
    pass


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
