from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.models import JsonResource
from app.schemas.requests import HumanReviewDecisionRequest
from app.services.human_review_service import _validate_closed_loop_target_changes


def _target(collection: str) -> JsonResource:
    return JsonResource(
        collection=collection,
        resource_key=f"{collection}_target",
        tenant_id="tenant_a",
        project_id="project_a",
        status="pending",
        trace_id="trace_root",
        data={"id": f"{collection}_target"},
    )


@pytest.mark.parametrize(
    ("collection", "fields", "expected_code"),
    [
        (
            "label_candidates",
            {"platform_connection_id": "forged"},
            "LABEL_CANDIDATE_REVIEW_FIELDS_FORBIDDEN",
        ),
        (
            "event_links",
            {"relation_state": "confirmed"},
            "EVENT_LINK_REVIEW_FIELDS_FORBIDDEN",
        ),
        (
            "conversation_boundaries",
            {"audio_session_id": "forged"},
            "CONVERSATION_BOUNDARY_REVIEW_FIELDS_FORBIDDEN",
        ),
        (
            "conversation_boundaries",
            {"start_ms": 9000, "end_ms": 1000},
            "CONVERSATION_BOUNDARY_REVIEW_WINDOW_INVALID",
        ),
    ],
)
def test_audio_review_changes_reject_unexpected_or_invalid_fields(
    collection: str,
    fields: dict[str, object],
    expected_code: str,
) -> None:
    with pytest.raises(ApiError) as error:
        _validate_closed_loop_target_changes(  # type: ignore[arg-type]
            None,
            None,
            target=_target(collection),
            fields=fields,
        )

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("collection", "fields"),
    [
        (
            "label_candidates",
            {"value": "人工修订标签", "confidence": 0.98},
        ),
        (
            "event_links",
            {
                "source_event_id": "evt_quote_001",
                "document_ref": "SO-2026-001",
                "relation_type": "quote",
                "confidence": 0.91,
                "evidence_window": "12:27:18 - 12:28:30",
            },
        ),
        (
            "conversation_boundaries",
            {
                "start_ms": 30_000,
                "end_ms": 694_000,
                "decision": "manual_confirmed",
                "merged_slice_ids": ["W1", "W2"],
                "split_slice_ids": [],
                "extension_ids": ["prev_1"],
            },
        ),
    ],
)
def test_audio_review_changes_accept_only_the_structured_frontend_contract(
    collection: str,
    fields: dict[str, object],
) -> None:
    _validate_closed_loop_target_changes(  # type: ignore[arg-type]
        None,
        None,
        target=_target(collection),
        fields=fields,
    )


def test_human_review_request_allows_only_the_boundary_business_decision() -> None:
    request = HumanReviewDecisionRequest.model_validate(
        {
            "decision": "modified",
            "changes": [
                {
                    "target_type": "conversation_boundary",
                    "target_id": "boundary_audio_001",
                    "fields": {
                        "start_ms": 0,
                        "end_ms": 1_000,
                        "decision": "manual_confirmed",
                        "merged_slice_ids": [],
                        "split_slice_ids": [],
                        "extension_ids": [],
                    },
                }
            ],
        }
    )

    assert request.changes[0].fields["decision"] == "manual_confirmed"

    with pytest.raises(ValidationError, match="server-managed fields cannot be modified"):
        HumanReviewDecisionRequest.model_validate(
            {
                "decision": "modified",
                "changes": [
                    {
                        "target_type": "evidence_pack",
                        "target_id": "evidence_audio_001",
                        "fields": {"decision": "accepted"},
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="manual_confirmed"):
        HumanReviewDecisionRequest.model_validate(
            {
                "decision": "modified",
                "changes": [
                    {
                        "target_type": "conversation_boundary",
                        "target_id": "boundary_audio_001",
                        "fields": {
                            "start_ms": 0,
                            "end_ms": 1_000,
                            "decision": "accepted",
                        },
                    }
                ],
            }
        )
