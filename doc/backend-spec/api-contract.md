# Auris Flow 后端 API 契约

本文档定义 Auris Flow 一期 FastAPI BFF 的后端开发就绪契约。依据：

- `doc/设计文档.md` 的“基于当前原型的后端开发设计基线”。
- `doc/UI设计文档.md` 的“当前原型到后端 API 的交互契约”。
- `prototype/auris-flow-ui/src/App.tsx` 中 `/api/v1` mock 文案和页面状态。

一期目标不是暴露底层 MySQL、Qdrant、对象存储或 Dagster，而是由 BFF 输出稳定的业务资源、UI Projection、权限校验、运行状态、错误格式和 trace。

## 1. 基线边界

- API 统一使用 `/api/v1/*`。
- URL 资源名使用复数、kebab-case，例如 `/api/v1/task-runs`。
- 前端只访问 FastAPI BFF；不直接访问 MySQL、Qdrant、对象存储或 Dagster。
- MySQL 是租户、项目、连接器、任务、音频、标签、评测、资产、知识源、人审、审计和运行状态的权威业务状态。
- Redis 只保存版本化缓存、短期锁和幂等加速，不保存 LabelObservation、LabelAggregate、LabelFact、PromptVersion 或 ReleaseDeployment 的权威状态。
- Qdrant 只做向量召回。前端看到的是知识命中、相似证据、标签候选、badcase 或洞察解释，不看到 collection、vector、embedding。
- Dagster 只作为执行引擎适配层。业务 API 返回任务版本、运行、资产、血缘、质量检查、回填、导出和回写结果，不把 Dagster UI 概念直接推给业务用户。
- 一期不以 ClickHouse 作为默认组件。洞察和大盘先使用 MySQL 聚合表、预计算结果、Redis 缓存和 Qdrant 召回解释。

## 2. 统一 API 规则

### 2.1 认证上下文

所有非公开接口必须有认证用户和租户项目上下文。浏览器产品路径使用通用 OIDC
Authorization Code + PKCE 和 BFF 不透明会话；服务到服务或兼容工具可使用经后端验证的
bearer。Keycloak 只作为参考 IdP，认证契约只依赖标准 OIDC discovery、authorization、token
与 JWKS 接口，不使用 Keycloak 私有角色或管理 API。

浏览器登录与会话接口：

| API | 语义 | 安全约束 |
| --- | --- | --- |
| `GET /api/v1/auth/oidc/login?return_path=/insights` | 生成 state、nonce、PKCE S256 challenge 并 303 到 IdP | `return_path` 仅接受站内绝对路径；state/nonce/verifier 短期、一次性 |
| `GET /api/v1/auth/oidc/callback` | 一次性消费 state、交换 code、校验 ID token 并 303 回站内页面 | 精确校验 issuer/audience、RS256 签名、exp/iat、nonce；未知 `kid` 只允许刷新 JWKS 后重试；IdP token 不返回浏览器 |
| `GET /api/v1/auth/session` | 由 bearer 或 HttpOnly cookie 恢复内部用户、scope 与当前角色 | cookie 可省略 scope Header；响应 `Cache-Control: no-store`，浏览器会话轮换 `csrf_token` |
| `POST /api/v1/auth/logout` | 幂等撤销本地 BFF 会话并清除 cookie | cookie 路径必须同时验证 `X-CSRF-Token` 与受信 `Origin`；开发 bearer 路径保留 scope Header 兼容；不宣称终止 IdP 全局 SSO 会话 |

`prod/release` cookie 固定命名为 `__Host-auris_session`，并设置 `HttpOnly; Secure;
SameSite=Lax; Path=/` 且不设置 Domain。cookie 是高熵不透明值，MySQL 仅保存 token SHA-256
与 CSRF SHA-256；OIDC access token、ID token、refresh token 和 cookie/CSRF 原值不得写入数据库、
浏览器持久存储或日志。本地 HTTP 可配置 `auris_session`，但不改变生产约束。
当前 scope 禁止 `offline_access`，BFF 不保留 refresh token；会话过期后客户端必须重新发起 Code +
PKCE。目标 IdP 如支持 RP-Initiated Logout，应作为独立可选集成验证，不能把本地撤销伪装为全局登出。

浏览器 cookie 请求可省略 scope Header，由服务端使用会话冻结的 tenant/project；如果显式传入，
必须精确匹配。bearer 和需要显式选择 scope 的兼容调用使用：

```http
Authorization: Bearer <access_token>
X-Request-Id: <client_request_id>
X-Tenant-Id: <tenant_id>
X-Project-Id: <project_id>
```

前端不得把 bearer 写入 `localStorage` 或 `sessionStorage`。所有 OIDC 不透明 cookie 认证的 POST/PATCH/PUT/DELETE
还必须带内存中的 CSRF token 和浏览器 Origin：

```http
X-CSRF-Token: <session_bound_csrf_token>
Origin: https://flow.example.com
```

写操作、异步动作和外部回写还必须带：

```http
Idempotency-Key: <tenant_id>:<project_id>:<business_key>:<operation>
```

BFF 解析后的 `auth_context` 至少包含：

```json
{
  "tenant_id": "aurora_auto",
  "project_id": "sales_qa",
  "user_id": "u_1001",
  "roles": ["project_admin"],
  "permissions": ["task:run", "review:decide"],
  "store_scope": ["BJ-AURORA-001"],
  "data_scope": ["audio", "events", "labels"]
}
```

校验规则：

- `tenant_id` 是强隔离边界，任何查询、写入、运行、召回、导出都必须带租户过滤。
- `project_id` 是工作空间边界，跨项目读取默认拒绝；需要共享时只返回脱敏引用。
- OIDC `(issuer, subject)` 必须先映射到明确 provision 的 `oidc_identities`；未知主体 default-deny，响应不回显 subject。请求 tenant/project 必须同时与 identity 及已签发 browser session 的冻结 scope 精确一致；同租户内第二项目的成员资格不能扩张原会话。
- 每次认证都读取 `user_security_states`、identity、租户、项目与当前项目成员角色；ID token/session 中的旧角色不授权，用户禁用、identity 禁用和角色降权即时生效。
- 浏览器写请求必须同时校验 CSRF token 和显式 allowlist Origin；缺失、错误或跨站请求返回稳定 403，且不执行写入。
- `store_id`、`date`、`model_version`、`label_version` 是业务筛选上下文，不替代租户项目校验。
- 权限失败返回 `403 forbidden`，不要返回空列表伪装成功。
- 找不到资源且用户无权确认其存在时，返回 `404 not_found`，避免泄漏跨租户对象。
- 认证失效/禁用返回稳定 401；跨 scope 的对象存在性按 404 隐藏；错误 envelope 不包含 token、subject、内部异常或配置。

### 2.2 分页、筛选、排序和搜索

列表接口必须支持：

```http
GET /api/v1/audio-sessions?cursor=<opaque>&limit=50&status=pending&q=报价&sort=-started_at
GET /api/v1/data-assets?asset_key=auris/label/event_tags&created_at[after]=2026-07-01T00:00:00Z
```

规则：

- `limit` 默认 50，最大 200。
- 大列表使用 cursor 分页；管理类小列表可兼容 `page`、`per_page`，但响应仍要给 `next_cursor`。
- `sort` 使用逗号分隔，多字段排序；前缀 `-` 表示降序。
- 多值筛选使用逗号，例如 `status=pending,blocked`。
- 范围筛选使用 bracket notation，例如 `started_at[after]`、`confidence[lte]`。
- 搜索统一使用 `q`，只做当前租户项目内搜索。

列表成功响应：

```json
{
  "data": [
    {
      "id": "as_128",
      "status": "pending",
      "created_at": "2026-07-06T02:00:00Z",
      "updated_at": "2026-07-06T02:10:00Z"
    }
  ],
  "meta": {
    "limit": 50,
    "has_next": true,
    "next_cursor": "eyJpZCI6ImFzXzEyOCJ9",
    "status_counts": {
      "pending": 12,
      "blocked": 3
    },
    "trace_id": "tr_01J..."
  },
  "links": {
    "self": "/api/v1/audio-sessions?limit=50"
  }
}
```

### 2.3 通用响应

单对象：

```json
{
  "data": {
    "id": "task_run_128",
    "status": "running",
    "trace_id": "tr_01J..."
  },
  "meta": {
    "trace_id": "tr_01J...",
    "request_id": "req_01J..."
  }
}
```

写操作：

```json
{
  "data": {
    "id": "task_run_128",
    "status": "pending",
    "affected_objects": [
      {
        "object_type": "task_version",
        "object_id": "tv_19_rc2",
        "effect": "read"
      },
      {
        "object_type": "data_asset",
        "object_id": "auris/label/event_tags",
        "effect": "will_materialize"
      }
    ],
    "next_actions": [
      {
        "type": "poll",
        "href": "/api/v1/task-runs/task_run_128"
      }
    ],
    "trace_id": "tr_01J..."
  },
  "meta": {
    "idempotency_key": "aurora_auto:sales_qa:tv_19_rc2:manual_run",
    "trace_id": "tr_01J..."
  }
}
```

### 2.4 错误响应

