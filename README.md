# Auris Flow

Auris Flow 是一个面向音频证据、标签治理、任务编排、知识库和业务洞察的企业级中台原型。当前仓库包含：

- React + TypeScript + Vite 高保真前端原型。
- FastAPI BFF 后端骨架。
- MySQL + Qdrant 后端规格包。
- 固定 seed fixture、OpenAPI 草案、数据库迁移、契约测试和一键验证脚本。
- 平台 readiness eval，用于持续检查评测、标注、洞察三域是否保持同一工程闭环。

当前树是 Auris Flow `v1.0.0` 的**候选实现**，不是已经发布或获生产支持的版本。仓库已包含 Apache-2.0 文本、单机 Linux Docker Compose、通用 OIDC/PKCE、真实 Dagster、语义 embedding 接口及可观测性/备份基线；项目 owner 的许可权利主体签字、`v1.0.0-rc.1` 真实发布演练、外部干净安装和正式 release 审批仍未完成。在这些人工与运行门禁通过前，不得宣称正式开源发布或生产可部署验收完成。

## Repository Layout

```text
backend/                  FastAPI BFF、SQLAlchemy models、Alembic、tests
doc/                      产品、UI、Agentic 和后端设计文档
doc/backend-spec/         OpenAPI、DB schema、状态机、RBAC、seed、runbook
docker/local/             MySQL、Redis、MinIO、Qdrant 本地依赖
production/               单机生产 Compose、Dagster、edge、可观测性与备份工具
prototype/auris-flow-ui/  React 高保真中台原型
scripts/verify_all.sh     后端规格、测试、smoke 和前端构建验证入口
scripts/check_platform_readiness.py  开源平台完整性与三域闭环检查
AGENTS.md                 后续 agent / 协作者的工程边界和验证要求
```

## Architecture Baseline

第一阶段后端基线：

- FastAPI BFF：统一认证、通用 OIDC Authorization Code + PKCE、不透明 HttpOnly 浏览器会话、租户/项目上下文、UI Projection、错误结构和幂等入口。
- MySQL：权威业务状态，包括租户、项目、连接器、任务、音频、标签、评测、资产、审计和运行状态。
- Redis：当前承担连接 readiness、固定窗口限流和运行辅助职责；仓库已有缓存键规范但尚未实现结果缓存读写，因此不宣称 cache hit 能力。幂等权威状态仍在 MySQL。单机基线不承诺 Redis HA，故障时不得把 Redis 当作唯一业务事实来源。
- MinIO / S3 / OBS / OSS：原始音频、处理后 WAV、ASR/Diar JSON、证据包、报告和导出文件；音频播放由 BFF 基于短时授权按登记 Provider 流式代理 HTTP Range，前端不直接持有云存储凭据。
- Dagster：`production/` 提供真实 Dagster code server、webserver、daemon 与 MySQL storage，承接提交、运行和签名 completion；Dagster 仍只是底层执行引擎，不作为业务 API 主语言或产品画布。开发专项 `verify_real_stack.sh` 仍使用协议 fake，不能替代生产 Compose E2E。
- Qdrant：承载知识、证据、标签样本、badcase 和可选声纹 embedding 的派生索引。生产配置必须接入 HTTPS 语义 embedding provider 并校验模型/维度；确定性向量只允许 local/test，在 `prod/release` fail closed。
- OpenTelemetry：已接入 OTel SDK、结构化脱敏日志、受限 `/metrics`，生产 Compose 包含 Collector、Tempo、Prometheus、Grafana 和 node-exporter；业务 `trace_id` 会与活动 OTel trace/span 关联。真实 RC 的全链路追踪、告警通知与负载/SLO 演练仍是发行门禁。

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

默认前端原型通过 `POST /api/v1/auth/dev-login` 获取服务端签发的短期兼容会话和 HttpOnly cookie，随后把会话绑定到本地演示上下文：

- tenant: `aurora_auto`
- project: `sales_qa`
- 预置账户：`demo.operator@auris.local`
- 本地密码：`auris-demo`
- session scope：`aurora_auto / sales_qa`

开发登录只在 `local/test/ci` 且 `ALLOW_DEV_AUTH=true` 时可用；未知邮箱不会回退成管理员。浏览器 bundle 不包含共享 bearer token，前端会主动清理旧版本遗留的 localStorage/sessionStorage 会话；开发兼容 bearer 只在当前 React 运行内存持有，刷新恢复依赖 HttpOnly cookie。

