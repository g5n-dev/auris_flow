# Auris Flow 开源与生产发布深度审计

**审计日期**：2026-07-21
**目标版本**：`v1.0.0`（首发形态：Linux 单机 Docker Compose）
**结论**：当前仓库是功能较完整的生产候选实现，但仍不是已经获授权、经过外部环境验收的正式
开源发行版。代码能力、自动门禁和外部发布证据必须分开表述。

## 1. 当前门禁快照

| 门禁 | 当前结果 | 解释 |
| --- | --- | --- |
| 平台基础 readiness | `7/7 passed` | 开源入口、质量入口、后端契约、运行底座、三域闭环、核心契约和 Eval 定义均可识别 |
| 开源 release readiness | `10/12 passed` | `release_license` 与 `release_git_tree_integrity` 按设计 fail closed |
| OpenAPI | 手工契约 `175/175`；运行时 `258/258` | 路由、operationId、关键闭合 response schema 与运行时一致 |
| Secret scan | 当前树与 Git 历史通过 | 不等于外部 secret manager 已部署 |
| 前端 | 架构零债务、类型检查、生产构建、预压缩与 bundle 预算通过 | 正式发行仍缺独立批准的不可变 bundle lock |
| 生产工具 | Compose 候选策略、Dagster 包、备份/恢复与可观测性测试通过 | 不等于已在外部 Linux RC 做真实故障/恢复演练 |

`10/12` 不是“差两个勾即可发布”的简单计数：其中一个失败项聚合了不可变视觉基线、前端 bundle
批准及发布树必须来自同一干净 commit 等条件；许可证项还要求真实权利主体签署，而不是仅存在
Apache-2.0 文本。

## 2. 已形成的候选能力

### P0：可信发布树

- 仓库已有 Apache-2.0 `LICENSE`、治理、安全、贡献、支持、变更和逐项发行清单。
- OpenAPI 与 FastAPI 运行时一致，`evaluation-lock` 等运行时接口已进入规范和契约测试。
- readiness 检查使用当前模块目录、catalog 与 bundle policy，不再依赖迁出前的单体实现细节。
- 构建产物、缓存、本地数据库、音频、截图、secret 与临时证据不进入发行树；当前树和历史均执行
  secret scan。
- 发布候选必须先提交全部源文件，再从 Git 对象做无本地 hardlink 的干净克隆验证；未跟踪文件和
  开发机虚拟环境不能成为隐式依赖。

### P1：身份与公开安全边界

- `prod/release` 使用通用 OIDC Authorization Code + PKCE；校验 issuer、audience、签名、过期、
  JWKS、时钟偏差，并把 `(issuer, subject)` 显式映射到内部 user/tenant/project/role。
- 浏览器只持有 `Secure`、`HttpOnly` 的不透明 session cookie；写请求同时受 CSRF、Origin、RBAC、
  tenant/project、幂等、审计与 trace 约束。
- 生产配置拒绝开发认证、demo/弱 secret、通配 CORS/TrustedHost、fake/local adapter 与非严格
  readiness；Keycloak 只是参考 IdP。
- Run、Trace、导出、标签抽取/优化与候选评审使用集中式闭合公共投影。执行引擎、GraphQL、endpoint、
  bucket/key、对象 URI、storage ID、外部运行 ID 和内部错误不会穿越公共 API。
- Trace/错误值执行 NFKC、格式控制字符、常见同形异义字符和凭据模式清洗；导出下载只暴露受鉴权
  的 BFF 路由，并校验 ETag、范围、内容类型、长度和响应头注入。

### P2：单机生产链路

- `production/compose.yaml` 当前包含 24 个受策略约束的服务：MySQL、Redis、MinIO、Qdrant、迁移、
  Keycloak、真实 Dagster code/webserver/daemon、BFF、Worker、TLS edge、Collector、Tempo、
  Prometheus、Alertmanager、Grafana、node-exporter 及联合健康探针等。
- MySQL 是权威业务事实；Redis 是可降级辅助状态；Qdrant 是可重建派生索引；对象存储按精确版本和
  SHA-256 绑定。
- 音频执行通过可替换 HTTPS provider 接口。Dagster 完整读取精确对象版本并验 SHA-256，provider
  只接收业务 envelope，不接收对象存储凭据；结果写成不可变 manifest，再由 BFF 进行限长、类型、
  哈希、版本、tenant/project/run/trace、输入、模型、能力和 provider 证据校验。
- provider、对象存储、manifest 与 callback 的 301/302/303/307/308 重定向均拒绝，避免凭据或签名
  被重放到另一目标。
- Outbox 已有 lease、fencing、指数退避、最大重试、dead-letter、attempt ledger 与受治理重放。
- 备份 manifest 使用宿主机外置 Ed25519 信任锚；即使攻击者篡改内容并重算仓库内全部自哈希，也
  不能伪造签名。`rebuild-required` 只进入 `pending-qdrant-rebuild`，必须经治理化 finalize、真实语义
  重建证据和严格 readiness 才能转成 `complete`。

### P3：可观测性与运维

- BFF、Worker、Dagster 使用 OTel，业务 trace 与 OTel trace/span 关联；结构化日志和 Collector
  在出口前删除认证头、cookie、SQL statement，并把通用 URL 收敛为不含动态路径、凭据或 query
  的 origin。生产环境拒绝禁用 OTel、零采样与初始化失败。
