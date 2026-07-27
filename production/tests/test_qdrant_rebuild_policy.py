from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ROOT / "backend" / "app" / "qdrant_rebuild.py"
RUNBOOK = ROOT / "doc" / "runbooks" / "backup-restore.md"


def test_rebuilder_uses_scoped_confirmed_outbox_authority() -> None:
    source = COMMAND.read_text(encoding="utf-8")

    assert '"processed"' in source
    assert '"confirmed"' in source
    assert 'attempt.adapter == "qdrant"' in source
    assert "confirmation must exactly equal the rebuild plan SHA-256" in source
    assert '"knowledge_chunks"' in source
    assert '"voiceprint_embeddings"' not in source
    assert "outside rebuild policy" in source
    assert (
        "minio-objects-verified-by-restore-runbook-not-read-by-this-command" in source
    )
    assert "tenant_id=tenant_id" in source
    assert "project_id=project_id" in source
    assert "aggregate_type=REBUILD_AGGREGATE_TYPE" in source
    assert '"qdrant.rebuild.enqueued"' in source

    # The command rebuilds through the existing Outbox/Worker path. Direct
    # collection deletion or a second unaudited Qdrant transport is forbidden.
    assert "configured_real_qdrant_client" not in source
    assert "DELETE" not in source
    assert "urllib" not in source


def test_runbook_keeps_ingress_fenced_and_requires_final_semantic_gate() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook.split("### 3. 不使用 Qdrant snapshot 的治理化重建", maxsplit=1)[
        1
    ].split("### 4.", maxsplit=1)[0]

    assert "保持 `edge` 停止" in section
    assert "python -m app.qdrant_rebuild plan" in section
    assert "python -m app.qdrant_rebuild enqueue" in section
    assert '--confirm-sha256 "${PLAN_SHA256}"' in section
    assert "python -m app.qdrant_rebuild verify" in section
    assert "production/scripts/finalize-restore.sh" in section
    assert "VOICEPRINT_VECTOR_PROVIDER_UNSUPPORTED" in section
    assert "禁止退化为文本 embedding" in section
    assert "每个" in section and "tenant/project" in section
    assert "不要手工 upsert" in section
    assert "第二个独立空 Compose project" in section
    assert "不能描述为“生产恢复已验收”" in section
