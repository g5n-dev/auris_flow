# 单机生产运维、SLO 与告警 Runbook

## 范围与状态

本 Runbook 对应 `production/compose.yaml` 的 Linux 单机基线及
`production/observability/alerts.yaml`。当前实现已接入 OTel SDK（HTTP、SQLAlchemy、Redis、HTTPX、
urllib 和 Worker/Dagster 领域 span）、结构化脱敏日志、OTel Collector/Tempo、Prometheus、Grafana 与
node-exporter；这说明遥测路径存在，不等于端到端生产演练已经完成。

当前仍是 `v1.0.0` 候选。Compose 暂未内置 Alertmanager/外部通知路由；正式 RC 必须连接实际
通知渠道并证明值班人员收到、确认和关闭告警。单机形态没有节点 HA，宿主机离线会造成整体不可用。

## 服务目标（SLO）

以下是首版候选的运营目标，不是无条件商业 SLA。正式 `v1.0.0` 前必须以 RC 真实负载与恢复演练
校准并由 owner 批准：

| 指标 | 初始目标 | 口径 |
| --- | --- | --- |
| API 可用性 | 每自然月 >= 99.5% | 每 1 分钟从外部执行一次授权合成读取；严格 `/readyz` 和合成请求均成功才计成功。预先公告维护窗单独记录，不计入分母 |
| API P95 延迟 | 每 5 分钟窗口 <= 750 ms | 非流式 `/api/v1/*`，排除上传、音频 Range、导出下载与外部异步任务；按 route template 聚合，不能用原始 URL 作 label |
| 异步任务完成 | 参考负载 P95 <= 15 min | 从受理到业务终态；参考负载为单个不超过 60 分钟音频且外部 provider 未限流。不同 workload 必须另设目标 |
| Outbox 投递 | P95 <= 60 s，最老 pending < 300 s | 从权威 Outbox 创建到成功 receipt；retry/dead-letter 不计成功，重复投递不能产生重复业务结果 |
| 数据恢复 | RPO <= 24 h；RTO <= 4 h | 权威数据 <= 100 GiB、替代主机/同 release digest/外部 secret/备份均可用；见备份恢复 Runbook |

99.5% 月可用性约对应 30 天 216 分钟未公告不可用预算。消耗 50% 预算时冻结高风险变更；耗尽时只
允许恢复性/安全修复，直至 postmortem 和改进项得到 owner 批准。

当前 Prometheus 直接覆盖请求量/延迟、认证失败、依赖 readiness、Outbox、callback、Worker 和
数据库连接池。异步任务 P95 与 Outbox 精确投递分位数还需由 MySQL 权威时间戳/业务 trace 计算；
在对应低基数直方图和自动 SLO 告警进入 release gate 前，不能声称这两项已自动化闭环。

推荐 PromQL：

```promql
sum(rate(auris_http_requests_total{status_class!="5xx"}[5m]))
/
clamp_min(sum(rate(auris_http_requests_total[5m])), 0.001)
```

```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(auris_http_request_duration_seconds_bucket[5m]))
)
```

## 健康、指标、日志与追踪

- 公开 `/healthz` 只表示 edge 进程存活。它返回 200 不能作为开放流量依据。
- BFF 内部 `/readyz` 在 production strict 模式检查 auth、MySQL、Redis、MinIO、Qdrant 和真实
  Dagster；强依赖失败返回 503。edge 将公开 `/readyz` 精确反代到该严格检查，负载均衡器必须使用
  它决定是否开放流量。OIDC discovery/JWKS 的真实登录仍需另加外部合成检查。
- `/metrics` 只允许 loopback、容器私网或明确 `METRICS_TRUSTED_CIDRS`，edge 固定返回 404。
- Grafana 只监听 `127.0.0.1:13000`，Keycloak 管理端只监听 `127.0.0.1:18080`；远程运维使用
  受控 SSH 隧道，不改成公网绑定。
- JSON 日志包含业务 `trace_id`、request ID 及有活动 span 时的 `otel_trace_id`/`otel_span_id`。
  OTel span 使用 `auris.business_trace_id` 关联领域链路，并在 exporter 前移除 credential、token、
  cookie、SQL 与 URL query 等敏感 attribute。
- BFF 向真实 Dagster 提交任务时，把当前 W3C trace ID、parent span ID 与 trace flags 写入内部
  Auris run config；`dagster-code` 只在三者完整且合法时重建 remote parent，再创建领域 span。该
  config 是内部执行契约，不是业务 API，也不得承载 token、cookie、完整 URL、原始音频或转写。