`prod/release` 使用通用 OIDC Authorization Code + PKCE：`GET /api/v1/auth/oidc/login`
生成一次性 state/nonce/verifier 并 303 至 IdP，回调只接受明确 provision 的内部 identity，随后设置
`__Host-auris_session` 不透明 cookie。数据库只保存会话 token 与 CSRF 的 SHA-256，IdP token 不返回
前端。`GET /api/v1/auth/session` 可直接从 cookie 恢复 scope、当前动态角色和会话绑定的 CSRF token；
cookie 认证的写操作与 `POST /api/v1/auth/logout` 必须同时提交 `X-CSRF-Token` 和受信 Origin。
生产 Compose 包含 Keycloak 参考 IdP，产品认证协议保持通用 OIDC，不依赖其私有 claim 或管理 API。

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

## Production Candidate

Linux 单机部署入口见 [production/README.md](production/README.md)。该基线包含 FastAPI BFF、
Worker、MySQL、Redis、MinIO、Qdrant、真实 Dagster、Keycloak 参考 IdP、TLS edge、OTel
Collector、Tempo、Prometheus 与 Grafana，并明确不提供节点 HA 或自动容灾。

`.env.example` 的 `:dev` 镜像只用于候选构建；正式安装必须使用 release 提供的固定 digest、SBOM、
签名和 checksum。当前尚无已批准的 `v1.0.0` 制品，不能把源码 `docker compose up` 结果当作正式
生产发行。运维入口：

- [SLO、告警与故障排查](doc/runbooks/operations.md)
- [备份与空环境恢复](doc/runbooks/backup-restore.md)
- [升级与回滚](doc/runbooks/upgrade-rollback.md)
- [密钥与证书轮换](doc/runbooks/key-rotation.md)
- [安全事件响应](doc/runbooks/security-incident-response.md)
- [版本与兼容策略](doc/release/versioning-and-compatibility.md)

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
- 浏览器 UI/BFF 联调：`AURIS_RUN_E2E=1 bash scripts/verify_all.sh` 会自动启动临时 SQLite BFF 和 Vite 服务，并在内容寻址的 `linux/amd64` Playwright 容器内比对由 Git 锁文件指向的不可变视觉 OCI 制品；跑完后清理进程与临时库。该路径需要可启动新容器的 Docker Engine 和 ORAS。
- 真实依赖栈专项：`AURIS_REAL_STACK_E2E=1 bash scripts/verify_ui_bff_e2e.sh` 会启动 MySQL、Redis、MinIO、Qdrant、一个 Dagster-compatible GraphQL fake endpoint 和一个 fake 平台回调接收端，用 MySQL 跑迁移和 seed，并要求 `/readyz` 在 strict 模式下确认 `database/redis/object_storage/qdrant/dagster` 全部 `ok`；该模式还会提交 Dagster protocol run request 并回读 receipt、真实写入并回读 Qdrant point 和 MinIO manifest 对象、发送 HMAC 签名外部 callback 并回读接收端 receipt，避免把本地 receipt 当作真实副作用。
- 上述开发“real-stack”只表示 MySQL、Redis、MinIO、Qdrant 等依赖服务和网络 I/O 被实际调用，不表示生产 Compose 已验收：Dagster 是 `scripts/fake_dagster_graphql_server.py` 提供的协议 fake，Qdrant point 使用确定性测试向量，且该路径不启动 OTel Collector。`production/` 已有真实实现，但 release candidate 必须另外执行真实 Dagster、语义 embedding、OIDC、遥测、故障恢复和备份恢复 E2E，不能用本项门禁替代。
- 独立真实栈门禁：`bash scripts/verify_real_stack.sh` 会等待四项 Compose healthcheck，执行上述 UI/BFF E2E，并硬校验 artifact 为隔离 MySQL、`real_qdrant`、真实 MinIO 回执和对象存储来源的 HTTP 206 Range；SQLite、mock 或 local receipt 会直接失败。
- 正式真实 Dagster 门禁：`bash scripts/verify_real_dagster.sh` 使用独立 Compose project 启动生产 Dagster code server、webserver、daemon 和 MySQL storage，精确校验 `auris_flow_defs` / `__repository__` / `auris_flow_generic_job` 以及 `instance.daemonHealth.allDaemonStatuses` 中全部 required daemon，再通过生产 `RealDagsterClient` 提交 BFF/outbox 形状的 payload，轮询真实 Dagster run 状态，覆盖成功、受控失败、`SAFE_TERMINATE` 取消、签名 completion receipt、进程重启后的终态持久化与再次提交。该门禁从不启动 `scripts/fake_dagster_graphql_server.py`，失败时不生成 release artifact。
- 该真实 Dagster artifact 的取消证明范围仅为 **Dagster 引擎层**：它证明 engine run 被取消且未产生成功/失败 completion receipt，不代替产品 BFF 的取消路由、`RunRecord` submitted/callback 顺序、业务状态回写和 outbox 重试闭环；这些必须由独立产品链路测试验收。`bash scripts/verify_real_dagster_local_process.sh` 只生成明确标记 `execution_environment=local-process` 的补充诊断证据，不能满足 Compose release 门禁。
- UI/BFF 联调评分：检查标签、评测、洞察、任务、知识库、资产、设置 7 类前端写动作是否都返回后端对象 id 和 `trace_id`；浏览器页面不得出现 console error、request failure 或 4xx/5xx 响应。403/400 负向契约在 Node 侧单独验证，避免污染真实页面观测。

