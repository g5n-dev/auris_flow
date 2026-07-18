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

当前 Prometheus 直接覆盖请求量/延迟、认证失败、依赖 readiness、Outbox、callback、Worker、
数据库连接池、限流结果，以及从 MySQL 权威 `run_records` 派生的 TaskRun 终态、24 小时滚动时长分布
和监控动作。TaskRun 时长从受理记录 `created_at` 到业务终态 `finished_at`，不是 Dagster 单独的引擎
运行时间。Outbox 精确投递 P95 仍需单独的权威时间戳分布；在该指标进入 release gate 前，不能声称
Outbox P95 已自动化闭环。

Redis 在当前生产基线中的可观测职责是严格 readiness、OTel client span 和固定窗口限流。仓库虽有
LLM cache-key 规范，但尚未实现 Redis 结果缓存读写，因此没有伪造“cache hit ratio”；未来真正接入
缓存时再按 bounded `outcome` 增加 hit/miss/error 指标。

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

```promql
histogram_quantile(
  0.95,
  sum by (le) (auris_task_run_duration_window_seconds_bucket)
)
```

## 健康、指标、日志与追踪

- 公开 `/healthz` 只表示 edge 进程存活。它返回 200 不能作为开放流量依据。
- BFF 内部 `/readyz` 在 production strict 模式检查 auth、MySQL、Redis、MinIO、Qdrant 和真实
  Dagster；强依赖失败返回 503。edge 将公开 `/readyz` 精确反代到该严格检查，负载均衡器必须使用
  它决定是否开放流量。MinIO 检查使用配置凭据对目标 bucket 发起签名 HEAD，Qdrant 只接受 2xx，
  不把匿名健康页或 4xx 当作就绪。OIDC discovery/JWKS 的真实登录仍需另加外部合成检查。
- AWS S3 的签名 `HeadBucket` 需要目标 bucket 上的 `s3:ListBucket`。运行身份缺少该权限时，即使仍有
  `GetObject`/`PutObject`，严格 readiness 也会因 403 fail closed；授予最小 bucket 权限，不得改用
  匿名访问或公开 bucket 绕过。其他 S3-compatible provider 使用其等价的 bucket-HEAD 权限。
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

## api-latency

对应 `AurisApiP95Latency`：按 route template 聚合的 API P95 延迟超过候选 SLO 750 ms 持续
10 分钟。该告警是聚合信号，不能证明每条 route 都超标。

1. 按 route template 对同一 5 分钟窗口重算 P50/P95/P99，并确认请求量足以形成有意义的分位数；
   禁止加入 tenant、project、user、request 或 trace 这类高基数 label。
2. 用慢请求的 request ID/业务 trace/OTel trace 检查 MySQL 连接池与慢查询、Redis、对象存储、
   Qdrant、Dagster span 及 callback dispatch/receipt；不得通过提高采样率后无限期输出客户内容。
3. 区分应用回归、依赖延迟、容量饱和和长任务误入同步 API。新 release 相关则停止开放新流量并按
   升级回滚 Runbook 处理；依赖故障则保持超时、幂等和 fail-closed 边界。
4. P95 连续两个告警窗口低于 750 ms、错误率正常且合成核心操作通过后关闭；保存查询窗口、release
   digest 和无敏感内容的 trace 证据。

## authentication-failures

对应 `AurisAuthenticationFailureSpike`：认证失败速率超过 0.5/s 持续 10 分钟。

1. 区分 IdP/JWKS 轮换、时钟漂移、issuer/audience 配置错误、失效 session 与攻击流量。
2. 检查 Keycloak/外部 IdP 健康、NTP、JWKS 中 `kid` 和最近 key rotation；不得放宽 issuer、audience、
   expiry 或 TLS。
3. 若疑似攻击，限制入口、保留源地址的最少必要证据、禁用受影响 identity/session，并进入
   [安全事件响应](security-incident-response.md)。
4. 用正常、禁用、过期和伪造 token/session 四类合成测试确认稳定 200/401/403/404 且无信息泄漏。

## rate-limit-backend-unavailable

对应 `AurisRateLimitBackendUnavailable`：生产 BFF 在最近 5 分钟至少观察到一次 Redis 限流后端不可用，
并持续 2 分钟。production/release 环境会 fail closed 返回 503，不会悄悄降级到进程内计数器。

