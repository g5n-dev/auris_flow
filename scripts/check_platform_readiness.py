#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, TypedDict

import yaml  # type: ignore[import-untyped]

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from verify_visual_baseline import (  # noqa: E402
    release_runtime_contract,
    validate_visual_baseline_lock,
)


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    paths: tuple[str, ...] = ()
    contains: dict[str, tuple[str, ...]] = field(default_factory=dict)
    rationale: str = ""


class ReadinessResult(TypedDict):
    key: str
    title: str
    status: Literal["pass", "fail"]
    failures: list[str]
    rationale: str


CHECKS: tuple[Check, ...] = (
    Check(
        key="oss_project_surface",
        title="开源项目基本入口",
        paths=(
            "README.md",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "CHANGELOG.md",
            "RELEASE_CHECKLIST.md",
            "SUPPORT.md",
            "MAINTAINERS.md",
            "GOVERNANCE.md",
            ".gitignore",
            ".github/workflows/verify.yml",
            ".github/workflows/release-images.yml",
            ".github/ISSUE_TEMPLATE/bug_report.yml",
            ".github/ISSUE_TEMPLATE/config.yml",
            ".github/ISSUE_TEMPLATE/feature_request.yml",
            ".github/pull_request_template.md",
            ".github/release.yml",
            "doc/reports/open-source-release-readiness.md",
            "doc/reports/repository-layout-review.md",
            "doc/reports/change-submission-plan.md",
            "NOTICE",
            "open-source-rights-authorization.md",
            "THIRD_PARTY_NOTICES.md",
            "production/compose.yaml",
            "production/README.md",
            "doc/release/versioning-and-compatibility.md",
            "doc/runbooks/backup-restore.md",
            "doc/runbooks/operations.md",
        ),
        contains={
            "README.md": (
                "Auris Flow",
                "`v1.0.0` 的**候选实现**",
                "Apache License 2.0",
                "production/README.md",
                "Authorization Code + PKCE",
                "不透明 HttpOnly 浏览器会话",
            ),
            ".github/workflows/verify.yml": ("bash scripts/verify_release.sh",),
            "CHANGELOG.md": ("Unreleased",),
            "RELEASE_CHECKLIST.md": (
                "bash scripts/verify_release.sh",
                "Human Release Authority",
                "repository-layout-review.md",
                "NOTICE",
                "secret scan ok",
                "protocol fake or deterministic test vector is not production evidence",
            ),
            "open-source-rights-authorization.md": (
                "Authorization status:",
                "Rights holder legal name:",
                "Authorized license: Apache-2.0",
                "Approval evidence reference:",
                "Final NOTICE confirmed:",
            ),
            "SUPPORT.md": ("SECURITY.md", "trace_id"),
            "MAINTAINERS.md": ("Release Authority",),
            "GOVERNANCE.md": ("tenant/project", "ClickHouse"),
            ".github/ISSUE_TEMPLATE/bug_report.yml": (
                "trace_id",
                "Do not include secrets",
            ),
            ".github/ISSUE_TEMPLATE/config.yml": (
                "SECURITY.md",
                "repository private advisory channel",
            ),
            ".github/release.yml": ("Verification And Security",),
            ".github/pull_request_template.md": (
                "Scope Boundary",
                "backend-contracts-and-migrations",
                "doc/reports/change-submission-plan.md",
            ),
            "doc/reports/repository-layout-review.md": (
                "No repository-wide directory migration is required",
                "Commit Boundary",
                "Future Migration Triggers",
            ),
            "doc/reports/change-submission-plan.md": (
                "Recommended Split",
                "governance-release",
                "frontend-bff-ux",
                "Final Aggregation Gate",
            ),
            "production/compose.yaml": (
                "dagster-code:",
                "dagster-webserver:",
                "dagster-daemon:",
                "otel-collector:",
                "prometheus:",
                "grafana:",
            ),
            "production/README.md": (
                "发行状态：`v1.0.0` 候选，尚未发布",
                "Authorization Code + PKCE",
                "`/readyz`",
                "`/metrics`",
                "没有节点级高可用",
            ),
            "doc/release/versioning-and-compatibility.md": (
                "SemVer",
                "expand",
                "migrate",
                "contract",
            ),
            "doc/runbooks/backup-restore.md": (
                "production/scripts/backup.sh",
                "production/scripts/restore.sh",
                "production/scripts/verify-backup.sh",
            ),
            "doc/runbooks/operations.md": (
                "SLO",
                "alert-testing",
                "trace_id",
            ),
        },
        rationale="开源仓库必须先让贡献者知道项目是什么、如何贡献、如何验证、当前许可边界。",
    ),
    Check(
        key="one_command_quality_gate",
        title="正式发布 gate 已接线且 fail-closed",
        paths=(
            "scripts/verify_all.sh",
            "scripts/verify_fast.sh",
            "scripts/verify_release.sh",
            "scripts/verify_real_stack.sh",
            "scripts/verify_real_dagster.sh",
            "scripts/verify_real_dagster.py",
            "scripts/verify_real_dagster_callback_server.py",
            "scripts/verify_product_dagster_path.sh",
            "scripts/verify_product_dagster_path.py",
            "scripts/verify_production_path.sh",
            "scripts/verify_production_path_gate.py",
            "scripts/verify_production_mysql_migrations.sh",
            "backend/scripts/verify_mysql_migration_security.py",
            "production/tests/test_mysql_migration_security.py",
            "scripts/verify_clean_clone.sh",
            "scripts/verify_release_authorization.py",
            "scripts/finalize_release_evidence.py",
            "production/tests/dagster-gate.compose.yaml",
            "production/tests/dagster-gate-callback.Dockerfile",
            "production/tests/dagster-product-gate.compose.yaml",
            "production/tests/production-path-gate.compose.yaml",
            "production/tests/production-path-gate.md",
            "backend/tests/unit/test_production_path_gate.py",
            "scripts/check_real_stack_artifact.sh",
            "scripts/dev_up.sh",
            "scripts/audit_ui.sh",
        ),
        contains={
            "scripts/verify_all.sh": (
                "validate_backend_spec.py",
                "scripts/scan_secrets.py",
                "uv sync --check --locked --all-extras --project backend",
                "uv sync --check --locked --all-extras --project production/dagster",
                "backend-runtime-requirements.txt",
                "dagster-runtime-requirements.txt",
                "--strict --require-hashes --disable-pip",
                "npm audit --prefix prototype/auris-flow-ui",
                "npm audit signatures --prefix prototype/auris-flow-ui",
                "scripts/verify_production_compose.py",
                "pytest production/tests",
                "pytest production/dagster/tests",
                "ruff format --check production/dagster/src production/dagster/tests",
                "ruff check production/dagster/src production/dagster/tests",
                "mypy production/dagster/src",
                "ruff format --check backend scripts production/tests",
                "ruff check backend scripts production/tests",
                "mypy backend/app",
                "verify_migrations.py",
                "pytest backend/tests/unit backend/tests/contract backend/tests/integration",
                "smoke_backend.py",
                "npm --prefix prototype/auris-flow-ui run build",
                "npm --prefix prototype/auris-flow-ui run e2e:ui",
                "AURIS_RUN_E2E",
            ),
            "scripts/verify_release.sh": (
                "AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1 bash scripts/verify_all.sh",
                "bash scripts/verify_real_stack.sh",
                "bash scripts/verify_real_dagster.sh",
                "bash scripts/verify_product_dagster_path.sh",
                "bash scripts/verify_production_path.sh",
                "bash scripts/verify_production_mysql_migrations.sh",
                "AURIS_SKIP_REAL_DAGSTER=1 is not allowed",
                "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed",
                "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed",
                "AURIS_RELEASE_CHECK=1 bash scripts/verify_clean_clone.sh",
                "scripts/verify_release_authorization.py",
                "scripts/finalize_release_evidence.py",
            ),
            "scripts/verify_production_path.sh": (
                "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed",
                "scripts/verify_production_path_gate.py",
                "scripts/verify_production_path_runtime.py",
                "build/release-evidence/production-path-gate.json",
            ),
            "scripts/check_real_stack_artifact.sh": (
                "real_qdrant",
                "metadata_registered",
                "registration_event_processed",
            ),
            "scripts/dev_up.sh": (
                "alembic upgrade head",
                "app.seed local_demo",
                "uvicorn app.main:app",
                "npm --prefix prototype/auris-flow-ui run dev",
                "http://127.0.0.1:${AURIS_BFF_PORT:-8000}/readyz",
                "http://127.0.0.1:5173",
            ),
            "scripts/audit_ui.sh": (
                "alembic upgrade head",
                "app.seed local_demo",
                "npm exec vite",
                "npm --prefix prototype/auris-flow-ui run audit:tabs",
                "npm --prefix prototype/auris-flow-ui run audit:capture",
            ),
        },
        rationale=(
            "这里验证 P0 正式发布 gate 的可执行接线和 fail-closed 约束；"
            "允许 P2 生产路径蓝图保持 blocked，不代表 P2 运行态已经 ready。"
        ),
    ),
    Check(
        key="backend_contract_package",
        title="后端开发契约包",
        paths=(
            "doc/backend-spec/README.md",
            "doc/backend-spec/openapi-v0.1.yaml",
            "doc/backend-spec/db-schema.md",
            "doc/backend-spec/mock-to-api-map.md",
            "doc/backend-spec/state-machines.md",
            "doc/backend-spec/test-plan.md",
            "doc/backend-spec/seed-fixture-v0.1.json",
            "doc/backend-spec/validate_backend_spec.py",
            "doc/backend-spec/migration-plan.md",
        ),
        contains={
            "doc/backend-spec/openapi-v0.1.yaml": (
                "/labels:",
                "/label-versions:",
                "/eval-datasets:",
                "/eval-runs:",
                "/insights/metrics:",
                "/insights/reports:",
                "/insights/actions:",
            ),
            "doc/backend-spec/mock-to-api-map.md": (
                "标签体系、候选、版本、冲突、发布门禁、Human Loop",
                "趋势、桑吉、漏斗、雷达、报告、证据下钻",
                "评测集、运行、指标、badcase、回流",
            ),
            "doc/backend-spec/validate_backend_spec.py": (
                "validate_runtime_openapi_drift",
                "OpenAPI matches FastAPI runtime operations",
            ),
            "doc/backend-spec/migration-plan.md": (
                "0041",
                "oidc_browser_sessions",
                "当前 head",
            ),
        },
        rationale="后端团队需要从产品 mock 直接落到 API、状态机、DB、seed 和测试。",
    ),
    Check(
        key="backend_runtime_foundation",
        title="后端运行底座",
        paths=(
            "backend/pyproject.toml",
            "backend/app/main.py",
            "backend/app/core/config.py",
            "backend/app/core/logging.py",
            "backend/app/services/idempotency_service.py",
            "backend/app/services/audit_service.py",
            "backend/app/services/outbox_service.py",
            "backend/app/workers/outbox_worker.py",
            "backend/scripts/verify_migrations.py",
            "backend/migrations/versions/0001_core_tables.py",
            "backend/migrations/versions/0008_storage_objects_table.py",
            "backend/app/core/audio_playback.py",
            "backend/app/services/adapters.py",
            "backend/tests/unit/test_object_storage_provider_range.py",
            "docker/local/docker-compose.yml",
        ),
        contains={
            "backend/app/main.py": (
                "request_logging_middleware",
                "X-Trace-Id",
                "app.include_router",
                "TrustedHostMiddleware",
                "apply_security_headers",
                "cors_allowed_origins",
            ),
            "backend/app/core/config.py": (
                "require_release_security_configuration",
                "CORS_ALLOWED_ORIGINS must be explicit",
                "TRUSTED_HOSTS must be explicit",
                "_require_strong_secret(",
                '"AUDIO_PLAYBACK_GRANT_SECRET"',
                "COMPLETION_RECEIPT_SECRET or COMPLETION_RECEIPT_KEY_BINDINGS",
                '"QDRANT_API_KEY"',
                "object storage real adapter missing prod/release config",
            ),
            "backend/app/core/logging.py": ("log_event", "trace_id", "idempotency_key"),
            "backend/app/services/idempotency_service.py": (
                "IDEMPOTENCY_KEY_REQUIRED",
                "IDEMPOTENCY_KEY_CONFLICT",
            ),
            "docker/local/docker-compose.yml": ("mysql", "redis", "minio", "qdrant"),
            "backend/scripts/verify_migrations.py": (
                "command.upgrade",
                "command.downgrade",
                "uq_json_resources_scope_key",
                "uq_storage_objects_scope_locator",
            ),
            "backend/app/core/audio_playback.py": (
                "create_audio_playback_grant",
                "verify_audio_playback_grant",
                "AUDIO_PLAYBACK_GRANT_INVALID",
            ),
            "backend/app/services/adapters.py": (
                "SUPPORTED_OBJECT_STORAGE_PROVIDERS",
                "object_storage_client_for_provider",
                "open_object",
            ),
            "backend/tests/unit/test_object_storage_provider_range.py": (
                "PROVIDER_CASES",
                "test_production_audio_storage_never_falls_back_to_synthetic_audio",
                "test_audio_range_route_uses_registered_provider",
            ),
        },
        rationale="平台操作链路必须有统一日志句柄、trace、幂等、审计、outbox 和本地真实依赖。",
    ),
    Check(
        key="evaluation_labeling_insights_domains",
        title="评测、标注、洞察三域闭环",
        paths=(
            "backend/app/api/routers/evaluation.py",
            "backend/app/api/routers/labels.py",
            "backend/app/api/routers/insights.py",
            "prototype/auris-flow-ui/src/api/client.ts",
            "prototype/auris-flow-ui/src/workspace/ModuleWorkspace.tsx",
            "prototype/auris-flow-ui/src/workspace/moduleWorkspaceCatalog.ts",
            "prototype/auris-flow-ui/src/features/labels/LabelsModule.tsx",
            "prototype/auris-flow-ui/src/features/evaluation/EvaluationModule.tsx",
            "prototype/auris-flow-ui/src/features/insights/InsightsModule.tsx",
            "prototype/auris-flow-ui/src/catalogs/module-catalog.json",
            "prototype/auris-flow-ui/e2e/platform-bff.mjs",
            "prototype/auris-flow-ui/e2e/ui-smoke.mjs",
            "prototype/auris-flow-ui/audit/capture-audit.mjs",
            "prototype/auris-flow-ui/audit/tab-similarity.mjs",
            "prototype/auris-flow-ui/scripts/check-bundle-budget.mjs",
            "prototype/auris-flow-ui/package.json",
            "scripts/verify_ui_bff_e2e.sh",
        ),
        contains={
            "backend/app/api/routers/evaluation.py": (
                "/eval-datasets",
                "/eval-runs",
                "/feedback-tasks",
            ),
            "backend/app/api/routers/labels.py": (
                "/labels",
                "/label-versions",
                "/label-optimization-runs",
            ),
            "backend/app/api/routers/insights.py": (
                "/insights/metrics",
                "/insights/reports",
                "/insights/actions",
            ),
            "prototype/auris-flow-ui/src/api/client.ts": (
                "createPlatformMutation",
                "/v1/label-versions",
                "/v1/eval-runs",
                "/v1/insights/actions",
            ),
            "prototype/auris-flow-ui/src/workspace/ModuleWorkspace.tsx": (
                "ModuleWorkspaceView",
                "useModuleCommands",
                "useModuleProjection",
            ),
            "prototype/auris-flow-ui/src/workspace/moduleWorkspaceCatalog.ts": (
                "moduleConfigs",
                "moduleInteractionModels",
                "staticCatalog",
            ),
            "prototype/auris-flow-ui/e2e/platform-bff.mjs": (
                "/api/v1/label-versions",
                "/api/v1/eval-runs",
                "/api/v1/insights/actions",
                "/api/v1/insights/reports",
                "/feedback-tasks",
            ),
            "prototype/auris-flow-ui/e2e/ui-smoke.mjs": (
                "createServer",
                "runModuleCommandSmoke",
                "知识库",
            ),
            "prototype/auris-flow-ui/audit/capture-audit.mjs": (
                "exposedTerms",
                "duplicateButtons",
                "blockedExposedTerms",
                "Auris Flow navigation did not render",
            ),
            "prototype/auris-flow-ui/audit/tab-similarity.mjs": (
                "similarPairs",
                "jaccard",
                "allowedSimilarPairs",
                "Unexpected high-similarity tabs found",
            ),
            "prototype/auris-flow-ui/scripts/check-bundle-budget.mjs": (
                "totalJsRawBytes",
                "totalJsBrotliBytes",
                "maxJsAssetBytes",
                "initialClosureBrotliBytes",
            ),
            "prototype/auris-flow-ui/package.json": ("audit:auto",),
            "prototype/auris-flow-ui/src/features/labels/LabelsModule.tsx": (
                "function LabelsModule",
            ),
            "prototype/auris-flow-ui/src/features/insights/InsightsModule.tsx": (
                "function InsightsModule",
            ),
            "prototype/auris-flow-ui/src/features/evaluation/EvaluationModule.tsx": (
                "function EvaluationModule",
            ),
            "prototype/auris-flow-ui/src/catalogs/module-catalog.json": (
                "打标评测",
                "Prompt优化",
                "业务大盘",
                "智能 BI 大盘",
            ),
        },
        rationale="目标不是三个孤立页面，而是评测、标注、洞察互相回流的同一产品面。",
    ),
    Check(
        key="contract_tests_cover_core_chain",
        title="核心链路契约测试",
        paths=(
            "backend/tests/contract/test_core_contract.py",
            "backend/tests/integration/test_outbox_worker.py",
        ),
        contains={
            "backend/tests/contract/test_core_contract.py": (
                "test_task_run_idempotency_replay_and_conflict",
                "test_work_items_require_idempotency_and_replay",
                "test_json_resources_are_scoped_by_tenant_and_project",
                "test_trace_lookup_is_scoped_by_tenant_and_project",
                "test_labeling_evaluation_insight_closed_loop_contract",
                "test_audio_recording_object_registration_is_scoped_idempotent_and_traceable",
                "test_audio_playback_grant_allows_native_media_range_without_custom_headers",
            ),
            "backend/tests/integration/test_outbox_worker.py": ("process_once",),
        },
        rationale="必须证明租户隔离、幂等、trace 和异步运行不是文档声明。",
    ),
    Check(
        key="eval_harness_artifact",
        title="平台级 Eval 定义",
        paths=(".claude/evals/open-source-platform-readiness.md",),
        contains={
            ".claude/evals/open-source-platform-readiness.md": (
                "Capability Evals",
                "Regression Evals",
                "scripts/check_platform_readiness.py",
            ),
        },
        rationale="开源一体平台目标需要稳定 eval，不应只靠人工记忆和截图反馈。",
    ),
)

