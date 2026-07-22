# Auris Flow BFF

本目录是基于 `doc/backend-spec` 的 FastAPI 后端实现骨架。当前版本优先打通原型联调：

- 统一 `/api/v1/*` 响应 envelope。
- 请求上下文、幂等、审计、Outbox 和运行状态。
- 使用 `seed-fixture-v0.1.json` 初始化极光汽车演示数据。
- 业务页面数据先通过 `json_resources` 投影表兼容原型，同时 0002 迁移已建立任务、音频、标签、人审、知识、评测、资产和外部回写的强表基线。

## Local

```bash
uv sync --frozen --all-extras --python 3.12
source .venv/bin/activate
docker compose -f ../docker/local/docker-compose.yml up -d mysql redis minio qdrant
cp .env.example .env
alembic upgrade head
python -m app.seed local_demo
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --no-access-log
python -m app.workers.outbox_worker
```

上述命令以已提交的 `uv.lock` 为准，并要求 `uv 0.10.0`。手工执行
`pip install -e ".[dev]"` 会解析当时最新的兼容版本，只用于依赖前向兼容探测，不属于干净克隆
或 release 复现证据；权威复现入口是仓库根目录的 `bash scripts/verify_clean_clone.sh`。

### Generic OIDC / browser session baseline

OIDC 使用标准 discovery、authorization、token 和 JWKS 能力。下面只列字段形状，不是可直接复制的生产凭据：

```dotenv
AUTH_PROVIDER=oidc
OIDC_ISSUER=https://identity.example.com/realms/auris
OIDC_CLIENT_ID=auris-flow-browser
OIDC_AUDIENCE=auris-flow-api
OIDC_REDIRECT_URI=https://flow.example.com/api/v1/auth/oidc/callback
OIDC_SCOPES=openid profile email
OIDC_DISCOVERY_URL=https://identity.example.com/realms/auris/.well-known/openid-configuration
OIDC_JWKS_CACHE_TTL_SECONDS=300
OIDC_CLOCK_SKEW_SECONDS=30
BROWSER_SESSION_COOKIE_NAME=__Host-auris_session
CORS_ALLOWED_ORIGINS=https://flow.example.com
```

机密客户端如需 `OIDC_CLIENT_SECRET`，生产应使用 `OIDC_CLIENT_SECRET_FILE=/run/secrets/...`
或等价外部 secret reference，不能写入仓库或镜像。`prod/release` 会拒绝非 HTTPS issuer/redirect、
缺失 issuer/client ID/audience/redirect、错误 cookie 名以及开发认证。OIDC identity 当前必须通过受控
运维流程预配置到内部 user/tenant/project；没有自注册或从 IdP role claim 自动授权。前端从
`GET /api/v1/auth/session` 取得当前用户与会话绑定的 CSRF token，并只在内存保存后者；并发标签页
取得同一 token，安全 GET 不会使其他标签页立即失效，token 随父会话撤销或过期。
生产 `CORS_ALLOWED_ORIGINS` 只接受精确 HTTPS Origin，`TRUSTED_HOSTS` 只接受无通配符的精确主机。
当前 BFF 不申请 `offline_access`，也不保存 IdP refresh token；浏览器会话过期后重新执行 Code + PKCE。
默认 OIDC HTTP transport 禁止重定向并以流式方式读取不可信 IdP 响应；discovery、JWKS、token response
在进入 JSON 缓冲区前分别限制为 128 KiB、512 KiB、128 KiB，超限或读取异常只返回稳定脱敏错误。
`POST /api/v1/auth/logout` 只撤销本地 BFF 会话并清除 cookie，不宣称终止 IdP 的全局 SSO 会话；如需
RP-Initiated Logout，必须按目标 IdP 能力另行配置并完成真实集成演练。

标签自动优化 scheduler 默认关闭。项目管理员先通过
`POST /api/v1/label-optimization-schedules` 锁定 label/Prompt/model/policy/eval dataset
与预算，再以独立进程运行：

