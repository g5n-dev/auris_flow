from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DatasetLicense(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    spdx: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9.+-]+$")
    url: str = Field(min_length=8, max_length=2048, pattern=r"^https://[^\s]+$")


class PublicAudioDatasetSplit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    split_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    role: Literal["train", "development", "evaluation", "test"]
    upstream_revision: str = Field(min_length=1, max_length=128)
    download_url: str = Field(
        min_length=8,
        max_length=2048,
        pattern=r"^https://[^\s]+$",
    )
    archive_format: Literal["tar.gz", "zip"]
    size_display: str = Field(min_length=1, max_length=32)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    integrity_status: Literal["verified", "pending-owner-lock"]
    download_enabled: bool
    ci_enabled: bool
    use_for_model_fitting: bool

    @field_validator("upstream_revision", "size_display")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def integrity_and_usage_are_fail_closed(self) -> PublicAudioDatasetSplit:
        if self.integrity_status == "verified" and self.sha256 is None:
            raise ValueError("verified split requires an upstream archive SHA-256")
        if self.integrity_status == "pending-owner-lock":
            if self.sha256 is not None:
                raise ValueError("pending split must not claim an unapproved SHA-256")
            if self.download_enabled or self.ci_enabled:
                raise ValueError("pending split must remain disabled")
        if self.download_enabled and self.sha256 is None:
            raise ValueError("download requires a pinned archive SHA-256")
        if self.ci_enabled and not self.download_enabled:
            raise ValueError("CI use requires an enabled, verified download")
        if self.role in {"evaluation", "test"} and self.use_for_model_fitting:
            raise ValueError("evaluation and test splits cannot be used for model fitting")
        return self


class PublicAudioDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    dataset_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]+$",
    )
    upstream_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    homepage: str = Field(min_length=8, max_length=2048, pattern=r"^https://[^\s]+$")
    source_organization: str = Field(min_length=1, max_length=255)
    data_license: DatasetLicense
    code_license: DatasetLicense | None = None
    repository_distribution: Literal["metadata-only"]
    contains_real_people: bool
    license_acceptance_required: Literal[True]
    domain: Literal["general_meeting", "domain_specific"]
    intended_capabilities: list[Literal["asr_cer", "speaker_diarization_der", "vad_boundary"]] = (
        Field(min_length=1, max_length=16)
    )
    citation: str = Field(min_length=1, max_length=4096)
    forbidden_claims: list[str] = Field(min_length=1, max_length=32)
    splits: list[PublicAudioDatasetSplit] = Field(min_length=1, max_length=32)

    @field_validator("upstream_id", "title", "source_organization", "citation")
    @classmethod
    def dataset_text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("forbidden_claims")
    @classmethod
    def claims_must_be_unique_and_non_blank(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("forbidden claims must not be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("forbidden claims must be unique")
        return normalized

    @model_validator(mode="after")
    def dataset_entries_are_consistent(self) -> PublicAudioDataset:
        split_ids = [split.split_id for split in self.splits]
        if len(split_ids) != len(set(split_ids)):
            raise ValueError("split_id must be unique within a dataset")
        if len(self.intended_capabilities) != len(set(self.intended_capabilities)):
            raise ValueError("intended capabilities must be unique")
        if self.contains_real_people and self.repository_distribution != "metadata-only":
            raise ValueError("recordings of real people must not be distributed in this repository")
        return self


class PublicAudioDatasetRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["auris.public-audio-datasets.v1"]
    updated_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    datasets: list[PublicAudioDataset] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def dataset_ids_are_unique(self) -> PublicAudioDatasetRegistry:
        dataset_ids = [dataset.dataset_id for dataset in self.datasets]
        if len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError("dataset_id must be unique")
        return self


def load_public_audio_dataset_registry(path: Path) -> PublicAudioDatasetRegistry:
    document = json.loads(path.read_text(encoding="utf-8"))
    return PublicAudioDatasetRegistry.model_validate(document)
