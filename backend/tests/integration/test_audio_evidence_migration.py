from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from sqlalchemy import create_engine, inspect, text

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_PLATFORM_CONNECTIONS = "0046_platform_connections"
REVISION_AUDIO_EVIDENCE = "0047_audio_evidence_packs"


class _RecordingBatch:
    def __init__(self) -> None:
        self.alter_calls: list[tuple[str, dict[str, Any]]] = []

    def alter_column(self, name: str, **options: Any) -> None:
        self.alter_calls.append((name, options))


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0047_mysql_column_changes_preserve_existing_definitions() -> None:
    migration = runpy.run_path(
        str(BACKEND_ROOT / "migrations" / "versions" / "0047_audio_evidence_packs.py")
    )
    enforce = cast(
        Callable[[_RecordingBatch], None],
        migration["_enforce_added_columns_not_null"],
    )
    batch = _RecordingBatch()

    enforce(batch)

    assert len(batch.alter_calls) == len(migration["_ADDED_COLUMNS"])
    for name, options in batch.alter_calls:
        assert options["existing_type"] is migration["_ADDED_COLUMN_TYPES"][name]
        assert options["existing_nullable"] is True
        assert options["nullable"] is False
        if name == "resource_version":
            assert options["existing_server_default"] == "1"


def test_0047_creates_immutable_audio_evidence_pack_table(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'audio-evidence.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_PLATFORM_CONNECTIONS)
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO evidence_packs "
                    "(evidence_pack_id, tenant_id, project_id, status, trace_id, payload) "
                    "VALUES (:id, :tenant, :project, :status, :trace_id, :payload)"
                ),
                {
                    "id": "legacy_evidence_pack",
                    "tenant": "tenant_migration",
                    "project": "project_migration",
                    "status": "pending",
                    "trace_id": "trace_legacy_evidence",
                    "payload": json.dumps(
                        {
                            "audio_session_id": "legacy_session",
                            "recording_id": "legacy_recording",
                            "evidence_sha256": "a" * 64,
                        }
                    ),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO evidence_packs "
                    "(evidence_pack_id, tenant_id, project_id, status, trace_id, payload) "
                    "VALUES (:id, :tenant, :project, :status, :trace_id, :payload)"
                ),
                {
                    "id": "legacy_evidence_pack_duplicate_hash",
                    "tenant": "tenant_migration",
                    "project": "project_migration",
                    "status": "pending",
                    "trace_id": "trace_legacy_evidence_duplicate",
                    "payload": json.dumps(
                        {
                            "audio_session_id": "legacy_session_duplicate",
                            "recording_id": "legacy_recording_duplicate",
                            "evidence_sha256": "a" * 64,
                        }
                    ),
                },
            )
    finally:
        engine.dispose()
    _alembic(database_url, "upgrade", REVISION_AUDIO_EVIDENCE)

    engine = create_engine(database_url, future=True)
    try:
        schema = inspect(engine)
        columns = {column["name"] for column in schema.get_columns("evidence_packs")}
        assert {
            "evidence_pack_id",
            "tenant_id",
            "project_id",
            "audio_session_id",
            "recording_id",
            "storage_object_id",
            "storage_object_version",
            "audio_sha256",
            "asr_result_id",
            "asr_result_version",
            "window_start_ms",
            "window_end_ms",
            "evidence_sha256",
            "source_run_id",
            "resource_version",
            "root_trace_id",
            "current_trace_id",
            "payload",
        }.issubset(columns)
        unique_sets = {
            tuple(constraint["column_names"])
            for constraint in schema.get_unique_constraints("evidence_packs")
        }
        assert ("tenant_id", "project_id", "evidence_pack_id") in unique_sets
        assert ("tenant_id", "project_id", "evidence_sha256") in unique_sets
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_AUDIO_EVIDENCE
            )
            legacy = (
                connection.execute(
                    text(
                        "SELECT status, audio_session_id, recording_id, "
                        "audio_sha256, evidence_sha256, root_trace_id "
                        "FROM evidence_packs WHERE evidence_pack_id = 'legacy_evidence_pack'"
                    )
                )
                .mappings()
                .one()
            )
            assert legacy["status"] == "superseded"
            assert legacy["audio_session_id"] == "legacy_session"
            assert legacy["recording_id"] == "legacy_recording"
            assert len(legacy["audio_sha256"]) == 64
            assert len(legacy["evidence_sha256"]) == 64
            assert legacy["root_trace_id"] == "trace_legacy_evidence"
            migrated_hashes = list(
                connection.scalars(
                    text(
                        "SELECT evidence_sha256 FROM evidence_packs "
                        "WHERE tenant_id = 'tenant_migration' "
                        "AND project_id = 'project_migration'"
                    )
                )
            )
            assert len(migrated_hashes) == 2
            assert len(set(migrated_hashes)) == 2
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_PLATFORM_CONNECTIONS)
    engine = create_engine(database_url, future=True)
    try:
        downgraded_columns = {
            column["name"] for column in inspect(engine).get_columns("evidence_packs")
        }
        assert "evidence_pack_id" in downgraded_columns
        assert "evidence_sha256" not in downgraded_columns
        assert "root_trace_id" not in downgraded_columns
    finally:
        engine.dispose()
