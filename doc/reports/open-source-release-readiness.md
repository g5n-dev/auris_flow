# Open-Source Release Readiness

本文把“仓库已有生产候选实现”“自动门禁通过”和“已经获得正式开源/生产发布授权”明确分开。
当前目标是 Auris Flow `v1.0.0`，首个支持形态为单台 Linux 主机上的 Docker Compose；不承诺
节点级高可用或宿主机故障自动容灾。

## 已落盘的候选能力

### P0：可信发布树

- React/Vite 前端、FastAPI BFF、Alembic 迁移、契约/集成测试、OpenAPI 与公共数据集注册表均有
  仓库级验证入口。
- `scripts/check_platform_readiness.py` 检查当前模块目录、catalog、bundle policy、生产 Compose、
  Runbook 与供应链入口；运行时 OpenAPI 漂移由 `validate_backend_spec.py` 阻断。
- `scripts/scan_secrets.py` 检查当前树与完整 Git 历史；构建产物、缓存、本地数据库、截图、音频、
  secret/TLS 和发布临时制品均在发布树之外。
- `scripts/verify_fast.sh` 是开发快速门禁，`scripts/verify_release.sh` 是严格候选门禁。正式候选仍须
  从精确 commit 做一次外部干净克隆验证。

### P1：身份与安全边界

- `prod/release` 使用通用 OIDC Authorization Code + PKCE，验证 issuer、audience、签名、过期
  时间和 JWKS；浏览器只持有不透明 `Secure`/`HttpOnly` session cookie，写请求同时校验 CSRF
  与 Origin。
- `(issuer, subject)` 只映射显式预置的内部 user/tenant/project/role；未知身份不自动创建租户或
  成为管理员。Keycloak 是参考 IdP，不是产品私有认证协议。
- 资源级 default-deny、tenant/project 隔离、幂等、审计、trace、Outbox、HMAC v2 callback、
  key overlap/retire 与重放保护已有实现和负向测试。
- 生产配置拒绝开发认证、demo/弱 secret、通配 CORS/TrustedHost、local/fake adapter、非严格
  readiness、确定性 embedding 和缺失强依赖配置；secret 通过 file/reference 注入。

### P2：单机生产运行链路

- `production/compose.yaml` 包含 MySQL、Redis、MinIO、Qdrant、迁移、Keycloak、身份预置、真实
  Dagster code server/webserver/daemon、BFF、Worker、TLS edge、OTel Collector、Tempo、
  Prometheus、Grafana 与 node-exporter。
- Dagster 使用独立锁图、MySQL storage、领域 job、fencing context 和受签名 completion callback；
  生产 embedding 使用可替换的 HTTPS 语义接口并严格校验模型与向量维度。
- MySQL 是权威业务事实；MinIO 保存权威对象；Redis 是可丢弃辅助状态，Qdrant 是可重建派生索引。
- Outbox 实现 lease、fencing、指数退避、最大重试、dead-letter 和受治理重放。备份工具冻结写入后
  保存 MySQL、MinIO 版本/删除标记、Qdrant snapshot/alias，并通过规范 manifest、checksum 和
  空环境恢复验证一致性。

### P3：可观测性与运维

- BFF/Worker 接入 OTel SDK 与 HTTP、SQLAlchemy、Redis、HTTPX instrumentation；业务
  `trace_id/request_id` 与 OTel span 关联，export/log 执行字段级脱敏。
- 内部 `/metrics` 提供低基数 API、认证、readiness、Outbox、callback、Worker 与连接池指标；edge
  固定拒绝 `/metrics`，公开 `/readyz` 精确反代 BFF 的严格依赖状态。
- Prometheus 告警、Grafana 生产概览、备份 freshness textfile metric、SLO 和安装/升级/回滚/
  恢复/轮换/故障/安全事件 Runbook 已进入候选树。
- Compose 未内置具体企业通知目标；真实 Alertmanager/私密通知和值班确认必须在 RC 环境提供证据。

### P4：不可变供应链

- `.github/workflows/release-images.yml` 从精确 commit 构建 amd64/arm64 BFF、Dagster、edge 镜像，
  生成 provenance/CycloneDX SBOM，阻断 HIGH/CRITICAL 漏洞并对通过的 digest 做 keyless Cosign
  签名与验证。
- 后端、生产 Dagster、npm 三个锁图分别生成依赖/许可证据并执行漏洞审计；自动许可门禁仅接受
  明确白名单中的 SPDX 表达式，未知、模糊、`WITH` 或白名单外结论只允许精确生态、包名、版本、
  具名审阅者、引用和到期日的受控记录，漏洞不以许可证例外豁免。当前 Dagster 锁图仍有三项待
  人工审阅，详见 `THIRD_PARTY_NOTICES.md`，因此供应链正式发行证据会按设计 fail closed。
- release renderer 把每个 Compose service image 固定到 `tag@sha256:digest`，删除所有 `build`，
  并产出 image lock、manifest 与 `SHA256SUMS`；源码、镜像和元数据必须绑定同一 tag/commit。

## 开发专项与生产证据边界

`bash scripts/verify_real_stack.sh` 仍是快速开发专项：它真实调用 MySQL、Redis、MinIO、Qdrant，
但 Dagster endpoint、外部 callback receiver 和 embedding 向量是协议/确定性测试夹具。这个路径可以
防止适配器退化为本地 receipt，却不能替代 `production/compose.yaml` 中真实 Dagster、真实语义
provider、企业 IdP、外部 callback、遥测、故障恢复和备份恢复 E2E。文档和 release 审批不得把
两类证据混为一谈。

## 自动检查

开发完整性：

```bash
python3 scripts/check_platform_readiness.py
bash scripts/verify_fast.sh
```

冻结候选：

```bash
python3 scripts/check_platform_readiness.py --release
bash scripts/verify_release.sh
python3 scripts/verify_production_compose.py
```

严格模式还必须运行两个 Python 锁图与 npm 的漏洞审计、生产 Dagster 测试、生产配置/备份/遥测
测试、浏览器 UI/BFF E2E、视觉回归和开发真实依赖专项。只有精确候选 commit 的 Git 索引与工作区
一致时，release tree 检查才会通过。

## 尚未完成且不能由代码伪造的门禁

以下任一项未完成时，不得 tag/publish 或宣称 `v1.0.0` 已正式开源、已获生产支持：

1. 项目所有者填写并签署 `open-source-rights-authorization.md`，给出真实个人版权/许可主体，并在
   同一候选 commit 中替换 `NOTICE` 占位内容。
2. 在托管 GitHub 仓库实际启用分支/标签保护、Private Vulnerability Reporting、secret scanning、
   push protection、CodeQL、Dependabot 和独立 Release 审批。
3. 用正式签名 digest 在外部干净 Linux 主机完成 OIDC、核心业务、真实 Dagster/embedding/callback、
   故障恢复、升级/回滚、告警通知和空环境备份恢复演练。
4. 先发布并验证 `v1.0.0-rc.1`，修复外部安装问题，然后从新的最终 commit 重复全部门禁再批准
   `v1.0.0`。

权威逐项清单见根目录 `RELEASE_CHECKLIST.md`；已知外部安全缺口见 `SECURITY.md`。