```bash
LABEL_OPTIMIZATION_SCHEDULER_ENABLED=true \
python -m app.workers.label_optimization_worker

# 单次 tick；Dagster schedule 也应调用同一个 run_once 语义
python -m app.workers.label_optimization_worker --once
```

该 worker 执行持久化的 15 分钟阈值扫描、每日 02:00 增量和每周日 02:00
全量回归；时区由 Schedule 的 IANA `schedule_timezone` 决定。每个
`tenant/project/label_version` 只有一行 Schedule，条件 UPDATE 的 claim 是数据库 scope
mutex；due 时钟、RunRecord、OptimizationRound、审计和 Outbox 同事务提交。因此首次并发只会
创建一个 root run，过密 blocked 探测不会重置 15 分钟时钟。

候选完成物化后，worker 为每个 PromptVersion 候选创建绑定完整 Bundle 的 EvalRun；评测终态
统一进入 `evaluate_iteration_budget`，最多 3 轮、每轮 2–5 候选、最长 2 小时，并受成本、
关键 recall、显著收益和连续失败门禁约束。自动化终点只有 `blocked` 或
`awaiting-review`，不会创建发布 Bundle 或自动 promote。MetricSnapshot 是 append-only 权威
输入；非法 JSON 计为 `invalid_json_output` 拒绝记录，自由文本失败原因只保存 hash，不参与
失败簇。

## Runtime Flow

1. 前端只访问 FastAPI BFF：`/api/v1/*`。生产浏览器路径使用 BFF OIDC 不透明 HttpOnly cookie，会话绑定默认 tenant/project，显式 scope Header 必须与其匹配；兼容 bearer 调用携带 `Authorization`、`X-Tenant-Id`、`X-Project-Id`。所有请求携带或由服务端生成 `X-Request-Id`，业务写操作另带 `Idempotency-Key`；OIDC cookie 认证的写操作还必须带内存中的 `X-CSRF-Token` 并通过 Origin allowlist。
2. `request_logging_middleware` 生成或继承 `trace_id/request_id`，所有日志输出为同一 JSON 行格式。
3. 写操作先过幂等检查，冲突返回 `409 IDEMPOTENCY_KEY_CONFLICT`，重复请求直接 replay 首次响应。
4. service 写入业务投影、`audit_logs`、`outbox_events`，再提交事务。
5. `outbox_worker` 拉取 pending 事件，按事件类型 dispatch 到 Dagster、Qdrant、对象存储或外部回写 adapter；默认 local adapter 只写可审计回执，失败会重试或进入 dead letter。真实栈专项可通过 `AURIS_DAGSTER_ADAPTER=real` 向 Dagster-compatible GraphQL endpoint 提交运行请求并回读协议 receipt，通过 `AURIS_QDRANT_ADAPTER=real` 对 Qdrant 执行 collection / point upsert，通过 `AURIS_OBJECT_STORAGE_ADAPTER=real` 对 MinIO/S3-compatible endpoint 写入并回读对象 manifest，通过 `AURIS_EXTERNAL_CALLBACK_ADAPTER=real` 发送带 key-id、时间窗、nonce 与全请求绑定的 HMAC v2 平台回调并回读接收端 receipt。
6. 运行状态区分协议分发和业务完成：底层 run request、对象存储 reservation/manifest、外部 callback receipt 派发成功后，`RunRecord.status=submitted` 且 `payload.business_status=awaiting_completion`；只有后续 materialization、真实对象可用或远端业务确认后才能迁移到 `success`。所有复用 `run_payload` 的公开响应使用同一执行引擎中立投影，递归省略 dispatch、adapter/协议实现名、job、远端运行 ID、签名和 secret；完整证据只留在 scoped MySQL、Outbox、完成回执与 Audit，不提供公网 internal 查询入口。
7. 业务完成回执通过 `POST /api/v1/task-runs/{id}/completion-receipts`、`POST /api/v1/exports/{id}/completion-receipts`、`POST /api/v1/output-sinks/platform-callbacks/{id}/completion-receipts` 或通用 `POST /api/v1/runs/{id}/completion-receipts` 写入。BFF 会校验租户、项目、幂等键、adapter 和外部 ID，再把 `submitted` 运行迁移到 `success` 或 `failed`。已验签 Dagster 回执可在 launch binding 前持久化为 `pending_binding`，但只有 external ID 完全匹配才会应用。`result_ref` 中的 `ref/resource/aggregate/object/subject` 类型化引用必须提交完整、非空的 `type + id` 对；字段键先执行 NFKC，再按大小写、camelCase 和连字符无关的语义指纹规范化，任何规范化碰撞都会被拒绝。
8. `POST /api/v1/task-runs/{id}/cancellations` 与 `POST /api/v1/task-runs/{id}/status-syncs` 创建独立控制 RunRecord + Outbox。取消使用 Dagster SAFE_TERMINATE，状态同步中的引擎 `SUCCESS` 只进入 `completion_pending`，不能代替业务完成回执。服务端为每个新 TaskRun 设置有界 `deadline_at`；Worker 使用数据库行锁、`monitor_generation`、确定性 deadline control ID 和 Outbox fencing 自动撤销尚未分发的超时请求、取消已绑定 Dagster 的超时运行，并按 `next_status_sync_at` 周期核对漏回调运行。调用方不能注入 deadline 或监控字段。
9. 前端通过响应 envelope 中的 `meta.trace_id` 或 `/api/v1/traces/{trace_id}` 追踪完整链路。