常用只读检查：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml ps

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml logs --since 15m bff worker dagster-code
```

分享日志前必须再次脱敏 Authorization/cookie/token/secret、完整 DSN、个人数据、原始音频/转写和
callback body。只保留必要 UTC 时间、service、event、request ID、业务 trace 与 OTel trace。

## 通用响应流程

1. **确认**：记录 alert name、开始 UTC、labels、当前 release/digest 和负责人；critical 立即建立
   私密事故频道。
2. **判定影响**：检查外部合成请求、`/readyz`、Grafana、相关业务 trace 和最近变更。不要只看
   `/healthz`。
3. **止损**：若存在越权、重复写或权威数据风险，先关闭 edge/停止 Worker；可用性不能凌驾于
   tenant/project 隔离与数据完整性。
4. **诊断**：按 alert anchor 检查对应服务，使用只读命令；不得临时关闭认证、签名、TLS、
   幂等或 strict readiness。
5. **恢复**：优先恢复固定 digest/config；需要回滚时执行
   [升级回滚 Runbook](upgrade-rollback.md)。
6. **验证**：严格 readiness、真实 OIDC、合成写读、Outbox/callback、trace 和告警恢复都通过后
   才开放流量。
7. **复盘**：保存不含 secret/客户内容的证据；critical 或 SLO 事件 5 个工作日内完成 postmortem。

## dependency-offline

对应 `AurisTargetDown`：`auris-flow-bff`、`otel-collector`、`keycloak` 或 `node` target 连续 2 分钟
不可抓取。

1. 在 Prometheus Targets 区分 DNS/网络、进程退出、healthcheck 和 scrape authorization。
2. `docker compose ... ps` 查看容器状态；对单个服务用 `logs --since 15m`，不要输出全环境配置。
3. BFF/Keycloak 下线会影响用户流量或登录；先关闭 edge。OTel 下线时业务可继续但会失去 trace，
   若关键操作要求可审计则暂停写入。node-exporter 下线会失去容量告警，应按监控盲区处理。
4. 检查磁盘、内存、证书、DNS/NTP 和最近配置；只 recreate 明确故障服务，不重建 volume。
5. 恢复后确认 target `up==1`、严格 readiness、OIDC 合成登录和一个端到端 trace。

## api-error-rate

对应 `AurisApiHighErrorRate`：5xx 比例超过 5% 持续 10 分钟。

1. 按 route template、status class、release 和开始时间定位，禁止把 tenant/user/project 加进 metric
   label。
2. 用 request ID/业务 trace/OTel trace 关联日志与 Tempo；检查数据库池、依赖 readiness、Worker
   backlog 和最近 deploy。
3. 如果新 release 相关，停止流量并按兼容边界回滚；如果外部 provider 失败，保持错误 envelope
   与幂等，不切到 fake/local adapter。
4. 5xx 恢复、P95 正常并完成合成写读后关闭告警。

## authentication-failures

对应 `AurisAuthenticationFailureSpike`：认证失败速率超过 0.5/s 持续 10 分钟。

1. 区分 IdP/JWKS 轮换、时钟漂移、issuer/audience 配置错误、失效 session 与攻击流量。
2. 检查 Keycloak/外部 IdP 健康、NTP、JWKS 中 `kid` 和最近 key rotation；不得放宽 issuer、audience、
   expiry 或 TLS。
3. 若疑似攻击，限制入口、保留源地址的最少必要证据、禁用受影响 identity/session，并进入
   [安全事件响应](security-incident-response.md)。
4. 用正常、禁用、过期和伪造 token/session 四类合成测试确认稳定 200/401/403/404 且无信息泄漏。

## outbox-backlog

对应 `AurisOutboxBacklog`：pending 超过 100 持续 10 分钟。

1. 同时查看 `auris_outbox_oldest_pending_age_seconds`、retry pending、Worker processing 和 callback
   outcome；单纯数量不能判断影响。
2. 检查 Worker heartbeat、数据库连接、Dagster/MinIO/Qdrant/callback 依赖和 lease/fencing 冲突。
3. 可以按既有 claim/retry 策略增加 Worker 消费，但不得手工把 pending 改成 processed，也不得并行
   绕过 fencing。
4. 对同一幂等键验证只有一个业务结果；积压归零且 oldest age 回到基线后关闭。

## dead-letters

对应 `AurisOutboxDeadLetters`：存在至少一个 dead-letter 持续 1 分钟。

1. 立即按 event type、稳定错误 code、tenant/project、attempt ledger 和 trace 分类；不要在工单复制
   原始 payload。
2. 修复根因并验证外部副作用是否已经发生。状态不确定时先向 provider 查询 receipt，禁止盲目重放。
3. 只通过受治理的人工 replay 操作，使用新幂等键/原事件 binding、fencing、审计和 trace；不得直接
   SQL 改状态。
4. 新事件成功、无重复结果、dead-letter 被明确处置并有审计记录后关闭。

## callback-failures

对应 `AurisCallbackFailureRate`：failure/dead-letter 超过 0.1/s 持续 10 分钟。

1. 检查 HTTPS/DNS、allowed host、key id/state/window、NTP、nonce store、接收方 4xx/5xx 和 body
   idempotency conflict。
2. 401/403 先按[密钥轮换](key-rotation.md)核对 overlap，不切回 legacy HMAC；409 核对同一
   Idempotency-Key 的 body SHA-256，不能用新 body 覆盖旧 key。
3. 接收方超时时先查询 receipt；只有确认未执行或幂等安全才允许 Worker 重试。
4. 新旧 overlap/retired、重放、超时重试和成功 receipt 均验证后关闭。

## disk-capacity

对应 `AurisHostDiskWillFill`（可用空间低于 15% 持续 15 分钟）与
`AurisHostDiskCritical`（低于 8% 持续 5 分钟）。

1. 15% 时冻结升级和大任务，估算 MySQL/MinIO/Qdrant/Tempo/Prometheus/容器日志增长；8% 时关闭
   edge 并停止新写入，避免权威存储损坏。
2. 只能清理已确认可再生且有保留策略的候选数据，例如过期构建缓存；不要删除 named volume、
   MySQL 文件、MinIO 对象版本、Qdrant snapshot 或未外送的 backup。
3. 优先扩容文件系统或把已验证 backup 复制到外部后按保留策略清理。任何数据删除要列出精确目标、
   审批和恢复方式。
4. 空间恢复到 20% 以上、文件系统/数据库检查通过、backup 成功后再开放写入。

## backup-failure

对应 `AurisBackupStale`：`auris_backup_last_success_timestamp_seconds` 超过 25 小时未更新。

1. 检查 `production/runtime-metrics/auris_backup.prom` 的 mtime/值及 node-exporter textfile collector；
   metric 缺失与备份失败同等处理。
2. 按[备份恢复 Runbook](backup-restore.md)检查容量、写端是否真正停止、MySQL/MinIO/Qdrant、外部
   加密目的地和 manifest 验证。不要通过手工改 timestamp 消除告警。
3. 在修复后创建新 quiesced backup、在外部位置再次验包；只有脚本原子写入成功 metric 后关闭。
4. 超过 RPO 时冻结非恢复性变更并通知数据 owner；必要时启动安全事件/业务连续性流程。

## alert-testing

每季度、每个 RC 以及 alerts/metrics 变更后执行。只在隔离 staging/演练 Compose project；不得
在生产填盘、制造死信或触发 IdP 账户锁定。

先做静态校验：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml exec -T prometheus \
  promtool check rules /etc/prometheus/alerts.yaml
```

