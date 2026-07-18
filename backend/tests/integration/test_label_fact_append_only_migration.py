from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0038_release_head_interval_closure"
REVISION_CONTRACT = "0039_label_fact_append_only_contract"
REVISION_RECOMPUTE = "0040_label_recomputation_fact_sets"


def _alembic(
    database_url: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=check,
        capture_output=True,
        text=True,
    )


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _insert_fact(
    connection: Connection,
    *,
    fact_id: str,
    revision: int,
    status: str,
    active_slot: str | None,
    supersedes_fact_id: str | None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO label_facts "
            "(fact_id, tenant_id, project_id, aggregate_id, supersedes_fact_id, "
            "fact_namespace, logical_key_sha, revision, event_or_segment_id, assertion_slot, "
            "occurred_at, recorded_at, occurred_at_origin, source_kind, "
            "human_review_decision_id, recompute_run_item_id, fact_set_id, content_sha256, "
            "root_trace_id, action_trace_id, label_version_id, subject_scope, subject_key, "
            "label_id, value_type, value_json, authority, status, active_slot, "
            "review_decision_id, trace_id, payload) VALUES "
            "(:fact_id, 'tenant-contract', 'project-contract', :aggregate_id, "
            ":supersedes_fact_id, 'native:version-contract', :logical_sha, :revision, "
            "'event-contract', 'presence', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'source', "
            "'aggregate', NULL, NULL, NULL, :content_sha, :root_trace, :action_trace, "
            "'version-contract', 'business-event', 'subject-contract', 'label-contract', "
            "'boolean', :value_json, 'l2-auto-accepted', :status, :active_slot, NULL, "
            ":root_trace, :payload)"
        ),
        {
            "action_trace": f"action-{fact_id}",
            "active_slot": active_slot,
            "aggregate_id": f"aggregate-{fact_id}",
            "content_sha": ("a" if revision == 1 else "b") * 64,
            "fact_id": fact_id,
            "logical_sha": "c" * 64,
            "payload": json.dumps({"fixture": fact_id}),
            "revision": revision,
            "root_trace": f"root-{fact_id}",
            "status": status,
            "supersedes_fact_id": supersedes_fact_id,
            "value_json": json.dumps(revision == 1),
        },
    )


def _insert_head(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO label_fact_heads "
            "(fact_head_id, tenant_id, project_id, fact_namespace, logical_key_sha, "
            "current_fact_id, current_revision, generation, root_trace_id, action_trace_id, "
            "trace_id, payload) VALUES "
            "('head-contract', 'tenant-contract', 'project-contract', "
            "'native:version-contract', :logical_sha, 'fact-contract-2', 2, 2, "
            "'root-fact-contract-2', 'action-fact-contract-2', "
            "'root-fact-contract-2', :payload)"
        ),
        {
            "logical_sha": "c" * 64,
            "payload": json.dumps(
                {
                    "current_content_sha256": "b" * 64,
                    "previous_fact_id": "fact-contract-1",
                }
            ),
        },
    )


def _insert_human_fact(
    connection: Connection,
    *,
    fact_id: str,
    aggregate_id: str | None,
    logical_sha: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO label_facts "
            "(fact_id, tenant_id, project_id, aggregate_id, supersedes_fact_id, "
            "fact_namespace, logical_key_sha, revision, event_or_segment_id, assertion_slot, "
            "occurred_at, recorded_at, occurred_at_origin, source_kind, "
            "human_review_decision_id, recompute_run_item_id, fact_set_id, content_sha256, "
            "root_trace_id, action_trace_id, label_version_id, subject_scope, subject_key, "
            "label_id, value_type, value_json, authority, status, active_slot, "
            "review_decision_id, trace_id, payload) VALUES "
            "(:fact_id, 'tenant-contract', 'project-contract', :aggregate_id, NULL, "
            "'native:version-contract', :logical_sha, 1, :event_id, 'presence', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'source', 'human-decision', "
            ":decision_id, NULL, NULL, :content_sha, :root_trace, :action_trace, "
            "'version-contract', 'business-event', :subject_key, 'label-contract', "
            "'boolean', 'true', 'l3-human-confirmed', 'recorded', NULL, "
            ":decision_id, :root_trace, :payload)"
        ),
        {
            "action_trace": f"action-{fact_id}",
            "aggregate_id": aggregate_id,
            "content_sha": "d" * 64,
            "decision_id": f"decision-{fact_id}",
            "event_id": f"event-{fact_id}",
            "fact_id": fact_id,
            "logical_sha": logical_sha,
            "payload": json.dumps({"fixture": fact_id, "reviewed": True}),
            "root_trace": f"root-{fact_id}",
            "subject_key": f"subject-{fact_id}",
        },
    )


