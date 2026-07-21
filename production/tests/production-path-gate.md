# 单一 Production Compose 全链路门禁合同

## 当前状态

`scripts/verify_production_path.sh` 是 fail-closed 的一次性运行入口。机器可读合同的
`status: ready` 只表示仓库已提供可执行的 driver、verifier 和原始证明绑定规则；
preflight 成功不是 release evidence。只有同一条命令在干净提交上完成完整 Compose
启动、故障注入、恢复校验和 teardown，才可生成一次性运行证据。

`finalize_release_evidence.py` 已把 `production-path-gate.json` 列为强制 core evidence，
并严格复用同一运行证明校验器执行 schema、commit、Compose 输入和原始 proof 绑定校验，
随后才把读取到的稳定字节纳入最终哈希。缺少 runtime capture、故障恢复证明、干净
commit 绑定或成功 teardown 时，入口与 finalizer 都必须 hard-fail；只修改 YAML 状态
或手工伪造同名 JSON 均不得启用发布。

现有门禁只能组成部分证据，不能拼接成一次生产验证：

| 现有门禁 | 已证明 | 未证明 |
| --- | --- | --- |
| `verify_real_stack.sh` | MySQL、Redis、MinIO、Qdrant 与 BFF/UI 的真实依赖路径 | production Compose、OIDC、真实 Dagster、OTel |
| `verify_real_dagster.sh` | MySQL 持久化的真实 Dagster 提交、完成、取消与重启恢复 | 产品 BFF/Worker、OIDC、对象/Qdrant/外部回调 |
| `verify_product_dagster_path.sh` | BFF、Worker 与真实 Dagster 的产品控制链路 | 它显式使用 dev auth、local 对象/Qdrant/回调 adapter、测试向量，并关闭 OTel |

因此，任何一个旧 artifact 或多个旧 artifact 的组合都不能满足
`auris.production-path-gate.v1`。

## 成功门禁必须一次证明的事实

同一个隔离的 Compose project 必须扩展 `production/compose.yaml`，并实际启动：

- BFF、Worker、MySQL、Redis、MinIO、Qdrant；
- Dagster code、webserver、daemon；
- Keycloak、edge；
- OpenTelemetry Collector、Tempo、Prometheus、Grafana、node-exporter；
- 仅作为外部系统测试端点的 HTTPS embedding receiver 与 HTTPS callback receiver；
- 只读、去 capability 的 verifier。

一次运行必须绑定干净 Git commit、base/overlay/rendered Compose 哈希、全部运行中
服务和一次性初始化服务的容器/镜像事实。每个业务写操作保留自己的服务端
`trace_id`；它们通过 tenant/project 和证据清单关联。OIDC、Dagster、对象存储、
Qdrant、外部回调必须分别使用 5 个互异且采样的 W3C `otel_trace_id`，并各自证明
本操作所需的 BFF、MySQL、Outbox、Worker 与目标依赖 Span；不得用一条 omnibus
trace 为另一操作补齐缺失 Span，也不能把业务 `trace_id` 与 OTel trace 混成一个字段。
门禁必须证明：

1. 经 edge 和 Keycloak 完成 OIDC Authorization Code + PKCE；BFF 实际执行
   discovery、JWKS 校验、code exchange，并签发 opaque browser session；dev auth
   保持关闭。
2. Worker 通过 `HTTPEmbeddingProvider` 调用 HTTPS embedding 测试端点；不得使用
   `DeterministicTestEmbeddingProvider`。向真实 Qdrant 写入带 tenant/project/trace
   payload 的点，并通过真实 recall 读回。
3. 真实对象 adapter 在 MinIO 写入并读取对象，MySQL 仍保存权威元数据。
4. 真实外部 callback adapter 使用 HTTPS 和 `hmac-sha256-v2`；receiver 校验签名、
   时间窗、nonce、幂等键，拒绝重放，并支持 receipt reconciliation。
5. BFF 提交真实 Dagster run；Dagster 使用签名 completion receipt 回写 BFF；全程
   保持同一业务 trace 绑定。
6. Collector 接收到 5 条独立操作 trace，并可从 Tempo 分别查询。每条 trace 的
   service、dependency CLIENT Span 与脱敏 signal 必须满足该操作的精确合同；只看到
   容器健康、Collector 进程存活或另一操作拥有相同组件均不算通过。
