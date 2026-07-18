# Auris Flow Platform Readiness

本文定义仓库级平台完整性门槛。它验证评测、标注、洞察、资产、任务、身份和异步链路仍属于同一
产品与工程闭环，不代表正式生产发行已经获批。

## 当前机器门槛

- 开源入口：README、贡献/治理/安全/支持、Apache-2.0 文本、NOTICE 候选、第三方许可边界、CI、
  release checklist 与忽略规则。
- 产品契约：`/api/v1/*`、复数资源、kebab-case、统一错误 envelope、OpenAPI、数据库、状态机、
  RBAC、事件、seed、迁移与 mock-to-API 映射。
- 运行基础：FastAPI BFF、OIDC/PKCE、不透明浏览器会话、tenant/project default-deny、结构化脱敏
  日志、trace、幂等、审计、Outbox/Worker、真实存储与 Dagster adapter。
- 产品表面：前端和后端均覆盖标注、评测和洞察，并通过同一 `trace_id`、label/fact/snapshot 版本
  与反馈任务回流，而不是三个孤立页面。
- 生产候选入口：单机 Compose、真实 Dagster、语义 embedding 边界、严格 `/readyz`、受限
  `/metrics`、OTel/Prometheus/Grafana、备份恢复、升级回滚和不可变供应链脚本。

运行结构/契约检查：

```bash
python3 scripts/check_platform_readiness.py
```

运行仓库快速验证：

```bash
bash scripts/verify_fast.sh
```

浏览器级 UI/BFF 验证：

```bash
AURIS_RUN_E2E=1 bash scripts/verify_all.sh
```

该路径自动启动临时 SQLite BFF 与 Vite，验证各模块写动作、tenant/project context、后端对象 ID、
`trace_id`、操作反馈和浏览器无 console/request/failed-response 污染。SQLite 只用于快速契约，不是
生产数据语义证据。

开发真实依赖专项：

```bash
bash scripts/verify_real_stack.sh
```

它实际连接 MySQL、Redis、MinIO 与 Qdrant，但仍使用 Dagster/callback 协议测试夹具和确定性测试
向量；只证明开发 adapter/network I/O，不能替代生产 Compose E2E。

## 严格候选门槛

```bash
python3 scripts/check_platform_readiness.py --release
bash scripts/verify_release.sh
```

严格模式要求 Git 索引包含全部候选源码且无 unstaged/untracked 运行依赖，检查完整 Git 历史 secret、
两个 Python 锁图和 npm 依赖、迁移、OpenAPI、后端测试、前端构建/E2E、视觉门禁与真实开发依赖
专项。生产 Compose、Dagster、备份、可观测性和供应链的独立测试也必须进入同一 release gate。

正式 `v1.0.0` 还需人类版权授权、托管仓库安全开关、签名镜像、外部干净安装、真实 IdP/provider/
callback、故障/告警和恢复演练；详见 `open-source-release-readiness.md` 与根目录
`RELEASE_CHECKLIST.md`。