def _seed_complete_chain(engine: Engine) -> None:
    with engine.begin() as connection:
        _insert_fact(
            connection,
            fact_id="fact-contract-1",
            revision=1,
            status="superseded",
            active_slot=None,
            supersedes_fact_id=None,
        )
        _insert_fact(
            connection,
            fact_id="fact-contract-2",
            revision=2,
            status="active",
            active_slot="active",
            supersedes_fact_id="fact-contract-1",
        )
        _insert_head(connection)


def test_0039_preserves_rows_removes_active_index_and_guards_history(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'fact-contract.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _engine(database_url)
    try:
        _seed_complete_chain(engine)
        with engine.connect() as connection:
            before = [
                dict(row._mapping)
                for row in connection.execute(text("SELECT * FROM label_facts ORDER BY revision"))
            ]
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_CONTRACT)
    engine = _engine(database_url)
    try:
        indexes = {item["name"] for item in inspect(engine).get_indexes("label_facts")}
        checks = {item["name"] for item in inspect(engine).get_check_constraints("label_facts")}
        assert "uq_label_facts_active_head" not in indexes
        assert "ck_label_facts_append_only_projection" in checks
        with engine.connect() as connection:
            after = [
                dict(row._mapping)
                for row in connection.execute(text("SELECT * FROM label_facts ORDER BY revision"))
            ]
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = 'label_facts'"
                    )
                )
            }
        assert after == before
        assert triggers == {
            "trg_label_facts_contract_insert",
            "trg_label_facts_no_delete",
            "trg_label_facts_no_update",
        }
        with pytest.raises(DBAPIError, match="append-only label_facts"):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE label_facts SET value_json = 'false' WHERE revision = 1")
                )
        with pytest.raises(DBAPIError, match="append-only label_facts"):
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM label_facts WHERE revision = 1"))
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = _engine(database_url)
    try:
        indexes = {item["name"] for item in inspect(engine).get_indexes("label_facts")}
        assert "uq_label_facts_active_head" in indexes
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT fact_id, status, active_slot FROM label_facts ORDER BY revision")
            ).all()
        assert rows == [
            ("fact-contract-1", "superseded", None),
            ("fact-contract-2", "active", "active"),
        ]
    finally:
        engine.dispose()


def test_0039_downgrade_fails_closed_instead_of_rewriting_new_recorded_history(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'fact-contract-downgrade.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _engine(database_url)
    try:
        _seed_complete_chain(engine)
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_CONTRACT)
    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_fact(
                connection,
                fact_id="fact-contract-3",
                revision=3,
                status="recorded",
                active_slot=None,
                supersedes_fact_id="fact-contract-2",
            )
            connection.execute(
                text(
                    "UPDATE label_fact_heads SET current_fact_id = 'fact-contract-3', "
                    "current_revision = 3, generation = 3 "
                    "WHERE fact_head_id = 'head-contract'"
                )
            )
    finally:
        engine.dispose()

    result = _alembic(database_url, "downgrade", REVISION_BEFORE, check=False)
    assert result.returncode != 0
    assert "would rewrite append-only recorded history" in result.stdout + result.stderr

    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM label_facts WHERE fact_id = 'fact-contract-3'")
                )
                == "recorded"
            )
    finally:
        engine.dispose()