## Current Backend Boundaries

当前后端是开源开发基线，不是生产 SaaS 后端：

- `json_resources` 仍承担 UI Projection 和兼容层；强表已建表，业务写入会逐步迁移到 repository/service。
- 本地 UI 通过 `/api/v1/auth/dev-login` 获取服务端签发、租户/项目范围受限的短期兼容会话；响应仍保留 bearer 字段供脚本兼容，同时设置 HttpOnly cookie，React 只在运行内存持有 bearer，不写入 localStorage/sessionStorage。令牌强制包含并校验 `iss/aud/iat/exp/jti`，对应 `auth_sessions` 强表；每次业务请求会检查会话存在、时效、撤销和主体一致性。兼容测试 token 不写会话表，仅供后端脚本和契约测试使用；两类开发认证都只在 `local/test/ci` 且 `ALLOW_DEV_AUTH=true` 时可用，`prod/release` fail closed。
- 正式认证边界已提供通用 OIDC Authorization Code + PKCE 技术基线：`/auth/oidc/login` 冻结一次性 state/nonce/verifier，并用短期 HttpOnly、SameSite=Lax 的浏览器 transaction cookie 绑定发起端，数据库只保存其 SHA-256；`/auth/oidc/callback` 恒定时间核对绑定并在所有终态清除 cookie。ID Token 单独以 `OIDC_CLIENT_ID` 校验 audience，多 audience 时强制 `azp=OIDC_CLIENT_ID`；API bearer 仍以 `OIDC_AUDIENCE` 校验，不套用 ID Token 的 `azp` 规则。回调同时验证 issuer、RS256、exp/iat、nonce 和 JWKS key rotation，只把明确 provision 的 `(issuer, subject)` 映射到内部 user/tenant/project。IdP access/ID/refresh token 不返回前端；BFF 只设置不透明 HttpOnly cookie，`browser_auth_sessions` 仅保存 token/CSRF SHA-256。每次请求动态读取 `user_security_states`、identity、租户/项目状态和项目成员角色，因此禁用与降权不依赖会话过期；请求 tenant/project 还必须与 identity 和 session 冻结 scope 精确一致，即使同一用户后来成为第二项目成员也不能复用原会话切换项目。未知主体和跨 scope 默认拒绝且不泄漏对象存在性。
- 这组 OIDC/browser session 能力仍是生产认证基线，不等于整套生产部署已经验收：启用前必须完成真实 IdP 集成、用户 provision/禁用流程、受信 Origin/CORS、HTTPS 与运维轮换演练。Keycloak 只可作为 Docker Compose 参考 IdP；实现保持标准 OIDC 兼容，不读取 Keycloak 私有 role claim 或管理 API。
- 默认 adapter 不会真的调用 Dagster、MinIO 或外部平台，只生成可审计的 dispatch 回执；`AURIS_DAGSTER_ADAPTER=real` 使用服务端事件 allowlist 选择内部 job，忽略请求方的 `job_name/run_config`。控制面与 CI 仍进入 `auris_flow_generic_job`；`audio_intelligence.requested` 只允许进入 `auris_flow_audio_intelligence_v1`，其不可变 execution envelope 绑定 tenant/project/run/trace、幂等与 fencing、截止时间、provider/model，以及录音 bucket/key/version-id/content-sha256。BFF 用 `AURIS_AUDIO_INFERENCE_PROVIDER` 与显式模型 allowlist 拒绝调用方越权选择；绑定 TaskVersion 时模型以已发布快照为权威，省略请求字段会采用冻结值，显式换模返回 `AUDIO_TASK_MODEL_BINDING_MISMATCH`，无 TaskVersion 的 real run 仍受全局白名单约束。Dagster code location 再执行相同绑定，按精确 MinIO/S3 version ID 重算哈希，调用唯一 HTTPS Provider，严格校验响应并把 canonical 结果 manifest 写回版本化对象存储。Provider 请求仅携带不可变对象身份，不携带存储凭据、presigned URL 或音频字节；真实 Provider 必须在受信网络内使用独立最小只读身份按 version/hash 取数，禁止复用 Dagster 凭据。Provider POST 与结果 PUT 均拒绝 3xx。公共 completion 只携带哈希证据，不包含 endpoint/token、对象定位符或转写。仓库 gate 的音频 endpoint 仅为协议夹具，不是模型质量认证；正式 RC 仍需真实 Provider E2E。Qdrant 已提供 `AURIS_QDRANT_ADAPTER=real` 专项路径，用于真实栈 E2E 验证知识索引 point 可回读；对象存储已提供 `AURIS_OBJECT_STORAGE_ADAPTER=real` 专项路径，用于真实栈 E2E 验证 export/audio/report manifest 可写入 MinIO 并按 sha256 回读；外部回写已提供 `AURIS_EXTERNAL_CALLBACK_ADAPTER=real` 协议路径，用于真实栈 E2E 验证 HMAC v2 全请求绑定、active/overlap/retired key 轮换、时间窗、原子 nonce 防重放、幂等 body 冲突和接收端 receipt，不代表真实 CRM/工单平台已接入。
- 录音对象已支持 `storage_objects` 强表登记、租户/项目路径约束、Provider 精确 version ID、SHA-256/ETag/大小元数据、短时签名播放授权，以及通过 BFF 按登记的 `provider` 向 MinIO、S3、OBS 或 OSS 流式转发 HTTP Range。真实登记缺少非空 version ID（包括未启用版本控制的 `null` 版本）会 fail closed。MinIO 使用 path-style S3 SigV4，S3 使用 SigV4，OBS 使用原生 OBS 签名，OSS 使用原生 OSS V4 (`OSS4-HMAC-SHA256`) 并强制 virtual-host style；浏览器统一接收严格的 `206`、`Accept-Ranges`、`Content-Range`、`Content-Length` 和 `ETag` 版本校验语义，客户端取消请求时 BFF 会关闭上游对象流。只有本地 MinIO 会自动创建缺失 bucket，S3/OBS/OSS bucket 必须由基础设施预先创建并授权。各 Provider 使用独立的 `OBJECT_STORAGE_<PROVIDER>_*` 配置；缺少配置时 fail closed，不会借用其他 Provider 的 AK/SK。首发 MinIO/S3 登记路径要求 Provider 校验的 SHA-256；OBS/OSS adapter 在提供同等级 checksum 证明前会拒绝生产登记，当前仅有协议级契约测试，接入生产账号前仍需补真实云端集成。真实栈会验证 MySQL 元数据与 MinIO WAV 对象一致。通用大文件上传会话、分块上传、证据包二进制生命周期和对象清理策略仍未完成，不能据此宣称完整对象存储生产链路。
- 外部回写已具备生产 endpoint allowlist、显式 keyring 轮换状态、时间窗、nonce 防重放和回调 receipt 强表登记；生产仍需把 keyring 接入外部 Secret/KMS、把接收端 nonce/idempotency 原子存储替换为共享持久服务，并完成真实 CRM/工单平台的权限矩阵与联调。

