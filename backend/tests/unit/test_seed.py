from __future__ import annotations

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    HumanReviewTask,
    KnowledgeEffect,
    KnowledgeIndex,
    KnowledgeQualityGate,
    KnowledgeSource,
    LabelCandidate,
    LabelVersion,
)
from app.services.resource_service import list_resource_data


def test_seed_contains_core_demo_objects():
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="test",
        roles=("project_admin",),
        request_id="unit",
        trace_id="trace_unit",
    )
    with SessionLocal() as session:
        assets = list_resource_data(session, ctx, "data_assets")
        tasks = list_resource_data(session, ctx, "human_review_tasks")
        assert any(asset["asset_key"] == "auris/label/event_tags" for asset in assets)
        assert any(task["id"] == "hrt_amount_001" for task in tasks)
        assert session.get(LabelVersion, "label_v1_8_4") is not None
        assert session.get(LabelCandidate, "cand_af128_amount_conflict") is not None
        assert session.get(HumanReviewTask, "hrt_amount_001") is not None
        assert session.get(KnowledgeSource, "ks_sales_policy") is not None
        assert session.get(KnowledgeIndex, "ki_sales_policy_v1") is not None
        assert session.get(KnowledgeQualityGate, "kg_recall") is not None
        assert session.get(KnowledgeEffect, "ke_sales_policy_v1") is not None