- Prometheus/Grafana/Alertmanager/Tempo 已有指标、仪表盘、22 条告警规则和合成规则测试。
- dead-letter 的“最近新增”和“未解决存量”均从权威数据库生成 gauge，BFF 在第一次 scrape 前
  重启也不会吞掉告警。
- `observability-health` 以拒绝重定向、限长 HTTP 探针访问 Collector health extension、Tempo、
  Prometheus、Alertmanager readiness 和 node-exporter metrics，并在后台强制导出标记、跨过
  Collector batch 后从 Tempo 按 trace ID 读回。BFF、Worker 与 Dagster code location 等待该
  内部监视服务健康；BFF 严格 `/readyz` 还会以 singleflight/短 TTL 验证自身 exporter 接受精确
  marker，依赖离线时返回 503，恢复后重新返回 200，公开探针不会按请求放大 Tempo 查询。
- 安装、升级/回滚、备份/恢复、轮换、故障排查、安全事件和告警演练已有 Runbook。

### P4：供应链候选实现

- release workflow 构建 amd64/arm64 镜像、生成 CycloneDX SBOM、扫描高危漏洞，并对精确 digest
  生成/验证 provenance、SBOM attestation 与 keyless 签名。
- 发行证据工具把仓库、workflow、source commit/ref、subject digest、predicate 与 signer identity
  绑定；所有 CycloneDX attestation 必须与可下载 SBOM 逐字节一致。
- 发布前会重新验证已归档证据、签名清单和远端 annotated tag，不把 workflow 自己生成的 JSON
  当成独立 registry/GitHub 证明。

## 3. HTTP Range 结论

音频播放已经真实使用 HTTP Range，不是前端假切片：

- GET 与 HEAD 语义一致，支持单段 `bytes=start-end`、开放尾端和 suffix range；
- 合法范围返回 `206`，非法或多段范围返回 `416`，并携带正确 `Content-Range`；
- `If-Range` 只接受强 ETag，不匹配时降级完整 `200`；对象存储请求同时使用精确版本与 `If-Match`；
- BFF 校验上游 `206`、ETag、`Content-Range` 和长度，按 64 KiB 流式传输，并在断连或截断时关闭；
- Dagster 推理前为了验证完整 SHA-256 会读取整个精确版本，这与浏览器播放 Range 是两条不同链路。

## 4. 仍然阻塞发布的事实

### 4.1 必须由项目所有者或外部系统完成

1. `open-source-rights-authorization.md` 仍是 `PENDING`，权利主体、版权文本、批准证据、日期和最终
   `NOTICE` 均未确认。存在 `LICENSE` 不代表已经取得发布授权。
2. 视觉基线与前端 bundle lock 仍为 `PENDING`；必须由独立批准者对 Linux/amd64 不可变 OCI
   产物完成签名提升。
3. `THIRD_PARTY_NOTICES.md` 中尚待人工判断的依赖许可证必须得到具名、可追溯、带到期日的结论。
4. 当前仓库没有可验证的托管 remote 证据；分支/标签保护、PVR、secret scanning、push protection、
   CodeQL、Dependabot、受保护 Release environment 与独立审批必须在真实 GitHub 管理面启用并演练。
5. 必须由 hosted workflow 对最终 commit 构建并产出真实 GHCR digest、SBOM、attestation、签名、
   image lock、校验和与远端 release/tag 证据。
6. 必须在外部干净 Linux 主机仅凭发行文档完成一次 `v1.0.0-rc.1`：真实 OIDC、语义 embedding、
   音频 provider、Dagster、callback、通知、故障注入、升级/回滚和空环境恢复都要留存脱敏证据。
7. Prometheus 无法可靠报告自身完全离线，Alertmanager 也无法交付自身完全离线通知；生产 RC
   需要 Compose 宿主机之外的 watchdog 和真实通知回执。

### 4.2 必须在真实 RC 取得的模型与数据证据

- `audio_intelligence` 的早到回执竞争已通过 tenant/project/run 绑定的 pending StorageObject、净化
  receipt、external ID/fence/envelope/manifest/version 复核闭合；错 scope、错绑定、跨运行复用与
  重放会转为 `rejected`。真实 RC 仍须用计划采用的 provider 做强制竞争 E2E，验证外部系统实际
  遵循该协议，而不能仅以仓库单元/集成测试替代。
- 参考音频/embedding 协议夹具只证明协议与安全边界，不证明真实模型质量、召回质量、成本或 SLA。
- 公共音频数据集注册表仍有 split 因缺批准 SHA-256 而 fail closed；数据不随仓库打包，未批准前
  不得在验收中使用该 split。

## 5. 明确的产品边界

- 首发仅支持 Linux 单机 Docker Compose，不承诺宿主机故障自动容灾或节点级高可用。
- Keycloak 是参考 IdP；Dagster 是内部执行引擎；前端不会直连 MySQL、Redis、MinIO、Qdrant 或
  Dagster。
- 不引入 ClickHouse；第一阶段洞察使用 MySQL 聚合/预计算、Redis 缓存和 Qdrant 召回解释。
- Kubernetes/多节点 HA 是后续独立路线，不属于本次 `v1.0.0` 验收。

## 6. 发布判断

当前可以继续形成并审查候选 commit，但不能创建或发布 `v1.0.0-rc.1`。代码门禁、干净克隆与发布
树一致性通过后，仍须依次关闭第 4 节的权利、独立批准、托管平台、外部 RC 和时序风险。任何一项
缺失时，README、Release Note 和市场表述只能称“候选实现”，不能称“正式开源发布”或“生产验收
完成”。
