# 密钥、凭据与证书轮换 Runbook

## 原则与适用范围

本 Runbook 覆盖单机 Compose 基线中的 OIDC JWKS、外部 callback、Dagster completion receipt、
数据库/Redis/对象存储/Qdrant/embedding、Grafana 和 TLS。当前仍是 `v1.0.0` 候选，所有流程须先在
隔离的无标签 staging 环境演练。

- 所有新 secret 由 CSPRNG 或企业 secret manager 生成，至少 32 个随机字节；不在工单、聊天、
  shell history、命令行参数、Git、镜像或日志中传值。
- 使用 key id 和重叠窗口做到“先接受、再签发、后退休”；没有双密钥能力的组件进入维护窗口。
- 每次轮换记录 secret 名称、key id、负责人、开始/结束 UTC、受影响服务和验证结果，绝不记录值。
- Docker secret source file 变化不会保证进程热加载；按本页明确 recreate 依赖服务。
- `production/scripts/init-secrets.sh` 会保留已存在文件，**不能**被当作轮换命令。
- 怀疑泄漏时跳过常规周期，按[安全事件响应](security-incident-response.md)立即吊销、轮换和取证。

## 通用准备与验收

1. 确认最近 backup 已离线验包，并从企业 secret manager 验证旧版本可恢复。
2. 列出所有生产者、消费者、缓存 TTL、token/session 生命周期和允许的重叠时长。
3. 在独立 secret path 写新值，权限 `0600`、目录 `0700`；用 checksum/metadata 验证写入，不输出值。
4. 先更新验证方接受新旧 key，再更新签发方只使用新 key；观察最长在途时间后退休旧 key。
5. 验证成功、失败、旧 key overlap、旧 key retired、重放和错误不泄漏六类路径。
6. 从 secret manager 和宿主机安全删除旧版本；保留审计元数据而非 secret 内容。

## OIDC 签名密钥与 JWKS

BFF 会精确校验 issuer/audience/签名/过期时间，并缓存 discovery/JWKS；遇到未知 `kid` 会强制
刷新，`OIDC_JWKS_CACHE_TTL_SECONDS` 默认 300 秒。参考 Keycloak 只是标准 OIDC IdP 的一种实现。

正常轮换：

1. 在 IdP 新建 signing key，但先让旧、新公钥同时出现在 JWKS；不要立刻删除旧私钥。
2. 用新 key 签发一个测试 ID token，完成真实 Authorization Code + PKCE 登录；确认 BFF 能因新
   `kid` 刷新 JWKS，并验证 issuer/audience/nonce/expiry。
3. 将新 key 设为 active，旧 key 继续 publish/verify。等待“IdP 最大 token 生命周期 + BFF 时钟
   偏差 + JWKS cache TTL”；参考配置 access token 为 300 秒，但以实际 IdP 配置为准。
4. 验证新登录、登出、session 恢复、禁用用户和角色降权。浏览器 BFF session 是不透明服务端
   session，不依赖持续保存 IdP token；需要强制重新认证时必须显式撤销相关 browser session。
5. 从 JWKS 移除旧 key，并验证旧 token 被拒绝、错误仅返回稳定认证错误而不泄漏 key/discovery。

issuer、client ID、audience 或 redirect URI 变更不是普通 key rotation；它会改变 identity binding，
必须走兼容迁移和重新预置，不能临时放宽 issuer/audience。

## 外部 callback HMAC v2

`external_callback_key_bindings` 的每个 key 有 `active`、`overlap`、`retired` 状态以及可选
`not_before`/`not_after`。任一时刻只能有一个 active key；发送方只用 active，验证方接受 active
与有效 overlap，retired/未知 key 返回同类非泄漏错误。签名绑定 method、path、规范化 query、
tenant、project、idempotency key、timestamp、nonce、key id 和 body SHA-256。

轮换顺序：

1. 生成 `callback-vN`，在 Auris Flow 与接收方 keyring 加入新 key；旧 key 仍为 active，新 key
   先不要签发。
2. 双方验证新 key 的签名、错误、时间窗和 nonce 原子重放保护。
3. 原子切换：新 key=`active`，旧 key=`overlap`；更新
   `EXTERNAL_CALLBACK_ACTIVE_KEY_ID`。recreate BFF/Worker，并让接收方同步生效。
4. 提交合成 callback，确认新 key id、幂等 replay 和 trace；观察至少
   `EXTERNAL_CALLBACK_SIGNATURE_TOLERANCE_SECONDS` 加最大重试/队列延迟。
5. 将旧 key 改为 `retired`，随后从 secret manager 删除值；确认旧/未知 key 都被拒绝且不会进入
   业务写路径。

keyring JSON 只存于 secret file；文档和工单只能记录如下**无 secret**结构：

```json
{
  "callback-v1": {"state": "overlap", "not_after": 0},
  "callback-v2": {"state": "active", "not_before": 0}
}
```