7. 在同一个临时 Compose project 内执行 MySQL 重启、Worker 崩溃、重复投递、
   callback 超时、Qdrant/Redis 短时不可用恢复；恢复后不得出现重复业务结果，且
   Outbox 租约/fencing、重试、reconciliation 与权威数据一致性均有原始证明。

## 运行时实现约束

`scripts/verify_production_path_runtime.py` 与容器内 verifier 必须遵守：

- 正式证据只允许在原生 Linux 主机的默认本地 rootful Docker socket
  (`unix:///var/run/docker.sock`) 上生成；macOS/Windows Docker Desktop、远程 daemon、
  rootless Docker、Colima、Rancher Desktop 与 OrbStack 都必须 fail closed。本地桌面
  运行只能作诊断，不能写入权威 release evidence。
- Compose 必须显式使用版本控制内的空 `production-path-gate.env`，拒绝默认 `.env`
  或宿主机 `COMPOSE_FILE`、镜像覆盖、应用凭证污染。Docker build context 必须排除
  Git-ignored 的截图、audit 输出、缓存、生成 fixture/catalog 与历史 dist。
- 在 `build/tmp` 下生成一次性 CA、服务器证书和随机 secrets；只把 CA 作为
  `SSL_CERT_FILE` 注入需要访问测试 HTTPS 端点的容器，不关闭 TLS 校验。
- 不给产品 callback SSRF 检查增加测试旁路。callback receiver 应位于一个
  `internal: true` 的隔离网络，但使用 Python `ipaddress` 判定为 global 的测试
  子网，使生产 SSRF 规则原样执行。
- 不修改 Keycloak 正式 realm 的安全默认值。通过一次性 gate bootstrap 设置测试
  用户，再由 verifier 驱动标准浏览器授权码表单和 PKCE 回调。
- embedding/callback receiver 只是外部依赖的协议测试端点；产品 BFF、Worker、
  Dagster、MinIO、Qdrant、Keycloak 和 OTel 不允许 fake/local adapter。
- 参考 embedding endpoint 只可证明可替换的 HTTPS provider 接口和真实 Qdrant
  写入/召回，不得把 feature-hash 或其他门禁参考向量声明为生产模型质量认证。
- 证据必须单独绑定 runtime driver、HTTPS support server、Keycloak gate realm、
  base/overlay/rendered Compose 的 SHA-256；仅依赖 overlay 间接引用不够。
- 证据必须精确列出所有运行中与成功退出的一次性 Compose 服务，并绑定容器 ID
  哈希、配置镜像、实际 image ID、平台、健康/退出状态；所有第三方镜像还必须记录
  与配置仓库一致的 registry RepoDigest。该运行时事实不替代 P4 的受审固定镜像
  digest/签名锁。
- 每条 raw proof 必须内嵌 `auris.production-path.capture.v1` 脱敏 capture；校验器
  重算 `capture_sha256`，并要求 facts 与 capture observations 完全相同且字段集合精确
  等于版本化合同。OIDC、Dagster、MinIO、Qdrant、callback、Tempo、Outbox lease
  generation/fencing 和六类恢复场景分别执行字段级及跨 proof 语义校验；重算哈希、
  增加自声明字段或写入 `recovered: true` 都不构成证据。
- artifact 先写同目录独占临时文件，完成成功 teardown 后再通过排他 hard-link 发布为
  `build/release-evidence/production-path-gate.json`；已存在目标永不覆盖或删除。证据
  不得包含 token、cookie、密码、key material、响应正文或个人绝对路径。
- 任一服务未健康、任一 proof 缺失、trace 分裂、工作区不干净或 teardown 失败，
  都必须退出非零，且不得创建权威 artifact。
- teardown 只能清理脚本生成且名称匹配白名单的 Compose project、volume 和
  `build/tmp` 子目录。

## Ready 合同与发布证据的边界

机器可读合同位于 `production-path-gate.compose.yaml`。`status: ready` 表示生产路径
诊断实现已纳入版本控制，绝不代表 Compose 演练已经通过。严格发布入口必须保留该门禁；
runtime driver、verifier、capture、六类故障恢复证明或成功 teardown 任一缺失时都持续
阻断发布。权威 `production-path-gate.json` 只能由一次全新、同进程协调、绑定干净提交
的成功运行原子写入，不能由旧门禁 artifact 拼接，也不能由人工编辑替代。