Docker Desktop Engine 不可用时，只能走不含浏览器视觉发布证据的快速开发验证：

```bash
PYTHON=/absolute/path/to/python bash scripts/verify_fast.sh
```

该路径完成契约测试、迁移烟测和前端构建等快速检查，但不生成 UI/BFF、冻结视觉基线或真实依赖栈的发布证据。Docker 恢复后，必须补跑：

```bash
AURIS_RUN_E2E=1 bash scripts/verify_all.sh
bash scripts/verify_real_stack.sh
bash scripts/verify_real_dagster.sh
bash scripts/verify_product_dagster_path.sh
```

视觉场景、runner contract 与规范化 seed 是 Git 源码；PNG、geometry、生成 manifest 和 Playwright 输出不是源码，必须留在忽略的 diagnostics 目录或不可变 OCI 制品中。正式基线只由 `production/visual/visual-baseline.lock.json` 指向，并且只接受 `ghcr.io/...@sha256:...`；下载后会校验 OCI 包哈希、manifest、76 张 PNG 哈希、场景/runner/seed digest 与生成时 source commit。`production/visual/runtime-contract.json` 继续固定 `linux/amd64` Playwright 镜像 digest、锁文件版本、Chromium 和镜像字体。release 模式禁止 goal/seed override、host runtime 与 update mode。

当前锁明确为 `PENDING`，表示尚无经过人工批准的 Linux 制品，因此严格 release 门禁会 fail closed。现有 Darwin 截图只能作为本机 diagnostics，不能改写锁或生成 `status=ok` 的发布证据。需要生成诊断或候选截图时，只能写到 `prototype/auris-flow-ui/e2e/artifacts/` 下的命名目录，例如：

```bash
AURIS_UPDATE_VISUAL_BASELINE=1 \
AURIS_VISUAL_GOAL_DIR="$PWD/prototype/auris-flow-ui/e2e/artifacts/visual-linux-candidate" \
AURIS_VISUAL_RUNTIME=container \
bash scripts/visual_regression.sh
```

候选 manifest 会记录 source commit、runner/scenario/seed digest、Playwright/Chromium/Node/OS 版本和全部 76 张截图的 SHA-256。正式候选只能由受保护的 `.github/workflows/visual-baseline-build.yml` 从精确 commit 调用固定 digest 的 `linux/amd64` Playwright 容器生成；它用 `create-package` 创建单 layer OCI 包，推送到当前仓库的 GHCR，取得 registry 返回的真实 digest，再以该 workflow 的 GitHub OIDC identity 进行 Cosign keyless 签名。仓库管理员必须在 GitHub 宿主端分别为 `visual-baseline-build` 与 `visual-baseline-production` 配置 required reviewers；YAML 中声明 environment 本身不是已启用人工保护的证据。promotion 会以当前仓库 build workflow 的精确 identity 和 GitHub token issuer 重新执行 `cosign verify`，然后下载并验证 76 张图片与全部契约，只产生 `visual-baseline.lock.json` 一处候选修改；人工审核后用独立 commit 提交该指针。不得手写 digest、签名 identity 或批准状态，host runtime 仅用于本机 diagnostics。

视觉比较只在真实通过后写入 `build/release-evidence/visual-regression.json`，并包含 `status=ok`、当前 release `source_commit`、生成基线的 `baseline_source_commit`、OCI digest、runner digest、`signature_identity`、固定 GitHub OIDC issuer 以及 `scenario_count=passed=76`。严格 release 每次都会在下载前对锁定 digest 重新执行 Cosign 验签，不能依赖 promotion 时的一次性结果。基线 commit 必须是 release commit 的祖先，两者之间任何前端、后端 seed/runtime 或视觉 runner 输入变化都会使证据失效；非视觉文档修改不会被误称为基线来自当前 commit。`PENDING`、签名/identity 不可信、下载失败、哈希漂移或截图差异时不会保留伪成功 evidence。

