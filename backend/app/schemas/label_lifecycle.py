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


class StrictLabelLifecycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LabelVersionDeprecationPreflightRequest(StrictLabelLifecycleModel):
    expected_resource_version: StrictInt = Field(ge=1)
    replacement_label_version_id: StrictStr | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    mapping_bundle_id: StrictStr | None = Field(
        default=None,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    reason: StrictStr = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized

    @model_validator(mode="after")
    def replacement_requires_a_mapping_bundle(
        self,
    ) -> LabelVersionDeprecationPreflightRequest:
        has_replacement = self.replacement_label_version_id is not None
        has_mapping = self.mapping_bundle_id is not None
        if has_replacement != has_mapping:
            raise ValueError(
                "replacement_label_version_id and mapping_bundle_id must be provided together"
            )
        return self


class LabelVersionTransitionRequest(LabelVersionDeprecationPreflightRequest):
    action: Literal["deprecate", "archive"]

    @model_validator(mode="after")
    def archive_does_not_rebind_replacement(self) -> LabelVersionTransitionRequest:
        if self.action == "archive" and (
            self.replacement_label_version_id is not None or self.mapping_bundle_id is not None
        ):
            raise ValueError("archive preserves the existing replacement and mapping binding")
        return self


class LabelVersionEnvironmentReference(StrictLabelLifecycleModel):
    deployment_id: StrictStr
    environment: StrictStr
    head_generation: StrictInt | None
    reference_status: Literal["active", "draining"]


class LabelVersionLifecycleBlocker(StrictLabelLifecycleModel):
    code: StrictStr
    reference_type: Literal["active-head", "draining-deployment", "in-flight-run"]
    deployment_id: StrictStr | None = None
    run_id: StrictStr | None = None
    environment: StrictStr


class LabelVersionInFlightRunReference(StrictLabelLifecycleModel):
    run_id: StrictStr
    run_status: StrictStr
    environment: StrictStr
    head_generation: StrictInt | None
    active_deployment_id: StrictStr | None
    active_bundle_sha256: StrictStr | None


class LabelVersionDeprecationPreflightResponse(StrictLabelLifecycleModel):
    preflight_id: StrictStr
    label_version_id: StrictStr
    expected_resource_version: StrictInt
    replacement_label_version_id: StrictStr | None
    mapping_bundle_id: StrictStr | None
    active_environment_references: list[LabelVersionEnvironmentReference]
    draining_environment_references: list[LabelVersionEnvironmentReference]
    in_flight_run_references: list[LabelVersionInFlightRunReference]
    blockers: list[LabelVersionLifecycleBlocker]
    ready_for_transition: bool
    safe_stop_required: bool
    audit_id: StrictInt
    outbox_event_id: StrictInt
    trace_id: StrictStr


class LabelVersionTransitionResponse(StrictLabelLifecycleModel):
    label_version_id: StrictStr
    action: Literal["deprecate", "archive"]
    status: Literal["deprecated", "archived"]
    artifact_status: Literal["deprecated", "archived"]
    resource_version: StrictInt
    replacement_label_version_id: StrictStr | None
    mapping_bundle_id: StrictStr | None
    normalized_disposition: Literal["mapped-replacement", "coverage-gap"]
    safe_stop_required: bool
    audit_id: StrictInt
    outbox_event_id: StrictInt
    trace_id: StrictStr