## Promptfoo 评测适配边界

`app/services/promptfoo_eval_adapter.py` 提供接口先行的 Promptfoo CI/CD 适配层，默认
`AURIS_PROMPTFOO_ADAPTER=disabled`。设置为 `optional` 后，仅当
`PROMPTFOO_EXECUTABLE` 可解析时才生成命令计划；可执行文件缺失会返回
`unavailable`，不会伪造评测成功，也不会绕过内部 Eval 门禁。

该接口刻意不直接执行进程、不拼接 shell 字符串、不接收调用方自定义 CLI 参数。Worker
必须先把已验证的配置 Artifact 物化到单次运行 sandbox，再按返回的不可变 `argv` 以
`shell=False` 执行。配置和结果路径都必须位于 sandbox 内，结果文件不允许复用旧文件。
推荐配置如下：

```dotenv
AURIS_PROMPTFOO_ADAPTER=optional
PROMPTFOO_EXECUTABLE=promptfoo
PROMPTFOO_TIMEOUT_SECONDS=7200
```

接入方必须从内部 EvalRun 读取完整 `locked_versions + binding_sha256`，配置文件只能引用
当前租户/项目下具有 SHA-256 的 `StorageObject`，并登记为
`source_type=promptfoo_eval_config`、`source_id=<eval_run_id>`，其 payload 中的
`binding_sha256` 必须等于锁定 Bundle。Promptfoo 输出必须转换为
`auris.promptfoo-eval-result.v1` canonical JSON，并登记为
`source_type=promptfoo_eval_result`、`source_id=<eval_run_id>` 的结果对象。适配器会校验对象
scope、状态、locator、内容哈希、Content-Type、EvalRun 绑定和六套件强 Schema，随后只生成
标准 `RunCompletionReceiptRequest` 载荷。该载荷仍需走现有完成回执鉴权、锁定 Bundle
重校验及 `LabelEvalResult` 门禁重算；Promptfoo 元数据固定标记为
`authoritative=false`，对象存储和 Promptfoo 都不是业务事实源。当前文件只提供可替换接口，
未直接接入 `run_service`，以避免外部工具成为核心运行状态机的隐式依赖。