错误必须使用 HTTP 状态码表达语义，同时返回标准错误体。

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "请求参数校验失败",
    "details": [
      {
        "field": "run_config.partition_key",
        "message": "partition_key 不能为空",
        "code": "required"
      }
    ],
    "status": 422,
    "retryable": false,
    "trace_id": "tr_01J..."
  }
}
```

状态码和错误码：

| HTTP | code | 使用场景 | 前端状态 |
| --- | --- | --- | --- |
| 400 | `INVALID_JSON` | JSON 无法解析、参数格式错误 | `failed` |
| 401 | `UNAUTHORIZED` | 未登录、token 过期 | `blocked` |
| 403 | `FORBIDDEN` | 无租户、项目、角色或数据权限 | `blocked` |
| 404 | `NOT_FOUND` | 当前上下文下资源不存在 | `failed` |
| 409 | `CONFLICT` | 版本冲突、幂等键请求体不一致、重复发布 | `retry` 或 `blocked` |
| 409 | `STATE_CONFLICT` | 当前状态不允许该动作 | `blocked` |
| 422 | `VALIDATION_ERROR` | 业务字段校验失败 | `failed` |
| 422 | `RELEASE_GATE_BLOCKED` | 发布门禁未通过 | `blocked` |
| 422 | `HOTWORD_SENSITIVE_TERM_FORBIDDEN` / `HOTWORD_DIAGNOSTICS_REQUIRED` | 敏感词入包，或请求热词版本的成功完成回执缺少诊断 | `blocked` |
| 409 | `HOTWORD_VERSION_NOT_PUBLISHED` / `HOTWORD_RESOURCE_VERSION_CONFLICT` | 候选词包用于生产，或乐观锁版本冲突 | `blocked` / `retry` |
| 409 | `LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE` / `RELEASE_HEAD_CAS_CONFLICT` / `STALE_LABEL_VERSION` | 制品仍被环境引用、Head generation 漂移、人工 draft 标签版本已过期 | `blocked` / `retry` |
| 422 | `INSIGHT_LABEL_VERSION_REQUIRED` / `INSIGHT_MAPPING_BUNDLE_REQUIRED` / `LABEL_MAPPING_COVERAGE_GAP` / `LABEL_MAPPING_RECOMPUTE_REQUIRED` | 标签指标 scope 不完整、归一化缺 Bundle、映射覆盖缺口或拆分必须重算 | `blocked` |
| 423 | `APPROVAL_REQUIRED` | 回填、覆盖、发布需要人工审批 | `blocked` |
| 429 | `RATE_LIMIT_EXCEEDED` | 触发限流 | `retry` |
| 500 | `INTERNAL_ERROR` | 未预期错误 | `failed` |
| 503 | `UPSTREAM_UNAVAILABLE` | 外部源、对象存储、执行引擎不可用 | `retry` |

### 2.5 trace 和审计

所有响应都必须返回 `trace_id`。以下对象还必须持久化 trace：

- 写操作：`trace_id`、`request_id`、`idempotency_key`、`created_by`。
- 异步运行：`run_id`、`trace_id`、`run_key`、`status`、`retry_count`。
- Agent 或模型调用：`agent_run_id`、`model_version`、`prompt_version`、`provider_ref`。
- 外部回写：`external_callback_id`、`remote_trace_id`、`receipt_status`。
- 资产物化：`materialization_id`、`asset_key`、`partition_key`、`run_id`。

标签闭环的人审使用双 Trace 语义：`trace_id` 继承任务冻结的 `source_trace_id`，作为 Prompt → Observation → Aggregate → Review → Eval → Release 的根链；本次审核 HTTP 请求的 Trace 单独写 `action_trace_id`。决策、Feedback、LabelFact、候选 LabelVersion/PromptVersion、审计与 Outbox 都保留这两个值，禁止用操作请求 Trace 覆盖根 Trace。

Header：

```http
X-Trace-Id: tr_01J...
X-Request-Id: req_01J...
```

内部排障入口：

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/traces/{trace_id}` | 查询一次请求、运行、模型调用、外部回写或资产生成链路 | `trace_id`、`request_id`、`spans[]`、`related_runs[]`、`related_assets[]`、`errors[]` |

审计日志最低字段：

```json
{
  "audit_id": "audit_128",
  "tenant_id": "aurora_auto",
  "project_id": "sales_qa",
  "actor_user_id": "u_1001",
  "action": "label_version.publish",
  "object_type": "label_version",
  "object_id": "lv_19_rc2",
  "before_ref": "snapshot://...",
  "after_ref": "snapshot://...",
  "trace_id": "tr_01J...",
  "created_at": "2026-07-06T02:00:00Z"
}
```

### 2.6 幂等

必须强制幂等的接口：

- `POST /api/v1/task-runs`
- `POST /api/v1/task-runs/{id}/retries`
- `POST /api/v1/task-runs/{id}/cancellations`
- `POST /api/v1/task-runs/{id}/status-syncs`
- `POST /api/v1/knowledge-sources/{id}/sync-runs`
- `POST /api/v1/knowledge-indexes/{id}/build-runs`
- `POST /api/v1/label-optimization-runs`
- `POST /api/v1/label-extraction-runs`
- `POST /api/v1/label-observations`
- `POST /api/v1/label-calibration-versions`
- `POST /api/v1/label-aggregation-policies`
- `POST /api/v1/label-aggregation-runs`
- `POST /api/v1/human-review-decision-batches`
- `POST /api/v1/prompt-assets`
- `POST /api/v1/prompt-versions`
- `POST /api/v1/prompt-version-candidates/{candidate_id}/review-submissions`
- `POST /api/v1/prompt-version-candidates/{candidate_id}/adjudications`
- `POST /api/v1/label-aggregates/{aggregate_id}/review-submissions`
- `POST /api/v1/label-aggregates/{aggregate_id}/adjudications`
- `POST /api/v1/label-taxonomy-suggestions/{suggestion_id}/review-submissions`
- `POST /api/v1/label-taxonomy-suggestions/{suggestion_id}/adjudications`
- `POST /api/v1/label-optimization-trigger-scans`
- `POST /api/v1/release-deployments`
- `POST /api/v1/release-deployments/{deployment_id}/monitor-samples`
- `POST /api/v1/release-deployments/{deployment_id}/transitions`
- `POST /api/v1/release-deployments/{deployment_id}/bootstrap-active-head`
- `POST /api/v1/eval-runs`
- `POST /api/v1/data-assets/{asset_key}/backfills`
- `POST /api/v1/exports`
- `POST /api/v1/output-sinks/platform-callbacks`
- `POST /api/v1/human-review-tasks/{id}/decisions`

规则：

- `Idempotency-Key` 在同一 `tenant_id`、`project_id`、`user_id` 下唯一。
- 同 key 且请求体 hash 一致，返回第一次的结果。
- 同 key 但请求体 hash 不一致，返回 `409 idempotency_conflict`。
- Redis 保存短期幂等记录；高风险动作必须同时落 MySQL 审计。
- 异步动作的 `run_key` 应由业务唯一键组成，例如 `task_version_id + partition_key + trigger + operator_id`。

### 2.7 异步动作

统一状态：

| 状态 | 含义 | 前端表现 |
| --- | --- | --- |
| `pending` | 已创建，等待执行 | pending、按钮禁用、可轮询 |
| `running` | 执行中 | pending、显示进度或阶段 |
| `submitted` | 已提交执行引擎，等待状态或业务完成回执 | pending、显示外部执行已接单 |
| `completion_pending` | 执行引擎已报告成功，但可信业务结果尚未物化 | pending、继续等待签名完成回执 |
| `cancelling` | 已请求安全终止，等待执行引擎确认 | pending、禁用重复取消 |
| `success` | 完成并写入结果 | success、可查看结果 |
| `failed` | 失败，可查看错误 | failed、可重试 |
| `blocked` | 权限、门禁、依赖或审批阻断 | blocked、展示原因和处理入口 |
| `cancelled` | 用户或系统取消 | failed 或中性结束态 |

标签闭环运行在上述传输状态之上返回业务 `stage`：`queued → running → materializing → awaiting-review/evaluating → shadowing → gray-releasing → monitoring → completed/blocked/rolled-back`。静态 ToolCall 计划、Outbox 已派发或执行引擎已接单都不是业务成功；只有强类型结果已物化且门禁完成，才能进入对应完成态。

前端事实语义固定如下：

- HTTP `202` 表示请求已接收，不表示完成；`status=queued/running/materializing` 时必须轮询详情。
- `status=blocked` 必须展示 `blocked_reasons[]` 和 `next_action`，不得显示成功 toast。
- LabelVersion、PromptVersion 或发布 Bundle 只有权威对象明确返回 `published`，或 ReleaseDeployment 完成 `promote` 且返回 `status=completed` 后，才能显示“已发布”。
- `trace_id`、`locked_versions`、阻断项和下一动作由后端返回，联调模式不得用 mock 指标覆盖。

异步动作响应最低字段：

```json
{
  "data": {
    "id": "run_128",
    "object_type": "task_run",
    "status": "pending",
    "run_key": "task_v19:manual:2026-07-06:BJ-AURORA-001",
    "trigger": "manual",
    "progress": {
      "stage": "queued",
      "percent": 0
    },
    "affected_objects": [],
    "next_actions": [
      {
        "type": "poll",
        "href": "/api/v1/task-runs/run_128"
      },
      {
        "type": "open_trace",
        "href": "/api/v1/traces/tr_01J..."
      }
    ],
    "trace_id": "tr_01J..."
  }
}
```

### 2.8 列表、详情、动作接口模式

列表接口：

```http
GET /api/v1/{resources}?cursor=&limit=&q=&sort=-updated_at&status=
```

最低响应字段：

- `data[]`：列表投影。
- `meta.limit`、`meta.has_next`、`meta.next_cursor`。
- `meta.status_counts`：状态筛选条需要。
- `meta.trace_id`。

详情接口：

```http
GET /api/v1/{resources}/{id}
```

最低响应字段：

- `data.id`、`data.status`、`data.version`。
- `data.tenant_id`、`data.project_id`。
- `data.created_at`、`data.updated_at`。
- `data.created_by`、`data.updated_by`。
- 详情页需要的 `relationships` 或业务化下钻链接。

动作接口：

```http
POST /api/v1/{resources}/{id}/{action-resources}
PATCH /api/v1/{resources}/{id}
```

规则：

- 能建成资源的动作使用名词资源，例如 `build-runs`、`sync-runs`、`feedback-tasks`、`backfills`。
- 单对象字段变更使用 `PATCH`。
- 发布、回填、导出、回写必须返回 `status`、`trace_id`、`affected_objects`、`next_actions`。