1. 同时检查 `auris_rate_limit_decisions_total{outcome="backend_unavailable"}`、Redis readiness、连接
   超时、ACL/密码、主机容量和 BFF 5xx；不要记录调用方 IP、token 或限流 key。
2. 不得临时设为无限流、切换 fail-open 或共享无密码 Redis。先恢复固定配置/secret 对应的 Redis，
   再用正常与超过阈值的隔离合成流量分别验证 allowed/limited。
3. 连续两个 5 分钟窗口没有新 backend-unavailable、严格 readiness 为 200 且 429/503 行为符合配置
   后关闭；若 Redis 数据丢失，限流窗口可重建，但必须确认没有把它当权威业务事实恢复。

## outbox-backlog

对应 `AurisOutboxBacklog`：pending 超过 100 持续 10 分钟。

1. 同时查看 `auris_outbox_oldest_pending_age_seconds`、retry pending、Worker processing 和 callback
   outcome；单纯数量不能判断影响。
2. 检查 Worker heartbeat、数据库连接、Dagster/MinIO/Qdrant/callback 依赖和 lease/fencing 冲突。
3. 可以按既有 claim/retry 策略增加 Worker 消费，但不得手工把 pending 改成 processed，也不得并行
   绕过 fencing。
4. 对同一幂等键验证只有一个业务结果；积压归零且 oldest age 回到基线后关闭。

## task-run-deadline-and-reconciliation

每个新 TaskRun 都有服务端 `deadline_at`。Worker 到期扫描会原子撤销仍为 pending 的原始 Outbox；
已有可信 Dagster binding 时生成唯一 `task_run.cancel_requested`，并将业务运行置为 `cancelling`。
未超时的 `submitted/running/completion_pending` 按 `next_status_sync_at` 生成 status-sync 控制；Dagster
`SUCCESS` 只能形成 `completion_pending`，不能作为业务完成证明。

从 0042 前 RC 升级时，旧行的 `deadline_at/next_status_sync_at` 保持 NULL。NULL deadline 是 grandfather
豁免，不代表“立即超时”，Worker 不会补值或自动取消。旧的已绑定活跃运行若 NULL next-status，在最后可信
观察/提交/启动/更新时间超过一个核对周期后，会经正常 fencing 调度一次 status-sync，并从此进入周期核对。
上线前必须冻结旧 writer、完成备份、只读导出非终态 TaskRun 与原始 Outbox/Dagster binding 清单；优先排空，
无法排空者登记负责人和处置决定。只能等待自然终态、调用 cancellation API，或终止后创建 retry；禁止批量
UPDATE deadline、手工改终态或跳过 Outbox。canary 后确认 grandfather 行没有 deadline cancel、fallback
status-sync 只有一个 generation，且 Worker health 的 `monitor_status=healthy` 后再恢复写流量。

1. 运行长期停留时，只读检查 `status/deadline_at/next_status_sync_at/monitor_generation/engine_status`、
   对应 control RunRecord、Outbox attempt ledger、审计和同一业务 Trace；不要复制业务 payload。
2. pending 已过期但原始事件为 `processing` 时，不要直接改表。等待 lease fencing/reconcile 收敛；若
   worker 已崩溃，按 Outbox 恢复流程处理。
3. `cancelling` 长期未终止时，检查 deadline cancel control 是否 dead-letter，以及后续 status-sync
   对真实 Dagster 的观察。SAFE_TERMINATE 被引擎拒绝时不得伪造 `cancelled`。
4. `completion_pending` 表示引擎完成但验签业务回执缺失。核对 callback key ID、scope、nonce、
   external run ID 和接收端投递，不得用手工 SQL 或 status-sync 把它改成 `success`。
5. 扩 Worker 前确认 MySQL 支持 `FOR UPDATE SKIP LOCKED`，并验证同一 deadline 只有一个 cancel control、
   每个 monitor generation 只有一个 status-sync control；禁止关闭 monitor 作为止损手段。
6. `auris_task_run_deadline_overdue` 或 `auris_task_run_status_sync_overdue` 连续 5 分钟大于 0 表示扫描
   没有在正常窗口内收敛；同时核对 `auris_task_run_monitor_actions{action,outcome}`，但不得把其中任何
   label 替换为 run/tenant/project/trace ID。

