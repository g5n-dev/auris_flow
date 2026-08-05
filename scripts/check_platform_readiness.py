#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
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
from finalize_release_evidence import (  # noqa: E402
    EvidenceError,
    validate_frontend_bundle_lock,
)


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_GITHUB_REPOSITORY = "g5n-dev/auris_flow"
OFFICIAL_GITHUB_REPOSITORY_PARTS = ("g5n-dev", "auris_flow")
OFFICIAL_GITHUB_URL = f"https://github.com/{OFFICIAL_GITHUB_REPOSITORY}"
OFFICIAL_GHCR_REPOSITORY = f"ghcr.io/{OFFICIAL_GITHUB_REPOSITORY}"
APACHE_2_LICENSE_SHA256 = (
    "44a4f8b565b014603e91bd5b2e1b50ae77cc9a7e50215d76b986e9992baba898"
)
EXPECTED_NOTICE = (
    "Auris Flow\n\n"
    "This distribution includes third-party software subject to its own license terms.\n"
    "See THIRD_PARTY_NOTICES.md.\n"
)
PRODUCT_VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
RELEASE_TAG_PATTERN = re.compile(
    rf"v(?P<version>{PRODUCT_VERSION_PATTERN.pattern})(?:-rc\.[1-9]\d*)?"
)
RELEASE_SKIP_VARIABLES = (
    "AURIS_SKIP_REAL_STACK_E2E",
    "AURIS_SKIP_REAL_DAGSTER",
    "AURIS_SKIP_PRODUCT_DAGSTER_GATE",
    "AURIS_SKIP_PRODUCTION_PATH_GATE",
    "AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE",
    "AURIS_SKIP_BACKUP_RESTORE_GATE",
)
RELEASE_SKIP_GUARD_PREAMBLE = (
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "",
    'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
    'cd "${ROOT}"',
    'BUILD_DIR="${ROOT}/build"',
    'EVIDENCE_REL="build/release-evidence"',
    'EVIDENCE_DIR="${ROOT}/${EVIDENCE_REL}"',
    "PRE_IMAGE_ONLY=false",
    "",
)
LEGACY_REPOSITORY_MARKERS = (
    "auris-flow/auris-flow",
    r"auris-flow\/auris-flow",
    '("auris-flow", "auris-flow")',
)
REPOSITORY_TRUST_BINDINGS: dict[str, tuple[str, ...]] = {
    ".github/workflows/release-images.yml": (
        f'if [ "${{GITHUB_REPOSITORY}}" != "{OFFICIAL_GITHUB_REPOSITORY}" ]; then',
        f"release trust policy only permits {OFFICIAL_GITHUB_REPOSITORY}",
        "release tag does not match VERSION",
        'expected_workflow_ref="${GITHUB_REPOSITORY}/.github/workflows/'
        'release-images.yml@${expected_ref}"',
    ),
    "scripts/verify_visual_baseline.py": (
        'OFFICIAL_VISUAL_REPOSITORY = ("g5n-dev", "auris_flow")',
    ),
    "scripts/finalize_release_evidence.py": (
        'OFFICIAL_VISUAL_REPOSITORY = ("g5n-dev", "auris_flow")',
        r"ghcr\.io/g5n-dev/auris_flow/frontend-bundle-candidate@",
        r"ghcr\.io/g5n-dev/auris_flow/frontend-bundle-approval@",
        r"https://github\.com/g5n-dev/auris_flow/\.github/workflows/",
    ),
    "prototype/auris-flow-ui/scripts/frontend-bundle-lock.mjs": (
        'FRONTEND_BUNDLE_OFFICIAL_REPOSITORY = "g5n-dev/auris_flow"',
        r"ghcr\.io\/g5n-dev\/auris_flow\/frontend-bundle-candidate@",
        r"ghcr\.io\/g5n-dev\/auris_flow\/frontend-bundle-approval@",
        r"github\.com\/g5n-dev\/auris_flow\/\.github\/workflows\/",
    ),
    "scripts/verify_frontend_bundle.mjs": (
        "FRONTEND_BUNDLE_OFFICIAL_REPOSITORY,",
        "FRONTEND_BUNDLE_VERIFIER_OFFICIAL_REPOSITORY =",
        "FRONTEND_BUNDLE_OFFICIAL_REPOSITORY;",
    ),
    "scripts/release_bundle.py": (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/",
    ),
    "production/visual/Dockerfile": (
        'org.opencontainers.image.source="https://github.com/g5n-dev/auris_flow"',
    ),
    "production/deployment-bundle.README.md": (
        "https://github.com/g5n-dev/auris_flow/.github/workflows/"
        "release-images.yml@refs/tags/${RELEASE_TAG}",
    ),
    "doc/runbooks/release-supply-chain.md": ("GitHub repository `g5n-dev/auris_flow`",),
}


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
                "`v1.0.0` 的候选实现",
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
                "dagster-storage-bootstrap:",
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
            "scripts/verify_audio_import_stack.sh",
            "scripts/verify_audio_import_stack.py",
            "scripts/verify_audio_import_browser_e2e.sh",
            "scripts/verify_production_mysql_migrations.sh",
            "backend/scripts/verify_mysql_migration_security.py",
            "production/tests/test_mysql_migration_security.py",
            "scripts/verify_clean_clone.sh",
            "scripts/verify_license_materials.py",
            "scripts/verify_static.sh",
            "scripts/verify_backend.sh",
            "scripts/verify_production_tests.sh",
            "scripts/verify_dagster_tests.sh",
            "scripts/verify_frontend.sh",
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
                "bash scripts/verify_static.sh",
                "bash scripts/verify_backend.sh",
                "bash scripts/verify_production_tests.sh",
                "bash scripts/verify_dagster_tests.sh",
                "bash scripts/verify_frontend.sh",
                "backend-runtime-requirements.txt",
                "dagster-runtime-requirements.txt",
                "--strict --require-hashes --disable-pip",
                "npm audit --prefix prototype/auris-flow-ui",
                "npm audit signatures --prefix prototype/auris-flow-ui",
                "AURIS_RUN_E2E",
            ),
            "scripts/verify_static.sh": (
                "validate_backend_spec.py",
                "scripts/scan_secrets.py",
                "uv sync --check --locked --all-extras --project backend",
                "uv sync --check --locked --all-extras --project production/dagster",
                "scripts/verify_production_compose.py",
                "scripts/verify_github_actions_pins.py",
                "ruff format --check backend scripts production/tests",
                "ruff check backend scripts production/tests",
                "mypy",
            ),
            "scripts/verify_backend.sh": (
                "verify_migrations.py",
                "backend/tests/unit backend/tests/contract backend/tests/integration",
                "smoke_backend.py",
            ),
            "scripts/verify_production_tests.sh": ("pytest production/tests",),
            "scripts/verify_dagster_tests.sh": (
                "pytest production/dagster/tests",
                "ruff format --check production/dagster/src production/dagster/tests",
                "ruff check production/dagster/src production/dagster/tests",
                "mypy production/dagster/src",
            ),
            "scripts/verify_frontend.sh": (
                "npm --prefix prototype/auris-flow-ui run build",
                "npm --prefix prototype/auris-flow-ui run e2e:ui",
            ),
            "scripts/verify_release.sh": (
                "AURIS_RELEASE_CHECK=1 AURIS_RUN_E2E=1 bash scripts/verify_all.sh",
                "bash scripts/verify_real_stack.sh",
                "bash scripts/verify_real_dagster.sh",
                "bash scripts/verify_product_dagster_path.sh",
                "bash scripts/verify_production_path.sh",
                "bash scripts/verify_audio_import_stack.sh",
                "bash scripts/verify_production_mysql_migrations.sh",
                "AURIS_SKIP_REAL_DAGSTER=1 is not allowed",
                "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed",
                "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed",
                "AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE=1 is not allowed",
                "AURIS_RELEASE_CHECK=1 bash scripts/verify_clean_clone.sh",
                "scripts/verify_license_materials.py",
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
    "VERSION",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    ".github/workflows/release-images.yml",
    "config/release/exact-artifact-license-conclusions.json",
    "third_party/licenses/README.md",
    "third_party/licenses/antlr4-python3-runtime-4.13.2.LICENSE.txt",
    "third_party/licenses/python-dateutil-2.9.0.post0.LICENSE",
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
    "production/frontend/frontend-bundle.lock.json",
    ".github/workflows/visual-baseline-build.yml",
    ".github/workflows/visual-baseline-promotion.yml",
    ".github/workflows/frontend-bundle-candidate.yml",
    ".github/workflows/frontend-bundle-promotion.yml",
    "prototype/auris-flow-ui/scripts/frontend-bundle-candidate.mjs",
    "prototype/auris-flow-ui/scripts/frontend-bundle-candidate-cli.mjs",
    "prototype/auris-flow-ui/scripts/frontend-bundle-lock.mjs",
    "prototype/auris-flow-ui/scripts/frontend-bundle-candidate.test.mjs",
    "prototype/auris-flow-ui/scripts/frontend-bundle-lock.test.mjs",
    "scripts/verify_frontend_bundle.mjs",
    "scripts/verify_frontend_bundle.test.mjs",
    "scripts/promote_visual_baseline.sh",
    "scripts/tests/test_frontend_bundle_readiness.py",
    "scripts/tests/test_verify_clean_clone.py",
    "scripts/tests/test_finalize_release_evidence.py",
    "scripts/tests/test_license_materials.py",
    "scripts/tests/test_verify_visual_baseline.py",
    "scripts/verify_clean_clone.sh",
    "scripts/verify_license_materials.py",
    "scripts/verify_github_actions_pins.py",
    "security/github-actions-lock.json",
    "security/codeql-exceptions.json",
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
    "scripts/release_bundle.py",
    "scripts/verify_production_compose.py",
    "scripts/generate_supply_chain_evidence.py",
    "scripts/tests/test_platform_readiness_git_tree.py",
    "backend/tests/unit/test_release_quality_gate_policy.py",
    "scripts/render_release_compose.py",
    "scripts/validate_public_audio_datasets.py",
    "doc/backend-spec/public-audio-datasets-v0.1.json",
    "production/compose.yaml",
    "production/backend/Dockerfile",
    "production/dagster/Dockerfile",
    "production/dagster/dagster-entrypoint.sh",
    "production/dagster/pyproject.toml",
    "production/dagster/uv.lock",
    "production/dagster/src/auris_flow_dagster/definitions.py",
    "production/edge/Dockerfile",
    "production/edge/nginx.conf",
    "production/observability/alerts.yaml",
    "production/observability/otel-collector.yaml",
    "production/observability/prometheus.yaml",
    "production/scripts/backup.sh",
    "production/scripts/scheduled-backup.sh",
    "production/scripts/restore.sh",
    "production/scripts/verify-backup.sh",
    "production/systemd/auris-flow-backup.service",
    "production/systemd/auris-flow-backup.timer",
    "production/systemd/backup.env.example",
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


def git_historical_files(
    root: Path = ROOT,
    ref: str = "HEAD",
) -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "log", "--format=", "--name-only", "-z", ref),
        cwd=root,
        check=True,
        capture_output=True,
        text=False,
    )
    return tuple(
        sorted({item.decode("utf-8") for item in completed.stdout.split(b"\0") if item})
    )


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