实际配置的每项还必须含强随机 `secret`。不要复制上面的时间占位值到生产。

## Dagster completion receipt

completion key binding 同时绑定允许的 `source` 和 tenant/project scope。它与 callback keyring 格式
不同，没有 `active/overlap/retired` 字段；BFF keyring 中存在的 key 都可验证，Dagster 通过
`AURIS_COMPLETION_RECEIPT_ACTIVE_KEY_ID` 选择签发 key。

1. 在 `completion_receipt_key_bindings` 增加新 key，并复制旧 key 的最小
   `allowed_sources`/`allowed_scopes`；先 recreate BFF/Worker，让验证方接受新旧 key。
2. 将 Dagster `AURIS_COMPLETION_RECEIPT_ACTIVE_KEY_ID` 切到新 key，recreate `dagster-code`。
3. 提交成功和失败各一次真实 run，验证 completion 的 tenant/project/run/trace/fencing 与 key id。
4. 等待所有旧 run 和最大回调时间窗终结，从 BFF keyring 删除旧 key，再 recreate BFF/Worker；
   验证旧 key 被拒绝且新 key 正常。

不得扩大 scope 来绕过轮换失败；新 tenant/project 必须单独审批并加入 binding。

## MySQL 凭据

MySQL 使用 root、runtime、migration、Keycloak 和 Dagster 分离账户。按账户逐一轮换，不共用密码。
MySQL 8.4 可用 dual password 做在线切换：由 DBA 在受控交互会话执行
`ALTER USER ... IDENTIFIED BY <new> RETAIN CURRENT PASSWORD`，然后更新对应 secret/URL 文件并
recreate 唯一消费者；验证后执行 `ALTER USER ... DISCARD OLD PASSWORD`。`<new>` 只在受控输入通道
提供，不粘贴到脚本或工单。

推荐顺序：runtime → migration → Dagster → Keycloak → root。每一步验证最小权限：runtime 不能
DDL，migration 不供 BFF 使用，Dagster/Keycloak 只能访问各自 schema。root 轮换放在最后，并验证
backup/restore 工具仍可读取 `/run/secrets/mysql_root_password`。

如果环境不能安全使用 dual password，进入停写窗口：停止所有消费者，交互式改密，原子替换
secret files，recreate 服务并严格检查 `/readyz`。不要把包含密码的 URL 输出到终端。

## Redis、MinIO、Qdrant 与 embedding

- **Redis**：在 default ACL 临时增加新密码、更新 `redis_url` 和 `redis_users.acl`，recreate
  BFF/Worker，验证限流/缓存后移除旧密码。失败时业务权威状态仍以 MySQL 为准，但不能在未知
  一致性下继续写。
- **MinIO**：当前参考栈使用一组服务凭据，没有双 key 控制面。进入停写窗口，原子替换 access/
  secret files，recreate MinIO bootstrap、BFF/Worker，再验证 bucket/version/Range；MinIO 对象不因
  凭据轮换而删除。
- **Qdrant**：当前 API key 轮换需要维护窗口。替换 key file，recreate Qdrant 与 BFF/Worker，验证
  collection、tenant/project payload 过滤和语义查询；禁止为恢复可用性暂时关闭认证。
- **Embedding provider**：先在 provider 端创建新 key并保留旧 key，替换
  `embedding_api_key`，recreate BFF/Worker，分别验证 document/query 与精确维度，再吊销旧 key。
  模型或维度变化不是密钥轮换，应走新索引构建和质量门禁。

## TLS、Grafana 与其他应用 secret

- **TLS**：在独立路径验证证书链、SAN、有效期和私钥匹配，原子替换 `fullchain.pem`/
  `privkey.pem` 后只 recreate edge。外部检查 TLS 1.2/1.3、HSTS 和 OIDC redirect；失败立即回退旧
  证书文件。私钥权限保持 `0600`。
- **Grafana**：通过管理 API/UI 创建具名管理员并验证后再撤销旧管理员；bootstrap admin secret
  只用于初始化。Grafana 仍仅监听 loopback。
- **audio playback grant / experiment assignment**：这些 secret 影响短时授权或稳定分桶。轮换前
  记录最大有效期/实验语义；必要时接受旧新双 key，否则在维护窗口轮换并明确现有 grant/assignment
  失效影响。
- **browser session**：session token 与 CSRF 在数据库只存 hash。用户/identity 禁用会使会话
  fail closed；全量应急失效必须通过受审计的 session revocation 操作完成，不能只清浏览器 cookie。

## 轮换完成证据

关闭变更单前保存：配置版本/secret metadata、服务 recreate 时间、严格 `/readyz`、OIDC 与业务
smoke、callback/completion key id、告警状态和相关 trace。不得保存 token、cookie、Authorization、
私钥、完整 DSN、原始 callback body 或客户内容。
