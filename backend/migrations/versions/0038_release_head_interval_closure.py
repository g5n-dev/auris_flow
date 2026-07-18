"""close release head activation intervals without weakening ledger immutability

Revision ID: 0038_release_head_interval_closure
Revises: 0037_label_fact_logical_active_heads
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_release_head_interval_closure"
down_revision = "0037_label_fact_logical_active_heads"
branch_labels = None
depends_on = None


_IMMUTABLE_COLUMNS = (
    "head_event_id",
    "tenant_id",
    "project_id",
    "environment",
    "generation",
    "previous_generation",
    "action",
    "activation_status",
    "old_deployment_id",
    "new_deployment_id",
    "old_label_version_id",
    "new_label_version_id",
    "old_bundle_sha256",
    "new_bundle_sha256",
    "effective_from",
    "command_id",
    "completion_receipt_id",
    "approval_id",
    "content_sha256",
    "actor_id",
    "root_trace_id",
    "trace_id",
    "payload",
    "created_at",
)


def _drop_interval_triggers() -> None:
    for name in (
        "trg_release_bundle_head_events_no_update",
        "trg_release_bundle_head_events_interval_update",
        "trg_release_bundle_head_events_interval_insert",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))


def _create_interval_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        immutable_equal = " AND ".join(
            f"OLD.{column} IS NEW.{column}" for column in _IMMUTABLE_COLUMNS
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_bundle_head_events_interval_update "
                "BEFORE UPDATE ON release_bundle_head_events "
                "WHEN NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL "
                "AND NEW.effective_to >= OLD.effective_from AND "
                f"{immutable_equal}) "
                "BEGIN SELECT RAISE(ABORT, "
                "'release head interval permits one effective_to closure only'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_bundle_head_events_interval_insert "
                "BEFORE INSERT ON release_bundle_head_events "
                "WHEN NEW.effective_to IS NOT NULL OR "
                "(NEW.generation = 1 AND EXISTS ("
                "SELECT 1 FROM release_bundle_head_events prior "
                "WHERE prior.tenant_id = NEW.tenant_id "
                "AND prior.project_id = NEW.project_id "
                "AND prior.environment = NEW.environment)) OR "
                "(NEW.generation > 1 AND NOT EXISTS ("
                "SELECT 1 FROM release_bundle_head_events prior "
                "WHERE prior.tenant_id = NEW.tenant_id "
                "AND prior.project_id = NEW.project_id "
                "AND prior.environment = NEW.environment "
                "AND prior.generation = NEW.previous_generation "
                "AND prior.effective_to = NEW.effective_from "
                "AND prior.new_deployment_id IS NEW.old_deployment_id "
                "AND prior.new_label_version_id IS NEW.old_label_version_id "
                "AND prior.new_bundle_sha256 IS NEW.old_bundle_sha256)) "
                "BEGIN SELECT RAISE(ABORT, "
                "'release head activation intervals must be continuous'); END"
            )
        )
    elif dialect in {"mysql", "mariadb"}:
        immutable_equal = " AND ".join(
            f"OLD.{column} <=> NEW.{column}" for column in _IMMUTABLE_COLUMNS
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_bundle_head_events_interval_update "
                "BEFORE UPDATE ON release_bundle_head_events FOR EACH ROW "
                "BEGIN IF NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL "
                "AND NEW.effective_to >= OLD.effective_from AND "
                f"{immutable_equal}) THEN SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = "
                "'release head interval permits one effective_to closure only'; "
                "END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_bundle_head_events_interval_insert "
                "BEFORE INSERT ON release_bundle_head_events FOR EACH ROW "
                "BEGIN IF NEW.effective_to IS NOT NULL OR "
                "(NEW.generation = 1 AND EXISTS ("
                "SELECT 1 FROM release_bundle_head_events prior "
                "WHERE prior.tenant_id = NEW.tenant_id "
                "AND prior.project_id = NEW.project_id "
                "AND prior.environment = NEW.environment)) OR "
                "(NEW.generation > 1 AND NOT EXISTS ("
                "SELECT 1 FROM release_bundle_head_events prior "
                "WHERE prior.tenant_id = NEW.tenant_id "
                "AND prior.project_id = NEW.project_id "
                "AND prior.environment = NEW.environment "
                "AND prior.generation = NEW.previous_generation "
                "AND prior.effective_to = NEW.effective_from "
                "AND prior.new_deployment_id <=> NEW.old_deployment_id "
                "AND prior.new_label_version_id <=> NEW.old_label_version_id "
                "AND prior.new_bundle_sha256 <=> NEW.old_bundle_sha256)) "
                "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
                "'release head activation intervals must be continuous'; "
                "END IF; END"
            )
        )


def _create_legacy_append_only_update_trigger() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_bundle_head_events_no_update "
                "BEFORE UPDATE ON release_bundle_head_events "
                "BEGIN SELECT RAISE(ABORT, "
                "'append-only release_bundle_head_events'); END"
            )
        )
    elif dialect in {"mysql", "mariadb"}:
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_bundle_head_events_no_update "
                "BEFORE UPDATE ON release_bundle_head_events FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'append-only release_bundle_head_events'"
            )
        )


def upgrade() -> None:
    _drop_interval_triggers()
    _create_interval_triggers()


def downgrade() -> None:
    _drop_interval_triggers()
    _create_legacy_append_only_update_trigger()