## 3. 一期 API 清单和最低字段

### 3.1 首页

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/insights/ops-summary` | 首页待办、异常、运行概览、趋势入口 | `pending_review_count`、`blocked_count`、`running_count`、`risk_facts[]`、`trend_cards[]`、`trace_id` |
| `GET /api/v1/human-review-tasks` | 首页复核队列摘要 | `id`、`queue`、`title`、`priority`、`status`、`evidence_ref`、`assignee_rule`、`created_at` |
| `GET /api/v1/data-assets/recent` | 最近资产和异常资产 | `asset_key`、`display_name`、`status`、`quality_score`、`freshness`、`latest_run_id`、`materialization_id` |
| `GET /api/v1/work-items` | 首页和洞察生成的处理草稿列表 | `id`、`source_signal`、`action_type`、`owner`、`status`、`affected_objects`、`trace_id` |
| `POST /api/v1/work-items` | 从首页创建可追踪处理草稿 | `source_signal`、`action_type`、`evidence_ref`、`owner`、`status`、`affected_objects`、`trace_id` |
| `GET /api/v1/work-items/{id}` | 处理草稿详情和执行链路 | `id`、`source_signal`、`evidence_ref`、`decision_log[]`、`status`、`next_actions[]`、`trace_id` |
| `PATCH /api/v1/work-items/{id}` | 更新处理草稿状态、负责人或下一步动作 | `changed_fields`、`owner`、`status`、`audit_log_id`、`trace_id` |

### 3.2 租户

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/tenants` | 租户列表和切换器 | `id`、`name`、`status`、`quota_policy`、`member_count`、`project_count`、`asr_binding_status` |
| `GET /api/v1/tenants/{id}` | 租户详情 | `id`、`name`、`status`、`quota_policy`、`audit_scope`、`members[]`、`created_at`、`updated_at` |
| `POST /api/v1/tenants` | 创建租户边界 | `name`、`admin_user_id`、`quota_policy`、`asr_binding_ref`、`status`、`trace_id` |
| `PATCH /api/v1/tenants/{id}` | 修改租户状态、配额、授权 | `changed_fields`、`quota_policy`、`asr_binding_ref`、`audit_log_id`、`trace_id` |

租户对象最低字段：

```json
{
  "id": "tenant_aurora",
  "tenant_code": "aurora_auto",
  "name": "极光汽车",
  "status": "active",
  "quota_policy": {
    "audio_hours_per_month": 5000
  },
  "asr_binding_status": "connected",
  "created_at": "2026-07-06T02:00:00Z",
  "updated_at": "2026-07-06T02:00:00Z"
}
```

### 3.3 项目

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/projects` | 项目列表和当前项目切换 | `id`、`tenant_id`、`name`、`scene`、`status`、`owner_user_id`、`label_version`、`quality_target` |
| `GET /api/v1/projects/{id}` | 项目详情 | `id`、`data_source_refs[]`、`member_scope[]`、`quality_target`、`settings_ref`、`audit_summary` |
| `POST /api/v1/projects` | 创建项目工作空间 | `tenant_id`、`name`、`scene`、`owner_user_id`、`quality_target`、`trace_id` |
| `PATCH /api/v1/projects/{id}` | 更新成员范围、质量目标、数据源绑定 | `changed_fields`、`member_scope`、`data_source_refs`、`quality_target`、`audit_log_id` |

### 3.4 连接器和外部接入

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/connectors` | 连接器配置列表 | `id`、`type`、`name`、`status`、`auth_mode`、`last_sync_at`、`owner` |
| `POST /api/v1/connectors` | 创建连接器配置 | `type`、`name`、`base_url`、`auth_mode`、`secret_ref`、`status`、`trace_id` |
| `PATCH /api/v1/connectors/{id}` | 更新连接器配置 | `changed_fields`、`secret_ref`、`status`、`audit_log_id`、`trace_id` |
| `POST /api/v1/platform-connections/{connection_id}/session` | 生成外部平台会话引用 | `session_id`、`access_token_ref`、`tenant_scope[]`、`expires_at`、`trace_id` |
| `GET /api/v1/data-sources/{source_id}/records` | 读取租户、员工、门店等主数据 | `resource_id`、`tenant_id`、`store_id`、`display_name`、`cursor`、`raw_payload_ref` |
| `POST /api/v1/audio-ingest/recordings` | 接入录音 URL 或原始音频引用 | `recording_id`、`audio_url_ref`、`device_id`、`started_at`、`duration_ms`、`asset_key` |
| `GET /api/v1/authenticated-events` | 读取接待、报价、试驾、风险事件 | `event_id`、`event_type`、`occurred_at`、`employee_id`、`recording_id`、`risk_hint` |
| `POST /api/v1/platform-sync-jobs` | 创建平台同步批次 | `sync_job_id`、`mode`、`resource_types[]`、`status`、`asset_keys[]`、`trace_id` |

### 3.5 任务配置

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/task-types` | 任务类型和节点模板 | `id`、`name`、`category`、`input_schema`、`output_schema`、`default_policy` |
| `GET /api/v1/task-versions` | 任务版本列表 | `id`、`task_type_id`、`version`、`status`、`canvas_variant`、`label_version`、`model_version` |
| `POST /api/v1/task-versions` | 保存任务草稿 | `task_type_id`、`flow_template`、`node_bindings[]`、`schedule`、`output_bindings[]`、`status` |
| `PATCH /api/v1/task-versions/{id}` | 修改草稿节点、连线、调度、门禁 | `changed_fields`、`node_bindings`、`run_config`、`validation_summary`、`trace_id` |
| `POST /api/v1/task-runs` | 手动运行、定时运行、事件触发或回填运行 | `task_run_id`、`task_version_id`、`trigger`、`run_key`、`status`、`partition_key`、`trace_id` |
| `GET /api/v1/task-runs` | 运行记录列表 | `id`、`task_version_id`、`status`、`trigger`、`started_at`、`finished_at`、`retry_count` |
| `GET /api/v1/task-runs/{id}` | 运行详情 | `id`、`status`、`progress`、`error`、`asset_outputs[]`、`trace_id`、`dagster_binding_ref` |
| `POST /api/v1/task-runs/{id}/retries` | 失败或死信运行的人工重试，创建新的运行记录 | `retry_run_id`、`retry_of_run_id`、`retry_of_event_id`、`retry_of_trace_id`、`status`、`trace_id` |
| `POST /api/v1/task-runs/{id}/cancellations` | 创建独立取消控制运行；未分发任务本地取消，已绑定 Dagster 任务执行 SAFE_TERMINATE | `run_id`、`run_type=task_run_cancellation`、`source_run_id`、`control_action=cancel`、`status`、`trace_id` |
| `POST /api/v1/task-runs/{id}/status-syncs` | 创建独立状态同步控制运行；读取可信 Dagster binding 并收敛状态 | `run_id`、`run_type=task_run_status_sync`、`source_run_id`、`control_action=status_sync`、`status`、`trace_id` |

TaskRun 的 `deadline_at`、`next_status_sync_at` 与 `monitor_generation` 全部由服务端生成，
`POST /task-runs` 对同名或其他控制面字段执行 `additionalProperties=false` 拒绝。生产 Worker 周期扫描
权威 `run_records`：超时且仍 pending 的原始 Outbox 在同一事务内撤销；已有可信 Dagster binding 的
运行进入 `cancelling` 并只生成一个 deadline cancel control；未超时的
`submitted/running/completion_pending` 按间隔生成单调代次的 status-sync control。多 Worker 使用行锁、
确定性 ID 与 Outbox fencing 去重；观察到引擎 `SUCCESS` 只能进入 `completion_pending`，仍须匹配且验签
的业务完成回执才能进入 `success`。
| `POST /api/v1/task-versions/{id}/publish` | 发布任务版本 | `version`、`release_gate`、`affected_objects`、`rollback_version`、`trace_id` |

`POST /api/v1/task-runs` 只接受业务输入。`run_id/task_run_id`、`job_name`、`pipeline_name`、
`repository_name`、`repository_location_name`、`dagster_run_draft` 和 `run_config` 均为服务端控制面字段，
调用方注入时返回 422；运行 ID、Dagster job 与 run config 由服务端从已发布 TaskVersion 和固定部署配置生成。

任务运行最低字段：

```json
{
  "id": "task_run_128",
  "task_version_id": "tv_19_rc2",
  "status": "running",
  "status_version": 2,
  "trigger": "manual",
  "run_key": "sales_qa:tv_19_rc2:2026-07-06:BJ-AURORA-001",
  "partition_key": "2026-07-06|BJ-AURORA-001",
  "submitted_at": "2026-07-06T02:00:01Z",
  "started_at": "2026-07-06T02:00:00Z",
  "deadline_at": "2026-07-06T04:00:00Z",
  "next_status_sync_at": "2026-07-06T02:01:01Z",
  "monitor_generation": 0,
  "engine_status": "STARTED",
  "engine_status_observed_at": "2026-07-06T02:00:02Z",
  "asset_outputs": [
    {
      "asset_key": "auris/label/event_tags",
      "materialization_id": "mat_128"
    }
  ],
  "trace_id": "tr_01J..."
}
```

Dagster `SUCCESS` 只证明执行引擎结束，状态同步最多把业务运行推进到 `completion_pending`；
只有 external ID 与可信 launch binding 完全一致的签名完成回执才能写入 `success`。签名回执若早于
launch 最终提交到达，会先以 `pending_binding` 持久化；绑定不一致时回执标记 `rejected` 并写审计，
不得修改业务终态。取消与完成并发时依赖 scoped row lock 和单向状态机只提交一个终态事件。

### 3.6 数据管理

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/audio-sessions` | 音频会话列表和聚合叶子对象 | `id`、`recording_id`、`store_id`、`employee_id`、`started_at`、`duration_ms`、`status`、`confidence` |
| `GET /api/v1/audio-sessions/aggregations` | 按时间、门店、销售、单据事件聚合 | `group_key`、`group_label`、`count`、`risk_count`、`sample_session_id`、`filters` |
| `GET /api/v1/audio-sessions/{id}` | 调听详情入口 | `id`、`boundary`、`tracks[]`、`event_links[]`、`review_tasks[]`、`asset_refs[]` |
| `PUT /api/v1/audio-sessions/{id}/recording-object` | 幂等登记 MinIO / S3 / OBS / OSS 录音对象元数据 | `recording_id`、`storage_object_id`、`provider`、`bucket`、`object_key`、`content_length`、`checksum_sha256`、`etag`、`trace_id` |
| `POST /api/v1/audio-sessions/{id}/playback-grants` | 为原生媒体元素签发短时播放授权 | `audio_session_id`、`playback_url`、`expires_at`、`trace_id` |
| `GET /api/v1/audio-playback?grant=...` | 校验播放授权和当前项目成员关系后，按已登记 `provider` 代理 MinIO / S3 / OBS / OSS 录音字节 | 单区间 `Range` 原样下推，返回 `200/206/416`、`Accept-Ranges`、`Content-Range`、`ETag`、`X-Storage-Object-Id`、`X-Storage-Provider`；Provider 未独立配置时返回 `503`，禁止跨 Provider 复用凭据 |
| `POST /api/v1/audio-sessions/{id}/intelligence-runs` | 创建 VAD、ASR、说话人分离、声纹和质量检测运行 | 请求：`execution_mode`、`language`、`hotword_pack_version_id` nullable、`return_word_timestamps`；响应：`audio_session_id`、`recording_id`、`capabilities[]`、`output_assets[]`、`trace_id` |
| `GET /api/v1/voiceprints` | 声纹对象列表 | `id`、`speaker_id`、`employee_ref`、`status`、`confidence`、`source_asset` |
| `GET /api/v1/event-links` | 音频和业务单据事件关联 | `id`、`audio_session_id`、`event_id`、`document_ref`、`match_score`、`status`、`risk_hint` |
| `GET /api/v1/event-links/{id}` | 事件关联详情、差异和证据窗口 | `id`、`audio_session_id`、`event_ref`、`join_keys[]`、`diffs[]`、`evidence_refs[]`、`trace_id` |
| `POST /api/v1/event-links` | 创建接待单、报价单、试驾单等事件关联 | `audio_session_id`、`event_ref`、`join_keys[]`、`diffs[]`、`status`、`trace_id` |
| `PATCH /api/v1/event-links/{id}` | 修复或改绑事件关联 | `target_event_ref`、`reason`、`status`、`affected_objects`、`trace_id` |
| `GET /api/v1/data-aggregation-views` | 数据页聚合视图配置和当前选择 | `id`、`name`、`aggregate_order[]`、`selected_leaf`、`filters`、`version` |
| `PATCH /api/v1/data-aggregation-views/{id}` | 保存聚合优先级、叶子对象状态和关联修复 | `aggregate_order[]`、`selected_leaf`、`relation_fix`、`version`、`trace_id` |