严格开源发布验证还会强制运行 UI/BFF E2E、前端视觉布局审计、真实依赖栈 E2E、真实 Dagster Compose 门禁、产品级 BFF→Outbox Worker→真实 Dagster→状态同步/签名回写/安全取消门禁、前端 `npm audit --audit-level=high` 和后端 `pip-audit backend`。如果只运行 `AURIS_RELEASE_CHECK=1 bash scripts/verify_all.sh` 而未开启 `AURIS_RUN_E2E=1`，脚本会直接失败，避免发布前漏掉浏览器/BFF 链路。`bash scripts/verify_release.sh` 会依次调用 `bash scripts/verify_real_stack.sh`、`bash scripts/verify_real_dagster.sh` 与 `bash scripts/verify_product_dagster_path.sh`；第一个验证 MySQL/Redis/MinIO/Qdrant 的开发真实依赖栈（其中 Dagster 仍是协议 fake），第二个验证真实 Dagster 引擎协议，第三个证明产品业务链路确实经过 BFF、Worker 和真实 Dagster，而不是只测底层 GraphQL。该入口拒绝 `AURIS_SKIP_REAL_STACK_E2E=1`、`AURIS_SKIP_REAL_DAGSTER=1` 和 `AURIS_SKIP_PRODUCT_DAGSTER_GATE=1`。所有门禁证据必须绑定同一干净 HEAD，最终由 `scripts/finalize_release_evidence.py` 校验并生成带逐文件 SHA-256 的 `build/release-evidence/release-gate-manifest.json`；缺失、陈旧、路径泄漏、非 Compose 证据或来源 commit 不一致均 fail closed。Docker 受限时请改跑 `bash scripts/verify_fast.sh` 或 `AURIS_RUN_E2E=1 bash scripts/verify_all.sh`，但不能把结果作为公开发布候选。

### Public Open-Source Release Check

当前仓库只能作为 `v1.0.0` 候选继续开发和联调。正式开源发布除通过严格自动门禁、生产 Compose 与恢复演练、外部干净安装外，还必须由项目 owner 完成 [Apache-2.0 许可权利主体和版权归属授权记录](open-source-rights-authorization.md)：

```bash
python3 scripts/check_platform_readiness.py --release
```

推荐使用一键 release 验证：

```bash
bash scripts/verify_release.sh
```

`bash scripts/verify_clean_clone.sh` 单独运行时只生成 `readiness_scope=base` 的功能性锁定源码复现证据，便于在权利签字或视觉基线尚未批准的 RC 阶段发现漏跟踪文件；它不是发行授权。`verify_release.sh` 会强制以 `AURIS_RELEASE_CHECK=1` 重新运行该门禁，使克隆内的 `--release` 12/12、权利记录和不可变视觉指针仍然 fail closed。

release check 会校验许可证文本存在、发布包卫生、安全披露、供应链审计、UI/BFF E2E、开发真实依赖栈 E2E、真实 Dagster Compose 门禁、产品级 Dagster 业务链路、同 commit 证据清单和 failed-response gate，不应绕过；自动检查不能替代项目 owner 对 Apache-2.0 许可权利主体和版权归属的确认。

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
- [单机生产候选安装](production/README.md)
- [生产运维 Runbook](doc/runbooks/operations.md)
- [备份恢复 Runbook](doc/runbooks/backup-restore.md)
- [版本与兼容策略](doc/release/versioning-and-compatibility.md)

## Security Posture

当前后端已具备通用 OIDC Authorization Code + PKCE、issuer/audience/JWKS 校验、不透明 HttpOnly browser session、CSRF/Origin 防护、开发认证隔离、动态用户/项目角色检查、租户/项目上下文、幂等、审计、Outbox、HMAC v2 callback/replay protection 和 OTel 技术基线。仍需完成的发行验收包括：

- 真实企业 IdP 的登录、禁用、JWKS 轮换与权限运维演练。
- 完整资源级 RBAC 负向矩阵和跨租户/项目安全验收。
- 外部 secret manager/KMS 托管、各类 key overlap/retire 与应急吊销演练。
- 真实 callback、死信人工重放、告警通知和安全事故响应演练。
- 审计/OTel 脱敏、托管 CI 安全功能、恢复后的数据一致性与外部干净安装验收。

请不要把本地开发登录、兼容测试 token、Docker 默认密码或 demo 账号用于生产环境。

## License

仓库当前包含 [Apache License 2.0](LICENSE) 标准文本、`NOTICE` 候选文本和第三方许可清单。Apache-2.0 的个人许可权利主体和版权归属授权记录仍标记为 blocked；在 owner 完成签字并替换 NOTICE 占位内容前，不得 tag、发布或把当前候选状态表述为正式开源发布完成。
