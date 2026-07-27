from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0044_oidc_backchannel_logout"
REVISION_IMPORT_BATCHES = "0045_audio_import_batches"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0045_creates_scoped_import_batch_tables_and_downgrades_cleanly(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'audio-import-batches.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    _alembic(database_url, "upgrade", REVISION_IMPORT_BATCHES)

    engine = create_engine(database_url, future=True)
    try:
        schema = inspect(engine)
        assert {"import_batches", "import_batch_items"}.issubset(schema.get_table_names())
        batch_columns = {column["name"] for column in schema.get_columns("import_batches")}
        assert {
            "import_batch_id",
            "tenant_id",
            "project_id",
            "task_run_id",
            "task_version_id",
            "connector_id",
            "status",
            "current_stage",
            "total_items",
            "succeeded_items",
            "skipped_items",
            "failed_items",
            "cursor_before",
            "cursor_after",
            "root_trace_id",
            "trace_id",
        }.issubset(batch_columns)
        item_columns = {column["name"] for column in schema.get_columns("import_batch_items")}
        assert {
            "import_item_id",
            "import_batch_id",
            "external_record_id",
            "status",
            "error_code",
            "object_version",
            "audio_session_id",
            "root_trace_id",
            "trace_id",
        }.issubset(item_columns)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_IMPORT_BATCHES
            )
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        schema = inspect(engine)
        assert "import_batches" not in schema.get_table_names()
        assert "import_batch_items" not in schema.get_table_names()
    finally:
        engine.dispose()