`AudioIntelligenceRunRequest` 的新写入不得使用旧 `hotwords_ref`；该字段只允许在历史 TaskVersion 响应中以 `legacy-read-through` 方式读取。`execution_mode=production` 的热词运行必须提交已发布 `task_version_id`，BFF 从不可变 TaskVersion 恢复 `hotword_pack_version_id + provider + language + root_trace_id`，客户端不得逐次覆盖；候选版本只能直接用于 `shadow`。当运行实际绑定热词版本时，成功完成回执必须在 `result_ref.hotword_diagnostics` 返回与冻结版本一致的 `matched_terms`、`missed_terms`、`false_boosted_terms` 和 `diagnostics_storage_object_id`；当 `return_word_timestamps=true` 时还必须返回 `word_timestamps_storage_object_id`，否则 `POST /api/v1/runs/{id}/completion-receipts` 返回 422。完整词时间戳与诊断保存在对象存储，MySQL 只保存引用、计数和聚合。

### 3.7 知识库

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/knowledge-sources` | 知识源列表 | `id`、`name`、`type`、`status`、`sync_status`、`chunk_count`、`index_version` |
| `GET /api/v1/knowledge-sources/{id}` | 知识源详情 | `id`、`source_ref`、`chunk_policy`、`quality_gate`、`last_sync_run`、`consumer_paths[]` |
| `POST /api/v1/knowledge-sources/{id}/sync-runs` | 同步一次知识源 | `sync_run_id`、`status`、`source_id`、`affected_chunks`、`trace_id` |
| `GET /api/v1/knowledge-indexes` | 索引版本列表 | `id`、`source_id`、`index_version`、`embedding_profile`、`status`、`quality_score` |
| `POST /api/v1/knowledge-indexes/{id}/build-runs` | 构建索引 | `build_run_id`、`status`、`index_id`、`quality_gate`、`trace_id` |
| `GET /api/v1/knowledge-indexes/{id}/effects` | 召回效果和消费路径 | `recall_rate`、`hit_examples[]`、`miss_examples[]`、`consumer_paths[]`、`trace_id` |

### 3.8 调听和证据链

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/audio-sessions/{id}` | 会话、边界、轨道、ASR、事件、人审上下文 | `id`、`boundary`、`speaker_turns[]`、`asr_segments[]`、`label_spans[]`、`event_links[]` |
| `PATCH /api/v1/conversation-boundaries/{id}` | 保存完整对话开始和结束 | `start_ms`、`end_ms`、`reason`、`status`、`affected_tracks[]`、`trace_id` |
| `POST /api/v1/evidence-packs` | 创建证据包 | `audio_session_id`、`window`、`evidence_refs[]`、`labels[]`、`status`、`evidence_pack_id` |
| `GET /api/v1/evidence-packs/{id}` | 查看证据包详情和导出引用 | `id`、`audio_session_id`、`window`、`evidence_refs[]`、`export_refs[]`、`trace_id` |
| `GET /api/v1/human-review-tasks` | 调听复核队列 | `id`、`queue`、`status`、`priority`、`sample_ref`、`evidence_ref`、`asset_key` |
| `GET /api/v1/human-review-tasks/{id}` | 人审任务详情 | `id`、`title`、`detail`、`evidence_refs[]`、`candidate_result`、`decision_history[]` |
| `POST /api/v1/human-review-tasks/{id}/decisions` | 接受、修改、拒绝、升级仲裁，并同步写回候选标签、事件关联和证据包 | `decision`、`decision_id`、`affected_objects`、`status`、`trace_id` |

调听边界规则：

- `conversation-boundary` 只保存完整对话边界。
- ASR、说话人、标签轨、事件关联跟随边界重建，不在边界接口里保存单独偏移。
- 串音、金额冲突、低置信进入 `human-review-tasks`，不自动覆盖人工结论。

