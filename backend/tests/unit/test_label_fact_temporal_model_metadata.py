from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError

from app.models import (
    Base,
    LabelFact,
    LabelFactHead,
    LabelFactSet,
    LabelFactSetHead,
    LabelFactSetHeadEvent,
)

TEMPORAL_FACT_COLUMNS = {
    "fact_namespace",
    "logical_key_sha",
    "revision",
    "event_or_segment_id",
    "assertion_slot",
    "occurred_at",
    "recorded_at",
    "occurred_at_origin",
    "source_kind",
    "human_review_decision_id",
    "recompute_run_item_id",
    "fact_set_id",
    "content_sha256",
    "root_trace_id",
    "action_trace_id",
}


def _constraint_names(model: Any, kind: type[Any]) -> set[str]:
    return {
        str(constraint.name)
        for constraint in model.__table__.constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def _index_names(model: Any) -> set[str]:
    return {str(index.name) for index in model.__table__.indexes if index.name is not None}


def test_label_fact_model_mirrors_nullable_0035_expand_columns() -> None:
    columns = LabelFact.__table__.columns

    assert TEMPORAL_FACT_COLUMNS <= set(columns.keys())
    assert all(columns[column_name].nullable for column_name in TEMPORAL_FACT_COLUMNS)
    assert columns.aggregate_id.nullable
    assert "active_slot" in columns

    assert {
        "uq_label_facts_temporal_revision",
        "uq_label_facts_temporal_head_binding",
    } <= _constraint_names(LabelFact, UniqueConstraint)
    assert {
        "fk_label_facts_scope_human_decision",
        "fk_label_facts_scope_recompute_item",
        "fk_label_facts_scope_fact_set",
    } <= _constraint_names(LabelFact, ForeignKeyConstraint)
    assert {
        "ck_label_facts_temporal_revision",
        "ck_label_facts_temporal_hashes",
        "ck_label_facts_occurred_origin",
        "ck_label_facts_expand_source",
        "ck_label_facts_temporal_completeness",
        "ck_label_facts_append_only_projection",
    } <= _constraint_names(LabelFact, CheckConstraint)
    assert {
        "ix_label_facts_temporal_as_of",
        "ix_label_facts_temporal_occurred",
        "ix_label_facts_temporal_source",
        "ix_label_facts_scope_fact_set",
    } <= _index_names(LabelFact)
    assert "uq_label_facts_active_head" not in _index_names(LabelFact)


def test_temporal_head_models_cover_strong_0035_tables() -> None:
    expected_models = (
        LabelFactHead,
        LabelFactSet,
        LabelFactSetHead,
        LabelFactSetHeadEvent,
    )
    expected_tables = {
        "label_fact_heads",
        "label_fact_sets",
        "label_fact_set_heads",
        "label_fact_set_head_events",
    }

    assert {model.__tablename__ for model in expected_models} == expected_tables
    assert expected_tables <= set(Base.metadata.tables)

    assert {
        "uq_label_fact_sets_scope_id",
        "uq_label_fact_sets_scope_hash",
        "uq_label_fact_sets_scope_binding",
    } <= _constraint_names(LabelFactSet, UniqueConstraint)
    assert "fk_label_fact_sets_scope_target" in _constraint_names(
        LabelFactSet,
        ForeignKeyConstraint,
    )
    assert {
        "ix_label_fact_sets_scope_status",
        "ix_label_fact_sets_scope_target",
        "ix_label_fact_sets_trace_id",
    } <= _index_names(LabelFactSet)

    assert "uq_label_fact_heads_scope_key" in _constraint_names(
        LabelFactHead,
        UniqueConstraint,
    )
    assert "fk_label_fact_heads_scope_current" in _constraint_names(
        LabelFactHead,
        ForeignKeyConstraint,
    )
    assert "ix_label_fact_heads_scope_current" in _index_names(LabelFactHead)

    assert "uq_label_fact_set_heads_scope_env" in _constraint_names(
        LabelFactSetHead,
        UniqueConstraint,
    )
    assert {
        "fk_label_fact_set_heads_scope_current",
        "fk_label_fact_set_heads_scope_previous",
    } <= _constraint_names(LabelFactSetHead, ForeignKeyConstraint)
    assert "ix_label_fact_set_heads_scope_current" in _index_names(LabelFactSetHead)

    assert {
        "uq_label_fact_set_events_generation",
        "uq_label_fact_set_events_hash",
    } <= _constraint_names(LabelFactSetHeadEvent, UniqueConstraint)
    assert {
        "fk_label_fact_set_events_scope_old",
        "fk_label_fact_set_events_scope_new",
    } <= _constraint_names(LabelFactSetHeadEvent, ForeignKeyConstraint)
    assert {
        "ix_label_fact_set_events_scope_timeline",
        "ix_label_fact_set_events_scope_new_set",
        "ix_label_fact_set_events_trace_id",
    } <= _index_names(LabelFactSetHeadEvent)
    assert "updated_at" not in LabelFactSetHeadEvent.__table__.columns


def test_create_all_installs_fact_set_head_event_append_only_guards() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        trigger_names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'label_fact_set_head_events'"
                )
            )
        }
        connection.execute(
            text(
                "INSERT INTO label_fact_set_head_events "
                "(head_event_id, tenant_id, project_id, environment, fact_namespace, "
                "generation, previous_generation, action, old_fact_set_id, "
                "old_manifest_sha256, new_fact_set_id, new_manifest_sha256, approval_id, "
                "effective_at, content_sha256, actor_id, root_trace_id, action_trace_id, "
                "trace_id, payload) VALUES "
                "('event-1', 'tenant-a', 'project-a', 'production', 'production', "
                "1, NULL, 'bootstrap', NULL, NULL, 'set-1', :manifest_sha, NULL, "
                "CURRENT_TIMESTAMP, :content_sha, 'actor-1', 'root-1', 'action-1', "
                "'trace-1', '{}')"
            ),
            {"manifest_sha": "a" * 64, "content_sha": "b" * 64},
        )

    assert trigger_names == {
        "trg_label_fact_set_head_events_no_update",
        "trg_label_fact_set_head_events_no_delete",
    }

    with pytest.raises(IntegrityError, match="append-only label_fact_set_head_events"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE label_fact_set_head_events SET actor_id = 'actor-2' "
                    "WHERE head_event_id = 'event-1'"
                )
            )

    with pytest.raises(IntegrityError, match="append-only label_fact_set_head_events"):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM label_fact_set_head_events WHERE head_event_id = 'event-1'")
            )

    assert inspect(engine).has_table("label_fact_set_head_events")