def historical_release_artifacts(
    root: Path = ROOT,
    ref: str = "HEAD",
) -> tuple[str, ...]:
    return tuple(
        path for path in git_historical_files(root, ref) if is_release_artifact(path)
    )


def validate_frontend_bundle_release_lock(lock: object) -> list[str]:
    if not isinstance(lock, dict):
        return ["frontend bundle lock must be an object"]
    if lock.get("status") != "APPROVED":
        return [
            "status is PENDING; no independently approved immutable candidate exists"
        ]
    try:
        validate_frontend_bundle_lock(lock)
    except EvidenceError as error:
        return [str(error)]
    return []


def validate_apache_2_license(root: Path = ROOT) -> list[str]:
    license_path = root / "LICENSE"
    if not license_path.is_file():
        return ["missing canonical Apache License 2.0 file: LICENSE"]
    try:
        digest = hashlib.sha256(license_path.read_bytes()).hexdigest()
    except OSError as error:
        return [f"LICENSE is unreadable: {error}"]
    if digest != APACHE_2_LICENSE_SHA256:
        return ["LICENSE must be the unmodified canonical Apache License 2.0 text"]
    return []


def validate_license_materials(root: Path = ROOT) -> list[str]:
    notice_path = root / "NOTICE"
    third_party_path = root / "THIRD_PARTY_NOTICES.md"
    failures: list[str] = []
    failures.extend(validate_apache_2_license(root))
    if not notice_path.is_file():
        failures.append("missing NOTICE")
    else:
        try:
            notice = notice_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            failures.append(f"NOTICE is unreadable: {error}")
        else:
            if notice != EXPECTED_NOTICE:
                failures.append("NOTICE must use the concise canonical project notice")
    if not third_party_path.is_file():
        failures.append("missing THIRD_PARTY_NOTICES.md")
    else:
        try:
            third_party = third_party_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            failures.append(f"THIRD_PARTY_NOTICES.md is unreadable: {error}")
        else:
            for marker in (
                "# Third-Party Notices",
                "Runtime and build dependencies",
                "Public datasets",
                "exact-artifact",
            ):
                if marker not in third_party:
                    failures.append(
                        f"THIRD_PARTY_NOTICES.md is missing inventory marker: {marker}"
                    )
    return failures