## task-run-duration

对应 `AurisTaskRunP95Duration`：MySQL 中最近 24 小时完成的 TaskRun，从 API 受理记录到业务终态的
P95 超过 900 秒并持续 10 分钟。指标使用固定、累积 `le` 桶的滚动 gauge；PromQL 直接对当前桶执行
`histogram_quantile`，不能再套 `rate()`，窗口内没有样本时不应人为填零。

1. 先看各 outcome 的 `auris_task_run_duration_window_seconds_count/sum`，确认样本量、失败/取消占比和
   24 小时窗口；小样本只能作为诊断信号。
2. 用业务 trace 关联受理、Outbox、Worker、Dagster、回调和完成回执，区分排队、引擎执行、回写与
   callback 延迟；禁止把原始 payload 或 ID 变成 metric label。
3. 检查 workload 是否超出 SLO 的参考边界；如是容量问题，停止新大任务并按证据扩容，不能把
   deadline 调大来掩盖积压。
4. P95 连续两个窗口低于 900 秒、overdue 为 0、失败率恢复且端到端合成任务通过后关闭。

## task-run-success-rate

对应 `AurisTaskRunFailureRatio`：最近 15 分钟至少 5 个新终态 TaskRun 中，failed 比例超过 20% 持续
10 分钟。`auris_task_run_terminal` 是 MySQL 权威终态行数的累计 gauge，查询用 `delta` 并对负数
clamp 为 0；cancelled 单独展示，不伪装成 success 或 failed。

1. 按 outcome 对账终态增量，并用审计/trace 区分引擎失败、回调验签失败、deadline 和人工取消。
2. 同时检查 TaskRun P95、monitor overdue、Outbox retry/dead-letter 和依赖 readiness；不要只根据
   全生命周期累计比例判断当前故障。
3. 修复后用真实 Dagster 和签名完成回执跑至少 5 个合成任务；失败比例连续两个窗口低于阈值且无
   不可解释重复业务结果后关闭。

## outbox-delivery-delay

对应 `AurisOutboxDeliveryDelayed`：最老 pending 事件年龄超过 300 秒持续 5 分钟。它直接覆盖候选
SLO 的最老事件上限，但不等同于 Outbox P95 投递时延；后者仍需权威时间戳直方图补齐。

1. 同时查看 pending 总量、retry pending、dead-letter、Worker processing 和 callback outcome，
   并按 event type 与最老事件的稳定 error code 分类，禁止把原始 payload 放入告警或工单。
2. 检查 Worker heartbeat、claim lease/fencing、数据库连接、真实 Dagster/provider receipt 和最近
   deploy；远端结果不明时先 reconcile，不得盲目重复外部写。
3. 对已过最大重试的事件按 dead-letter 流程处置；对仍可重试的事件保留原业务 binding、幂等和
   trace。禁止直接 SQL 修改 `created_at` 或 `status` 来清除告警。
4. oldest age 连续两个窗口低于 300 秒、无不可解释重复结果且相关业务 trace 完整后关闭。

## dead-letters

对应 `AurisOutboxDeadLetters`：最近 5 分钟新增至少一个 dead-letter，且条件持续 1 分钟。历史
dead-letter 为了审计会保留在 `auris_outbox_dead_letter` gauge 中，不应让新增事件告警永久 firing。

1. 立即按 event type、稳定错误 code、tenant/project、attempt ledger 和 trace 分类；不要在工单复制
   原始 payload。
2. 修复根因并验证外部副作用是否已经发生。状态不确定时先向 provider 查询 receipt，禁止盲目重放。
3. 只通过受治理的人工 replay 操作，使用新幂等键/原事件 binding、fencing、审计和 trace；不得直接
   SQL 改状态。
4. 最近 5 分钟不再新增 dead-letter、无重复结果，且存量 dead-letter 均有明确处置和审计记录后
   关闭；存量未归零仍应保留运维工单，不能因增量告警恢复就视为已解决。

## callback-failures

对应 `AurisCallbackFailureRate`：权威 attempt ledger 中 failure/retry/dead-letter 合计超过 0.1/s
持续 10 分钟。一次 callback 可产生多个 retry attempt，不能只按最终 Outbox 行数计数。

