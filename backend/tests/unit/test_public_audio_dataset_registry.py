from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.evaluation.public_dataset_registry import (
    PublicAudioDatasetRegistry,
    load_public_audio_dataset_registry,
)
from app.schemas.evaluation import EvalDatasetVersionCreateRequest

ROOT = Path(__file__).resolve().parents[3]
REGISTRY_PATH = ROOT / "doc/backend-spec/public-audio-datasets-v0.1.json"


def test_committed_public_audio_registry_is_metadata_only_and_fail_closed() -> None:
    registry = load_public_audio_dataset_registry(REGISTRY_PATH)

    assert registry.schema_version == "auris.public-audio-datasets.v1"
    ali_meeting = next(
        dataset for dataset in registry.datasets if dataset.dataset_id == "alimeeting-slr119"
    )
    assert ali_meeting.data_license.spdx == "CC-BY-SA-4.0"
    assert ali_meeting.repository_distribution == "metadata-only"
    assert ali_meeting.code_license is not None
    assert ali_meeting.code_license.spdx == "Apache-2.0"
    assert ali_meeting.data_license.spdx != ali_meeting.code_license.spdx
    assert ali_meeting.splits[0].integrity_status == "pending-owner-lock"
    assert ali_meeting.splits[0].download_enabled is False
    assert ali_meeting.splits[0].ci_enabled is False


def test_registry_rejects_unpinned_or_leaking_eval_split() -> None:
    document = load_public_audio_dataset_registry(REGISTRY_PATH).model_dump(mode="json")
    split = document["datasets"][0]["splits"][0]
    split.update(
        {
            "integrity_status": "verified",
            "download_enabled": True,
            "use_for_model_fitting": True,
        }
    )

    with pytest.raises(ValidationError):
        PublicAudioDatasetRegistry.model_validate(document)


def test_public_eval_dataset_request_requires_license_and_manifest_binding() -> None:
    manifest_sha256 = "a" * 64
    base = {
        "eval_dataset_id": "evalset-alimeeting-eval-v1",
        "name": "AliMeeting Eval",
        "capability": "speaker_diarization",
        "dataset_version": "SLR119-2022-release-eval",
        "manifest_storage_object_id": "storage-alimeeting-eval-manifest",
        "manifest_sha256": manifest_sha256,
        "sample_count": 8,
        "source": "public_dataset",
    }
    with pytest.raises(ValidationError):
        EvalDatasetVersionCreateRequest.model_validate(base)

    provenance = {
        "source_type": "public_dataset",
        "registry_dataset_id": "alimeeting-slr119",
        "registry_schema_version": "auris.public-audio-datasets.v1",
        "upstream_id": "SLR119",
        "upstream_revision": "SLR119-2022-release",
        "upstream_split": "eval",
        "upstream_url": (
            "https://speech-lab-share-data.oss-cn-shanghai.aliyuncs.com/"
            "AliMeeting/openlr/Eval_Ali.tar.gz"
        ),
        "license_spdx": "CC-BY-SA-4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "license_accepted": True,
        "repository_distribution": "metadata-only",
        "evaluation_only": True,
        "archive_sha256": "b" * 64,
        "prepared_manifest_sha256": "c" * 64,
        "citation": "Yu et al., M2MeT, ICASSP 2022.",
    }
    with pytest.raises(ValidationError):
        EvalDatasetVersionCreateRequest.model_validate({**base, "provenance": provenance})

    request = EvalDatasetVersionCreateRequest.model_validate(
        {
            **base,
            "provenance": {
                **provenance,
                "prepared_manifest_sha256": manifest_sha256,
            },
        }
    )
    assert request.provenance is not None
    assert request.provenance.license_accepted is True
    assert request.provenance.prepared_manifest_sha256 == request.manifest_sha256
