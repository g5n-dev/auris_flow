# Auris Flow

Auris Flow 是一个面向音频证据、标签治理、任务编排、知识库和业务洞察的企业级中台原型。当前仓库包含：

- React + TypeScript + Vite 高保真前端原型。
- FastAPI BFF 后端骨架。
- MySQL + Qdrant 后端规格包。
- 固定 seed fixture、OpenAPI 草案、数据库迁移、契约测试和一键验证脚本。
- 平台 readiness eval，用于持续检查评测、标注、洞察三域是否保持同一工程闭环。

当前状态是“可联调的原型与后端开发基线”，不是生产可直接上线版本。仓库当前包含 [Apache License 2.0](LICENSE) 标准文本，但 Apache-2.0 的许可权利主体和版权归属仍待项目 owner 确认；确认前不得宣称正式开源发布完成。生产部署仍需要补齐正式鉴权、密钥管理和外部系统适配。

## Repository Layout

```text
backend/                  FastAPI BFF、SQLAlchemy models、Alembic、tests
doc/                      产品、UI、Agentic 和后端设计文档
doc/backend-spec/         OpenAPI、DB schema、状态机、RBAC、seed、runbook
docker/local/             MySQL、Redis、MinIO、Qdrant 本地依赖
prototype/auris-flow-ui/  React 高保真中台原型
scripts/verify_all.sh     后端规格、测试、smoke 和前端构建验证入口
scripts/check_platform_readiness.py  开源平台完整性与三域闭环检查
AGENTS.md                 后续 agent / 协作者的工程边界和验证要求
```

## Architecture Baseline

第一阶段后端基线：

- FastAPI BFF：统一认证、租户/项目上下文、UI Projection、错误结构和幂等入口。
- MySQL：权威业务状态，包括租户、项目、连接器、任务、音频、标签、评测、资产、审计和运行状态。
- Redis：当前只实现连接 readiness 和部分固定窗口限流基线；幂等权威状态仍在 MySQL，缓存、锁、运行状态以及生产级高可用和故障恢复尚未完成。
- MinIO / S3 / OBS / OSS：原始音频、处理后 WAV、ASR/Diar JSON、证据包、报告和导出文件；音频播放由 BFF 基于短时授权按登记 Provider 流式代理 HTTP Range，前端不直接持有云存储凭据。
- Dagster：目标是作为底层执行引擎映射，不作为业务 API 主语言；当前 real-stack 只连接 Dagster-compatible GraphQL 协议 fake，不包含真实 Dagster 控制面、调度器或执行器。
- Qdrant：目标是承载知识、证据、标签样本、badcase 和可选声纹 embedding 的召回索引；当前 real-stack 读写真正的 Qdrant 服务，但向量由 payload 确定性生成，仅是测试向量，不代表生产 embedding 模型、召回质量或容量。
- OpenTelemetry：当前只有内部 `trace_id` 传播、结构化日志和追踪投影等部分基线，尚未接入完整 OTel SDK、exporter 与 Collector，也未证明 BFF、Worker 和外部回写的生产级端到端遥测。

目标架构不使用 ClickHouse。洞察和运营大盘第一阶段计划使用 MySQL 聚合表、预计算结果、Redis 缓存和 Qdrant 召回解释；当前实现不代表这些组件已经达到生产级完备性。

## Local Development

一键启动本地开发栈：

```bash
bash scripts/dev_up.sh
```

该脚本会检查后端/前端依赖，尝试启动 Docker 本地依赖，执行迁移和 seed，并以前台方式启动 BFF、outbox worker 和 Vite。退出脚本会清理本轮启动的子进程。

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

启动本地依赖：

```bash
docker compose -f docker/local/docker-compose.yml up -d mysql redis minio qdrant
```

初始化和启动：

```bash
cd backend
alembic upgrade head
python -m app.seed local_demo
APP_ENV=local ALLOW_DEV_AUTH=true uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
python -m app.workers.outbox_worker
```

### Frontend

```bash
cd prototype/auris-flow-ui
npm install
npm run dev
```

默认前端原型通过 `POST /api/v1/auth/dev-login` 获取服务端签发的短期会话，随后把会话绑定到本地演示上下文：

- tenant: `aurora_auto`
- project: `sales_qa`
- 预置账户：`demo.operator@auris.local`
- 本地密码：`auris-demo`
- session scope：`aurora_auto / sales_qa`