### 3.9 标签治理

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/labels` | 标签体系和规则列表 | `id`、`label_domain`、`label_group`、`label`、`value_or_action`、`status`、`owner` |
| `GET /api/v1/label-versions` | 标签制品版本列表 | `id`、`taxonomy_id`、semantic version、artifact status/timestamps、base/replacement、content hash、各环境 activation 摘要 |
| `POST /api/v1/label-versions` | 创建候选标签版本或草稿 | `taxonomy_id`、`base_label_version_id`、`changeset[]`、`status=draft`、`trace_id` |
| `GET /api/v1/label-versions/{id}` | 标签制品、版本项与环境激活时间线 | 制品字段、immutable items、replacement/mapping、activation timeline、影响运行、next actions、`trace_id` |
| `PATCH /api/v1/label-versions/{id}` | 保存规则、Prompt、正负例、冲突策略 | `changed_fields`、`prompt_version`、`rule_candidates[]`、`trace_id` |
| `POST /api/v1/label-versions/{id}/evaluation-lock` | 冻结标签、Prompt、模型、聚合策略和评测集绑定 | `expected_resource_version`、`prompt_version_id`、`model_version`、`aggregation_policy_version_id`、`eval_dataset_version_id`、`optimization_run_id`、`confirmation` |
| `POST /api/v1/label-optimization-runs` | 标签候选抽取和优化运行 | `optimization_run_id`、`candidate_version`、`status`、`sample_set`、`trace_id` |
| `GET /api/v1/label-optimization-runs/{id}` | 标签或 Prompt 优化运行详情 | `optimization_run_id`、`status`、`candidate_version`、`suggestions[]`、`eval_summary`、`trace_id` |
| `POST /api/v1/label-versions/{id}/publish` | 兼容发布入口 | 仅可发布不可变制品并创建/委托 ReleaseDeployment command；不得直接改生产 Head、灰度比例或回滚指针 |
| `POST /api/v1/human-review-tasks` | 生成标签人审任务 | `type`、`candidate_ref`、`evidence_refs[]`、`assignee_rule`、`status` |

LabelVersion artifact lifecycle 与 environment activation lifecycle 必须分离。目标制品状态为 `draft/candidate/validated/locked/evaluating/gate_blocked/review_required/approved/published/deprecated/archived`；legacy `gray_releasing/rollback_pending/rolled_back` 在迁移期只读映射到 Release activation ledger。每个环境当前有效版本只由 `release-bundle-heads` 决定。

以下是 S3/S5/S6/S8 待实现的冻结资源路径，不属于当前 live runtime operation table，不能以空壳 2xx 路由冒充完成：

- `/api/v1/label-versions/{id}/deprecation-preflights`：冻结 expected resource version，返回 active/draining 环境、在途运行，以及按 `impact_type:resource_id` 游标分页（`impact_limit<=100`）的 FactSet、Label MetricResult、冻结 ReportDocument 绑定和可识别下游资产；每项给出 `blocking/migration-required/historical-reference`，冻结历史快照只提示不阻断，当前 Head/进行中候选/发布下游阻断；扫描超服务端上限 fail-closed。
- `/api/v1/label-versions/{id}/transitions`：制品 deprecate/archive；replacement 时必须绑定已发布 `mapping_bundle_id`，且无受保护环境引用。
- `/api/v1/label-mapping-versions` 与 `/api/v1/label-mapping-bundles`：不可变 edge/bundle 的 validate、review、publish、coverage 与 compiled path。
- `/api/v1/audio-sessions/{session_id}/annotations/{annotation_id}/submissions` 与 `/rebases`：人工 draft 强绑定 label/version/event/evidence/occurred_at；过期版本显式 rebase。
- `/api/v1/label-recompute-runs` 及 Item completion/retry：候选 FactSet/Asset Manifest 重算；整批晋级统一复用 `/api/v1/label-fact-sets/{id}/promotions` 的 Head CAS，不另建第二套 promotion 真相源。

#### 3.9.1 标签观察、聚合与事实闭环

MySQL 是标签事实唯一权威源。`LabelObservation` 是不可变来源观察，`LabelAggregate` 是绑定策略版本的可重放聚合，`LabelFact` 只由 L2 低风险自动接受或 Human Loop 终态产生；Redis/Qdrant 均不得充当事实源。Qdrant 只用于未知标签聚类、相似 badcase 与边界样本召回。

| API | 用途 | 请求锁定 / 响应最低字段 |
| --- | --- | --- |
| `POST /api/v1/label-extraction-runs` | 创建真实模型抽取运行 | 请求必须锁定 `label_version_id`、`prompt_version_id`、`model_version`、`schema_version`、`aggregation_policy_version_id`、输入哈希、规范 subject refs 和唯一 `source_bindings[]`；L2 每个 subject 必须有可验证 `evidence_ref`。`202` 仅返回 queued 运行事实 |
| `GET /api/v1/label-extraction-runs/{extraction_run_id}` | 轮询抽取、Observation 与自动聚合物化 | `status`、`observation_count`、`source_bindings`、强 Manifest hash、`aggregation_run_id/aggregate_ids`、`dispatch`、`next_actions[]`、根 `trace_id`；完成回执前不得显示成功 |
| `POST /api/v1/label-observations` | 受信模型/worker 写入不可变观察 | 来源族/type 必须与抽取 Manifest 一致；服务端恢复 provider/adapter/correlation group，验证 subject evidence 或 scoped storage SHA，并按 published 校准器计算置信度。客户端不得伪造强校准或 `human-confirmed` Observation |
| `GET /api/v1/label-observations`、`GET /api/v1/label-observations/{observation_id}` | 按锁定标签版本、subject 读取观察 | 原始标签、canonical `label_id`、value/type、`source_lineage`、`evidence_verification`、校准版本、哈希、`status=materialized`、根 `trace_id` |
| `POST /api/v1/label-calibration-versions` | 从锁定 Gold 创建 append-only 校准版本 | 锁定 label/version、来源族、方法、GoldSetVersion、参数和指标；published 要求同 LabelVersion 的稳定 Gold，isotonic/Platt/全局保守最少分别 200/100/50 条，系统身份不得发布 |
| `GET /api/v1/label-calibration-versions`、`GET /api/v1/label-calibration-versions/{calibration_version_id}` | 读取服务端锁定校准器 | 方法/参数、样本量、Gold、training manifest、content SHA-256、状态与 Trace；Qdrant/Redis 或客户端字段不得替代 |
| `POST /api/v1/label-aggregation-policies` | 创建 L1/L2 聚合策略版本 | 锁定 `label_version_id`、mode、来源权重、校准版本、阈值、标签定义/别名/风险/互斥/层级；响应 `policy_version_id`、canonical hash、状态、`trace_id` |
| `GET /api/v1/label-aggregation-policies`、`GET /api/v1/label-aggregation-policies/{policy_version_id}` | 读取聚合策略版本 | 完整策略、阈值、校准版本和 canonical hash |
| `POST /api/v1/label-aggregation-runs` | 对显式 Observation 集执行确定性聚合 | 请求锁定 `label_version_id`、`policy_version_id`、`mode`、唯一 `observation_ids[]`；`202` 返回 `aggregation_run_id`、`status=awaiting-review/completed`、结果哈希、aggregate/review/taxonomy refs、`trace_id` |
| `GET /api/v1/label-aggregation-runs/{aggregation_run_id}` | 重放并核对聚合运行 | 输入/结果哈希、观察/聚合计数、阻断的人审/Taxonomy 引用、`trace_id` |
| `GET /api/v1/label-aggregates`、`GET /api/v1/label-aggregates/{aggregate_id}` | 读取聚合候选和全部贡献解释 | subject/label/value、score/margin/risk、decision/status、reason codes、策略/校准版本、成员贡献、证据哈希、确定性哈希、候选级 `review_task_id`、`trace_id` |
| `GET /api/v1/label-taxonomy-suggestions` | 读取无法 canonicalize 的未知标签建议 | `suggestion_id`、锁定标签版本、normalized/raw labels、observation refs、影响数、候选动作、候选级人审任务、状态、`trace_id` |

抽取成功回执必须一次性通过强 Schema 并物化全部 Observation；服务端随后以 `extraction_run_id + policy_version_id + sorted(observation_ids)` 生成确定性 AggregationRun ID，自动执行聚合并把 `aggregation_run_id/aggregate_ids/review_task_ids` 回写抽取运行。客户端不得再重复 POST 聚合来制造第二条闭环。

聚合写入必须先按证据哈希、重叠区间和服务端来源相关组去重/去相关，再按锁定策略执行；同一可信相关组的重复或冲突切片只取最大可信贡献。硬优先级、布尔/分类/数值/时间/层级算子和服务端校准必须固化在策略版本；无法映射的自由文本只能进入 `label-taxonomy-suggestions`，不能直接成为线上标签。

#### 3.9.2 Human Loop、反馈与批量决策

| API | 用途 | 最低字段 / 约束 |
| --- | --- | --- |
| `POST /api/v1/human-review-tasks/{id}/decisions` | 对单个 Aggregate、Prompt 候选或 Taxonomy 建议作终态决策 | 显式 `target_refs`；接受、修改、拒绝、升级；修改包含字段 diff 和原因；终态恰好一次；响应/持久化同时给根 `trace_id` 与本次 `action_trace_id` |
| `POST /api/v1/human-review-decision-batches` | 批量处理服务端判定可批的低风险 Aggregate | 请求 `items[]`；只允许同标签、同低风险、同策略 cohort；响应 `batch_id`、`status=completed/partial/failed`、逐项 `success/skipped/failed`、原因码、`trace_id` |
| `POST /api/v1/label-aggregates/{aggregate_id}/review-submissions`、`POST /api/v1/label-taxonomy-suggestions/{suggestion_id}/review-submissions` | 提交高风险 Aggregate 或 Taxonomy 候选的双盲密封审核 | 一任务一目标；同一自然人只能提交一次；两份一致才形成终态，不一致进入 `awaiting-adjudication`；终态前不得泄漏密封内容 |
| `POST /api/v1/label-aggregates/{aggregate_id}/adjudications`、`POST /api/v1/label-taxonomy-suggestions/{suggestion_id}/adjudications` | 对两份不一致结论做独立仲裁 | 仅人工 `project_admin/review_arbitrator`；仲裁人不得是任一盲审人；终态原子写入 LabelFact/Taxonomy 候选、Gold Feedback、审计和 Outbox |

每个 Aggregate/Candidate 独立绑定一个人审任务，决策只作用于任务的显式 `target_refs`。普通接受仅生成 `gold_status=candidate` 的 `human-confirmed` FeedbackExample；修改/拒绝同时生成结构化 diff 与 `capability=labeling` Badcase。只有双评一致或仲裁完成后才能晋级锁定 Gold。

#### 3.9.3 Prompt 强版本与自动优化

| API | 用途 | 请求锁定 / 响应最低字段 |
| --- | --- | --- |
| `POST /api/v1/prompt-assets` | 创建标签/Prompt 优化能力的逻辑 Prompt 资产 | `prompt_asset_id`、name、`capability=labeling|prompt-optimization`、可选标签版本；响应状态/current version/trace |
| `GET /api/v1/prompt-assets`、`GET /api/v1/prompt-assets/{prompt_asset_id}` | 读取 Prompt 资产 | capability、绑定标签版本、状态、当前版本、`trace_id` |
| `POST /api/v1/prompt-versions` | 持久化真实 PromptVersion | 锁定 asset/父版本/标签/Schema/模型；保存 P-CODE 模板、输出 Schema、生成参数、结构化 diff、来源 badcase；响应内容 SHA-256、状态、`trace_id` |
| `GET /api/v1/prompt-versions`、`GET /api/v1/prompt-versions/{prompt_version_id}` | 读取真实模板及版本链 | 正文、Schema、父版本、diff、参数、badcase refs、内容 hash、状态、`trace_id` |
| `POST /api/v1/prompt-version-candidates/{candidate_id}/review-submissions` | 提交 Prompt 候选双盲密封审核 | `decision`、可选 note；modified 必须提供仅限模板/Schema/生成参数的结构化 diff；返回密封状态、已收审核数和下一动作 |
| `POST /api/v1/prompt-version-candidates/{candidate_id}/adjudications` | 对两份不一致密封审核做独立仲裁 | 仅独立 `review_arbitrator`；请求 decision/reason/diff，响应候选终态、`adjudication_id`、下一动作和 `trace_id` |
| `POST /api/v1/label-optimization-trigger-scans` | 扫描阈值/定时触发并在同一范围单活创建优化运行 | 请求锁定标签/Prompt/模型/聚合策略/评测集版本和预算；响应 `scan_id/run_id`、trigger reasons/hash、metrics/provenance、`blocked_reasons[]`、`next_action`、`stage/status`、`trace_id` |
| `GET /api/v1/label-optimization-trigger-scans/{run_or_scan_id}` | 查询扫描或优化运行 | 阶段、锁定版本、预算、指标、阻断项、下一动作、`trace_id` |
| `POST /api/v1/label-optimization-schedules` | 创建或更新可运行的 15m/daily/weekly 自动优化计划 | 每个 tenant/project/label version 唯一；锁定 Prompt/模型/聚合策略/评测集、2–5 候选、3 轮/2h/成本预算和 IANA 时区；幂等写入 |
| `GET /api/v1/label-optimization-schedules`、`GET /api/v1/label-optimization-schedules/{schedule_id}` | 查询计划、due 时钟与当前单活 session | 返回 `next_threshold_scan_at/next_daily_at/next_weekly_at`、`active_run_id`、baseline snapshot、预算、资源版本和 Trace |
| `GET /api/v1/label-optimization-schedules/{schedule_id}/metric-snapshots` | 查询权威触发指标快照 | append-only 24h metrics、规范 `reason_code` 聚类、非法 JSON/自由文本 reason 拒绝记录、确定性 hash 与来源 |
| `GET /api/v1/label-optimization-schedules/{schedule_id}/rounds` | 查询候选→锁定 EvalRun→预算决策轮次 | generation/eval run、候选 IDs、收益/关键回退/成本、stop reason、`awaiting-review`；最多三轮且不自动发布 |
| `GET /api/v1/prompt-version-candidates`、`GET /api/v1/prompt-version-candidates/{id}` | 旧前端兼容投影 | 仅作为 PromptVersion 候选投影；不得替代 `prompt-assets/prompt-versions` 权威版本 |

预算上限为 3 轮、每轮 2–5 个候选、最长 2 小时；同一租户/项目/标签版本单活，canonical trigger hash 去重并执行 24 小时冷却。独立 scheduler worker 或 Dagster schedule 调用相同 `run_once`，使用数据库条件更新作为 scope mutex；过密 blocked 探测不推进 15 分钟时钟。每次 reconcile 都从该 session 最早 Round 的 `started_at` 计算墙钟；达到 2 小时即把仍处于 queued/submitted/running 的 generation 与 EvalRun 全部置 `blocked(time_budget_exceeded)`，关闭 `active_run_id` 并发出 hard-stop 审计/事件，不能让卡住的外部运行绕过预算。候选物化后自动创建完整锁定 EvalRun，评测完成后进入下一轮、`blocked` 或 `awaiting-review`。自动化范围锁定 L1→L2，Prompt、Taxonomy、聚合策略及正式发布仍需自然人批准。

Prompt 审核的 `modified` 不是原地批准：两份密封修改一致或独立仲裁选择修改时，原候选固定为 `revision-required`；服务端应用允许字段 diff，创建 content-hash 去重的 child PromptVersion（`parent_version_id=原候选`）、对应兼容候选投影和全新双盲任务。只有 child 再通过审核与锁定评测，才可进入发布 Bundle。

#### 3.9.4 发布 Bundle

| API | 用途 | 请求锁定 / 响应最低字段 |
| --- | --- | --- |
| `POST /api/v1/release-deployments` | 创建不可变发布 Bundle | 锁定完整 Bundle；门禁失败返回 `blocked`，通过只进入 `pending` 并创建 publish ReleaseCommand，受信 ACK 前绝不显示 shadowing |
| `GET /api/v1/release-deployments`、`GET /api/v1/release-deployments/{deployment_id}` | 读取发布事实和状态时间线 | 锁定 Bundle、stage/status、`pending_command_id/pending_run_id/pending_action`、在线指标、阻断项、回滚目标、批准人、`trace_id` |
| `GET /api/v1/release-bundle-heads/{environment}` | 读取环境唯一有效 Bundle | active deployment/bundle、Prompt/Label/模型/策略/数据集绑定、CAS `generation`、bootstrap/command 来源与 Trace |
| `POST /api/v1/release-deployments/{deployment_id}/bootstrap-active-head` | 迁移期一次性确认初始 production LKG | 仅自然人项目管理员；目标只因缺少 rollback/head 而 blocked、无 active command/歧义旧 head，且强 Bundle 可重验；确认后原子设为 completed/100% 并建立 head，不得用于日常绕过 ACK |
| `POST /api/v1/release-deployments/{deployment_id}/monitor-samples` | system-only 写入类型化在线窗口并执行自动保护 | 无退化进入 monitoring；硬退化有稳定目标时只创建 rollback command 并进入 materializing，受信 ACK 后才 rolled-back；无目标 safe-stop blocked |
| `POST /api/v1/release-deployments/{deployment_id}/transitions` | 请求人工批准 10% gray、人工晋级或回滚命令 | `202` 创建唯一 active ReleaseCommand/RunRecord；`expected_status`、命令 SHA、Bundle、head generation/deployment/hash 全部冻结，客户端不得提交监控事实 |

有效路径固定为 `pending → materializing → shadowing → materializing → gray-releasing(10%) → monitoring → materializing → completed`；每次 publish/approve-gray/promote/rollback 只在可信执行 ACK 精确回显 `release_command_id/command_sha256/deployment/environment/action/bundle_sha256/applied=true`、重验 Bundle 且 active head CAS 成功后生效。ACK 不匹配、Bundle 漂移或 generation 冲突转 `blocked`，不会切换 Prompt/Bundle 指针。人工 gray/promote 不得由系统代签；UI 只有部署 `completed`、PromptVersion `published` 且 `release-bundle-heads` 指向该 Bundle 时显示成功。

`stable_window_complete` 默认 `false`，只能由 system 监控样本形成并写入 ReleaseDeployment 的权威 `monitor_metrics`；人工 transition 请求中的自由 `monitor_metrics` 不能伪造该事实。只有它为 `true` 且 JSON、冲突、关键 recall 与成本等其余门禁全部通过，`promote` 才允许创建执行命令；正式完成仍以受信 ACK 和 head CAS 为准。

`capability=labeling` 的成功完成回执必须携带强类型 `labeling_eval_result`：六个锁定 suite（Golden、Boundary、Adversarial、Fresh、Canary、Regression）各且仅一次，每个 suite 固化 `sample_count + sample_manifest_sha256 + metrics`；总体样本清单哈希必须可由六套件确定性重算。成对 bootstrap 固定为 95% CI、至少 1000 次重采样，保存随机种子和 paired sample count；该计数必须等于六套件样本数之和。创建 EvalRun、完成回执和发布三个时点都重新校验数据集对象快照、标签/Prompt/聚合策略/优化运行及 binding hash；任一漂移、缺套件、门禁失败或伪造 `passed` 都只能落为 `blocked`，不得成为发布成功事实。

标签候选最低字段：

```json
{
  "id": "lc_quote_001",
  "label_version_id": "lv_19_rc2",
  "label_domain": "汽车销售质检",
  "label_group": "报价",
  "label": "报价金额",
  "value_or_action": "金额冲突",
  "evidence_span": "12:27:18-12:27:50",
  "confidence": 0.82,
  "human_state": "pending",
  "prompt_version": "prompt_quote_guard_v19_rc2",
  "model_version": "tagger-llm-2026.06",
  "trace_id": "tr_01J..."
}
```

### 3.10 洞察

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `POST /api/v1/insights/metric-runs` | 冻结 scope 并创建不可变指标物化运行 | `metric_keys[]`、`time_range`；标签派生指标还必须带 `label_scope={taxonomy_mode,source_label_version_ids,target_label_version_id,mapping_bundle_id,fact_set_generation,fact_as_of,metric_definition_versions,timezone,period_boundary,denominator_definition}` |
| `GET /api/v1/insights/metrics` | 按 `source_run_id` 与重复 `metric_key` 精确读取唯一 current 快照；重复或缺失时 409 fail-closed | 同一 `metric_result_id` 冻结的 value/unit/sample、`label_version_applicability`、完整 label scope、scope/source/content SHA、服务端双快照 `comparison`、evidence、Trace |
| `GET /api/v1/insights/metric-comparisons` | 比较显式 baseline/current 两个不可变物化快照 | 完整 baseline/current anchor、`comparison_status`、`reason_codes`、`continuous_trend_allowed`、`comparison_sha256` |
| `GET /api/v1/insights/funnels` | 漏斗和桑吉图 | `nodes[]`、`edges[]`、`drop_offs[]`、`filters`、`trace_id` |
| `GET /api/v1/insights/reports` | 报告列表 | `id`、`title`、`status`、`range`、`owner`、`asset_ref`、`created_at` |
| `GET /api/v1/insights/reports/{report_id}` | 读取服务端冻结报告正文及精确指标绑定 | `report_document`、有序 `metric_result_ids[]/metric_results[]`、`report_metric_binding_id/content_sha256`、`metric_scope_sha256`；绑定漂移时 409，禁止客户端拼装正文 |
| `POST /api/v1/insights/reports` | 从同 scope 的已物化快照生成报告草稿 | `metric_result_ids[]`、`metric_scope_sha256`、`evidence_refs[]`、`report_sections[]`、`status`、`trace_id`；不得按 metric key 重查最新事实 |
| `POST /api/v1/insights/actions` | 从洞察生成动作草稿 | `metric_key`、`action_type`、`owner`、`evidence_refs[]`、`work_item_id` |

统计口径固定为 `taxonomy_mode=native|normalized|recomputed`。标签派生指标由服务端指标目录声明 `label_version_applicability=required`，缺完整 label scope 返回 `INSIGHT_LABEL_VERSION_REQUIRED`；非标签指标显式声明 applicability=none。normalized 必须绑定已发布 `mapping_bundle_id`，否则返回 `INSIGHT_MAPPING_BUNDLE_REQUIRED`；split/语义变化无 approved 重算资产时返回 `LABEL_MAPPING_RECOMPUTE_REQUIRED`。

双快照比较必须同时冻结并核对 source/target version、taxonomy mode、mapping bundle ID/SHA、FactSet namespace/ID/generation/manifest SHA、metric definition/version、dimensions、unit、时间窗口规则、`fact_as_of` 单调规则、时区/周期边界/分母定义；不能用单个快照的 mapping path 状态代替跨快照比较。任一锚点不可验证或漂移即 `comparability_status=structural-break`，客户端必须隐藏连续线、目标线和涨跌；没有逐点 scope/comparison 的趋势也不得连线。coverage-gap、structural-break、not-applicable 与数值 0 是不同结果，客户端不得自行计算或覆盖 comparability。

报告生成后只允许展示和导出 `GET /insights/reports/{report_id}` 返回的冻结 `report_document`。客户端必须精确校验创建时的有序 MetricResult ID、三类快照 SHA 与报告 binding hash；缺失、重复、乱序或哈希不合法时整体阻断，不得回退到 fixture、当前筛选值或本地拼接正文。

指标结果必须显式冻结 `result_status=value|not-applicable|zero-denominator|coverage-gap|recompute-required` 与受控 `reason_codes`。真实数值 0 必须是 `result_status=value` 且 `sample_size>=1`；只有受控非数值状态允许 `value=null,sample_size=0`，前端与报告统一展示 `N/A + 原因`，不得转成 0，且双快照比较和连续图表必须断线。

洞察解释可以使用 Qdrant 召回，但响应只返回业务解释和证据链接：

```json
{
  "explanation": "价格异议上升主要集中在北京 SKP 店。",
  "evidence_links": [
    {
      "type": "audio_session",
      "id": "as_128",
      "window": "12:27:18-12:27:50"
    }
  ]
}
```

### 3.11 评测

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/eval-datasets` | 固定集、badcase、人工黄金集的版本列表 | `id`、`name`、`capability`、`dataset_version`、`sample_count`、`status`、`snapshot_sha256` |
| `POST /api/v1/eval-datasets` | 登记对象存储 manifest 和哈希，创建评测集版本草稿 | 请求 `name`、`capability`、`dataset_version`、`manifest_storage_object_id`、`manifest_sha256`、`sample_count`；服务端校验 source/content type/范围/命名空间及强 ETag，响应冻结的 `manifest_provider/bucket/object_key/content_type/size_bytes/etag`、`resource_version`、`root_trace_id` |
| `GET /api/v1/eval-datasets/{id}` | 获取评测集版本及冻结信息 | `dataset_id`、`manifest_storage_object_id`、`manifest_sha256`、`manifest_provider`、`manifest_bucket`、`manifest_object_key`、`manifest_size_bytes`、`manifest_etag`、`snapshot_sha256`、`locked`、`locked_at`、`resource_version` |
| `POST /api/v1/eval-datasets/{id}/lock` | 以 HEAD `Content-Length + strong ETag` 复核真实对象及乐观锁后冻结评测集快照 | 请求 `expected_resource_version`、`confirmation=lock`；响应 `status=locked`、对象冻结字段、`snapshot_sha256`、`root_trace_id` |
| `GET /api/v1/eval-runs` | 评测运行列表 | `id`、`dataset_id`、`status`、`current_version`、`candidate_version`、`started_at` |
| `POST /api/v1/eval-runs` | 运行评测 | `capability=labeling` 必须锁定 dataset version、标签/Prompt/模型/聚合策略与 optimization run，并精确覆盖 `golden/boundary/adversarial/fresh/canary/regression` 六类 suite；返回锁定版本、`status`、`trace_id` |
| `GET /api/v1/eval-runs/{id}` | 评测详情 | `id`、`metrics[]`、`badcases[]`、`release_gate`、`trace_id` |
| `POST /api/v1/eval-runs/{id}/feedback-tasks` | badcase 回流任务 | `feedback_task_id`、`badcase_refs[]`、`target`、`status`、`trace_id` |

