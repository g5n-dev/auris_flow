# 后端开发运行手册

本文档定义阶段 0/1 本地开发、环境变量、依赖服务、启动顺序和排查方法。真实后端代码创建后，命令以本文件为准。

## 1. 本地依赖

| 服务 | 用途 | 默认端口 |
| --- | --- | --- |
| FastAPI BFF | 前端访问入口 | `8000` |
| MySQL 8 | 权威业务状态 | `3306` |
| Redis | 幂等锁、短期运行状态、限流 | `6379` |
| MinIO | 本地对象存储 | `9000` / `9001` |
| Qdrant | 向量召回 | `6333` |
| Dagster / Dagster-compatible GraphQL | 执行引擎或本地协议测试桩 | `3000` / 临时端口 |
| Platform Callback Fake | 外部平台回调协议测试桩 | 临时端口 |
| OTel Collector | Trace 汇聚 | `4317` / `4318` |

## 2. 环境变量

```bash
APP_ENV=local
APP_NAME=auris-flow-bff
API_PREFIX=/api/v1

DATABASE_URL=mysql+asyncmy://auris:auris@127.0.0.1:3306/auris_flow
REDIS_URL=redis://127.0.0.1:6379/0

OBJECT_STORAGE_ENDPOINT=http://127.0.0.1:9000
OBJECT_STORAGE_BUCKET=auris-flow-local
OBJECT_STORAGE_ACCESS_KEY=minioadmin
OBJECT_STORAGE_SECRET_KEY=minioadmin

QDRANT_URL=http://127.0.0.1:6333
AURIS_QDRANT_ADAPTER=local
DAGSTER_GRAPHQL_URL=http://127.0.0.1:3000/graphql
AURIS_DAGSTER_ADAPTER=local
DAGSTER_REPOSITORY_LOCATION_NAME=auris_flow_defs
DAGSTER_REPOSITORY_NAME=auris_flow
DAGSTER_DEFAULT_JOB_NAME=auris_flow_generic_job
AURIS_EXTERNAL_CALLBACK_ADAPTER=local
EXTERNAL_CALLBACK_URL=http://127.0.0.1:8089/callbacks/platform
EXTERNAL_CALLBACK_SECRET=auris-dev-callback-secret
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317

JWT_PUBLIC_KEY_PATH=./secrets/local_jwt_public.pem
JWT_ISSUER=auris-flow-local
JWT_AUDIENCE=auris-flow-bff
```

敏感变量不得提交到 git。示例值应写入 `.env.example`，真实值由本地 `.env` 或部署环境注入。

## 3. 启动顺序

1. 启动 MySQL、Redis、MinIO、Qdrant、Dagster 或 Dagster-compatible GraphQL 测试桩、Platform Callback fake、OTel Collector。
2. 执行数据库迁移：`alembic upgrade head`。
3. 执行本地 seed：`python -m app.seed local_demo`。
4. 启动 BFF：`uvicorn app.main:app --reload --port 8000`。
5. 启动 Worker：`python -m app.workers.outbox_worker`。
6. 前端继续使用现有 `http://127.0.0.1:5173`，后续通过 proxy 指向 BFF。

## 4. 健康检查

必须提供：

- `GET /healthz`：进程存活，不检查下游。
- `GET /readyz`：检查 MySQL、Redis、对象存储、Qdrant、Dagster GraphQL 连接。真实栈 E2E 中 Dagster 项使用协议测试桩，只证明 run request 已提交并可回读 receipt，不证明生产 Dagster job 完成。
- `GET /api/v1/insights/ops-summary`：带租户/项目上下文的业务 smoke。

`readyz` 失败不能返回敏感连接信息，只返回服务名、状态和 trace。

## 5. 规格静态校验

每次修改 `api-contract.md`、`openapi-v0.1.yaml` 或 `mock-to-api-map.md` 后，先在仓库根目录运行以下检查：

```bash
python3 doc/backend-spec/validate_backend_spec.py
```

如果需要排查脚本失败原因，可拆成以下检查：

