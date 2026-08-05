from __future__ import annotations

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import JsonResource

AUDIO_SESSION_ID = "S20250526-000128"


def _relation_row(
    *,
    collection: str,
    resource_key: str,
    audio_session_id: str,
) -> JsonResource:
    return JsonResource(
        collection=collection,
        resource_key=resource_key,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        status="ready",
        trace_id=f"trace-{resource_key}",
        data={
            "id": resource_key,
            "audio_session_id": audio_session_id,
            "status": "ready",
            "trace_id": f"trace-{resource_key}",
        },
    )


def test_audio_session_detail_queries_all_relations_by_audio_session_id(
    client,
    auth_headers,
) -> None:
    relation_fields = {
        "conversation_boundaries": "boundaries",
        "asr_segments": "asr_segments",
        "vad_segments": "vad_segments",
        "speaker_turns": "speaker_turns",
        "voiceprint_samples": "voiceprint_samples",
        "audio_quality_reports": "audio_quality_reports",
        "event_links": "event_links",
        "listening_annotations": "listening_annotations",
    }
    expected_ids: dict[str, str] = {}
    with SessionLocal() as session:
        for collection in relation_fields:
            # listening_annotations previously used a larger but still unsafe
            # global limit. Put the authoritative row beyond both legacy caps.
            unrelated_count = 205 if collection == "listening_annotations" else 55
            session.add_all(
                [
                    _relation_row(
                        collection=collection,
                        resource_key=f"unrelated-{collection}-{index:03d}",
                        audio_session_id=f"unrelated-session-{index:03d}",
                    )
                    for index in range(unrelated_count)
                ]
            )
            expected_id = f"late-{collection}-authoritative"
            expected_ids[collection] = expected_id
            session.add(
                _relation_row(
                    collection=collection,
                    resource_key=expected_id,
                    audio_session_id=AUDIO_SESSION_ID,
                )
            )
        session.commit()

    response = client.get(
        f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    detail = response.json()["data"]
    for collection, response_field in relation_fields.items():
        assert any(item.get("id") == expected_ids[collection] for item in detail[response_field]), (
            collection
        )
        assert all(
            item["audio_session_id"] == AUDIO_SESSION_ID for item in detail[response_field]
        ), collection

    annotations_response = client.get(
        f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}/annotations",
        headers=auth_headers,
    )
    assert annotations_response.status_code == 200
    annotations = annotations_response.json()["data"]["items"]
    assert any(item.get("id") == expected_ids["listening_annotations"] for item in annotations)
    assert all(item["audio_session_id"] == AUDIO_SESSION_ID for item in annotations)


def test_audio_session_detail_binds_evidence_by_strong_audio_session_id(
    client,
    auth_headers,
) -> None:
    with SessionLocal() as session:
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "evidence_packs",
                JsonResource.resource_key == "AF-128",
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert projection is not None
        projection.data = {
            **projection.data,
            "audio_session_id": "forged-projection-session",
        }
        session.commit()

    response = client.get(
        f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    evidence_packs = response.json()["data"]["evidence_packs"]
    evidence = next(item for item in evidence_packs if item["evidence_pack_id"] == "AF-128")
    assert evidence["audio_session_id"] == AUDIO_SESSION_ID
    assert evidence["schema_version"] == "audio-evidence-pack/1"