def release_tag_matches_version(tag: str, version: str) -> bool:
    if PRODUCT_VERSION_PATTERN.fullmatch(version) is None:
        return False
    match = RELEASE_TAG_PATTERN.fullmatch(tag)
    return match is not None and match.group("version") == version


def _required_version_source(
    root: Path,
    relative_path: str,
    failures: list[str],
) -> str | None:
    path = root / relative_path
    if not path.is_file():
        failures.append(f"release version source is missing: {relative_path}")
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        failures.append(
            f"release version source is unreadable: {relative_path}: {error}"
        )
        return None


def _toml_component_versions(
    source: str,
    *,
    expected_name: str,
    lock: bool,
) -> tuple[tuple[str, str], ...]:
    document = tomllib.loads(source)
    if lock:
        packages = document.get("package")
        matches = (
            [
                package
                for package in packages
                if isinstance(package, dict) and package.get("name") == expected_name
            ]
            if isinstance(packages, list)
            else []
        )
        if len(matches) != 1:
            raise ValueError(f"must contain exactly one {expected_name} package")
        component = matches[0]
    else:
        project = document.get("project")
        if not isinstance(project, dict) or project.get("name") != expected_name:
            raise ValueError(f"[project].name must be {expected_name}")
        component = project
    version = component.get("version")
    if not isinstance(version, str):
        raise ValueError(f"{expected_name} version must be a string")
    return (("", version),)