### 3.12 ASR 热词治理

热词是 ASR 领域词包，与业务热门标签无关。所有列表按 `cursor/limit` 分页，所有写操作要求 `Idempotency-Key`；更新类请求使用 `expected_resource_version`，响应返回 `resource_version`，冲突返回 409。

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/hotword-statistics` | 按日期、门店、provider、模型和 `hotword_pack_version_id` 读取可信快照中的覆盖率、召回率、易错率、误增强率、影响会话数及 Top 易错词；同时独立返回 ASR 标注修正发现信号 | 可信口径：`data.summary`、`data.items[]`；发现口径：`data.discovery_summary`、`data.discovery_items[]`，含 `annotation_correction_count`、`threshold_met`、`eligible_for_release_gate=false`；共同返回 `data.dimensions` |
| `POST /api/v1/audio-sessions/{id}/annotations`（`annotation_kind=asr-transcript-correction`） | 显式提交“识别文本 → 正确文本”、错误类型和词级证据窗口；严格校验证据、源 Badcase、源词包版本、敏感实体与项目范围，普通标签草稿仍走兼容分支且不计数 | 必填 `confirmation=record_correction`、`track=asr`、`recognized_text`、`corrected_text`、`error_type`、`evidence_storage_object_id`、`hotword_pack_version_id`；返回 `correction_id`、`source_badcase_id`、`stat_eligibility=discovery-only`、Trace 和 `eligible_for_release_gate=false` |
| `POST /api/v1/hotword-analysis-runs` | 创建统计与易错词分析运行 | `date_from`、`date_to`、`store_id`、`provider`、`model_version`、`hotword_pack_version_id`、`run_id`、`status`、`root_trace_id` |
| `GET /api/v1/badcases` | 按 `capability=asr-hotword|labeling|prompt-optimization`、错误/失败类型、状态和版本查询 | ASR 返回词包/识别字段；标签类返回 source、evidence、标签/Prompt/Aggregate/Review refs、期望/实际值、field diff、状态、Trace |
| `POST /api/v1/badcases` | 从 ASR Diff、评测、人工修改/拒绝或 Prompt 失败簇创建 Badcase | `asr-hotword` 保持旧 Schema；`labeling|prompt-optimization` 使用 `failure_reason`、severity、source/evidence refs、锁定版本和结构化 diff；旧客户端兼容 |
| `PATCH /api/v1/badcases/{badcase_id}` | 更新归因、修复建议和看板状态 | `expected_resource_version`、`root_cause`、`fix_suggestion`、`status`、`resource_version` |
| `POST /api/v1/badcases/{badcase_id}/decisions` | 人工确认、驳回或要求补证据 | `decision`、`reason`、`expected_resource_version`、`root_trace_id` |
| `GET /api/v1/hotword-packs` | 热词逻辑包列表、候选基线与生产版本 | `pack_id`、`name`、`language`、`domain`、`current_version_id`、`production_version_id`、`resource_version` |
| `POST /api/v1/hotword-packs` | 创建逻辑词包 | `name`、`language`、`domain`、`pack_id`、`root_trace_id` |
| `GET /api/v1/hotword-packs/{pack_id}/versions` | 不可变版本列表 | `version_id`、`pack_id`、`version`、`baseline_version_id`、`status`、`resource_version` |
| `POST /api/v1/hotword-packs/{pack_id}/versions` | 基于可选基线创建 draft 版本 | `version`、`baseline_version_id`、`manifest_storage_object_id`、`version_id` |
| `GET /api/v1/hotword-pack-versions/{version_id}` | 版本、词项、评测、审批和血缘详情 | `version_id`、`content_sha256`、`items[]`、`eval_run_id`、`eval_locked`、`provider_artifact_ref`、`compiled_provider`、`status` |
| `PATCH /api/v1/hotword-pack-versions/{version_id}` | 用乐观锁推进校验/构建或记录模型批准；请求进入 `validating` 时可指定本次 build 的目标 provider | `expected_resource_version`、`status`、`provider`（仅随 `status=validating`）、`eval_run_id`；客户端不可写 `manifest_storage_object_id/compiled_provider/provider_artifact_ref` |
| `POST /api/v1/hotword-pack-versions/{version_id}/items` | 新增规范词、显式别名、类别、来源类型和 `0–100` 权重 | `canonical_term`、`aliases[]`、`category`、`weight`、`source_type`、`source_badcase_id`、`item_id` |
| `PATCH /api/v1/hotword-pack-versions/{version_id}/items/{item_id}` | 更新 draft 词项 | `expected_resource_version`、可变词项字段、`resource_version` |
| `DELETE /api/v1/hotword-pack-versions/{version_id}/items/{item_id}` | 删除 draft 词项 | query `expected_resource_version`；成功 200 返回 `item_id/version_id/deleted=true`，已冻结版本返回 409 |
| `POST /api/v1/hotword-pack-versions/{version_id}/eval-runs` | 202 创建固定评测集与冻结 provider/版本绑定的影子复测 | 请求仅含 `eval_dataset_id`、`provider`、`expected_resource_version`；响应为 `pending RunAction`，不得携带客户端自报 metrics/gate |
| `POST /api/v1/hotword-pack-versions/{version_id}/publish` | 202 创建人工发布运行 | 请求 `eval_run_id`、`expected_resource_version`、`confirmation=publish`；响应为 `pending RunAction`，此时版本仍未 published |
| `POST /api/v1/hotword-pack-versions/{version_id}/rollback` | 202 由模型负责人发起受控回滚 | 请求 `target_version_id`、源版本 `expected_resource_version`、`reason`；首次 Outbox 处理进入 `blocked`，必须由不同自然人的 `project_admin` 通过 `/api/v1/runs/{run_id}/decisions` 批准 |

构建、评测、发布和回滚都先写 `RunRecord + Outbox`。`PATCH` 中的 `provider` 只是构建目标，必须和 `status=validating` 同时出现；BFF 规范化后把它写入 build RunRecord 的冻结输入，`compiled_provider` 和 `provider_artifact_ref` 只能由受信构建完成回执固化。重新构建会使既有 EvalRun、gate 和模型批准失效。评测请求不接收 baseline/candidate 指标；只有受信 worker 完成回执同时匹配 `version_id + content_sha256 + manifest_storage_object_id + compiled_provider + provider_artifact_ref + eval_dataset_id + dataset_version + manifest_sha256` 时，才物化 metrics/gate 并把版本从 `evaluating` 推进到 `gate_blocked | review_required`。词包发布接口也只入队；受信发布完成回执再次校验锁定 EvalRun、模型负责人批准、不同自然人的项目管理员确认及冻结绑定后，原子写 `published`、更新候选 `current_version_id` 并创建 TaskVersion `draft`，但写入 `production_active=false`。只有该 TaskVersion 再通过独立 ReleaseGate，worker 才在同事务内切换 `production_version_id`、标记版本 `production_active=true` 并写审计/Outbox。回滚冻结源版本、目标历史版本、逻辑包三者的 `resource_version + root_trace_id`；审批或 worker 物化前任何漂移都 fail closed。成功回滚只把当前源版本标记为 `rolled_back` 并恢复候选基线，不伪造生产切换，也不覆盖历史 ASR 或资产物化。

热词分析完成回执的 `metric_snapshots` 必须把诊断、词级时间戳和 Badcase 证据以 `storage_objects` descriptor 原子登记；服务端将其固定为 `source_type=hotword_analysis`、`source_id=run_id`、角色和根 Trace，物化时再次校验。worker 不能自报 `evidence_confidence`：`ground_truth_source=gold | human-confirmed | business-master` 必须引用同版本、同根 Trace 且当前仍为 confirmed 的 `source_badcase_ids`，可信度由服务端映射为 `1.0 / 1.0 / 0.8`；无可验引用时一律按 `discovery=0.4`。分析 worker 只能生成 discovery Badcase，只有人工 decision 可晋级为 human-confirmed；已否决或证据不足的 Badcase 不再参与 Top 易错词等级、人工修正次数和优先级计算。汇总 KPI 或单词的应出现分母为零时返回 `null` 或不进入 Top 词，前端显示 `--`，不伪装为 0%。

### 3.13 数据资产

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/data-assets` | 资产目录、筛选、游标分页 | `asset_key`、`display_name`、`domain`、`status`、`quality_score`、`freshness`、`owner` |
| `GET /api/v1/data-assets/recent` | 首页最近资产 | `asset_key`、`latest_materialization_id`、`latest_run_id`、`status`、`freshness` |
| `GET /api/v1/data-assets/{asset_key}` | 资产详情 | `asset_key`、`definition`、`partition_policy`、`upstream[]`、`downstream[]`、`checks[]` |
| `GET /api/v1/data-assets/{asset_key}/partitions` | 分区窗口 | `partition_key`、`status`、`record_count`、`started_at`、`finished_at` |
| `GET /api/v1/data-assets/{asset_key}/materializations` | 生成记录 | `materialization_id`、`run_id`、`partition_key`、`status`、`checks[]`；热词回填物化另含 `source_materialization_id`、`hotword_pack_version_id`、`eval_run_id`、`task_version_id`、`root_trace_id`、`overwrite_history=false` |
| `GET /api/v1/data-assets/{asset_key}/lineage` | 业务化血缘和下游影响 | `asset`、`nodes[]`、`edges[]`、`materializations[]`；ASR 热词链路节点覆盖原物化、证据、Badcase、词包版本、EvalRun、TaskVersion、回填 Run 与新物化 |
| `POST /api/v1/data-assets/{asset_key}/backfills` | 创建受控回填 | `backfill_id`、`approval_status`、`impact_scope`、`run_request`、`trace_id` |
| `POST /api/v1/data-assets/{asset_key}/checks/retry` | 重跑失败质量校验 | `retry_id`、`status`、`failed_partitions[]`、`trace_id` |
| `POST /api/v1/exports` | 创建导出任务 | `export_job_id`、`format`、`scope`、`status`、`download_ref`、`trace_id` |

