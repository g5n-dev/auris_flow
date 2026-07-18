# 安全事件响应 Runbook

## 目标与触发条件

本 Runbook 用于疑似或确认的鉴权绕过、跨 tenant/project 访问、凭据/私钥泄漏、恶意文件、日志
注入、callback 重放/伪造、重复业务写、供应链制品异常以及权威数据未授权修改。漏洞报告入口和
对外响应时限以 [SECURITY.md](../../SECURITY.md) 为准。

当前仓库仍是 `v1.0.0` 候选，没有生产支持版本。没有正式 SLA 不意味着可以公开漏洞细节、忽略
证据或虚构已修复状态。

## 分级与职责

- **SEV-1**：已确认跨租户泄漏、远程代码执行、生产签名/数据库/TLS root key 泄漏、权威数据
  篡改或活跃供应链攻击。立即关闭相关入口，incident commander、安全负责人和项目 owner 到场。
- **SEV-2**：可利用的单租户越权、活跃账户接管、callback 重放、敏感日志泄漏或高可信攻击，
  但影响范围受限。立即限制受影响功能并在同一工作时段完成 containment。
- **SEV-3**：未证实的扫描发现、低影响配置问题或无敏感数据的安全缺陷。保持私密，按风险排期。

每个事件至少指定 incident commander、技术处置、证据保全、沟通/合规和记录人。参与者只访问
完成职责所需的数据；公开 issue、普通聊天和普通日志都不是事件协作空间。

## 前 30 分钟

1. 建立私密事件编号、开始 UTC、报告来源、当前 release/source commit/镜像 digest 与已知影响。
2. 保留原始报告，不在公共 issue 回复复现；通过 GitHub Private Vulnerability Reporting 或受控
   渠道沟通。
3. 以只读方式保存必要 evidence：alert、容器状态、相关结构化日志、request ID、业务 `trace_id`、
   OTel trace ID、audit/outbox/attempt 记录和镜像/SBOM/signature metadata。记录采集者、时间和
   SHA-256；不批量复制客户音频/转写。
4. 存在继续越权或污染风险时，先关闭 edge；异步副作用风险则停止 Worker/Dagster。不要为了
   可用性绕过 OIDC、RBAC、HMAC、TLS、幂等或 fencing。
5. 识别受影响 tenant/project、identity/session、key id、对象和时间窗。查询必须 default-deny，
   不扩大管理员可见范围。

## 遏制矩阵

| 事件 | 立即遏制 | 后续动作 |
| --- | --- | --- |
| OIDC 用户/会话接管 | 禁用内部 user security state/identity，撤销相关 browser session，必要时限制 IdP client | 检查 issuer/subject 映射、角色变更与登录 trace；要求重新认证 |
| OIDC signing key/IdP 管理员泄漏 | IdP 停止旧 key 签发并发布新 JWKS，限制登录入口 | 按密钥 Runbook 完成 overlap/retire；审计所有 identity/session |
| callback/completion key 泄漏 | 停止相关 Worker/Dagster callback，接收方拒绝泄漏 key id | 新 key 先接受后签发，缩短/结束旧 window，核对 nonce、receipt 和重复副作用 |
| 数据库/Redis/MinIO/Qdrant 凭据泄漏 | 限制网络并停止消费者，保留进程/审计证据 | 逐账户轮换、验证最小权限与数据一致性；不清库“消除痕迹” |
| TLS 私钥泄漏 | 撤下受影响 edge，申请新证书并吊销旧证书 | 替换 key/cert、验证链/SAN/HSTS/OIDC redirect |
| embedding/provider key 泄漏 | provider 侧限制/吊销 key，暂停知识索引写入 | 轮换并检查 provider usage；模型/维度未变才可原索引继续 |
| 跨 tenant/project 读取 | 关闭受影响 API/edge，冻结相关管理员会话 | 确定对象/时间/主体，修复 default-deny，执行负向矩阵和通知评估 |
| 重复写/回调重放 | 停止 Worker/入口，保留幂等、nonce、fencing 和 provider receipt | 以 MySQL 权威记录对账；不要盲目重放或删除冲突记录 |
| 恶意镜像/依赖 | 隔离镜像和 runner，停止从相关 registry 部署 | 固定可信 digest 重建，重新生成 SBOM/签名，轮换 CI/registry 凭据 |