LICENSE_FILES = ("LICENSE", "LICENSE.md", "COPYING", "COPYING.txt")
RELEASE_ARTIFACT_PATTERNS = (
    ".DS_Store",
    "**/.DS_Store",
    ".coverage",
    "backend/.coverage",
    ".next/**",
    ".vite/**",
    "dist/**",
    "build/**",
    "**/__pycache__/**",
    "**/*.pyc",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".mypy_cache/**",
    ".venv/**",
    "backend/.venv/**",
    "*.sqlite",
    "*.sqlite3",
    "backend/*.sqlite",
    "backend/*.sqlite3",
    "prototype/auris-flow-ui/dist/**",
    "prototype/auris-flow-ui/dist-*/**",
    "prototype/auris-flow-ui/e2e/artifacts/**",
    "prototype/auris-flow-ui/e2e/screenshots/**",
    "prototype/auris-flow-ui/test-baselines/**",
    "prototype/auris-flow-ui/test-results/**",
    "prototype/auris-flow-ui/playwright-report/**",
    "prototype/auris-flow-ui/audit/*.png",
    "prototype/auris-flow-ui/audit/*.json",
    "prototype/auris-flow-ui/audit/screenshots/**",
    "prototype/auris-flow-ui/audit/prototype-interaction-audit*/**",
    "prototype/auris-flow-ui/audit-iteration/**",
    "prototype/auris-flow-ui/audits/**",
    "coverage.xml",
    "htmlcov/**",
    "**/*.wav",
    "**/*.wave",
    "**/*.mp3",
    "**/*.flac",
    "**/*.m4a",
    "**/*.ogg",
    "**/*.opus",
    "**/*.aac",
    "**/*.rttm",
    "**/*.stm",
    "**/*.ctm",
    "**/*.tar",
    "**/*.tar.gz",
    "**/*.tgz",
    "public-audio-data/**",
    "datasets-cache/**",
)
RELEASE_REQUIRED_TRACKED_PATHS = (
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    ".github/workflows/release-images.yml",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/app/api/routers/auth.py",
    "backend/app/api/routers/calibrations.py",
    "backend/app/api/routers/quality_appeals.py",
    "backend/app/services/audio_playback_service.py",
    "backend/app/services/label_policy_service.py",
    "backend/app/services/read_policy_service.py",
    "backend/app/core/browser_session.py",
    "backend/app/core/embeddings.py",
    "backend/app/core/metrics.py",
    "backend/app/core/observability.py",
    "backend/app/core/oidc.py",
    "backend/app/core/oidc_flow.py",
    "backend/app/core/oidc_state.py",
    "backend/migrations/versions/0009_outbox_delivery_leases.py",
    "backend/migrations/versions/0010_label_policy_engine.py",
    "backend/migrations/versions/0020_auth_sessions.py",
    "backend/migrations/versions/0041_oidc_browser_sessions.py",
    "backend/migrations/versions/0042_task_run_control_plane.py",
    "backend/tests/contract/test_release_blocker_rbac_trace.py",
    "backend/tests/contract/test_resource_read_policy_inventory.py",
    "backend/tests/unit/test_read_policy_service.py",
    "prototype/auris-flow-ui/e2e/preview-smoke.mjs",
    "prototype/auris-flow-ui/package.json",
    "prototype/auris-flow-ui/package-lock.json",
    "prototype/auris-flow-ui/src/modules/moduleCatalog.ts",
    "backend/tests/unit/test_real_dagster_control.py",
    "backend/tests/unit/test_real_dagster_gate_scripts.py",
    "backend/tests/unit/test_product_dagster_gate_scripts.py",
    "backend/tests/unit/test_production_path_gate.py",
    "backend/app/services/task_run_control_service.py",
    "backend/app/services/task_run_monitor_service.py",
    "backend/tests/integration/test_task_run_controls.py",
    "backend/tests/integration/test_task_run_monitor.py",
    "open-source-rights-authorization.md",
    "production/tests/dagster-gate-callback.Dockerfile",
    "production/tests/dagster-gate.compose.yaml",
    "production/tests/dagster-product-gate.compose.yaml",
    "production/tests/production-path-gate.compose.yaml",
    "production/tests/production-path-gate.md",
    "production/visual/Dockerfile",
    "production/visual/runtime-contract.json",
    "production/visual/runtime.mjs",
    "production/visual/seed-overlay.json",
    "production/visual/visual-baseline.lock.json",
    ".github/workflows/visual-baseline-build.yml",
    ".github/workflows/visual-baseline-promotion.yml",
    "scripts/promote_visual_baseline.sh",
    "scripts/tests/test_verify_clean_clone.py",
    "scripts/tests/test_finalize_release_evidence.py",
    "scripts/tests/test_release_authorization.py",
    "scripts/tests/test_verify_visual_baseline.py",
    "scripts/verify_clean_clone.sh",
    "scripts/verify_release_authorization.py",
    "scripts/finalize_release_evidence.py",
    "scripts/verify_real_dagster.py",
    "scripts/verify_real_dagster.sh",
    "scripts/verify_real_dagster_callback_server.py",
    "scripts/verify_product_dagster_path.py",
    "scripts/verify_product_dagster_path.sh",
    "scripts/verify_production_path_gate.py",
    "scripts/verify_production_path.sh",
    "scripts/verify_visual_baseline.py",
    "scripts/visual_regression.sh",
    "scripts/verify_release.sh",
    "scripts/verify_production_compose.py",
    "scripts/generate_supply_chain_evidence.py",
    "scripts/render_release_compose.py",
    "scripts/validate_public_audio_datasets.py",
    "doc/backend-spec/public-audio-datasets-v0.1.json",
    "production/compose.yaml",
    "production/backend/Dockerfile",
    "production/dagster/Dockerfile",
    "production/dagster/pyproject.toml",
    "production/dagster/uv.lock",
    "production/dagster/src/auris_flow_dagster/definitions.py",
    "production/edge/Dockerfile",
    "production/edge/nginx.conf",
    "production/observability/alerts.yaml",
    "production/observability/otel-collector.yaml",
    "production/observability/prometheus.yaml",
    "production/scripts/backup.sh",
    "production/scripts/restore.sh",
    "production/scripts/verify-backup.sh",
    "doc/release/versioning-and-compatibility.md",
    "doc/runbooks/backup-restore.md",
    "doc/runbooks/operations.md",
    "doc/runbooks/upgrade-rollback.md",
)