def _json_component_versions(
    source: str,
    expected_name: str,
    lock: bool,
) -> tuple[tuple[str, str], ...]:
    document = json.loads(source)
    if not isinstance(document, dict) or document.get("name") != expected_name:
        raise ValueError(f"name must be {expected_name}")
    version = document.get("version")
    if not isinstance(version, str):
        raise ValueError("version must be a string")
    values: list[tuple[str, str]] = [("", version)]
    if lock:
        packages = document.get("packages")
        root_package = packages.get("") if isinstance(packages, dict) else None
        lock_version = (
            root_package.get("version") if isinstance(root_package, dict) else None
        )
        if (
            not isinstance(root_package, dict)
            or root_package.get("name") != expected_name
            or not isinstance(lock_version, str)
        ):
            raise ValueError(
                f"packages[''] must identify {expected_name} with a version"
            )
        values.append((" packages['']", lock_version))
    return tuple(values)


def _python_literal_version(
    source: str,
    relative_path: str,
    assignment_name: str,
    call_keyword: tuple[str, str] | None = None,
) -> str:
    module = ast.parse(source, filename=relative_path)
    values: list[ast.expr] = []
    for node in ast.walk(module):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        else:
            targets = [node.target]
            value = node.value
        if value is None or not any(
            isinstance(target, ast.Name) and target.id == assignment_name
            for target in targets
        ):
            continue
        if call_keyword is None:
            values.append(value)
        elif isinstance(value, ast.Call):
            function = value.func
            actual_call = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            if actual_call == call_keyword[0]:
                values.extend(
                    keyword.value
                    for keyword in value.keywords
                    if keyword.arg == call_keyword[1]
                )
    if len(values) != 1:
        raise ValueError(f"must define one literal {assignment_name} version source")
    value = ast.literal_eval(values[0])
    if not isinstance(value, str):
        raise ValueError(f"{assignment_name} version source must be a string literal")
    return value


def _record_component_versions(
    failures: list[str],
    relative_path: str,
    product_version: str,
    values: tuple[tuple[str, str], ...],
) -> None:
    failures.extend(
        f"{relative_path}{suffix} version {version!r} does not match "
        f"VERSION {product_version!r}"
        for suffix, version in values
        if version != product_version
    )


def validate_release_version_contract(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    version_source = _required_version_source(root, "VERSION", failures)
    if version_source is None:
        return failures
    if re.fullmatch(rf"{PRODUCT_VERSION_PATTERN.pattern}\n?", version_source) is None:
        return ["VERSION must contain one stable SemVer value"]
    product_version = version_source.removesuffix("\n")
    expected_api_prefix = f"/api/v{product_version.split('.', 1)[0]}"

    def check(
        relative_path: str,
        parser: Callable[[str], tuple[tuple[str, str], ...]],
    ) -> None:
        source = _required_version_source(root, relative_path, failures)
        if source is None:
            return
        try:
            values = parser(source)
        except (SyntaxError, TypeError, ValueError, yaml.YAMLError) as error:
            failures.append(
                f"release version source is invalid: {relative_path}: {error}"
            )
            return
        _record_component_versions(
            failures,
            relative_path,
            product_version,
            values,
        )

    for relative_path, expected_name, lock in (
        ("backend/pyproject.toml", "auris-flow-bff", False),
        ("backend/uv.lock", "auris-flow-bff", True),
        ("production/dagster/pyproject.toml", "auris-flow-dagster", False),
        ("production/dagster/uv.lock", "auris-flow-dagster", True),
    ):
        check(
            relative_path,
            partial(
                _toml_component_versions,
                expected_name=expected_name,
                lock=lock,
            ),
        )

    for relative_path, lock in (
        ("prototype/auris-flow-ui/package.json", False),
        ("prototype/auris-flow-ui/package-lock.json", True),
    ):
        check(
            relative_path,
            partial(
                _json_component_versions,
                expected_name="auris-flow-ui",
                lock=lock,
            ),
        )

    check(
        "backend/app/main.py",
        lambda source: (
            (
                "",
                _python_literal_version(
                    source,
                    "backend/app/main.py",
                    "app",
                    ("FastAPI", "version"),
                ),
            ),
        ),
    )

    def api_prefix(source: str) -> tuple[tuple[str, str], ...]:
        actual = _python_literal_version(
            source,
            "backend/app/core/config.py",
            "api_prefix",
        )
        if actual != expected_api_prefix:
            raise ValueError(
                f"API major does not match VERSION: "
                f"expected {expected_api_prefix}, found {actual}"
            )
        return ()

    check("backend/app/core/config.py", api_prefix)

    def openapi(source: str) -> tuple[tuple[str, str], ...]:
        document = yaml.safe_load(source)
        if not isinstance(document, dict):
            raise ValueError("root must be an object")
        info, servers = document.get("info"), document.get("servers")
        version = info.get("version") if isinstance(info, dict) else None
        urls = (
            {
                server.get("url")
                for server in servers
                if isinstance(server, dict) and isinstance(server.get("url"), str)
            }
            if isinstance(servers, list)
            else set()
        )
        if not isinstance(version, str):
            raise ValueError("info.version must be a string")
        if expected_api_prefix not in urls:
            raise ValueError(
                f"API major does not match VERSION: "
                f"expected server {expected_api_prefix}"
            )
        return (("", version),)

    check("doc/backend-spec/openapi-v0.1.yaml", openapi)
    return failures


def _python_assignment(
    root: Path,
    relative_path: str,
    name: str,
) -> object:
    path = root / relative_path
    module = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
    values: list[ast.expr] = []
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in statement.targets
            ):
                values.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
            and statement.value is not None
        ):
            values.append(statement.value)
    if len(values) != 1:
        raise ValueError(f"{relative_path} must assign {name} exactly once")
    value = values[0]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "compile"
        and value.args
    ):
        value = value.args[0]
    return ast.literal_eval(value)