def test_0039_fails_closed_until_temporal_backfill_and_head_are_complete(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'fact-contract-incomplete.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO label_facts "
                    "(fact_id, tenant_id, project_id, aggregate_id, label_version_id, "
                    "subject_scope, subject_key, label_id, value_type, value_json, authority, "
                    "status, active_slot, trace_id, payload) VALUES "
                    "('legacy-incomplete', 'tenant-contract', 'project-contract', "
                    "'aggregate-legacy', 'version-contract', 'business-event', "
                    "'subject-contract', 'label-contract', 'boolean', 'true', "
                    "'l2-auto-accepted', 'active', 'active', 'root-legacy', '{}')"
                )
            )
    finally:
        engine.dispose()

    result = _alembic(database_url, "upgrade", REVISION_CONTRACT, check=False)
    assert result.returncode != 0
    assert "requires temporal backfill" in result.stdout + result.stderr
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_BEFORE
            )
    finally:
        engine.dispose()


def test_0040_preserves_legacy_human_fact_bytes_and_rejects_new_dual_source_rows(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'recompute-human-history.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_CONTRACT)
    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_human_fact(
                connection,
                fact_id="fact-human-legacy",
                aggregate_id="aggregate-reviewed-legacy",
                logical_sha="e" * 64,
            )
        with engine.connect() as connection:
            before = dict(
                connection.execute(
                    text("SELECT * FROM label_facts WHERE fact_id = 'fact-human-legacy'")
                )
                .one()
                ._mapping
            )
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_RECOMPUTE)
    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            after = dict(
                connection.execute(
                    text("SELECT * FROM label_facts WHERE fact_id = 'fact-human-legacy'")
                )
                .one()
                ._mapping
            )
        assert after == before

        with pytest.raises(DBAPIError, match="label_facts contract requires recorded rows"):
            with engine.begin() as connection:
                _insert_human_fact(
                    connection,
                    fact_id="fact-human-new-invalid",
                    aggregate_id="aggregate-reviewed-new",
                    logical_sha="f" * 64,
                )

        with engine.begin() as connection:
            _insert_human_fact(
                connection,
                fact_id="fact-human-new-valid",
                aggregate_id=None,
                logical_sha="0" * 64,
            )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT aggregate_id FROM label_facts "
                        "WHERE fact_id = 'fact-human-new-valid'"
                    )
                )
                is None
            )
    finally:
        engine.dispose()


def test_0040_failed_downgrade_preserves_schema_triggers_and_fact_bytes(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'recompute-failed-downgrade.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_RECOMPUTE)
    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_human_fact(
                connection,
                fact_id="fact-human-new-layout",
                aggregate_id=None,
                logical_sha="9" * 64,
            )
        with engine.connect() as connection:
            before = dict(
                connection.execute(
                    text("SELECT * FROM label_facts WHERE fact_id = 'fact-human-new-layout'")
                )
                .one()
                ._mapping
            )
            before_foreign_keys = {
                item["name"] for item in inspect(connection).get_foreign_keys("label_facts")
            }
    finally:
        engine.dispose()

    result = _alembic(database_url, "downgrade", REVISION_CONTRACT, check=False)
    assert result.returncode != 0
    assert "cannot restore legacy non-null aggregate projection" in (result.stdout + result.stderr)

    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                REVISION_RECOMPUTE
            )
            after = dict(
                connection.execute(
                    text("SELECT * FROM label_facts WHERE fact_id = 'fact-human-new-layout'")
                )
                .one()
                ._mapping
            )
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = 'label_facts'"
                    )
                )
            }
            after_foreign_keys = {
                item["name"] for item in inspect(connection).get_foreign_keys("label_facts")
            }
        assert after == before
        assert after_foreign_keys == before_foreign_keys
        assert "fk_label_facts_scope_recompute_item" in after_foreign_keys
        assert triggers == {
            "trg_label_facts_contract_insert",
            "trg_label_facts_no_delete",
            "trg_label_facts_no_update",
        }
        with pytest.raises(DBAPIError, match="append-only label_facts"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE label_facts SET value_json = 'false' "
                        "WHERE fact_id = 'fact-human-new-layout'"
                    )
                )
    finally:
        engine.dispose()


