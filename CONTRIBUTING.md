# Contributing

感谢参与 Auris Flow。当前项目仍处于高保真原型和后端开发基线阶段，贡献应优先保持契约一致、可验证和可回滚。

## Development Rules

- 不要绕过 `tenant_id`、`project_id`、`trace_id`、`request_id`。
- 写操作必须考虑幂等、审计、错误 envelope 和可重试状态。
- Agent 或异步任务不能直接覆盖线上结果，只能写候选、草稿、人审任务、评测运行或回填草稿。
- 前端不要直接访问 MySQL、Qdrant、对象存储或 Dagster。
- 不要在业务页面暴露 Qdrant collection、裸 Dagster UI、明文 secret 或签名 URL。

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