def _function_argument_names(module: ast.Module, function_name: str) -> tuple[str, ...]:
    functions = [
        statement
        for statement in module.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == function_name
    ]
    if len(functions) != 1:
        raise ValueError(f"must define {function_name} exactly once")
    arguments = functions[0].args
    return tuple(
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    )


def _shell_command(run_block: str, prefix: str) -> tuple[str, ...]:
    lines = [line.strip() for line in run_block.splitlines()]
    starts = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(starts) != 1:
        return ()
    command: list[str] = []
    for line in lines[starts[0] :]:
        command.append(line)
        if not line.endswith("\\"):
            break
    return tuple(command)


def _validate_frontend_repository_module(root: Path) -> list[str]:
    module_path = (
        root / "prototype/auris-flow-ui/scripts/frontend-bundle-lock.mjs"
    ).resolve()
    official_candidate = (
        f"{OFFICIAL_GHCR_REPOSITORY}/frontend-bundle-candidate@sha256:" + ("a" * 64)
    )
    official_approval = (
        f"{OFFICIAL_GHCR_REPOSITORY}/frontend-bundle-approval@sha256:" + ("b" * 64)
    )
    official_candidate_identity = (
        f"{OFFICIAL_GITHUB_URL}/.github/workflows/"
        "frontend-bundle-candidate.yml@refs/heads/main"
    )
    official_approval_identity = (
        f"{OFFICIAL_GITHUB_URL}/.github/workflows/"
        "frontend-bundle-promotion.yml@refs/heads/main"
    )
    probe = f"""
const module = await import({json.dumps(module_path.as_uri())});
const patterns = module.frontendBundleLockPatterns;
const result = {{
  repository: module.FRONTEND_BUNDLE_OFFICIAL_REPOSITORY,
  officialCandidate: patterns.officialArtifactPattern.test(
    {json.dumps(official_candidate)}
  ),
  officialApproval: patterns.officialApprovalArtifactPattern.test(
    {json.dumps(official_approval)}
  ),
  officialCandidateIdentity: patterns.officialIdentityPattern.test(
    {json.dumps(official_candidate_identity)}
  ),
  officialApprovalIdentity: patterns.officialApprovalIdentityPattern.test(
    {json.dumps(official_approval_identity)}
  ),
  foreignCandidate: patterns.officialArtifactPattern.test(
    {json.dumps(official_candidate.replace("g5n-dev/auris_flow", "attacker/repo"))}
  ),
  foreignApproval: patterns.officialApprovalArtifactPattern.test(
    {json.dumps(official_approval.replace("g5n-dev/auris_flow", "attacker/repo"))}
  ),
  foreignCandidateIdentity: patterns.officialIdentityPattern.test(
    {json.dumps(official_candidate_identity.replace("g5n-dev/auris_flow", "attacker/repo"))}
  ),
  foreignApprovalIdentity: patterns.officialApprovalIdentityPattern.test(
    {json.dumps(official_approval_identity.replace("g5n-dev/auris_flow", "attacker/repo"))}
  )
}};
process.stdout.write(JSON.stringify(result));
"""
    try:
        completed = subprocess.run(
            ("node", "--input-type=module", "--eval", probe),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [f"unable to evaluate frontend repository trust contract: {error}"]
    if completed.returncode != 0:
        return ["frontend repository trust module could not be evaluated"]
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ["frontend repository trust module returned invalid JSON"]
    expected = {
        "repository": OFFICIAL_GITHUB_REPOSITORY,
        "officialCandidate": True,
        "officialApproval": True,
        "officialCandidateIdentity": True,
        "officialApprovalIdentity": True,
        "foreignCandidate": False,
        "foreignApproval": False,
        "foreignCandidateIdentity": False,
        "foreignApprovalIdentity": False,
    }
    if result != expected:
        return ["frontend repository trust patterns do not fail closed"]
    return []


def validate_repository_trust_contract(root: Path = ROOT) -> list[str]:
    failures: list[str] = []
    sources: dict[str, str] = {}
    for relative_path, required_tokens in REPOSITORY_TRUST_BINDINGS.items():
        path = root / relative_path
        if not path.is_file():
            failures.append(f"repository trust source is missing: {relative_path}")
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as error:
            failures.append(
                f"repository trust source is unreadable: {relative_path}: {error}"
            )
            continue
        sources[relative_path] = source
        for token in required_tokens:
            if token not in source:
                failures.append(f"repository trust binding drifted: {relative_path}")
                break
        if any(marker in source for marker in LEGACY_REPOSITORY_MARKERS):
            failures.append(
                f"legacy repository identity remains in trust source: {relative_path}"
            )

    workflow_source = sources.get(".github/workflows/release-images.yml")
    if workflow_source is not None:
        workflow: dict[object, object] | None = None
        try:
            workflow_payload = yaml.safe_load(workflow_source)
            if not isinstance(workflow_payload, dict):
                raise TypeError
            jobs_payload = workflow_payload.get("jobs")
            if not isinstance(jobs_payload, dict):
                raise TypeError
            release_context = jobs_payload.get("release-context")
            if not isinstance(release_context, dict):
                raise TypeError
            steps = release_context.get("steps")
            if not isinstance(steps, list):
                raise TypeError
            workflow = workflow_payload
        except (KeyError, TypeError, yaml.YAMLError):
            failures.append("release workflow repository guard is structurally invalid")
        else:
            context_steps = [
                step
                for step in steps
                if isinstance(step, dict) and step.get("id") == "context"
            ]
            if len(context_steps) != 1 or not isinstance(
                context_steps[0].get("run"), str
            ):
                failures.append(
                    "release workflow must define one active repository context guard"
                )
            else:
                active_lines = [
                    line.strip()
                    for line in context_steps[0]["run"].splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
                guard = (
                    f'if [ "${{GITHUB_REPOSITORY}}" != '
                    f'"{OFFICIAL_GITHUB_REPOSITORY}" ]; then'
                )
                guard_indexes = [
                    index for index, line in enumerate(active_lines) if line == guard
                ]
                if len(guard_indexes) != 1:
                    failures.append("release workflow repository guard is not exact")
                else:
                    index = guard_indexes[0]
                    if active_lines[index + 2 : index + 4] != ["exit 2", "fi"]:
                        failures.append(
                            "release workflow repository guard does not fail closed"
                        )
                for required_line in (
                    'expected_workflow_ref="${GITHUB_REPOSITORY}/.github/workflows/'
                    'release-images.yml@${expected_ref}"',
                    'if [ "${GITHUB_WORKFLOW_REF}" != "${expected_workflow_ref}" ]; then',
                ):
                    if required_line not in active_lines:
                        failures.append(
                            "release workflow ref is not bound to the guarded repository"
                        )
                        break

        try:
            if workflow is None:
                raise TypeError
            jobs = workflow.get("jobs")
            if not isinstance(jobs, dict):
                raise TypeError
            assembly_steps = [
                step
                for job in jobs.values()
                if isinstance(job, dict)
                for step in job.get("steps", [])
                if isinstance(step, dict)
                and step.get("name")
                == "Assemble and sign the verified production release bundle"
            ]
        except (KeyError, TypeError, AttributeError):
            assembly_steps = []
        if len(assembly_steps) != 1:
            failures.append("release bundle assembly step is missing or duplicated")
        else:
            assembly = assembly_steps[0]
            environment = assembly.get("env")
            if (
                not isinstance(environment, dict)
                or environment.get("WORKFLOW_IDENTITY")
                != "https://github.com/${{ github.workflow_ref }}"
            ):
                failures.append(
                    "release bundle assembly signer is not bound to github.workflow_ref"
                )
            run_block = assembly.get("run")
            command = (
                _shell_command(
                    run_block,
                    "python3 scripts/release_bundle.py verify",
                )
                if isinstance(run_block, str)
                else ()
            )
            if not command or "--verify-signature \\" not in command:
                failures.append(
                    "release bundle assembly does not verify the signed bundle"
                )
            if any("--certificate-identity" in line for line in command):
                failures.append(
                    "release bundle verifier exposes a workflow identity override"
                )

    try:
        visual_repository = _python_assignment(
            root,
            "scripts/verify_visual_baseline.py",
            "OFFICIAL_VISUAL_REPOSITORY",
        )
        finalizer_visual_repository = _python_assignment(
            root,
            "scripts/finalize_release_evidence.py",
            "OFFICIAL_VISUAL_REPOSITORY",
        )
        release_workflow_prefix = _python_assignment(
            root,
            "scripts/release_bundle.py",
            "OFFICIAL_RELEASE_WORKFLOW_PREFIX",
        )
    except (OSError, SyntaxError, ValueError) as error:
        failures.append(f"Python repository trust constants are invalid: {error}")
    else:
        if (
            visual_repository != OFFICIAL_GITHUB_REPOSITORY_PARTS
            or finalizer_visual_repository != OFFICIAL_GITHUB_REPOSITORY_PARTS
        ):
            failures.append(
                "visual repository trust root does not match publication target"
            )
        expected_prefix = (
            f"{OFFICIAL_GITHUB_URL}/.github/workflows/release-images.yml@refs/tags/"
        )
        if release_workflow_prefix != expected_prefix:
            failures.append(
                "release bundle workflow identity does not match publication target"
            )

    finalizer_patterns = {
        "FRONTEND_CANDIDATE_OCI_REF_PATTERN": (
            f"{OFFICIAL_GHCR_REPOSITORY}/frontend-bundle-candidate@sha256:" + ("a" * 64)
        ),
        "FRONTEND_APPROVAL_OCI_REF_PATTERN": (
            f"{OFFICIAL_GHCR_REPOSITORY}/frontend-bundle-approval@sha256:" + ("b" * 64)
        ),
        "FRONTEND_CANDIDATE_SIGNATURE_IDENTITY_PATTERN": (
            f"{OFFICIAL_GITHUB_URL}/.github/workflows/"
            "frontend-bundle-candidate.yml@refs/heads/main"
        ),
        "FRONTEND_APPROVAL_SIGNATURE_IDENTITY_PATTERN": (
            f"{OFFICIAL_GITHUB_URL}/.github/workflows/"
            "frontend-bundle-promotion.yml@refs/heads/main"
        ),
    }
    for name, official_value in finalizer_patterns.items():
        try:
            pattern = _python_assignment(
                root,
                "scripts/finalize_release_evidence.py",
                name,
            )
            if not isinstance(pattern, str):
                raise TypeError("compiled pattern must be a string")
            compiled = re.compile(pattern)
        except (OSError, SyntaxError, TypeError, ValueError, re.error) as error:
            failures.append(
                f"finalizer repository trust pattern is invalid: {name}: {error}"
            )
            continue
        foreign_value = official_value.replace(
            OFFICIAL_GITHUB_REPOSITORY,
            "attacker/repo",
        )
        if compiled.fullmatch(official_value) is None or compiled.fullmatch(
            foreign_value
        ):
            failures.append(
                f"finalizer repository trust pattern does not fail closed: {name}"
            )

    release_bundle_path = root / "scripts/release_bundle.py"
    if release_bundle_path.is_file():
        try:
            release_bundle_module = ast.parse(
                release_bundle_path.read_text(encoding="utf-8"),
                filename="scripts/release_bundle.py",
            )
            for function_name in (
                "verify_bundle_signature",
                "verify_restore_source",
                "verify_running_images",
            ):
                if "certificate_identity" in _function_argument_names(
                    release_bundle_module,
                    function_name,
                ):
                    failures.append(
                        f"{function_name} exposes a certificate identity override"
                    )
            for node in ast.walk(release_bundle_module):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "--certificate-identity"
                ):
                    failures.append(
                        "release bundle CLI exposes a certificate identity override"
                    )
        except (OSError, SyntaxError, ValueError) as error:
            failures.append(f"release bundle trust API is invalid: {error}")

    failures.extend(_validate_frontend_repository_module(root))
    return failures


def validate_release_skip_guards(source: str) -> list[str]:
    """Validate each release-only skip guard without conflating unrelated exits."""

    failures: list[str] = []
    lines = source.splitlines()
    preamble_size = len(RELEASE_SKIP_GUARD_PREAMBLE)
    canonical_preamble = tuple(lines[:preamble_size]) == RELEASE_SKIP_GUARD_PREAMBLE
    if not canonical_preamble:
        failures.append(
            "scripts/verify_release.sh release skip guards must follow the "
            "canonical top-level preamble"
        )
    expected_guard_index = preamble_size
    guard_indexes: list[int] = []
    for variable in RELEASE_SKIP_VARIABLES:
        guard = f'if [ "${{{variable}:-0}}" = "1" ]; then'
        indexes = [index for index, line in enumerate(lines) if line == guard]
        if len(indexes) != 1:
            failures.append(
                f"scripts/verify_release.sh is missing the {variable} fail-closed guard"
            )
            continue
        guard_index = indexes[0]
        guard_indexes.append(guard_index)
        if not canonical_preamble or guard_index != expected_guard_index:
            failures.append(
                f"scripts/verify_release.sh must keep {variable} in the "
                "canonical top-level guard section"
            )
        try:
            closing_index = lines.index("fi", guard_index + 1)
        except ValueError:
            failures.append(
                f"scripts/verify_release.sh has an invalid {variable} fail-closed guard"
            )
            continue
        body = lines[guard_index + 1 : closing_index]
        literal_error = re.compile(r'  echo "[^"\\`$]*" >&2')
        if (
            not body
            or body[-1] != "  exit 2"
            or any(literal_error.fullmatch(line) is None for line in body[:-1])
            or not body[:-1]
        ):
            failures.append(
                f"scripts/verify_release.sh must reject {variable} "
                "with an unconditional exit 2"
            )
        expected_guard_index = closing_index + 1
    if guard_indexes != sorted(guard_indexes):
        failures.append(
            "scripts/verify_release.sh release skip guards are not in canonical order"
        )
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
    else:
        license_failures.extend(validate_apache_2_license())
    readme_path = ROOT / "README.md"
    if readme_path.exists() and "Apache License 2.0" not in readme_path.read_text(
        encoding="utf-8"
    ):
        license_failures.append("README.md does not declare Apache License 2.0")
    license_failures.extend(validate_license_materials())
    release_results.append(
        {
            "key": "release_license",
            "title": "许可证与第三方材料",
            "status": "pass" if not license_failures else "fail",
            "failures": license_failures,
            "rationale": "发行树只公开标准许可证、简洁 NOTICE、第三方清单与制品级许可证证据。",
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
        historical_artifacts = historical_release_artifacts()
    except (OSError, subprocess.CalledProcessError) as error:
        hygiene_failures.append(
            f"unable to inspect Git release artifact history: {error}"
        )
        tracked_artifacts = []
        historical_artifacts = ()
    for artifact in tracked_artifacts:
        hygiene_failures.append(
            f"generated or local-only artifact is tracked: {artifact}"
        )
    for artifact in historical_artifacts:
        if artifact not in tracked_artifacts:
            hygiene_failures.append(
                "generated or local-only artifact exists in published Git history: "
                f"{artifact}"
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
    tree_failures.extend(validate_repository_trust_contract())
    tree_failures.extend(validate_release_version_contract())

    verify_release_text = (ROOT / "scripts/verify_release.sh").read_text(
        encoding="utf-8"
    )
    if verify_release_text.count("verify_frontend_bundle.mjs verify-release") != 1:
        tree_failures.append(
            "strict release gate must invoke frontend bundle online verification once"
        )
    if "frontend-bundle.json" not in verify_release_text:
        tree_failures.append(
            "strict release gate does not produce frontend bundle evidence"
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

    promotion_failures = [
        f"visual baseline lock: {failure}"
        for failure in validate_visual_baseline_lock(
            ROOT / "production/visual/visual-baseline.lock.json",
            require_approved=True,
        )
    ]
    frontend_lock_path = ROOT / "production/frontend/frontend-bundle.lock.json"
    try:
        frontend_lock = json.loads(frontend_lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        promotion_failures.append(f"frontend bundle lock: unable to read lock: {error}")
    else:
        promotion_failures.extend(
            f"frontend bundle lock: {failure}"
            for failure in validate_frontend_bundle_release_lock(frontend_lock)
        )
    release_results.append(
        {
            "key": "release_promotion_evidence",
            "title": "前端与视觉提升证据",
            "status": "pass" if not promotion_failures else "fail",
            "failures": promotion_failures,
            "rationale": "源码树完整性与不可变前端/视觉制品审批分别报告，避免工作区状态掩盖真实 promotion 缺口。",
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
    verification_paths = (
        "scripts/verify_all.sh",
        "scripts/verify_static.sh",
        "scripts/verify_backend.sh",
        "scripts/verify_production_tests.sh",
        "scripts/verify_dagster_tests.sh",
        "scripts/verify_frontend.sh",
    )
    verify_text = "\n".join(
        (ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in verification_paths
        if (ROOT / relative_path).is_file()
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
        "scripts/verify_license_materials.py",
        "AURIS_RELEASE_CHECK=1 bash scripts/verify_clean_clone.sh",
        "bash scripts/verify_real_stack.sh",
        "bash scripts/verify_real_dagster.sh",
        "bash scripts/verify_product_dagster_path.sh",
        "bash scripts/verify_production_path.sh",
        "bash scripts/verify_audio_import_stack.sh",
        "scripts/generate_supply_chain_evidence.py",
        "scripts/finalize_release_evidence.py",
        "AURIS_SKIP_REAL_STACK_E2E=1 is not allowed",
        "AURIS_SKIP_REAL_DAGSTER=1 is not allowed",
        "AURIS_SKIP_PRODUCT_DAGSTER_GATE=1 is not allowed",
        "AURIS_SKIP_PRODUCTION_PATH_GATE=1 is not allowed",
        "AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE=1 is not allowed",
        "AURIS_SKIP_BACKUP_RESTORE_GATE=1 is not allowed",
    ):
        if pattern not in release_verify_text:
            verification_failures.append(
                f"scripts/verify_release.sh missing release gate: {pattern}"
            )
    verification_failures.extend(validate_release_gate_wiring())
    verification_failures.extend(validate_release_skip_guards(release_verify_text))
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
    """Require each governed runtime gate as one real top-level command."""

    path = root / "scripts" / "verify_release.sh"
    if not path.is_file():
        return ["scripts/verify_release.sh is missing"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return [f"scripts/verify_release.sh is unreadable: {error}"]

    failures: list[str] = []
    for expected in (
        "bash scripts/verify_production_path.sh",
        "bash scripts/verify_audio_import_stack.sh",
    ):
        executable_lines = [
            index for index, line in enumerate(lines) if line.strip() == expected
        ]
        if len(executable_lines) != 1:
            failures.append(
                "scripts/verify_release.sh must execute exactly one top-level "
                f"{expected} command"
            )
    return failures


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
