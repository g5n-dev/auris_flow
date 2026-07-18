# Security Policy

## Supported Status

当前仓库是原型与后端开发基线，尚未声明生产支持版本。安全问题仍应按高优先级处理。

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

- API 需要 Bearer token；本地演示 token 只在 `local/test/ci` 且 `ALLOW_DEV_AUTH=true` 时可用。
- `prod/release` 需要配置签名 token provider 或正式身份系统，缺少认证配置时应 fail closed。
- `/api/v1/*` 请求必须携带 `X-Tenant-Id` 和 `X-Project-Id`。
- 写操作按资源要求使用 `Idempotency-Key`。
- 运行、审计和 Outbox 带 `trace_id`。
- 审计服务会脱敏常见 secret、token、password、Authorization 等字段。
- CORS origins 和 TrustedHost 必须显式配置；`prod/release` 不允许 `*`。
- 默认响应包含 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy` 和 CSP；`prod/release` 额外启用 HSTS。
- `prod/release` 会对 `AUTH_TOKEN_SECRET`、`AUDIO_PLAYBACK_GRANT_SECRET`、`COMPLETION_RECEIPT_SECRET` 或 key binding、真实 Qdrant API key、真实对象存储配置执行 fail-closed 校验。
- 外部回调使用 HMAC 签名、时间窗和幂等标识；Outbox 已具备租约、重试、fencing、完成回执和死信状态的开发基线验证。
- release gate 已包含 secret scan、Python/npm 依赖审计、跨租户/跨项目负向测试和浏览器失败响应检查。

## Known Gaps Before Public Release

以下项目不否定当前仓库已经验证的开发基线护栏，指正式生产部署前仍需完成的身份、密钥托管和运营强化：

- 当前签名 token provider 只是生产前过渡护栏；仍需替换为正式 JWT/OIDC/SSO 或后端会话体系。
- 前端 local/sessionStorage token 持久化仅用于本地原型；公开发布不得把它描述为生产认证方案。
- 生产前端不能注入共享 bearer token 到 JS bundle。
- 将当前资源级 default-deny、项目作用域和成员权限基线接入正式身份目录与组织策略。
- 使用密钥管理服务保存 secret reference，不保存明文密钥。
- 把现有外部回写 HMAC、重放保护、重试和死信基线接入生产 KMS、告警和值班流程。
- 把现有本地/CI 安全扫描、依赖审计、E2E 权限与日志脱敏门禁接入托管 CI、分支保护和持续漏洞管理。

## Demo Credentials

仓库中的 `dev-token`、Docker 默认密码、MinIO 默认账号和演示账号仅用于本地开发。不要在生产环境复用。
