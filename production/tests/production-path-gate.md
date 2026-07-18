# 单一 Production Compose 全链路门禁合同

## 当前状态

`scripts/verify_production_path.sh` 当前必须退出非零。它是 fail-closed
入口，不是成功 release evidence。严格发布入口 `verify_release.sh` 已在 supply-chain
生成和 finalizer 之前显式调用它，所以当前 blocked 合同会令正式发布 hard-fail。

`finalize_release_evidence.py` 已把 `production-path-gate.json` 列为强制 core evidence，
并严格复用同一运行证明校验器执行 schema、commit、Compose 输入和原始 proof 绑定校验，
随后才把读取到的稳定字节纳入最终哈希。由于上述前置 hard-fail，当前 finalizer 的成功
路径仍不可达；只新增 runtime driver、只修改 YAML 状态或手工伪造同名 JSON 均不得启用发布。

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
- OpenTelemetry Collector、Tempo；
- 仅作为外部系统测试端点的 HTTPS embedding receiver 与 HTTPS callback receiver；
- 只读、去 capability 的 verifier。

一次运行必须绑定干净 Git commit、base/overlay/rendered Compose 哈希。每个业务
写操作保留自己的服务端 `trace_id`；它们通过 tenant/project 和证据清单关联。至少
Dagster 核心链路必须由同一个 W3C `otel_trace_id` 串联 BFF、Outbox、Worker 与
Dagster，不能把业务 `trace_id` 与 OTel trace 混成一个字段。门禁必须证明：

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
6. Collector 接收到至少来自 BFF、Worker、Dagster code 的同一 OTel trace，并可从
   Tempo 查询。只看到容器健康或 Collector 进程存活不算通过。
7. 在同一个临时 Compose project 内执行 MySQL 重启、Worker 崩溃、重复投递、
   callback 超时、Qdrant/Redis 短时不可用恢复；恢复后不得出现重复业务结果，且
   Outbox 租约/fencing、重试、reconciliation 与权威数据一致性均有原始证明。

## 运行时实现约束

后续实现 `scripts/verify_production_path_runtime.py` 时必须遵守：

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
- artifact 先写同目录临时文件，再原子替换
  `build/release-evidence/production-path-gate.json`；不得包含 token、cookie、密码、
  key material、响应正文或个人绝对路径。
- 任一服务未健康、任一 proof 缺失、trace 分裂、工作区不干净或 teardown 失败，
  都必须退出非零并删除未完成 artifact。
- teardown 只能清理脚本生成且名称匹配白名单的 Compose project、volume 和
  `build/tmp` 子目录。

## 当前精确阻塞项

机器可读清单位于 `production-path-gate.compose.yaml` 的
`x-auris-production-path-gate.missing_capabilities`。在这些项全部落地前，合同
`status` 必须保持 `blocked`，不得改成 `ready`。当前入口必须保留在严格发布路径中，
以便生产闭环缺失时持续阻断发布，而不是被跳过。