def test_create_all_installs_label_fact_append_only_contract_guards() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        trigger_names = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'label_facts'"
                )
            )
        }
        connection.execute(
            text(
                "INSERT INTO label_facts "
                "(fact_id, tenant_id, project_id, aggregate_id, supersedes_fact_id, "
                "fact_namespace, logical_key_sha, revision, event_or_segment_id, "
                "assertion_slot, occurred_at, recorded_at, occurred_at_origin, source_kind, "
                "human_review_decision_id, recompute_run_item_id, fact_set_id, content_sha256, "
                "root_trace_id, action_trace_id, label_version_id, subject_scope, subject_key, "
                "label_id, value_type, value_json, authority, status, active_slot, "
                "review_decision_id, trace_id, payload) VALUES "
                "('fact-append-only', 'tenant-a', 'project-a', 'aggregate-a', NULL, "
                "'native:version-a', :logical_sha, 1, 'event-a', 'presence', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'source', 'aggregate', NULL, NULL, "
                "NULL, :content_sha, 'root-a', 'action-a', 'version-a', 'business-event', "
                "'subject-a', 'label-a', 'boolean', 'true', 'l2-auto-accepted', "
                "'recorded', NULL, NULL, 'root-a', '{}')"
            ),
            {"content_sha": "b" * 64, "logical_sha": "a" * 64},
        )

    assert trigger_names == {
        "trg_label_facts_contract_insert",
        "trg_label_facts_no_delete",
        "trg_label_facts_no_update",
    }

    with pytest.raises(IntegrityError, match="append-only label_facts"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE label_facts SET value_json = 'false' WHERE fact_id = 'fact-append-only'"
                )
            )

    with pytest.raises(IntegrityError, match="append-only label_facts"):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM label_facts WHERE fact_id = 'fact-append-only'"))

    with pytest.raises(IntegrityError, match="label_facts contract requires recorded rows"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO label_facts "
                    "(fact_id, tenant_id, project_id, aggregate_id, label_version_id, "
                    "subject_scope, subject_key, label_id, value_type, value_json, authority, "
                    "status, active_slot, trace_id, payload) VALUES "
                    "('legacy-after-contract', 'tenant-a', 'project-a', 'aggregate-b', "
                    "'version-a', 'business-event', 'subject-b', 'label-a', 'boolean', "
                    "'true', 'l2-auto-accepted', 'active', 'active', 'root-b', '{}')"
                )
            )