1. 检查 HTTPS/DNS、allowed host、key id/state/window、NTP、nonce store、接收方 4xx/5xx 和 body
   idempotency conflict。
2. 401/403 先按[密钥轮换](key-rotation.md)核对 overlap，不切回 legacy HMAC；409 核对同一
   Idempotency-Key 的 body SHA-256，不能用新 body 覆盖旧 key。
3. 接收方超时时先查询 receipt；只有确认未执行或幂等安全才允许 Worker 重试。
4. 新旧 overlap/retired、重放、超时重试和成功 receipt 均验证后关闭。

## metrics-collection-failed

对应 `AurisMetricsCollectionFailed`：BFF `/metrics` 仍可响应，但从 MySQL/连接池刷新 Outbox、callback、
Worker 与 TaskRun 运行指标失败超过 5 分钟。此时相关图表可能保留旧值或归零，必须按监控盲区处理。

1. 先确认 `up{job="auris-flow-bff"} == 1` 且 `auris_metrics_collection_success == 0`；若 target 本身
   不可抓取，按 `dependency-offline` 处理。
2. 检查严格 `/readyz`、MySQL 权限/schema 版本、连接池和 `outbox_events`/
   `outbox_delivery_attempts`、`run_records` 是否与运行版本匹配；只运行有界只读查询，不输出 DSN
   或 payload。
3. 不得手工把 gauge 改为 1，也不得在采集失败时把 Outbox/callback 告警视为正常。高风险写操作
   应暂停，直至权威表查询和指标刷新恢复。
4. `auris_metrics_collection_success` 连续两个 scrape 窗口为 1、相关 gauge/counter 与 MySQL 有界
   对账一致后关闭，并记录造成盲区的开始/结束 UTC。

## disk-capacity

对应 `AurisHostDiskWillFill`（可用空间低于 15% 持续 15 分钟）与
`AurisHostDiskCritical`（低于 8% 持续 5 分钟），以及基于 6 小时斜率预测 24 小时内耗尽的
`AurisHostDiskWillFillIn24Hours`。三者只选择 node-exporter 实际观测到的可写持久文件系统，排除
overlay、tmpfs、squashfs 和伪文件系统。Docker named volume 共用其所在宿主文件系统的容量；这些
指标不代表 MinIO bucket quota、对象数或某个 volume 的独占空间，不能据此伪造组件级容量结论。

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
| API P95 | 用受控延迟 upstream 在 staging 使聚合 P95 刚超过 750 ms，恢复后验证关闭 | `AurisApiP95Latency` |
| 认证失败 | 使用专用非锁定测试 identity 发送无效/过期凭据，速率刚超过阈值 | `AurisAuthenticationFailureSpike` |
| 限流后端 | 在隔离环境短暂停止 Redis，再恢复并验证 production fail closed | `AurisRateLimitBackendUnavailable` |
| Outbox 积压 | 暂停 staging Worker 并通过受支持 API 创建合成事件 | `AurisOutboxBacklog` |
| Outbox 延迟 | 暂停 staging Worker 直到最老 pending 超过 300 秒，随后恢复消费 | `AurisOutboxDeliveryDelayed` |
| 死信 | 让隔离 callback 按测试契约持续失败直至正常最大重试 | `AurisOutboxDeadLetters`、`AurisCallbackFailureRate` |
| 指标采集失败 | 在 staging 用最小权限测试账户暂时拒绝指标只读查询，随后恢复 | `AurisMetricsCollectionFailed` |
| TaskRun P95/失败率 | 用真实 Dagster 合成任务注入受控延迟/失败，保留签名回执 | `AurisTaskRunP95Duration`、`AurisTaskRunFailureRatio` |
| TaskRun monitor | 暂停隔离 Worker 超过 `for` 后恢复，不能手工改 deadline/sync 时间 | `AurisTaskRunDeadlineOverdue`、`AurisTaskRunStatusSyncOverdue` |
| 磁盘容量 | 使用 `promtool test rules` 的合成 node filesystem series；绝不实际填满磁盘 | `AurisHostDiskWillFill`、`AurisHostDiskCritical`、`AurisHostDiskWillFillIn24Hours` |
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