开发登录只在 `local/test/ci` 且 `ALLOW_DEV_AUTH=true` 时可用；未知邮箱不会回退成管理员。浏览器 bundle 不再包含共享 bearer token，也不能由调用方覆盖当前页面会话。

当前 React 原型为了本地演示和刷新恢复，会把 dev-login 返回的短期会话保存在 `localStorage` 或 `sessionStorage`；这是 local/sessionStorage 原型策略，不是生产认证方案。`prod/release` 必须接入正式 OIDC/JWT/SSO 或后端 httpOnly cookie 会话层，并重新评估 CSRF、CSP、登出和 token 轮换策略。

交互审计脚本需要先启动前端服务，然后在另一个终端运行：

```bash
cd prototype/auris-flow-ui
AURIS_AUDIT_URL=http://127.0.0.1:5173/ npm run audit:tabs
AURIS_AUDIT_URL=http://127.0.0.1:5173/ npm run audit:capture
```

如果只想跑一次完整 UI 审计，可以使用自动临时栈入口；它会创建临时 SQLite BFF、执行迁移和 seed、启动临时 Vite，并在 1920px 主视图和 1440px 高风险视图中检查 tab 相似度、大块空白层、横向溢出、关键文字裁切、操作反馈和工程词暴露，结束后清理本轮进程与临时库：

```bash
npm --prefix prototype/auris-flow-ui run audit:auto
```

纯前端冒烟脚本会自动启动一个临时 Vite 服务，适合本地和 CI 验证原型主导航、关键 tab 和顶部操作反馈：

```bash
cd prototype/auris-flow-ui
npm run e2e:ui
```

## Verification

仓库级验证入口：

```bash
bash scripts/verify_fast.sh
```

`verify_fast.sh` 会调用 `scripts/verify_all.sh`，优先使用 `backend/.venv/bin/python`，否则使用当前 `PATH` 里的 `python3`。如果本机有多个 Python 发行版，可以显式指定：

```bash
PYTHON=/absolute/path/to/python bash scripts/verify_fast.sh
```

当前脚本覆盖：

- `doc/backend-spec/validate_backend_spec.py`
- `scripts/check_platform_readiness.py`：开源入口、后端契约、运行底座、核心链路测试和 eval harness
- 后端与仓库脚本 Ruff format / lint
- 后端与关键验证脚本 mypy
- Alembic 迁移 upgrade / 幂等 upgrade / downgrade 烟测
- 校准迁移会在 `0018` 注入历史兼容夹具后升级 `0019`；默认验证临时 SQLite。MySQL 验证只接受显式的独立可销毁 `MIGRATION_DATABASE_URL`，该库会被完整升降级，严禁与应用 `DATABASE_URL` 共用。
- 后端 unit / contract / integration tests
- 后端 TestClient smoke
- 前端 TypeScript build + Vite production build
- 前端 bundle budget：检查最大 JS/CSS 资产和总 JS 体积，防止原型继续无感膨胀
- 前端 Playwright UI smoke：登录、一级模块、关键 tab、搜索/筛选/导出反馈
- 浏览器 UI/BFF 联调：`AURIS_RUN_E2E=1 bash scripts/verify_all.sh` 会自动启动临时 SQLite BFF 和 Vite 服务，跑完后清理进程与临时库。
- 真实依赖栈专项：`AURIS_REAL_STACK_E2E=1 bash scripts/verify_ui_bff_e2e.sh` 会启动 MySQL、Redis、MinIO、Qdrant、一个 Dagster-compatible GraphQL fake endpoint 和一个 fake 平台回调接收端，用 MySQL 跑迁移和 seed，并要求 `/readyz` 在 strict 模式下确认 `database/redis/object_storage/qdrant/dagster` 全部 `ok`；该模式还会提交 Dagster protocol run request 并回读 receipt、真实写入并回读 Qdrant point 和 MinIO manifest 对象、发送 HMAC 签名外部 callback 并回读接收端 receipt，避免把本地 receipt 当作真实副作用。
- 上述“real-stack”只表示 MySQL、Redis、MinIO、Qdrant 等依赖服务和网络 I/O 被实际调用，不表示所有适配器均为生产实现：Dagster 是 `scripts/fake_dagster_graphql_server.py` 提供的协议 fake；Qdrant point 使用 payload 派生的确定性测试向量；Redis 只验证 readiness 与有限功能基线；该路径未启动 OTel Collector。
- 独立真实栈门禁：`bash scripts/verify_real_stack.sh` 会等待四项 Compose healthcheck，执行上述 UI/BFF E2E，并硬校验 artifact 为隔离 MySQL、`real_qdrant`、真实 MinIO 回执和对象存储来源的 HTTP 206 Range；SQLite、mock 或 local receipt 会直接失败。
- UI/BFF 联调评分：检查标签、评测、洞察、任务、知识库、资产、设置 7 类前端写动作是否都返回后端对象 id 和 `trace_id`；浏览器页面不得出现 console error、request failure 或 4xx/5xx 响应。403/400 负向契约在 Node 侧单独验证，避免污染真实页面观测。