资产对象最低字段：

```json
{
  "asset_key": "auris/label/event_tags",
  "display_name": "事件标签资产",
  "domain": "label",
  "status": "risk",
  "quality_score": 91,
  "freshness": "45 分钟内",
  "partition_policy": "daily/store/event_type",
  "latest_materialization_id": "MAT-20250526-tag-1288",
  "latest_run_id": "run-tag-20250526-1288",
  "trace_id": "tr_01J..."
}
```

### 3.14 外部回写和输出

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `POST /api/v1/output-sinks/platform-callbacks` | 回写处理后 WAV、标签、证据包、复核结论 | `callback_id`、`recording_id`、`processed_wav_url_ref`、`labels[]`、`evidence_pack_ref`、`status`、`trace_id` |
| `GET /api/v1/output-sinks/platform-callbacks` | 回写记录和失败队列 | `callback_id`、`status`、`retry_count`、`remote_trace_id`、`dead_letter_reason` |

回写必须：

- 带 `Idempotency-Key`。
- 保存请求体 hash、远端回执、失败原因和重试次数。
- 不把失败吞掉；失败进入可检索的回写记录或死信队列。

### 3.15 设置

| API | 用途 | 最低字段 |
| --- | --- | --- |
| `GET /api/v1/settings` | 设置域列表 | `id`、`domain`、`key`、`value_summary`、`risk_level`、`status`、`owner` |
| `GET /api/v1/settings/{id}` | 设置详情 | `id`、`domain`、`key`、`value`、`policy`、`asset_key`、`audit_history[]` |
| `PATCH /api/v1/settings/{id}` | 保存低风险设置或草稿 | `changed_fields`、`risk_level`、`status`、`audit_log_id`、`trace_id` |
| `POST /api/v1/settings/drafts` | 高风险配置草稿 | `config_domain`、`changed_fields`、`risk_level`、`rollback_version`、`status` |
| `POST /api/v1/settings/publish-requests` | 设置发布审批 | `draft_id`、`approver`、`policy_guard_result`、`status`、`trace_id` |
| `POST /api/v1/settings/provider-tests` | Provider 或服务连通性测试 | `provider_ref`、`test_scope`、`status`、`latency_ms`、`error`、`trace_id` |