```bash
ruby -e "require 'yaml'; YAML.load_file('doc/backend-spec/openapi-v0.1.yaml'); puts 'openapi yaml ok'"

python3 - <<'PY'
from pathlib import Path
import re, yaml
api = Path("doc/backend-spec/api-contract.md").read_text()
openapi = yaml.safe_load(Path("doc/backend-spec/openapi-v0.1.yaml").read_text())
ops = {
    ("/api/v1" + path, method.upper())
    for path, spec in openapi["paths"].items()
    for method in spec
    if method.lower() in {"get", "post", "patch", "put", "delete"}
}
operation_ids = []
missing_operation_ids = []
declared = []
for path, spec in openapi["paths"].items():
    for method, op in spec.items():
        if method.lower() not in {"get", "post", "patch", "put", "delete"}:
            continue
        operation_id = op.get("operationId")
        if not operation_id:
            missing_operation_ids.append((method.upper(), path))
        else:
            operation_ids.append(operation_id)
for match in re.finditer(r'`((?:GET|POST|PATCH|DELETE|PUT)\s+/api/v1/[^`]+)`', api):
    method, path = match.group(1).split(" ", 1)
    item = (path.split("?")[0], method)
    if item not in declared:
        declared.append(item)
missing = [(method, path) for path, method in declared if (path, method) not in ops]
duplicate_operation_ids = sorted({op_id for op_id in operation_ids if operation_ids.count(op_id) > 1})
assert not missing, missing
assert not missing_operation_ids, missing_operation_ids
assert not duplicate_operation_ids, duplicate_operation_ids
print(f"api coverage ok: {len(declared)}/{len(declared)}")
print("operationId ok")
PY

python3 - <<'PY'
from pathlib import Path
import yaml
openapi = yaml.safe_load(Path("doc/backend-spec/openapi-v0.1.yaml").read_text())
missing_idempotency = []
missing_paging = []
critical_generic = []
typed_resource_prefixes = (
    "/work-items",
    "/event-links",
    "/data-assets",
    "/traces",
    "/label-optimization-runs",
)
for path, spec in openapi["paths"].items():
    for method, op in spec.items():
        if method not in {"get", "post", "patch", "put", "delete"}:
            continue
        for code, response in (op.get("responses") or {}).items():
            if not isinstance(response, dict):
                continue
            response_ref = response.get("$ref", "")
            if response_ref.endswith(("GenericObject", "GenericList")) and path.startswith(typed_resource_prefixes):
                critical_generic.append((method.upper(), path, code, response_ref))
        refs = [p.get("$ref", "") for p in op.get("parameters", []) if isinstance(p, dict)]
        if method in {"post", "patch", "put", "delete"} and not any("IdempotencyKey" in ref for ref in refs):
            missing_idempotency.append((method.upper(), path))
        if method == "get" and ("列表" in op.get("summary", "") or path.endswith("s")):
            if not any("Cursor" in ref for ref in refs) or not any("Limit" in ref for ref in refs):
                missing_paging.append((method.upper(), path, op.get("summary", "")))
assert not missing_idempotency, missing_idempotency
assert not missing_paging, missing_paging
assert not critical_generic, critical_generic
print("idempotency and paging ok")
print("critical typed responses ok")
PY

rg -n "T[O]DO|F[I]XME|待[补]|待[定]|data-assets/\{i[d]\}" doc/backend-spec -S || true
```

最后一条 `rg` 正常情况下应无输出。若需要保留业务语义里的“等待”等中文状态，不要使用上面的占位词。

## 6. 本地联调账号

默认 seed 后可用：

| 账号 | 角色 | 用途 |
| --- | --- | --- |
| `admin@auris.local` | `project_admin` | 管理、发布、设置、回填审批 |
| `release.approver@auris.local` | `project_admin` | 以独立自然人完成发布门禁复核，不得审批自己发起的发布 |
| `operator@auris.local` | `business_operator` | 首页、洞察、报告 |
| `annotator@auris.local` | `annotator` | 调听、人审、标签候选 |
| `model@auris.local` | `model_engineer` | 评测、badcase、Prompt 优化 |

开发环境可以显式设置 `ALLOW_DEV_AUTH=true` 使用固定测试 token。生产或发布环境不能使用本地演示 token；过渡期签名 token 的 payload 必须包含 `tenant_ids`、`project_ids`、`sub`、`roles`、`exp`，并由服务端密钥签名。生产前端不能把共享 bearer token 注入 JS bundle。

## 7. 常见排查

### 7.1 403 权限失败

检查：

- token 中 `tenant_id` 与 `X-Tenant-Id` 是否一致。
- `project_members` 是否存在对应用户。
- 动作是否属于高风险，需要审批或更高角色。
- 是否跨项目访问且未开启脱敏引用策略。

### 7.2 幂等冲突

检查：

- 同一个 `Idempotency-Key` 是否被不同请求体复用。
- `request_hash` 是否稳定。
- replay 是否命中已有成功响应。
- Worker 是否重复消费同一 outbox 事件。

### 7.3 运行一直 pending

检查：

- `outbox_events.status` 是否仍为 `pending`。
- Worker 是否启动。
- Redis 锁是否卡住。
- `available_at` 是否在未来。
- 事件处理器是否注册。

### 7.4 blocked 无法继续

检查：

- `run_errors` 或 `gate` 记录中的 `blocked_reason`。
- 是否需要 HumanReviewTask 决策。
- 是否缺上游资产、标签版本、模型版本或外部连接器授权。
- RBAC 是否要求双人审批。

### 7.5 Qdrant 召回为空

检查：

- MySQL 源对象是否已生成 chunk。
- payload 是否包含 `tenant_id`、`project_id`、`asset_key`。
- 查询是否带正确 payload filter。
- 索引质量门禁是否失败。

## 8. 日志与 Trace

日志必须是结构化 JSON，至少包含：

```text
timestamp
level
message
trace_id
request_id
tenant_id
project_id
user_id 或 service_account_id
object_type
object_id
error_code
```

禁止记录：

- 明文 token、密钥、session。
- 原始音频、完整转写。
- 客户手机号、身份证、完整姓名等敏感明文。
- 对象存储签名 URL。

## 8. 开发完成定义

任一后端模块完成必须满足：

- OpenAPI 路径和 schema 已更新。
- Alembic 迁移已创建并可从空库执行。
- Unit、Contract、Integration 测试通过。
- 写操作有幂等、审计和 trace。
- 高风险动作有 blocked、人审或审批路径。
- 前端 UI Projection 不泄漏 Dagster、Qdrant、对象存储内部细节。