def path_exists_any(paths: tuple[str, ...]) -> bool:
    return any((ROOT / path).exists() for path in paths)


def git_tracked_files() -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    )
    return tuple(item.decode("utf-8") for item in completed.stdout.split(b"\0") if item)


def git_untracked_files() -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    )
    return tuple(item.decode("utf-8") for item in completed.stdout.split(b"\0") if item)


def git_unstaged_files(root: Path = ROOT) -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "diff", "--name-only", "-z"),
        cwd=root,
        check=True,
        capture_output=True,
        text=False,
    )
    return tuple(item.decode("utf-8") for item in completed.stdout.split(b"\0") if item)


def git_staged_files(root: Path = ROOT) -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "diff", "--cached", "--name-only", "-z"),
        cwd=root,
        check=True,
        capture_output=True,
        text=False,
    )
    return tuple(item.decode("utf-8") for item in completed.stdout.split(b"\0") if item)


def is_release_artifact(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in RELEASE_ARTIFACT_PATTERNS)


def tracked_release_artifacts() -> list[str]:
    return [path for path in git_tracked_files() if is_release_artifact(path)]


def validate_release_authorization(root: Path = ROOT) -> list[str]:
    authorization_path = root / "open-source-rights-authorization.md"
    notice_path = root / "NOTICE"
    failures: list[str] = []
    if not authorization_path.is_file():
        return ["missing open-source-rights-authorization.md"]
    if not notice_path.is_file():
        return ["missing NOTICE"]

    authorization = authorization_path.read_text(encoding="utf-8")
    notice = notice_path.read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for line in authorization.splitlines():
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        fields[key.strip()] = value.strip()

    expected_fields = {
        "Authorization status",
        "Rights holder legal name",
        "Copyright notice",
        "Authorized license",
        "Approval date (UTC)",
        "Approval evidence reference",
        "Final NOTICE confirmed",
    }
    missing_fields = sorted(expected_fields - fields.keys())
    failures.extend(
        f"rights authorization missing field: {field}" for field in missing_fields
    )
    if missing_fields:
        return failures

    placeholder_values = {
        "",
        "PENDING",
        "NO",
        "TBD",
        "TODO",
        "YYYY-MM-DD",
        "PROJECT OWNER TO COMPLETE",
    }
    if fields["Authorization status"] != "APPROVED":
        failures.append("rights authorization status is not APPROVED")
    if fields["Authorized license"] != "Apache-2.0":
        failures.append("rights authorization does not approve Apache-2.0")
    if fields["Final NOTICE confirmed"] != "YES":
        failures.append("rights holder has not confirmed the final NOTICE")
    for field_name in (
        "Rights holder legal name",
        "Copyright notice",
        "Approval evidence reference",
    ):
        if fields[field_name].upper() in placeholder_values:
            failures.append(
                f"rights authorization field is still a placeholder: {field_name}"
            )
    approval_date_raw = fields["Approval date (UTC)"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", approval_date_raw):
        failures.append("rights authorization approval date must be YYYY-MM-DD")
    else:
        try:
            approval_date = date.fromisoformat(approval_date_raw)
        except ValueError:
            failures.append("rights authorization approval date is not a real UTC date")
        else:
            if approval_date > date.today():
                failures.append(
                    "rights authorization approval date cannot be in the future"
                )

    placeholder_markers = (
        "confirmation required",
        "project owner to complete",
        "unidentified person",
    )
    lowered_notice = notice.lower()
    if any(marker in lowered_notice for marker in placeholder_markers):
        failures.append("NOTICE still contains an unapproved rights-holder placeholder")
    if fields["Rights holder legal name"] not in notice:
        failures.append("NOTICE does not identify the approved rights holder")
    if fields["Copyright notice"] not in notice:
        failures.append("NOTICE does not contain the approved copyright notice")
    return failures


def run_release_checks() -> list[ReadinessResult]:
    results = run_checks()
    release_results: list[ReadinessResult] = []

    license_failures: list[str] = []
    if not path_exists_any(LICENSE_FILES):
        license_failures.append(
            "missing committed open-source license: expected one of "
            + ", ".join(LICENSE_FILES)
        )
    readme_path = ROOT / "README.md"
    if readme_path.exists() and "Apache License 2.0" not in readme_path.read_text(
        encoding="utf-8"
    ):
        license_failures.append("README.md does not declare Apache License 2.0")
    license_failures.extend(validate_release_authorization())
    release_results.append(
        {
            "key": "release_license",
            "title": "正式开源许可证",
            "status": "pass" if not license_failures else "fail",
            "failures": license_failures,
            "rationale": "公开开源发布必须有明确许可证；没有 LICENSE 只能视为内部开发基线。",
        }
    )

    hygiene_failures: list[str] = []
    gitignore_path = ROOT / ".gitignore"
    gitignore_text = (
        gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    )
    for pattern in (
        ".DS_Store",
        "node_modules/",
        "dist/",
        ".next/",
        ".env",
        "*.sqlite",
        "__pycache__/",
        "e2e/artifacts/",
        "prototype/auris-flow-ui/e2e/screenshots/",
        "prototype/auris-flow-ui/test-baselines/",
        "prototype/auris-flow-ui/test-results/",
        "prototype/auris-flow-ui/playwright-report/",
        ".vite/",
        "prototype/auris-flow-ui/audit/*.json",
        "prototype/auris-flow-ui/audit/screenshots/",
        "prototype/auris-flow-ui/audits/",
        "*.wav",
        "*.rttm",
        "*.tar.gz",
        "public-audio-data/",
        "datasets-cache/",
    ):
        if pattern not in gitignore_text:
            hygiene_failures.append(
                f".gitignore missing release hygiene pattern: {pattern}"
            )
    try:
        tracked_artifacts = tracked_release_artifacts()
    except (OSError, subprocess.CalledProcessError) as error:
        hygiene_failures.append(f"unable to inspect git-tracked files: {error}")
        tracked_artifacts = []
    for artifact in tracked_artifacts:
        hygiene_failures.append(
            f"generated or local-only artifact is tracked: {artifact}"
        )
    release_results.append(
        {
            "key": "release_distribution_hygiene",
            "title": "发布包卫生",
            "status": "pass" if not hygiene_failures else "fail",
            "failures": hygiene_failures,
            "rationale": "开源发布不能把本地依赖、构建产物、缓存、临时数据库或 E2E 产物纳入源码包。",
        }
    )

    tree_failures: list[str] = []
    try:
        tracked_files = set(git_tracked_files())
        untracked_files = [
            path for path in git_untracked_files() if not is_release_artifact(path)
        ]
        unstaged_files = [
            path for path in git_unstaged_files() if not is_release_artifact(path)
        ]
        staged_files = [
            path for path in git_staged_files() if not is_release_artifact(path)
        ]
    except (OSError, subprocess.CalledProcessError) as error:
        tree_failures.append(f"unable to inspect Git release tree: {error}")
        tracked_files = set()
        untracked_files = []
        unstaged_files = []
        staged_files = []

    for path in RELEASE_REQUIRED_TRACKED_PATHS:
        if path not in tracked_files:
            tree_failures.append(f"required release source is not in Git index: {path}")

    visual_lock_failures = validate_visual_baseline_lock(
        ROOT / "production/visual/visual-baseline.lock.json",
        require_approved=True,
    )
    tree_failures.extend(
        f"visual baseline lock: {failure}" for failure in visual_lock_failures
    )
    for path in sorted(untracked_files):
        tree_failures.append(f"untracked release source would be omitted: {path}")
    for path in sorted(unstaged_files):
        tree_failures.append(
            f"unstaged release change is not represented by HEAD: {path}"
        )
    for path in sorted(staged_files):
        tree_failures.append(f"staged release change is not committed in HEAD: {path}")
    release_results.append(
        {
            "key": "release_git_tree_integrity",
            "title": "发布树与验证工作区一致",
            "status": "pass" if not tree_failures else "fail",
            "failures": tree_failures,
            "rationale": "严格发布门禁必须验证干净 HEAD 中的候选源码，不能依赖未跟踪、未暂存或仅暂存的本地实现。",
        }
    )

    security_failures: list[str] = []
    security_path = ROOT / "SECURITY.md"
    security_text = (
        security_path.read_text(encoding="utf-8") if security_path.exists() else ""
    )
    for pattern in (
        "Reporting a Vulnerability",
        "GitHub Security Advisory",
        "3 个工作日内确认收到报告",
        "Known Gaps Before Public Release",
        "Demo Credentials",
        "OIDC Authorization Code + PKCE",
        "CORS origins",
        "TrustedHost",
        "fail-closed",
        "生产 bundle 不持久化共享 bearer token",
        "Docker secret source file",
    ):
        if pattern not in security_text:
            security_failures.append(f"SECURITY.md missing release section: {pattern}")
    dependabot_path = ROOT / ".github/dependabot.yml"
    dependabot_text = (
        dependabot_path.read_text(encoding="utf-8") if dependabot_path.exists() else ""
    )
    for pattern in (
        'package-ecosystem: "github-actions"',
        'package-ecosystem: "npm"',
        'package-ecosystem: "uv"',
        'directory: "/backend"',
        'directory: "/production/dagster"',
    ):
        if pattern not in dependabot_text:
            security_failures.append(
                f".github/dependabot.yml missing ecosystem: {pattern}"
            )
    codeql_path = ROOT / ".github/workflows/codeql.yml"
    codeql_text = (
        codeql_path.read_text(encoding="utf-8") if codeql_path.exists() else ""
    )
    for pattern in (
        "github/codeql-action/init",
        "github/codeql-action/analyze",
        "javascript-typescript",
        "python",
    ):
        if pattern not in codeql_text:
            security_failures.append(
                f".github/workflows/codeql.yml missing pattern: {pattern}"
            )
    release_results.append(
        {
            "key": "release_security_disclosure",
            "title": "安全披露与已知边界",
            "status": "pass" if not security_failures else "fail",
            "failures": security_failures,
            "rationale": "开源项目前必须说明漏洞报告方式、演示凭据边界和生产前安全缺口。",
        }
    )

    verification_failures: list[str] = []
    verify_path = ROOT / "scripts/verify_all.sh"
    verify_text = (
        verify_path.read_text(encoding="utf-8") if verify_path.exists() else ""
    )
    for pattern in (
        "scripts/check_platform_readiness.py",
        "scripts/scan_secrets.py",
        "scripts/verify_production_compose.py",
        "pytest production/tests",
        "pytest production/dagster/tests",
        "ruff format --check production/dagster/src production/dagster/tests",
        "ruff check production/dagster/src production/dagster/tests",
        "mypy production/dagster/src",
        "ruff format --check backend scripts production/tests",
        "ruff check backend scripts production/tests",
        "npm --prefix prototype/auris-flow-ui run e2e:ui",
        "AURIS_RUN_E2E",
        "bash scripts/audit_ui.sh",
    ):
        if pattern not in verify_text:
            verification_failures.append(
                f"scripts/verify_all.sh missing release gate: {pattern}"
            )
    real_stack_verify_path = ROOT / "scripts/verify_ui_bff_e2e.sh"
    real_stack_verify_text = (
        real_stack_verify_path.read_text(encoding="utf-8")
        if real_stack_verify_path.exists()
        else ""
    )
    for pattern in ("AURIS_REAL_STACK_E2E", "DEPENDENCY_CHECK_MODE=strict"):
        if pattern not in real_stack_verify_text:
            verification_failures.append(
                f"scripts/verify_ui_bff_e2e.sh missing real-stack gate: {pattern}"
            )
    release_verify_path = ROOT / "scripts/verify_release.sh"
    release_verify_text = (
        release_verify_path.read_text(encoding="utf-8")
        if release_verify_path.exists()
        else ""
    )
    for pattern in (
        "AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1",
        "scripts/verify_release_authorization.py",
        "AURIS_RELEASE_CHECK=1 bash scripts/verify_clean_clone.sh",
        "bash scripts/verify_real_stack.sh",
        "bash scripts/verify_real_dagster.sh",
        "bash scripts/verify_product_dagster_path.sh",
        "bash scripts/verify_production_path.sh",
        "scripts/generate_supply_chain_evidence.py",
        "scripts/finalize_release_evidence.py",
        "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed",
        "AURIS_SKIP_REAL_DAGSTER=1 is not allowed",
        "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed",
        "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed",
    ):
        if pattern not in release_verify_text:
            verification_failures.append(
                f"scripts/verify_release.sh missing release gate: {pattern}"
            )
    verification_failures.extend(validate_release_gate_wiring())
    if (
        "exit 0" in release_verify_text
        and "AURIS_SKIP_REAL_STACK_E2E" in release_verify_text
    ):
        verification_failures.append(
            "scripts/verify_release.sh must not allow AURIS_SKIP_REAL_STACK_E2E to exit 0"
        )
    if (
        "exit 0" in release_verify_text
        and "AURIS_SKIP_REAL_DAGSTER" in release_verify_text
    ):
        verification_failures.append(
            "scripts/verify_release.sh must not allow AURIS_SKIP_REAL_DAGSTER to exit 0"
        )
    if (
        "exit 0" in release_verify_text
        and "AURIS_SKIP_PRODUCT_DAGSTER_GATE" in release_verify_text
    ):
        verification_failures.append(
            "scripts/verify_release.sh must not allow AURIS_SKIP_PRODUCT_DAGSTER_GATE to exit 0"
        )
    if (
        "exit 0" in release_verify_text
        and "AURIS_SKIP_PRODUCTION_PATH_GATE" in release_verify_text
    ):
        verification_failures.append(
            "scripts/verify_release.sh must not allow AURIS_SKIP_PRODUCTION_PATH_GATE to exit 0"
        )
    workflow_path = ROOT / ".github/workflows/verify.yml"
    workflow_text = (
        workflow_path.read_text(encoding="utf-8") if workflow_path.exists() else ""
    )
    if "bash scripts/verify_release.sh" not in workflow_text:
        verification_failures.append(
            ".github/workflows/verify.yml does not run release verification"
        )
    for pattern, failure in (
        ("fetch-depth: 0", "does not fetch full history for release secret scanning"),
        ("persist-credentials: false", "keeps checkout credentials after checkout"),
        ("uv sync --locked --all-extras --project backend", "does not install uv.lock"),
        (
            "permissions:\n  contents: read",
            "does not declare least-privilege permissions",
        ),
    ):
        if pattern not in workflow_text:
            verification_failures.append(f".github/workflows/verify.yml {failure}")
    if (
        "AURIS_REAL_STACK_E2E=1" not in workflow_text
        and "bash scripts/verify_real_stack.sh" not in release_verify_text
    ):
        verification_failures.append(
            ".github/workflows/verify.yml does not run the real dependency stack E2E gate"
        )
    visual_verify_path = ROOT / "scripts/visual_regression.sh"
    visual_verify_text = (
        visual_verify_path.read_text(encoding="utf-8")
        if visual_verify_path.exists()
        else ""
    )
    for pattern in (
        "verify_visual_baseline.py check-execution-policy",
        "verify_visual_baseline.py materialize-locked",
        "verify_visual_baseline.py verify",
        "verify_visual_baseline.py write-manifest",
        "verify_visual_baseline.py write-evidence",
        "runner-contract-sha256",
        "--require-release-runtime",
        "--verify-signature",
        "--runtime-descriptor",
        "--platform linux/amd64",
        "production/visual/visual-baseline.lock.json",
        "build/release-evidence/visual-regression.json",
    ):
        if pattern not in visual_verify_text:
            verification_failures.append(
                f"scripts/visual_regression.sh missing frozen baseline gate: {pattern}"
            )
    if "AURIS_ALLOW_UPDATE_FROZEN_BASELINE" in visual_verify_text:
        verification_failures.append(
            "scripts/visual_regression.sh allows an environment override of the frozen baseline"
        )
    if 'AURIS_VISUAL_RUNTIME: "container"' not in workflow_text:
        verification_failures.append(
            ".github/workflows/verify.yml does not force the pinned visual container runtime"
        )
    if (
        "oras-project/setup-oras@8d34698a59f5ffe24821f0b48ab62a3de8b64b20"
        not in workflow_text
    ):
        verification_failures.append(
            ".github/workflows/verify.yml does not install ORAS for the immutable visual artifact"
        )
    if (
        "sigstore/cosign-installer@d58896d6a1865668819e1d91763c7751a165e159"
        not in workflow_text
    ):
        verification_failures.append(
            ".github/workflows/verify.yml does not install pinned Cosign for visual provenance"
        )
    visual_dockerfile_path = ROOT / "production/visual/Dockerfile"
    visual_dockerfile_text = (
        visual_dockerfile_path.read_text(encoding="utf-8")
        if visual_dockerfile_path.exists()
        else ""
    )
    try:
        visual_runner_image = release_runtime_contract()["runner_image"]
    except (OSError, ValueError) as error:
        verification_failures.append(f"visual runtime contract is invalid: {error}")
    else:
        if f"FROM {visual_runner_image}" not in visual_dockerfile_text:
            verification_failures.append(
                "production/visual/Dockerfile does not use the pinned visual image digest"
            )
    release_results.append(
        {
            "key": "release_verification_gate",
            "title": "发布验证门禁",
            "status": "pass" if not verification_failures else "fail",
            "failures": verification_failures,
            "rationale": "公开发布必须能在 CI 和本地通过同一个质量门禁复现验证结果。",
        }
    )

    return results + release_results


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def validate_release_gate_wiring(root: Path = ROOT) -> list[str]:
    """Require a real top-level production-path command, not a comment or echo."""

    path = root / "scripts" / "verify_release.sh"
    if not path.is_file():
        return ["scripts/verify_release.sh is missing"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return [f"scripts/verify_release.sh is unreadable: {error}"]

    expected = "bash scripts/verify_production_path.sh"
    executable_lines = [
        index for index, line in enumerate(lines) if line.strip() == expected
    ]
    if len(executable_lines) != 1:
        return [
            "scripts/verify_release.sh must execute exactly one top-level "
            f"{expected} command"
        ]
    return []


def validate_production_path_readiness_contract(root: Path = ROOT) -> list[str]:
    """Validate the checked-in gate surface without claiming runtime success."""

    path = root / "production" / "tests" / "production-path-gate.compose.yaml"
    if not path.is_file():
        return []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [f"production path gate contract is unreadable: {error}"]
    if not isinstance(document, dict):
        return ["production path gate contract must be a YAML object"]

    failures: list[str] = []
    contract = document.get("x-auris-production-path-gate")
    if not isinstance(contract, dict):
        return ["production path gate contract metadata is missing"]
    expected_metadata = {
        "schema_version": "auris.production-path-gate-contract.v1",
        "source_compose": "production/compose.yaml",
        "runtime_driver": "scripts/verify_production_path_runtime.py",
    }
    for metadata_field, expected in expected_metadata.items():
        if contract.get(metadata_field) != expected:
            failures.append(f"production path gate {metadata_field} must be {expected}")
    status = contract.get("status")
    if status not in {"blocked", "ready"}:
        failures.append("production path gate status must be blocked or ready")
    missing_capabilities = contract.get("missing_capabilities")
    if status == "blocked" and (
        not isinstance(missing_capabilities, list)
        or not missing_capabilities
        or any(
            not isinstance(item, str) or not item.strip()
            for item in missing_capabilities
        )
    ):
        failures.append(
            "blocked production path gate must name concrete missing capabilities"
        )
    if status == "ready" and missing_capabilities not in (None, []):
        failures.append("ready production path gate cannot retain missing capabilities")
    if set(contract.get("required_external_stubs") or []) != {
        "production-gate-embedding",
        "production-gate-callback",
    }:
        failures.append(
            "production path gate must require both hardened HTTPS test endpoints"
        )

    services = document.get("services")
    services = services if isinstance(services, dict) else {}
    for service_name in (
        "production-gate-embedding",
        "production-gate-callback",
        "production-path-verifier",
    ):
        service = services.get(service_name)
        if not isinstance(service, dict):
            failures.append(f"production path gate service is missing: {service_name}")
            continue
        security_opt = service.get("security_opt")
        user = str(service.get("user") or "").strip().lower()
        if (
            service.get("read_only") is not True
            or service.get("cap_drop") != ["ALL"]
            or security_opt != ["no-new-privileges:true"]
            or not user
            or user in {"0", "0:0", "root", "root:root"}
        ):
            failures.append(
                f"production path gate service is not hardened: {service_name}"
            )
    for service_name in ("bff", "worker"):
        service = services.get(service_name)
        environment = service.get("environment") if isinstance(service, dict) else None
        if not isinstance(environment, dict):
            failures.append(
                f"production path gate environment is missing: {service_name}"
            )
            continue
        expected_environment = {
            "AUTH_PROVIDER": "oidc",
            "ALLOW_DEV_AUTH": "false",
            "AURIS_DAGSTER_ADAPTER": "real",
            "AURIS_OBJECT_STORAGE_ADAPTER": "real",
            "AURIS_QDRANT_ADAPTER": "real",
            "AURIS_EXTERNAL_CALLBACK_ADAPTER": "real",
            "AURIS_EMBEDDING_PROVIDER": "http",
            "OTEL_ENABLED": "true",
        }
        for environment_field, expected in expected_environment.items():
            if environment.get(environment_field) != expected:
                failures.append(
                    f"production path gate {service_name}.{environment_field} must be {expected}"
                )
    return failures


def run_checks() -> list[ReadinessResult]:
    results: list[ReadinessResult] = []
    for check in CHECKS:
        failures: list[str] = []
        for path in check.paths:
            if not (ROOT / path).exists():
                failures.append(f"missing file: {path}")
        for path, patterns in check.contains.items():
            file_path = ROOT / path
            if not file_path.exists():
                continue
            text = read_text(path)
            for pattern in patterns:
                if pattern not in text:
                    failures.append(f"{path} missing pattern: {pattern}")
        if check.key == "one_command_quality_gate":
            failures.extend(validate_release_gate_wiring())
            failures.extend(validate_production_path_readiness_contract())
        results.append(
            {
                "key": check.key,
                "title": check.title,
                "status": "pass" if not failures else "fail",
                "failures": failures,
                "rationale": check.rationale,
            }
        )
    return results


def render_markdown(results: list[ReadinessResult]) -> str:
    passed = sum(1 for item in results if item["status"] == "pass")
    total = len(results)
    lines = [
        "# Auris Flow Platform Readiness",
        "",
        f"Status: {passed}/{total} checks passed",
        "",
        "This report is generated from deterministic repository checks. It verifies that the evaluation, labeling, and insights platform has an open-source project surface, backend contract package, runtime foundation, and regression gate.",
        "",
        "## Checks",
        "",
    ]
    for item in results:
        marker = "PASS" if item["status"] == "pass" else "FAIL"
        lines.append(f"### {marker} - {item['title']}")
        lines.append("")
        lines.append(str(item["rationale"]))
        failures = item["failures"]
        if failures:
            lines.append("")
            lines.append("Failures:")
            for failure in failures:
                lines.append(f"- {failure}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Auris Flow platform readiness.")
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    parser.add_argument("--markdown", action="store_true", help="print markdown report")
    parser.add_argument(
        "--release",
        action="store_true",
        help="include strict public open-source release checks",
    )
    args = parser.parse_args()

    results = run_release_checks() if args.release else run_checks()
    failed = [item for item in results if item["status"] != "pass"]
    if args.json:
        print(
            json.dumps(
                {"mode": "release" if args.release else "baseline", "results": results},
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.markdown:
        print(render_markdown(results), end="")
    else:
        for item in results:
            print(f"{item['status'].upper()} {item['key']}: {item['title']}")
            for failure in item["failures"]:
                print(f"  - {failure}")
        label = (
            "open_source_release_readiness" if args.release else "platform_readiness"
        )
        print(f"{label}: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
