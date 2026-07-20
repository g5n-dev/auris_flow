# Contributing

感谢参与 Auris Flow。当前项目仍处于高保真原型和后端开发基线阶段，贡献应优先保持契约一致、可验证和可回滚。

## Development Rules

- 不要绕过 `tenant_id`、`project_id`、`trace_id`、`request_id`。
- 写操作必须考虑幂等、审计、错误 envelope 和可重试状态。
- Agent 或异步任务不能直接覆盖线上结果，只能写候选、草稿、人审任务、评测运行或回填草稿。
- 前端不要直接访问 MySQL、Qdrant、对象存储或 Dagster。
- 不要在业务页面暴露 Qdrant collection、裸 Dagster UI、明文 secret 或签名 URL。
- MySQL 是权威业务存储；Redis/Qdrant 不得成为不可恢复的唯一事实来源。不要把 ClickHouse 加入
  默认或推荐基线。
- `prod/release` 不得回退到 dev auth、fake adapter、确定性 embedding、通配 CORS/TrustedHost 或
  弱/demo secret；配置错误必须 fail closed。
- 新增依赖、镜像、公开数据集或素材时同步 lockfile、SBOM/许可证据、`THIRD_PARTY_NOTICES.md` 和
  来源/按需下载边界。
- 改动生产配置、指标、告警、迁移或恢复格式时，同步 `production/README.md`、对应 Runbook、
  CHANGELOG 和 release compatibility notes。

## Before Submitting Changes

运行：

```bash
bash scripts/verify_all.sh
```

至少确认：

- backend-spec 静态校验通过。
- 后端 unit / contract / integration tests 通过。
- 后端 smoke 通过。
- 前端 `npm run build` 通过。

涉及依赖、迁移、构建或发布树的变更，还必须从清洁且已提交的 HEAD 运行锁定复现门禁；
`verify_all.sh` 复用当前工作区环境，不能替代该证明：

```bash
bash scripts/verify_clean_clone.sh
```

涉及 production/release 的变更还应按风险运行：

```bash
python3 scripts/check_platform_readiness.py --release
python3 scripts/scan_secrets.py
bash scripts/verify_release.sh
```

`verify_real_stack.sh` 的开发栈仍包含 fake Dagster 和测试向量，不能单独证明生产 Compose。生产
变更还必须在隔离环境验证真实 OIDC、Dagster、embedding、OTel/metrics、故障恢复和备份恢复；
无法在当前机器执行时，在 PR 中明确列为未完成门禁，不得用 mock 结果替代。

### Migration and compatibility

- 数据库破坏性变化必须使用 expand/migrate/contract，至少保留一个正式兼容窗口；同一 release
  不得同时新增替代结构并删除旧结构。
- 回填必须有界、可恢复、可重复，并携带 tenant/project、幂等、审计和 trace。生产回滚不能把
  Alembic downgrade 当作数据恢复。
- `/api/v1/*` 内不要改变既有字段类型/语义或把可选改为必填。弃用同步 OpenAPI、CHANGELOG 和
  [兼容策略](doc/release/versioning-and-compatibility.md)。
- 生产镜像用固定 digest；源码、镜像、SBOM、签名、checksum、NOTICE 和迁移说明来自同一 commit。

## Commit Scope

建议按以下范围拆分：

- `frontend:` 原型交互、UI、前端 API client。
- `backend:` FastAPI、服务层、模型、迁移、Worker。
- `spec:` OpenAPI、DB schema、状态机、RBAC、seed fixture。
- `docs:` 产品、UI、Agentic、开发说明。
- `test:` 契约测试、集成测试、E2E、验证脚本。

大规模开源准备变更应优先按 [变更提交计划](doc/reports/change-submission-plan.md) 拆成
`governance-release`、`backend-runtime`、`backend-contracts-and-migrations`、`frontend-bff-ux`
四类审查边界。不要把缓存、构建产物、本地数据库、截图、E2E artifact、真实 secret
或未脱敏数据混入源码提交。

## Review Checklist

- 是否破坏当前原型可见主流程。
- 是否新增了未脱敏日志或审计字段。
- 是否有跨租户/跨项目读取风险。
- 是否让前端依赖后端内部实现细节。
- 是否新增了无测试的状态迁移、异步运行或外部回写。
- 是否新增高基数/敏感 metric 或 OTel attribute，或让 `/metrics` 暴露到 edge。
- 是否提供升级、回滚、备份、轮换与告警影响说明，并保持单机无 HA 的边界可见。
