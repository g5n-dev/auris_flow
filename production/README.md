# Auris Flow 单机生产 Compose 基线

> **发行状态：`v1.0.0` 候选，尚未发布。** 本目录已经包含单机 Linux Compose、真实
> Dagster、OIDC/PKCE、语义 embedding 接口、OTel Collector、Prometheus、Tempo、Grafana
> 及备份恢复工具，但这不等于已完成正式生产发行。项目所有者尚未完成许可权利主体签字，
> `v1.0.0-rc.1` 的真实发布演练与外部干净安装也尚未完成。当前不得把 `:dev` 镜像或本页步骤
> 描述为已获生产支持的正式版本。

## 支持边界

首个支持目标是 **一台 64 位 Linux 宿主机上的 Docker Compose**。MySQL 是权威业务存储，
MinIO 保存权威对象；Redis 当前承担固定窗口限流和运行辅助状态，尚未实现结果缓存读写，Qdrant
是可重建派生索引。Dagster 是内部执行引擎，不作为产品 API 或前端画布暴露。

该形态没有节点级高可用、自动故障转移或宿主机故障自动容灾。维护、升级、部分密钥轮换和完整
备份需要停写窗口。默认恢复目标及前提见
[备份恢复 Runbook](../doc/runbooks/backup-restore.md)，不能把 named volume 当作备份。

## 主机、网络与容量

候选支持矩阵如下；正式版本必须以 RC 验收记录为准：

- 64 位 Linux，内核 5.15 或更新；不把 Docker Desktop、macOS 或 Windows 作为生产环境。
- Docker Engine 26+、Docker Compose plugin 2.27+、Git、OpenSSL、curl，以及可校时的 UTC/NTP。
- 最低 4 vCPU、16 GiB RAM、100 GiB SSD；建议 8 vCPU、32 GiB RAM、500 GiB SSD 起步。
- 备份目标额外预留至少源数据估算的 2 倍加 1 GiB，并复制到独立加密故障域。
- 对外只开放 TCP 80/443。Keycloak 管理端默认仅绑定 `127.0.0.1:18080`，Grafana 默认仅绑定
  `127.0.0.1:13000`；不得把它们改成公网监听。
- DNS 的 `AURIS_PUBLIC_HOST` 必须解析到该宿主机。出站网络至少允许企业 IdP、语义 embedding
  endpoint、外部回调目标、镜像仓库和时间同步服务。
- `AURIS_EDGE_INTERNAL_IP` 必须是 `AURIS_INTERNAL_SUBNET` 内未占用的固定地址。BFF 只信任该
  精确地址提供的代理头；edge 会用真实 socket peer 覆盖用户提交的 `X-Forwarded-For`，不得把
  Uvicorn 信任范围扩大为 `*` 或整个用户可达网络。

容量必须按真实音频保留期、对象版本、MySQL 增长、Qdrant 向量维度，以及当前 Tempo 7 天、
Prometheus 30 天的配置保留期重新测算；修改保留期后必须重新做容量与磁盘告警演练。上述硬件只是
小规模候选基线，不是容量承诺。

## 发行制品规则

正式安装只接受同一 release commit 生成并验证的固定镜像 digest、SBOM、签名、checksum、
`NOTICE`、迁移说明和 release notes。禁止使用 `latest`，也不要把 `.env.example` 中的 `:dev`
当作正式镜像。

在正式 `v1.0.0-rc.1` 制品出现前，可以从当前 checkout 构建**验收环境**，但必须明确标记为
candidate evaluation。正式 release 的部署 tar 以根目录 `README.md` 为唯一安装入口，其中
`production/compose.yaml` 已把全部镜像固定到 digest，并由
`production/release-metadata.json`、其独立 `production/release-metadata.sigstore.json`、外层
`SHA256SUMS` 与 checksum Sigstore bundle 共同绑定；不存在额外的镜像环境覆盖文件。metadata
Sigstore bundle 作为单独 Release asset 下载、经外层 checksum 校验后按根 README 安装。缺少任一
正式签名、checksum 或 metadata 时停止生产安装。

## 安装前准备

1. 创建专用系统账户与目录，例如 `/opt/auris-flow`；仓库、secret、TLS 和备份目录都不得被普通
   用户读取。
2. 为 `AURIS_PUBLIC_HOST` 配置 DNS 和可信 CA 签发的证书。证书链写入
   `production/tls/fullchain.pem`，私钥写入 `production/tls/privkey.pem`；目录权限 `0700`，私钥
   `0600`。`init-secrets.sh` 不生成 TLS 私钥。