设置域：

- `model-chain`
- `provider-route`
- `tagger`
- `judge`
- `tools.audio-intelligence`
- `tools.crosstalk`
- `tools.backfill`
- `tools.document-api`
- `thresholds.low-confidence`
- `thresholds.crosstalk`
- `thresholds.field-diff`
- `policy-guard`
- `permissions.tenant-guard`
- `permissions.human-override`
- `permissions.backfill`
- `permissions.cross-project`
- `storage.object`
- `storage.analytics`
- `storage.business-db`
- `storage.retention`
- `notifications.asset-failure`
- `notifications.human-review`
- `notifications.release-block`
- `notifications.daily-report`

注意：原型设置页可能展示不同存储或分析组件名称，一期实现以基线为准，默认业务库为 MySQL，分析大盘不默认依赖 ClickHouse。

### 3.16 标签 full recompute

| API | 用途 | 最低字段/约束 |
| --- | --- | --- |
| `POST /api/v1/label-recompute-runs` | 冻结真实全量重算请求 | target LabelVersion、可选 Mapping Bundle、source FactSet Head generation/manifest、独立 candidate namespace、fact cutoff、partition/asset scope、coverage、budget；同事务 Audit/Outbox/幂等 |
| `POST /api/v1/label-recompute-runs/{run_id}/items/{item_id}/completions` | 接收分区执行终态并物化候选事实 | attempt generation、可信 completion receipt、目标 Observation/Aggregate lineage；客户端不得提交 row_count 或 manifest SHA，全部由 BFF 从实际行计算 |
| `POST /api/v1/label-recompute-runs/{run_id}/items/{item_id}/retries` | CAS 重试 retryable 失败分区 | expected attempt generation；新 attempt 绑定新内部执行，旧回执/Audit/Outbox 保留 |

`LabelFact.source_kind` 在 Contract 后严格为 `aggregate | human-decision | recompute-run-item`
三选一引用；重算事实只写 candidate FactSet/namespace，不能逐 Fact 切生产 Head。FactSet validate、
人工 approve、promote/rollback 必须重新查询实际分区 Item 和 append-only Fact 行并校验完整 manifest；
生产切换只走单个 FactSet Head generation CAS。API 只暴露业务 Run/Item，不暴露 Dagster 名称、run ID 或画布。

## 4. 前端状态映射

| 后端状态或错误 | 前端状态 | 前端动作 |
| --- | --- | --- |
| `pending` | `pending` | 禁用重复提交，显示“排队中” |
| `running` | `pending` | 展示阶段、进度、轮询详情 |
| `success` | `success` | 展示回执、刷新列表或详情 |
| `failed` | `failed` | 展示错误、允许重试 |
| `blocked` | `blocked` | 展示阻断原因和处理入口 |
| `cancelled` | `failed` 或中性结束态 | 展示取消原因 |
| `409 conflict` | `retry` 或 `blocked` | 版本冲突可刷新，状态冲突需处理 |
| `423 approval_required` | `blocked` | 打开审批、人审或发布门禁 |
| `503 upstream_unavailable` | `retry` | 支持稍后重试 |

前端不应根据文案判断成功失败，只根据 HTTP 状态码、`data.status`、`error.front_state` 和 `retryable` 处理。

## 5. 一期验收口径

- 所有列表接口可分页、筛选、排序和搜索。
- 所有写操作返回 `status`、`trace_id`、`affected_objects`、`next_actions`。
- 所有异步任务可查询运行记录、错误、重试次数、影响对象和 trace。
- 发布、导出、回填、外部回写和人审决策必须有幂等键、审计记录和可解释阻断。
- BFF 必须执行租户、项目、角色和数据范围过滤。
- Qdrant、Dagster、对象存储只通过 BFF 业务投影出现在响应中。