然后验证 Prometheus 从 pending 到 firing、通知渠道收到、值班确认、runbook 链接和恢复关闭：

| 场景 | 隔离环境注入方式 | 必须触发 |
| --- | --- | --- |
| 依赖离线 | 停止一个被 scrape 的候选服务超过 2 分钟后恢复 | `AurisTargetDown` |
| API 5xx | 用测试故障开关/合成 upstream 在 staging 产生受控 5xx，不修改生产代码 | `AurisApiHighErrorRate` |
| 认证失败 | 使用专用非锁定测试 identity 发送无效/过期凭据，速率刚超过阈值 | `AurisAuthenticationFailureSpike` |
| Outbox 积压 | 暂停 staging Worker 并通过受支持 API 创建合成事件 | `AurisOutboxBacklog` |
| 死信 | 让隔离 callback 按测试契约持续失败直至正常最大重试 | `AurisOutboxDeadLetters`、`AurisCallbackFailureRate` |
| 磁盘容量 | 使用 `promtool test rules` 的合成 node filesystem series；绝不实际填满磁盘 | `AurisHostDiskWillFill`、`AurisHostDiskCritical` |
| 备份过期 | 在隔离 node-exporter textfile 目录提供过期时间戳，随后由成功测试 backup 覆盖 | `AurisBackupStale` |

当前仓库没有提交 `promtool test rules` 场景文件，也没有 bundled Alertmanager；在这两项及真实通知
证据补齐前，告警只能算配置基线，不能声称已完成生产告警验收。演练记录至少包含 source commit、
alert 名、阈值/for、注入与恢复 UTC、通知/确认耗时和工单链接，不含 secret/客户数据。

## 常见依赖排查

- **MySQL**：检查 health、连接池和慢查询；不要在线执行无界 `SELECT *` 或 destructive DDL。
- **Redis**：确认 ACL、密码轮换和 noeviction；Redis 丢失不应丢权威业务事实。
- **MinIO**：检查 ready、bucket/version 和容量；不要通过关闭签名或公开 bucket 恢复播放。
- **Qdrant**：检查 API key、collection/alias、模型与维度；索引损坏按权威数据重建，不手工伪造点。
- **Dagster**：检查 code gRPC、webserver、daemon 与 MySQL storage；产品 API 仍使用 Auris 领域语言。
- **OIDC**：检查 discovery/JWKS/TLS/NTP/redirect；未知 identity 必须 fail closed。
- **OTel**：Collector/Tempo 失败不应改变业务结果，但遥测盲区期间暂停高风险变更并保留业务审计。