3. 选择通用 OIDC IdP。当前 Compose 默认启动 Keycloak 参考实现；替换为外部 IdP 需要受审查的
   Compose override/config，显式覆盖 issuer/discovery/client/audience/redirect 并重新执行门禁。
   外部 IdP 仍须满足 PKCE S256、JWKS 和 scope 契约，不能依赖 Keycloak 私有 claim。
4. 准备 HTTPS 语义 embedding 服务。请求格式为 `input` 数组、`model` 与 `input_type`
   (`document`/`query`)，响应接受单项 `data[0].embedding` 或 `embeddings[0]`；返回维度必须与
   `AURIS_EMBEDDING_DIMENSION` 完全一致。确定性测试向量在 `prod/release` 会被拒绝。
5. 准备外部回调 HTTPS endpoint，并确认双方已约定 HMAC v2 key id、时间窗、nonce、幂等键和
   轮换窗口。
6. 在独立加密介质准备 backup 输出路径，并确认安全上报、值班和告警通知渠道已经存在。

## 配置、secret 与 TLS

从仓库根目录执行：

```bash
cp production/.env.example production/.env
chmod 600 production/.env
${EDITOR:?set EDITOR} production/.env
bash production/scripts/init-secrets.sh
```

至少替换以下非 secret 配置：

- `AURIS_PUBLIC_HOST`：不带 scheme/path 的公开 FQDN。
- `AURIS_EXTERNAL_CALLBACK_URL` 与 `AURIS_EXTERNAL_CALLBACK_HOST`：必须相互匹配且使用 HTTPS。
- `AURIS_EMBEDDING_ENDPOINT`、`AURIS_EMBEDDING_MODEL`、`AURIS_EMBEDDING_DIMENSION`。
- `AURIS_INTERNAL_SUBNET` 与 `AURIS_EDGE_INTERNAL_IP`：两者必须匹配且不得与宿主机/VPN 网段冲突；
  修改后必须重新渲染 Compose 并执行生产策略门禁。
- 所有应用和上游镜像引用；正式版本必须是发布清单给出的 digest。
- `AURIS_OTEL_TRACE_SAMPLE_RATIO`（默认 0.1）与所需的内部 metrics CIDR。

`production/secrets/` 中是 Docker secret source file，不得进入 Git、镜像、`.env`、命令行参数
或日志。初始化脚本只创建缺失文件并保留现有值；它不是企业 secret manager。将这些 secret
用独立 KMS/secret manager 加密备份，并按
[密钥轮换 Runbook](../doc/runbooks/key-rotation.md)执行轮换。不要把 secret、TLS 私钥和数据备份
存放在同一故障域。

启动前检查：

```bash
test -s production/tls/fullchain.pem
test -s production/tls/privkey.pem
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml config --quiet
```

源码 checkout 的 secret scan 由仓库根验证入口执行；部署 tar 不携带源码扫描器。部署 tar 必须改用
`python3 scripts/release_bundle.py verify --bundle-root . --verify-signature` 先按固定官方 tag workflow
identity 验证 metadata，再校验包内 schema、commit、Compose、image lock 与恢复兼容策略哈希；不能
把源码扫描命令误当成发布包完整性校验。

生产配置会 fail closed：开发认证、demo/弱密码、通配 CORS/TrustedHost、local/fake adapter、
非严格依赖检查、缺失 OIDC/真实存储/回调配置和生产确定性 embedding 均不被接受。

`GET /api/v1/audio-playback` 的短期 grant 位于 query string，以兼容浏览器原生媒体 Range 请求；
edge 对该精确 location 关闭 access log，BFF 结构化 HTTP 日志也只记录 path、不记录 query。不要在
上游负载均衡、WAF、APM 或故障工单中重新记录完整 URL；需要排障时使用 request/trace ID 和服务端
审计中的 grant nonce。

MySQL 8.4 默认启用 binary log；本项目的不可变事实约束需要 Alembic 创建 `TRIGGER`。Compose
因此显式固定 `log_bin_trust_function_creators=1`，但 bootstrap 会先撤销历史宽授权，再只给
`auris_migration` 当前 schema 所需的 DDL/DML、`REFERENCES` 与 `TRIGGER`；runtime 没有
`TRIGGER`，并清除两个业务账号遗留的 MySQL role membership；Keycloak/Dagster 只管理各自
schema，所有业务账号均没有全局 `SUPER`。不得靠临时全局提权后把迁移结果
记为生产通过，也不得把 migration URL 提供给 BFF/Worker。变更此策略时必须用原 production
Compose、`auris_migration` 身份重新执行完整迁移循环并检查 `SHOW GRANTS`。