def test_0040_orphan_run_item_blocks_downgrade_before_any_schema_change(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'recompute-orphan-item.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_RECOMPUTE)
    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO label_recompute_run_items "
                    "(recompute_run_item_id, tenant_id, project_id, recompute_run_id, "
                    "partition_id, status, attempt_generation, execution_run_id, row_count, "
                    "lineage_manifest, root_trace_id, action_trace_id, trace_id, payload) "
                    "VALUES ('orphan-item', 'tenant-orphan', 'project-orphan', "
                    "'missing-run', 'partition-a', 'queued', 1, 'missing-execution', 0, "
                    "'{}', 'trace-orphan', 'trace-orphan', 'trace-orphan', '{}')"
                )
            )
    finally:
        engine.dispose()

    result = _alembic(database_url, "downgrade", REVISION_CONTRACT, check=False)
    assert result.returncode != 0
    assert "immutable run-item history" in result.stdout + result.stderr

    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                REVISION_RECOMPUTE
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT recompute_run_item_id FROM label_recompute_run_items "
                        "WHERE recompute_run_item_id = 'orphan-item'"
                    )
                )
                == "orphan-item"
            )
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = 'label_facts'"
                    )
                )
            }
        assert triggers == {
            "trg_label_facts_contract_insert",
            "trg_label_facts_no_delete",
            "trg_label_facts_no_update",
        }
    finally:
        engine.dispose()


def test_0040_successful_downgrade_restores_0039_human_insert_contract(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'recompute-clean-downgrade.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_RECOMPUTE)
    _alembic(database_url, "downgrade", REVISION_CONTRACT)

    engine = _engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_human_fact(
                connection,
                fact_id="fact-human-0039-restored",
                aggregate_id="aggregate-reviewed-0039",
                logical_sha="8" * 64,
            )
        with engine.connect() as connection:
            restored = connection.execute(
                text(
                    "SELECT aggregate_id, source_kind, status, active_slot "
                    "FROM label_facts WHERE fact_id = 'fact-human-0039-restored'"
                )
            ).one()
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = 'label_facts'"
                    )
                )
            }
        assert restored == (
            "aggregate-reviewed-0039",
            "human-decision",
            "recorded",
            None,
        )
        assert triggers == {
            "trg_label_facts_contract_insert",
            "trg_label_facts_no_delete",
            "trg_label_facts_no_update",
        }
        with pytest.raises(DBAPIError, match="append-only label_facts"):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM label_facts WHERE fact_id = 'fact-human-0039-restored'")
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("case", "mutation", "message"),
    [
        (
            "stale-head",
            "UPDATE label_fact_heads SET current_fact_id = 'fact-contract-1', "
            "current_revision = 1 WHERE fact_head_id = 'head-contract'",
            "Head points to the latest revision",
        ),
        (
            "wrong-current-id-same-max-revision",
            "UPDATE label_fact_heads SET current_fact_id = 'fact-contract-1' "
            "WHERE fact_head_id = 'head-contract'",
            "Head points to the latest revision",
        ),
        (
            "broken-supersedes",
            "UPDATE label_facts SET supersedes_fact_id = NULL WHERE fact_id = 'fact-contract-2'",
            "immediately preceding Fact",
        ),
    ],
)
def test_0039_fails_closed_for_invalid_temporal_chain(
    tmp_path: Path,
    case: str,
    mutation: str,
    message: str,
) -> None:
    database_url = f"sqlite:///{tmp_path / f'fact-contract-{case}.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _engine(database_url)
    try:
        _seed_complete_chain(engine)
        with engine.begin() as connection:
            connection.execute(text(mutation))
    finally:
        engine.dispose()

    result = _alembic(database_url, "upgrade", REVISION_CONTRACT, check=False)
    assert result.returncode != 0
    assert message in result.stdout + result.stderr

    engine = _engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_BEFORE
            )
    finally:
        engine.dispose()
