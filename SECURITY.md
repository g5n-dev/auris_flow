# Security Policy

## Supported Status

当前仓库是 `v1.0.0` 单机生产基线的候选实现，尚未发布或声明受支持版本。安全问题仍应按高优先级处理，任何候选门禁通过都不能替代正式 release 审批。

## Reporting a Vulnerability

如果发现漏洞，请不要在公开 issue 中披露可利用细节。请使用仓库 **Security → Advisories → Report a vulnerability** 的 GitHub Security Advisory / Private Vulnerability Reporting 通道提交报告。若该入口不可用，只创建标题为 `Private security reporting unavailable` 的公开 issue，请勿附带复现步骤、日志、客户数据或可利用细节；维护者必须先启用私有上报通道再接收报告。未启用可验证的私有安全上报通道时，不得发布正式 release candidate。

期望响应：

- 3 个工作日内确认收到报告。
- 7 个工作日内给出初步影响判断或补充信息请求。
- 高危问题在修复或缓解方案可用前，不公开利用细节。

报告请包含：

- 影响模块和版本。
- 复现步骤。
- 影响范围。
- 是否涉及租户隔离、鉴权、审计、外部回写、对象存储或敏感数据。

## Current Security Baseline

- `prod/release` 强制使用通用 OIDC Authorization Code + PKCE；BFF 精确验证 issuer、audience、
  签名、过期时间和 JWKS。缺失/不安全 OIDC 配置 fail closed，不能回退到开发 token。
- 浏览器使用 `Secure`、`HttpOnly`、`SameSite` 的不透明 session cookie；数据库只保存 session token
  与 CSRF 的 SHA-256。IdP token 不返回前端，生产 bundle 不持久化共享 bearer token。
- cookie 认证写操作同时校验 `X-CSRF-Token` 和受信 Origin；登出使本地 BFF session 失效，但不
  伪称终止 IdP 全局 SSO。BFF 不保存 refresh token，会话过期后重新执行 Code + PKCE。identity、
  user、tenant 或 project 禁用以及角色变化会在后续请求动态生效。
- 本地演示 token/dev-login 只在 `local/test/ci` 且 `ALLOW_DEV_AUTH=true` 时可用；生产配置会拒绝它。
- 受保护的 `/api/v1/*` 请求绑定 `X-Tenant-Id` 和 `X-Project-Id`；资源读取/写入执行项目作用域和
  default-deny 检查，未知身份不会自动 provision 为管理员。
- 写操作按资源要求使用 `Idempotency-Key`。
- 运行、审计和 Outbox 带业务 `trace_id`；结构化日志关联活动 OTel trace/span，并对常见 secret、
  token、password、Authorization、cookie、SQL 和 URL query 等字段脱敏。
- CORS origins 和 TrustedHost 必须显式配置；`prod/release` 的 CORS 只接受精确 HTTPS Origin，
  TrustedHost 只接受无通配符的精确主机。
- 默认响应包含 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy` 和 CSP；`prod/release` 额外启用 HSTS。
- `prod/release` 对 demo/弱数据库与 Redis 密码、通配 CORS/TrustedHost、local/fake adapter、
  非严格 readiness、真实 Qdrant/对象存储、completion/callback key binding 和确定性 embedding 执行
  fail-closed 校验。
- 外部 callback 使用请求全绑定的 HMAC v2、key id/overlap/retire、时间窗、原子 nonce 重放保护和
  body-bound idempotency；Outbox 具备 lease、指数退避、fencing、最大重试、receipt 和 dead-letter。
- `/metrics` 不经 edge 暴露，只允许 loopback/容器私网或显式可信 CIDR；Grafana和 Keycloak 管理端
  默认只绑定 loopback。
- release gate 包含 secret scan、Python/npm 依赖审计、SBOM/license evidence、跨租户/项目负向
  测试和浏览器失败响应检查。

## Known Gaps Before Public Release

以下是正式发布/生产声明前仍需完成的运营与外部状态门禁，不应被误写成 OIDC/会话或 callback
协议尚未实现：

- 在真实企业 IdP 演练登录、登出、重新认证、禁用用户、角色降权、JWKS overlap/retire 和 IdP
  故障；完成所有资源的跨租户/项目/角色负向矩阵。
- 将 Docker secret source file 接入外部 KMS/secret manager，演练 OIDC、callback/completion、
  数据库、Redis、MinIO、Qdrant、embedding、TLS 和 CI/registry 凭据的常规/应急轮换。
- 在真实 callback 接收方验证 HMAC v2、nonce、body conflict、超时查询 receipt、dead-letter 告警和
  受治理人工 replay；不能用 fake receiver 作为生产证据。
- 接入 Alertmanager/实际私密通知和值班升级链路，并完成认证失败、依赖离线、死信、磁盘和备份
  失败演练。当前 Compose 有规则和 dashboard，但未内置外部通知路由。
- 在托管仓库实际启用并验证分支保护、Private Vulnerability Reporting、secret scanning、push
  protection、CodeQL、Dependabot 和正式 release 审批；本地文件不能证明这些外部开关已启用。
- 完成签名固定 digest 的多架构镜像、全依赖 SBOM/许可清单、外部干净安装、恢复与安全事故桌面
  演练。当前仍无获支持的 `v1.0.0`。

详细操作见[密钥轮换](doc/runbooks/key-rotation.md)和
[安全事件响应](doc/runbooks/security-incident-response.md)。

## Demo Credentials

仓库中的 `dev-token`、本地 Docker 默认密码和演示账号仅用于 local/test。生产初始化脚本生成的
随机 secret 也只是 Docker secret file 基线，不代替企业 secret manager；不要复用、提交、截图或
复制到普通日志/工单。