## LLM Result Cache Key

`app/services/llm_result_cache_key.py` 只生成版本化 Redis-compatible key 和可审计元数据，
不读写 Redis，也不产生标签事实。`auris:llm-result:v1:<sha256>` 的稳定摘要强制包含：

- `tenant_id/project_id`（防止跨租户缓存复用）；
- task、model、Prompt content SHA-256；
- taxonomy/label SHA-256、Schema SHA-256；
- canonical generation params；
- normalized input SHA-256（元数据不保留原始输入）。

`trace_id`、`request_id`、`timestamp` 明确只属于 runtime context，不进入摘要或返回元数据；
它们若混入 generation params 会直接被拒绝。API key、Authorization、token 等秘密字段及
NaN/Infinity 同样禁止进入 key。调用方应使用 `normalized_input_sha256()` 完成 Unicode
NFKC、换行和首尾空白规范化，再通过 `build_llm_result_cache_key()` 生成 key。缓存 miss、
过期或 Redis 故障必须回源模型及 MySQL 强版本，Redis 不能作为 Observation、Aggregate、
Eval 或 Release 的事实来源。

## Logging

后端统一使用 `app.core.logging`：

- `get_logger("component")` 获取组件日志句柄。
- `log_event(logger, "event.name", ctx=ctx, **fields)` 输出结构化 JSON。
- 请求、幂等、运行、审计、outbox、seed 都必须带 `trace_id/request_id` 或等价上下文字段。

## Observability