## OIDC 配置与身份预置

参考 Keycloak realm 使用：

- issuer：`https://<AURIS_PUBLIC_HOST>/realms/auris-flow`
- client ID：`auris-flow-web`（public client）
- audience：`auris-flow-api`
- redirect URI：`https://<AURIS_PUBLIC_HOST>/api/v1/auth/oidc/callback`
- scope：`openid profile email`
- Authorization Code + PKCE S256；禁用 implicit、password grant 和 `offline_access`

BFF 验证 issuer、audience、签名、过期时间和 JWKS，并把 `(issuer, subject)` 映射到已预置的
内部用户、tenant、project 与角色。**未知身份不会自动成为管理员，也不会自动创建租户。** 参考
Keycloak 的首次干净安装会导入固定 subject `9d1c5cc4-e661-4af6-8a6f-7402d2555c35` 的
`bootstrap-operator`，其随机初始密码只写入
`production/secrets/keycloak_bootstrap_operator_password` 并以 Docker secret 注入，不在 realm
模板、Compose 环境或日志中出现。该凭据是 temporary，首次登录必须改密。

`identity-bootstrap` one-shot 会在数据库迁移和 Keycloak 就绪后执行
`python -m app.identity_bootstrap`，以一个事务建立或核验该 subject 对内部用户、默认 tenant/project
及唯一 `project_admin` 角色的映射，同时写入不含原始 subject 的 trace 和 audit。BFF 只在该服务
成功后启动。重复运行不会重新授权；映射、用户角色、项目角色或审计证据发生漂移时会 fail closed，
维护者必须先调查，不能通过删除证据或重跑脚本强行覆盖。浏览器只持有 `Secure`、`HttpOnly`、
`SameSite` 的不透明 session cookie；写操作同时校验 CSRF token 与 Origin，不向 bundle 暴露 IdP
token。

首次登录由有权读取生产 secret 的两人受控完成：通过安全通道交付一次性密码，立即改密、启用 MFA、
验证项目范围和审计，再创建具名日常管理员。不要在 shell history、工单或聊天中复制初始密码；完成
交接后把该 secret 视为已失效并按密钥管理制度销毁其外部副本。若使用外部 IdP，应在首次启动前通过
受控 Compose override 将 `AURIS_BOOTSTRAP_OIDC_ISSUER` 与
`AURIS_BOOTSTRAP_OIDC_SUBJECT` 改为已审批主体；首次成功后再改值会被视为漂移并阻止 BFF 启动。

Keycloak 管理端只允许通过宿主机本地端口或受控 SSH 隧道访问。首次登录后立即创建具名管理员、
启用 MFA、验证审计，再禁用 bootstrap admin。外部 IdP 的客户端 secret（如使用 confidential
client）必须通过 secret file/reference 注入；当前参考 public PKCE client 没有共享客户端 secret。

## 候选环境启动与验收

以下 `build` 命令**仅适用于包含 `.git` 与 Dockerfile 的源码 checkout**。若目录中存在
`production/release-metadata.json`，说明当前是正式部署包，必须回到包根目录按 `README.md` 执行
`pull` 与 `up`，不得运行本节：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml build

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  mysql redis minio qdrant tempo node-exporter

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml run --rm --no-deps db-bootstrap

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml run --rm --no-deps minio-bootstrap

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml run --rm --no-deps migrate

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  keycloak otel-collector

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml run --rm --no-deps identity-bootstrap

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  dagster-code dagster-webserver dagster-daemon bff worker prometheus grafana edge
```

`db-bootstrap`、`minio-bootstrap`、`migrate` 与 `identity-bootstrap` 是必须成功退出的 one-shot；
不要把它们混入 detached `up --wait`。每个阶段失败都应停止上线并查看该阶段日志。

检查状态和公开入口：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml ps
curl --fail --silent --show-error https://auris.example.com/healthz
curl --fail --silent --show-error https://auris.example.com/readyz
```

