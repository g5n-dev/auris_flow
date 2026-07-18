"""harden blind calibration privacy, metrics and gold versioning

Revision ID: 0019_calibration_hardening
Revises: 0018_blind_calibration_gold_loop
Create Date: 2026-07-12
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0019_calibration_hardening"
down_revision = "0018_blind_calibration_gold_loop"
branch_labels = None
depends_on = None


CALIBRATION_CATEGORIES = frozenset({"pass", "fail"})
PASS_ALIASES = frozenset(
    {
        "pass",
        "passed",
        "true",
        "yes",
        "accept",
        "accepted",
        "approved",
        "qualified",
        "通过",
        "符合",
        "合格",
    }
)
FAIL_ALIASES = frozenset(
    {
        "fail",
        "failed",
        "false",
        "no",
        "reject",
        "rejected",
        "unqualified",
        "不通过",
        "不符合",
        "不合格",
    }
)
REASON_CODES = frozenset(
    {
        "evidence_consistent",
        "evidence_conflict",
        "insufficient_evidence",
        "policy_exception",
        "other",
    }
)


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


APPEND_ONLY_TABLES = (
    "calibration_submissions",
    "calibration_adjudications",
    "gold_set_versions",
    "gold_annotations",
)


def _decode_json(value: Any) -> Any:
    if isinstance(value, (dict, list, bool, int, float)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _normalize_decision(decision: Any) -> str | None:
    if isinstance(decision, bool):
        return "pass" if decision else "fail"
    if isinstance(decision, int) and decision in {0, 1}:
        return "pass" if decision == 1 else "fail"
    if not isinstance(decision, str):
        return None
    normalized = decision.strip().lower()
    if normalized in PASS_ALIASES:
        return "pass"
    if normalized in FAIL_ALIASES:
        return "fail"
    return None


def _normalize_submission_value(value: Any, submission_id: str) -> dict[str, Any]:
    decoded = _decode_json(value)
    if isinstance(decoded, dict):
        raw_decision = decoded.get("decision")
        if raw_decision is None:
            raw_decision = decoded.get("label", decoded.get("value"))
        reason_code = decoded.get("reason_code")
        evidence_refs = decoded.get("evidence_refs", [])
    else:
        raw_decision = decoded
        reason_code = None
        evidence_refs = []

    decision = _normalize_decision(raw_decision)
    if decision not in CALIBRATION_CATEGORIES:
        raise RuntimeError(
            "0019 calibration preflight rejected an unsupported legacy submission "
            f"{submission_id}: expected a pass/fail-compatible value"
        )
    if reason_code not in REASON_CODES:
        reason_code = "other" if reason_code else None
    if isinstance(evidence_refs, str):
        evidence_refs = [evidence_refs]
    if not isinstance(evidence_refs, list) or any(
        not isinstance(ref, str) or not ref.strip() for ref in evidence_refs
    ):
        evidence_refs = []
    normalized_refs = list(dict.fromkeys(ref.strip() for ref in evidence_refs))[:16]
    return {
        "decision": decision,
        "reason_code": reason_code,
        "evidence_refs": normalized_refs,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _kappa_defined(pairs: list[tuple[str, str]]) -> bool:
    if not pairs:
        return False
    count_a = Counter(left for left, _ in pairs)
    count_b = Counter(right for _, right in pairs)
    pair_count = len(pairs)
    expected_match_numerator = sum(
        left_count * count_b.get(category, 0) for category, left_count in count_a.items()
    )
    return pair_count * pair_count - expected_match_numerator != 0


def _collect_legacy_backfill(bind: sa.engine.Connection) -> dict[str, Any]:
    """Read and validate all 0018 data before the first DDL statement.

    MySQL DDL is not fully transactional. Failing here leaves the database at
    0018 instead of after a partially applied 0019 schema.
    """

    submission_rows = (
        bind.execute(
            sa.text(
                "SELECT s.submission_id, s.round_id, s.item_id, s.value_json, a.slot "
                "FROM calibration_submissions s JOIN calibration_assignments a ON "
                "a.tenant_id = s.tenant_id AND a.project_id = s.project_id AND "
                "a.assignment_id = s.assignment_id AND a.round_id = s.round_id AND "
                "a.item_id = s.item_id AND a.reviewer_id = s.reviewer_id"
            )
        )
        .mappings()
        .all()
    )
    submissions: list[dict[str, Any]] = []
    pair_values: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for row in submission_rows:
        original = _decode_json(row["value_json"])
        normalized = _normalize_submission_value(original, str(row["submission_id"]))
        submissions.append(
            {
                "submission_id": row["submission_id"],
                "normalized_json": _canonical_json(normalized),
                "normalized_sha256": _canonical_sha256(normalized),
                "legacy_json": (None if original == normalized else _canonical_json(original)),
            }
        )
        pair_values[str(row["round_id"])][str(row["item_id"])][str(row["slot"])] = str(
            normalized["decision"]
        )

    round_rows = (
        bind.execute(
            sa.text(
                "SELECT round_id, paired_submission_count, cohen_kappa_micros "
                "FROM calibration_rounds"
            )
        )
        .mappings()
        .all()
    )
    round_flags: dict[str, bool] = {}
    for row in round_rows:
        round_id = str(row["round_id"])
        paired_count = int(row["paired_submission_count"])
        kappa_micros = int(row["cohen_kappa_micros"])
        if kappa_micros != 0:
            round_flags[round_id] = True
            continue
        if paired_count == 0:
            round_flags[round_id] = False
            continue
        pairs = [
            (slots["A"], slots["B"])
            for slots in pair_values.get(round_id, {}).values()
            if set(slots) == {"A", "B"}
        ]
        if len(pairs) != paired_count:
            raise RuntimeError(
                "0019 calibration preflight cannot determine zero-kappa semantics for "
                f"{round_id}: expected {paired_count} paired submissions, found {len(pairs)}"
            )
        round_flags[round_id] = _kappa_defined(pairs)

    gold_rows = (
        bind.execute(
            sa.text(
                "SELECT gold_set_version_id, round_id, annotation_count, cohen_kappa_micros "
                "FROM gold_set_versions"
            )
        )
        .mappings()
        .all()
    )
    gold_flags: dict[str, dict[str, Any]] = {}
    for row in gold_rows:
        round_id = str(row["round_id"])
        gold_flags[str(row["gold_set_version_id"])] = {
            "cohen_kappa_defined": (
                True if int(row["cohen_kappa_micros"]) != 0 else round_flags.get(round_id, False)
            ),
            "legacy_empty_compatible": int(row["annotation_count"]) == 0,
        }

    return {
        "submissions": submissions,
        "round_flags": round_flags,
        "gold_flags": gold_flags,
    }


def _apply_legacy_backfill(bind: sa.engine.Connection, backfill: dict[str, Any]) -> None:
    for row in backfill["submissions"]:
        bind.execute(
            sa.text(
                "UPDATE calibration_submissions SET value_json = :normalized_json, "
                "canonical_value_sha256 = :normalized_sha256, "
                "legacy_value_json = :legacy_json, value_schema_version = 1 "
                "WHERE submission_id = :submission_id"
            ),
            row,
        )
    for round_id, is_defined in backfill["round_flags"].items():
        bind.execute(
            sa.text(
                "UPDATE calibration_rounds SET cohen_kappa_defined = :is_defined "
                "WHERE round_id = :round_id"
            ),
            {"round_id": round_id, "is_defined": 1 if is_defined else 0},
        )
    for gold_set_version_id, values in backfill["gold_flags"].items():
        bind.execute(
            sa.text(
                "UPDATE gold_set_versions SET cohen_kappa_defined = :is_defined, "
                "legacy_empty_compatible = :legacy_empty "
                "WHERE gold_set_version_id = :gold_set_version_id"
            ),
            {
                "gold_set_version_id": gold_set_version_id,
                "is_defined": 1 if values["cohen_kappa_defined"] else 0,
                "legacy_empty": 1 if values["legacy_empty_compatible"] else 0,
            },
        )


def _assert_backfill_complete(bind: sa.engine.Connection) -> None:
    probes = {
        "calibration_rounds.cohen_kappa_defined": (
            "SELECT COUNT(*) FROM calibration_rounds WHERE cohen_kappa_defined IS NULL"
        ),
        "gold_set_versions compatibility flags": (
            "SELECT COUNT(*) FROM gold_set_versions WHERE cohen_kappa_defined IS NULL "
            "OR legacy_empty_compatible IS NULL"
        ),
        "calibration_submissions.value_schema_version": (
            "SELECT COUNT(*) FROM calibration_submissions WHERE value_schema_version IS NULL"
        ),
    }
    failures = [
        label for label, query in probes.items() if int(bind.scalar(sa.text(query)) or 0) != 0
    ]
    if failures:
        raise RuntimeError("0019 calibration backfill incomplete: " + ", ".join(failures))


def _create_append_only_triggers() -> None:
    dialect = op.get_bind().dialect.name
    for table_name in APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_no_{action.lower()}"
            if dialect == "sqlite":
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table_name} "
                        "BEGIN SELECT RAISE(ABORT, 'append-only calibration record'); END"
                    )
                )
            elif dialect == "mysql":
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table_name} "
                        "FOR EACH ROW SIGNAL SQLSTATE '45000' "
                        "SET MESSAGE_TEXT = 'append-only calibration record'"
                    )
                )


def _drop_append_only_triggers() -> None:
    for table_name in APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_{action.lower()}"))


def _replace_gold_metrics_constraint(*, hardened: bool) -> None:
    annotation_condition = (
        "((annotation_count > 0 AND legacy_empty_compatible = 0) OR "
        "(annotation_count = 0 AND legacy_empty_compatible = 1))"
        if hardened
        else "annotation_count >= 0"
    )
    condition = (
        f"version_number > 0 AND sample_count > 0 AND {annotation_condition} AND "
        "excluded_count >= 0 AND annotation_count + excluded_count = sample_count AND "
        "conflict_count >= 0 AND conflict_count <= sample_count AND "
        "adjudication_count = conflict_count AND excluded_count <= adjudication_count AND "
        "observed_agreement_ppm BETWEEN 0 AND 1000000 AND "
        "cohen_kappa_micros BETWEEN -1000000 AND 1000000"
    )
    with op.batch_alter_table("gold_set_versions", recreate="auto") as batch:
        batch.drop_constraint("ck_gold_versions_metrics", type_="check")
        batch.create_check_constraint("ck_gold_versions_metrics", condition)


def upgrade() -> None:
    bind = op.get_bind()
    backfill = _collect_legacy_backfill(bind)

    # Expand with nullable columns. Historical rows are populated before any
    # NOT NULL or semantic check constraint is introduced.
    op.add_column(
        "calibration_rounds",
        sa.Column("cohen_kappa_defined", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "gold_set_versions",
        sa.Column("cohen_kappa_defined", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "gold_set_versions",
        sa.Column("legacy_empty_compatible", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "calibration_submissions",
        sa.Column("legacy_value_json", json_type(), nullable=True),
    )
    op.add_column(
        "calibration_submissions",
        sa.Column("value_schema_version", sa.Integer(), nullable=True),
    )

    _apply_legacy_backfill(bind, backfill)
    _assert_backfill_complete(bind)

    with op.batch_alter_table("calibration_rounds", recreate="auto") as batch:
        batch.alter_column(
            "cohen_kappa_defined",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
        batch.create_check_constraint(
            "ck_cal_rounds_kappa_defined",
            "cohen_kappa_defined OR cohen_kappa_micros = 0",
        )
    with op.batch_alter_table("calibration_submissions", recreate="auto") as batch:
        batch.alter_column(
            "value_schema_version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )
        batch.create_check_constraint(
            "ck_cal_subs_value_schema",
            "value_schema_version = 1",
        )
    with op.batch_alter_table("gold_set_versions", recreate="auto") as batch:
        batch.alter_column(
            "cohen_kappa_defined",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
        batch.alter_column(
            "legacy_empty_compatible",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
    _replace_gold_metrics_constraint(hardened=True)
    with op.batch_alter_table("gold_set_versions", recreate="auto") as batch:
        batch.create_check_constraint(
            "ck_gold_versions_kappa_defined",
            "cohen_kappa_defined OR cohen_kappa_micros = 0",
        )

    op.create_table(
        "gold_set_series",
        sa.Column("gold_set_series_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("gold_set_key", sa.String(128), nullable=False),
        sa.Column("next_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_key",
            name="uq_gold_series_scope_key",
        ),
        sa.CheckConstraint("next_version > 0", name="ck_gold_series_next_version"),
    )
    op.create_index(
        "ix_gold_series_scope_key",
        "gold_set_series",
        ["tenant_id", "project_id", "gold_set_key"],
    )

    rows = bind.execute(
        sa.text(
            "SELECT tenant_id, project_id, gold_set_key, "
            "MAX(version_number) AS max_version, MAX(trace_id) AS trace_id "
            "FROM gold_set_versions GROUP BY tenant_id, project_id, gold_set_key"
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO gold_set_series "
                "(gold_set_series_id, tenant_id, project_id, gold_set_key, next_version, "
                "resource_version, trace_id) VALUES "
                "(:series_id, :tenant_id, :project_id, :gold_set_key, "
                ":next_version, 1, :trace_id)"
            ),
            {
                "series_id": f"gseries_{uuid.uuid4().hex[:20]}",
                "tenant_id": row["tenant_id"],
                "project_id": row["project_id"],
                "gold_set_key": row["gold_set_key"],
                "next_version": int(row["max_version"]) + 1,
                "trace_id": row["trace_id"] or "migration-0019",
            },
        )

    _create_append_only_triggers()


def downgrade() -> None:
    _drop_append_only_triggers()
    op.drop_index("ix_gold_series_scope_key", table_name="gold_set_series")
    op.drop_table("gold_set_series")

    with op.batch_alter_table("gold_set_versions", recreate="auto") as batch:
        batch.drop_constraint("ck_gold_versions_kappa_defined", type_="check")
    _replace_gold_metrics_constraint(hardened=False)
    with op.batch_alter_table("calibration_rounds", recreate="auto") as batch:
        batch.drop_constraint("ck_cal_rounds_kappa_defined", type_="check")
    with op.batch_alter_table("calibration_submissions", recreate="auto") as batch:
        batch.drop_constraint("ck_cal_subs_value_schema", type_="check")

    op.drop_column("calibration_submissions", "value_schema_version")
    op.drop_column("calibration_submissions", "legacy_value_json")
    op.drop_column("gold_set_versions", "legacy_empty_compatible")
    op.drop_column("gold_set_versions", "cohen_kappa_defined")
    op.drop_column("calibration_rounds", "cohen_kappa_defined")
