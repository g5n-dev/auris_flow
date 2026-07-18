"""make temporal LabelFact rows strictly append-only

Revision ID: 0039_label_fact_append_only_contract
Revises: 0038_release_head_interval_closure
Create Date: 2026-07-18

The Expand schema kept ``status/active_slot`` as a mutable compatibility
projection.  The Contract schema makes ``LabelFactHead`` the only current-row
projection, preserves every pre-existing Fact byte-for-byte, requires new rows
to use ``recorded/NULL``, removes the single-active index, and installs database
guards against future mutation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_label_fact_append_only_contract"
down_revision = "0038_release_head_interval_closure"
branch_labels = None
depends_on = None

ACTIVE_INDEX = "uq_label_facts_active_head"
OLD_CHECK = "ck_label_facts_active_slot"
CONTRACT_CHECK = "ck_label_facts_append_only_projection"


def _assert_backfill_complete() -> None:
    bind = op.get_bind()
    incomplete = bind.execute(
        sa.text(
            "SELECT f.fact_id FROM label_facts f "
            "LEFT JOIN label_fact_heads h ON "
            "h.tenant_id = f.tenant_id AND h.project_id = f.project_id "
            "AND h.fact_namespace = f.fact_namespace "
            "AND h.logical_key_sha = f.logical_key_sha "
            "WHERE f.source_kind IS NULL OR f.fact_namespace IS NULL "
            "OR f.logical_key_sha IS NULL OR f.revision IS NULL "
            "OR f.recorded_at IS NULL OR f.content_sha256 IS NULL "
            "OR h.fact_head_id IS NULL LIMIT 1"
        )
    ).first()
    if incomplete is not None:
        raise RuntimeError(
            "LabelFact Contract requires temporal backfill and one LabelFactHead "
            f"per logical chain before upgrade; blocked by fact_id={incomplete[0]}"
        )

    invalid_chain = bind.execute(
        sa.text(
            "SELECT chains.tenant_id, chains.project_id, chains.fact_namespace, "
            "chains.logical_key_sha FROM ("
            "SELECT tenant_id, project_id, fact_namespace, logical_key_sha, "
            "MIN(revision) AS min_revision, MAX(revision) AS max_revision, "
            "COUNT(*) AS revision_count FROM label_facts "
            "GROUP BY tenant_id, project_id, fact_namespace, logical_key_sha"
            ") chains LEFT JOIN label_fact_heads h ON "
            "h.tenant_id = chains.tenant_id AND h.project_id = chains.project_id "
            "AND h.fact_namespace = chains.fact_namespace "
            "AND h.logical_key_sha = chains.logical_key_sha "
            "LEFT JOIN label_facts current_fact ON "
            "current_fact.tenant_id = h.tenant_id "
            "AND current_fact.project_id = h.project_id "
            "AND current_fact.fact_namespace = h.fact_namespace "
            "AND current_fact.logical_key_sha = h.logical_key_sha "
            "AND current_fact.fact_id = h.current_fact_id "
            "WHERE h.fact_head_id IS NULL OR chains.min_revision <> 1 "
            "OR chains.max_revision <> chains.revision_count "
            "OR h.current_revision <> chains.max_revision "
            "OR current_fact.fact_id IS NULL "
            "OR current_fact.revision <> chains.max_revision LIMIT 1"
        )
    ).first()
    if invalid_chain is not None:
        raise RuntimeError(
            "LabelFact Contract requires a contiguous revision chain whose Head points "
            "to the latest revision before upgrade; blocked by scope/key="
            f"{invalid_chain[0]}/{invalid_chain[1]}/{invalid_chain[2]}/{invalid_chain[3]}"
        )

    orphan_head = bind.execute(
        sa.text(
            "SELECT h.fact_head_id FROM label_fact_heads h "
            "LEFT JOIN label_facts current_fact ON "
            "current_fact.tenant_id = h.tenant_id "
            "AND current_fact.project_id = h.project_id "
            "AND current_fact.fact_namespace = h.fact_namespace "
            "AND current_fact.logical_key_sha = h.logical_key_sha "
            "AND current_fact.fact_id = h.current_fact_id "
            "WHERE current_fact.fact_id IS NULL "
            "OR current_fact.revision <> h.current_revision LIMIT 1"
        )
    ).first()
    if orphan_head is not None:
        raise RuntimeError(
            "LabelFact Contract requires every Head ID/revision to resolve to a Fact "
            "in the same scope and logical chain before upgrade; blocked by fact_head_id="
            f"{orphan_head[0]}"
        )

    invalid_link = bind.execute(
        sa.text(
            "SELECT f.fact_id FROM label_facts f LEFT JOIN label_facts previous ON "
            "previous.tenant_id = f.tenant_id AND previous.project_id = f.project_id "
            "AND previous.fact_namespace = f.fact_namespace "
            "AND previous.logical_key_sha = f.logical_key_sha "
            "AND previous.fact_id = f.supersedes_fact_id "
            "WHERE (f.revision = 1 AND f.supersedes_fact_id IS NOT NULL) "
            "OR (f.revision > 1 AND (previous.fact_id IS NULL "
            "OR previous.revision <> f.revision - 1)) LIMIT 1"
        )
    ).first()
    if invalid_link is not None:
        raise RuntimeError(
            "LabelFact Contract requires each revision to supersede the immediately "
            f"preceding Fact in the same logical chain; blocked by fact_id={invalid_link[0]}"
        )


def _drop_contract_triggers() -> None:
    for name in (
        "trg_label_facts_contract_insert",
        "trg_label_facts_no_update",
        "trg_label_facts_no_delete",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))


def _create_contract_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_facts_contract_insert "
                "BEFORE INSERT ON label_facts "
                "WHEN NEW.source_kind IS NULL OR NEW.status <> 'recorded' "
                "OR NEW.active_slot IS NOT NULL "
                "BEGIN SELECT RAISE(ABORT, "
                "'label_facts contract requires recorded rows'); END"
            )
        )
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_facts_no_{action.lower()} "
                    f"BEFORE {action} ON label_facts "
                    "BEGIN SELECT RAISE(ABORT, 'append-only label_facts'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_facts_contract_insert "
                "BEFORE INSERT ON label_facts FOR EACH ROW "
                "BEGIN IF NEW.source_kind IS NULL OR NEW.status <> 'recorded' "
                "OR NEW.active_slot IS NOT NULL THEN SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'label_facts contract requires recorded rows'; END IF; END"
            )
        )
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_facts_no_{action.lower()} "
                    f"BEFORE {action} ON label_facts FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'append-only label_facts'"
                )
            )


def upgrade() -> None:
    _assert_backfill_complete()
    op.drop_index(ACTIVE_INDEX, table_name="label_facts")
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.drop_constraint(OLD_CHECK, type_="check")
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.create_check_constraint(
            CONTRACT_CHECK,
            "(status = 'active' AND active_slot = 'active') OR "
            "(status = 'superseded' AND active_slot IS NULL) OR "
            "(status = 'recorded' AND active_slot IS NULL)",
        )
    _create_contract_triggers()


def downgrade() -> None:
    recorded = (
        op.get_bind()
        .execute(sa.text("SELECT fact_id FROM label_facts WHERE status = 'recorded' LIMIT 1"))
        .first()
    )
    if recorded is not None:
        raise RuntimeError(
            "LabelFact Contract downgrade would rewrite append-only recorded history; "
            f"blocked by fact_id={recorded[0]}. Use a forward compensation migration."
        )
    _drop_contract_triggers()
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.drop_constraint(CONTRACT_CHECK, type_="check")
    with op.batch_alter_table("label_facts", recreate="auto") as batch_op:
        batch_op.create_check_constraint(
            OLD_CHECK,
            "(status = 'active' AND active_slot = 'active') OR "
            "(status = 'superseded' AND active_slot IS NULL)",
        )
    op.create_index(
        ACTIVE_INDEX,
        "label_facts",
        [
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            "active_slot",
        ],
        unique=True,
    )