密钥轮换细节见[密钥轮换 Runbook](key-rotation.md)。泄漏凭据必须视为已经被读取，不能以“日志中
没有使用记录”作为不轮换理由。

## 调查与范围确认

建立不可变时间线，至少回答：

- 入口是身份、API、上传、callback、CI/镜像、宿主机还是运维凭据？
- 第一次和最后一次可信事件是什么，是否受 NTP/时区影响？
- 哪些 tenant/project/user/object/run/outbox/secret 受影响；哪些明确未受影响，证据是什么？
- 是否发生读取、修改、删除、外送、重复副作用或审计规避？
- MySQL 权威数据、MinIO 对象版本和 audit/outbox 是否一致；Qdrant/Redis 派生状态能否安全重建？
- 同一 source commit 的 SBOM、镜像签名、checksum 与运行 digest 是否匹配？

使用业务 `trace_id` 连接 API、数据库记录、Worker、Dagster、Outbox 和 callback；用
`otel_trace_id` 补充时序。OTel 采样可能没有每个 trace，因此 audit/Outbox/MySQL 仍是业务证据，
不能因 Tempo 无 span 就判定没有行为。

严禁在原始宿主机执行大范围清理、覆盖日志、删除容器/volume 或重建系统后再取证。需要隔离时在
网络层限制访问，并用新的干净主机恢复服务。

## 根除、恢复与验证

1. 修复根因并新增最小复现、跨租户负向、重放/幂等和日志脱敏测试；敏感复现留在私密 advisory。
2. 从同一可信 commit 重新构建/验证镜像、SBOM、签名和 checksum，不复用可疑 runner cache。
3. 轮换所有可能暴露的下游凭据；包括 backup、CI、registry 和 IdP 管理凭据，而不只应用 key。
4. 如果权威数据可能被污染，在新 Compose project/空卷按
   [备份恢复 Runbook](backup-restore.md)恢复到事件前可信点，再重建 Qdrant；原环境保留取证。
5. 以固定 digest 启动，验证严格 `/readyz`、OIDC 正常/禁用/伪造路径、tenant/project 负向矩阵、
   核心业务、Worker/Dagster/Outbox/callback 和每个相关告警。
6. 观察至少一个最大 session/token/callback/Outbox 时间窗，确认旧凭据被拒绝、没有新增异常副作用，
   再由 incident commander 批准开放流量。

镜像回滚不等于安全恢复；若旧版本仍含漏洞或 secret 已泄漏，禁止单独回滚后开放流量。

## 沟通与披露

- 3 个工作日内确认私密漏洞报告，7 个工作日内给出初步影响判断或信息请求。
- 对受影响用户、监管方和第三方 provider 的通知由项目 owner/法律负责人依据实际司法辖区、合同
  和证据决定；不得延迟必要通知，也不得在范围未证实时夸大结论。
- 对外内容只包含确认事实、受影响版本/时间/数据类别、缓解措施和用户动作。利用细节在修复可用
  前保持私密，并与报告者协调披露。
- release notes/CVE/advisory 必须指向已验证修复 commit/digest；不能宣称未完成的恢复或演练。

## 关闭与复盘

SEV-1/2 在 5 个工作日内完成无责 postmortem，记录根因、探测差距、SLO/数据影响、轮换清单、
恢复证据和带 owner/期限的改进项。关闭条件：

- 影响范围与未影响范围都有证据；必要通知完成；
- 旧 session/key/credential 已撤销，修复 release 的制品链一致；
- 数据对账、备份恢复、负向安全矩阵和告警测试通过；
- 临时访问和网络限制已审计撤销，长期改进进入公开或私密跟踪渠道；
- 原始证据按最小权限和适用保留政策封存，不混入普通 backup 或源码仓库。

## 桌面演练

每季度和每个正式 RC 至少演练一次：跨租户疑似读取、OIDC/JWKS key compromise、callback replay、
供应链镜像异常和宿主机/backup 恢复。记录发现到确认、遏制、轮换、恢复、通知的耗时，以及本
Runbook 中无法执行的步骤；未解决的 SEV-1/2 演练缺口是 release blocker。