公开 `/healthz` 只证明 edge 进程存活，不证明依赖可用。公开 `/readyz` 精确反代 BFF 的严格
readiness，在生产检查 auth、MySQL、Redis、MinIO、Qdrant 和真实 Dagster，任一强依赖失败返回
503；MinIO 必须以配置凭据签名访问目标 bucket，Qdrant 必须返回 2xx。不要在负载均衡器上用
`/healthz` 代替它。OIDC discovery/JWKS 的真实登录另行验收。
若把对象存储切换为 AWS S3，运行身份除实际业务所需的对象级读写权限外，还必须对目标 bucket
拥有 `s3:ListBucket`，因为严格 readiness 使用签名 `HeadBucket`；缺少该权限会得到 403 并按未就绪
处理。此要求不需要、也不得通过公开 bucket 或匿名访问来满足；其他 S3-compatible provider 应授予
对应的最小 bucket-HEAD 权限。
`/metrics` 不经 edge 暴露（外部返回 404），只由内部 Prometheus 抓取；Grafana 通过本机
`127.0.0.1:13000` 或 SSH 隧道访问。仪表盘使用 MySQL 权威记录展示 TaskRun 终态、24 小时滚动
完成时长、deadline/status-sync 监控动作，使用 attempt ledger 展示 callback retry/failure，并展示
真实限流结果。容量告警来自 node-exporter 所见的可写宿主文件系统，不冒充 MinIO bucket quota 或
单个 Docker volume 的独占容量；详细口径见[运维 Runbook](../doc/runbooks/operations.md)。

BFF、Worker 与 `dagster-code` 分别使用固定 OTel service name，并仅向内部
`otel-collector:4318` 发送 OTLP/HTTP。BFF 提交真实 Dagster run 时会通过内部 run config 传递完整
W3C parent context，code server 据此延续同一 trace；Tempo 中的执行跨度仍使用业务 `trace_id`
关联领域链路。固定 DNS/IP 的签名外部回调使用显式 HTTP CLIENT 子 span，并向对端注入 W3C
`traceparent`；该 span 只记录 method、scheme、已校验 host、port 和数字状态码，不记录路径、query、
请求头、HMAC、body 或异常消息。Collector 在转发 Tempo 前删除认证头、cookie、SQL statement 与
URL query；run config 和 span attribute 禁止承载凭据、原始音频或转写。

Worker 同时执行 TaskRun deadline 与漏回调核对。`AURIS_TASK_RUN_DEFAULT_DEADLINE_SECONDS`、
`AURIS_TASK_RUN_STATUS_SYNC_INTERVAL_SECONDS`、`AURIS_TASK_RUN_MONITOR_POLL_SECONDS` 和
`AURIS_TASK_RUN_MONITOR_BATCH_SIZE` 通过 Compose 映射为服务端配置；请求方不能逐运行覆盖。生产固定
启用 monitor，禁用会使 BFF/Worker 配置校验 fail closed。扩 Worker 时无需外部分布式锁：候选 RunRecord
使用数据库 `SKIP LOCKED`，控制代次、审计与 Outbox 在同一事务提交，远端副作用继续由 Outbox lease
generation/fencing token 保护。

上线前至少完成：

- OIDC 登录、登出、会话恢复、用户禁用、角色变化和 JWKS 轮换演练。
- 核心业务从数据资产到标注、评测、洞察、复核与发布，并沿同一业务 `trace_id` 回溯。
- 真实 Dagster 提交/完成/失败/取消/超时，语义 embedding/Qdrant，MinIO Range 和签名回调 E2E。
- MySQL/Worker 重启、重复投递、回调超时以及 Redis/Qdrant 短时故障恢复测试。
- [备份恢复](../doc/runbooks/backup-restore.md)、[升级回滚](../doc/runbooks/upgrade-rollback.md)、
  [告警](../doc/runbooks/operations.md#alert-testing)和安全事故桌面演练。

## 日常运维入口

- [SLO、告警与故障排查](../doc/runbooks/operations.md)
- [备份与空环境恢复](../doc/runbooks/backup-restore.md)
- [升级、数据库迁移与回滚](../doc/runbooks/upgrade-rollback.md)
- [密钥和证书轮换](../doc/runbooks/key-rotation.md)
- [安全事件响应](../doc/runbooks/security-incident-response.md)
- [版本、API、数据库与配置兼容策略](../doc/release/versioning-and-compatibility.md)

## 停止与卸载

计划停机只执行 `docker compose down`，不要附加 `--volumes`。删除 named volume 会删除生产数据，
不能作为普通卸载或重试步骤。任何需要清空环境的恢复演练都应使用新的、显式命名的 Compose
project；具体安全边界以备份恢复 Runbook 为准。