`OTEL_ENABLED=false` 时不会构造 exporter、启动批处理线程或发起网络连接。生产 Compose 通过
OTLP/HTTP 把 BFF 与 worker 的 FastAPI、SQLAlchemy、Redis、HTTP client span 发往 Collector；
业务 `trace_id/request_id` 写入 span attributes，结构化日志同时注入 W3C
`otel_trace_id/otel_span_id`。最终导出边界会删除 Authorization、Cookie、查询串、SQL 和秘密值；
Collector 不可用只会丢失遥测，不改变业务响应或 outbox 状态。

Prometheus 端点固定为根路径 `/metrics`，不属于 `/api/v1`。默认关闭；启用后只信任真实 socket
peer 的内部地址或 `METRICS_TRUSTED_CIDRS` 显式网段，忽略 `X-Forwarded-For`，边缘代理也不公开
转发该路径。指标 label 只使用 method、路由模板、状态类别、固定依赖名、固定 monitor action、
时长桶和有限 outcome，不使用 tenant/project/user/resource/trace ID。端点包含 HTTP、认证失败、
依赖 readiness、outbox backlog/死信/重试/最老事件、callback attempt、worker、数据库连接池、
限流结果，以及从 MySQL 权威 `run_records` 派生的 TaskRun 终态、24 小时滚动完成时长、deadline/
status-sync overdue 与自动 monitor control 指标。当前 Redis 结果缓存尚未实现，因此不输出虚构的
cache hit ratio；Redis 的实际业务指标是固定窗口限流 outcome。

## Verify

```bash
../scripts/verify_fast.sh
```

迁移兼容验证默认创建临时 SQLite 数据库，并在 `0018` 写入非零 κ、定义为零 κ、
未定义零 κ、零 annotation Gold 和旧 submission 格式后再升级 `0019`。如需验证
MySQL 8.4，必须提供独立、可销毁且为空的迁移测试库；验证会执行完整 upgrade / downgrade，
不能指向应用 `DATABASE_URL`：

```bash
MIGRATION_DATABASE_URL='mysql+pymysql://root:auris_root@127.0.0.1:3306/auris_flow_migration_test' \
  .venv/bin/python scripts/verify_migrations.py
```

如果没有激活 venv，也可以从仓库根目录显式指定解释器：

```bash
PYTHON=backend/.venv/bin/python bash scripts/verify_fast.sh
```

公开发布候选必须从仓库根目录运行完整 release gate：

```bash
bash scripts/verify_release.sh
```

该命令默认还会运行真实依赖栈专项门禁，并会拒绝 `AURIS_SKIP_REAL_STACK_E2E=1`。受限开发机请改跑 `bash scripts/verify_fast.sh` 或 `AURIS_RUN_E2E=1 bash scripts/verify_all.sh` 做开发验证，但不能把结果作为公开发布候选。真实依赖栈专项门禁需要 Docker，并会使用 MySQL、Redis、MinIO 和 Qdrant，而不是临时 SQLite：

```bash
AURIS_REAL_STACK_E2E=1 bash scripts/verify_ui_bff_e2e.sh
```

该模式会以 `DEPENDENCY_CHECK_MODE=strict` 启动 BFF，`/readyz` 必须确认 `database/redis/object_storage/qdrant/dagster` 全部可用；脚本会启动一个 Dagster-compatible GraphQL fake endpoint，并启用 `AURIS_DAGSTER_ADAPTER=real`，让 `verify_e2e_outbox_dispatch.py` 校验 run request、run key、tags 和 request hash；同时启用 `AURIS_QDRANT_ADAPTER=real`、`AURIS_OBJECT_STORAGE_ADAPTER=real` 和 `AURIS_EXTERNAL_CALLBACK_ADAPTER=real`，真实写入并回读 Qdrant 知识索引 point、MinIO 对象 manifest，并向 fake 平台回调服务发送 HMAC 签名请求、校验 receipt/log。Dagster 仍只作为执行引擎映射，不作为产品 API 命名暴露；fake endpoint 不等于生产 Dagster 集群，fake callback 不等于真实 CRM/工单平台。