Docker Desktop Engine 不可用时，仍可走无 Docker 验证路径：

```bash
PYTHON=/absolute/path/to/python AURIS_RUN_E2E=1 bash scripts/verify_all.sh
```

该路径使用临时 SQLite BFF 和临时 Vite 服务完成契约测试、迁移烟测、前端构建、UI smoke 和 UI/BFF 联调，不依赖 MySQL/Redis/MinIO/Qdrant/Dagster protocol endpoint。它证明开发基线可跑通，但不证明真实依赖栈可用。Docker 恢复后，必须补跑：

```bash
bash scripts/verify_real_stack.sh
```

严格开源发布验证还会强制运行 UI/BFF E2E、前端视觉布局审计、真实依赖栈 E2E、前端 `npm audit --audit-level=high` 和后端 `pip-audit backend`。如果只运行 `AURIS_RELEASE_CHECK=1 bash scripts/verify_all.sh` 而未开启 `AURIS_RUN_E2E=1`，脚本会直接失败，避免发布前漏掉浏览器/BFF 链路。`bash scripts/verify_release.sh` 会继续调用 `bash scripts/verify_real_stack.sh`，避免把 SQLite/mock 路径误认为 MySQL/Redis/MinIO/Qdrant 已联通；该入口会拒绝 `AURIS_SKIP_REAL_STACK_E2E=1`。Docker 受限时请改跑 `bash scripts/verify_fast.sh` 或 `AURIS_RUN_E2E=1 bash scripts/verify_all.sh`，但不能把结果作为公开发布候选。

### Public Open-Source Release Check

当前仓库只能作为“拟开源开发基线”继续开发和联调。正式开源发布除通过严格门禁外，还必须由项目 owner 确认 Apache-2.0 许可权利主体和版权归属：

```bash
python3 scripts/check_platform_readiness.py --release
```

推荐使用一键 release 验证：

```bash
bash scripts/verify_release.sh
```

release check 会校验许可证文本存在、发布包卫生、安全披露、供应链审计、UI/BFF E2E、真实依赖栈 E2E 和 failed-response gate，不应绕过；自动检查不能替代项目 owner 对 Apache-2.0 许可权利主体和版权归属的确认。

## Important Documents

- [后端规格入口](doc/backend-spec/README.md)
- [产品设计文档](doc/设计文档.md)
- [UI 设计文档](doc/UI设计文档.md)
- [Agentic 智能化设计文档](doc/Agentic智能化设计文档.md)
- [当前原型评审与优化计划](doc/当前原型评审与优化计划.md)
- [开源发布清单](RELEASE_CHECKLIST.md)
- [平台 readiness 检查](doc/reports/platform-readiness.md)
- [正式开源发布 readiness](doc/reports/open-source-release-readiness.md)
- [仓库目录评审](doc/reports/repository-layout-review.md)
- [变更提交计划](doc/reports/change-submission-plan.md)

## Security Posture

当前后端已具备基础认证门槛、租户/项目上下文、幂等、审计、Outbox 和 trace。仍需补齐：

- 正式 JWT/OIDC 或企业 SSO。
- 完整 RBAC default-deny。
- 密钥管理服务和 secret reference。
- 外部回写签名、重试和死信。
- 审计脱敏覆盖更复杂的数据类型。

请不要把本地开发登录、兼容测试 token、Docker 默认密码或 demo 账号用于生产环境。

## License

仓库当前包含 [Apache License 2.0](LICENSE) 标准文本。Apache-2.0 的许可权利主体和版权归属尚待项目 owner 确认；在确认并记录前，不得将当前候选状态表述为正式开源发布完成。
