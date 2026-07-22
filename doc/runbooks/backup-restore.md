# 单机生产版备份与恢复 Runbook

## 适用范围与承诺边界

本 Runbook 只适用于 Auris Flow 官方支持的 Linux 单机 Docker Compose 基线。它提供经过校验的备份与空环境恢复流程，但**不提供节点级高可用、自动故障转移或宿主机故障自动容灾**。宿主机、其本地卷和本机备份同时损坏时，只能依赖已经复制到独立故障域的备份。

默认运营目标（不是无条件保证）：

- 每 24 小时执行一次完整、停写恢复点，目标 `RPO <= 24h`；重要发布前后额外执行一次。
- 在替代宿主机、相同 release 镜像、Compose 配置、外部 secrets 和备份均可用，且权威数据不超过 100 GiB 时，目标 `RTO <= 4h`。
- 数据量、磁盘/网络吞吐或镜像拉取时间超出上述前提时，RTO 必须通过本环境的季度恢复演练重新测量，不能沿用默认值。

MySQL 和 MinIO 是权威来源。Qdrant 是可从 MySQL/对象数据重建的派生索引；Redis 是可丢弃缓存。Dagster 运行元数据与 Keycloak 配置随 MySQL 一并备份，但仍不改变业务数据权威边界。

## 安全边界

备份包含数据库内容、音频/转写对象、身份配置和可能的个人数据，按最高数据等级处理：

1. `backup.sh` 只接受绝对输出路径，并要求显式声明 `--storage-boundary encrypted-external`。
2. 输出路径必须位于经过验证的加密文件系统；完成后立即复制到与生产宿主机独立的加密对象存储或备份设备。
3. 脚本不备份 Compose secrets、TLS 私钥或 OIDC 客户端 secret。它们必须由独立 secret manager 备份和轮换，不能与数据备份使用同一密钥/故障域。
4. 传输使用加密通道；远端开启不可变保留、最小权限和审计。建议保留 7 个日备、4 个周备和 12 个发布前备份，实际周期服从数据法规。
5. `init-secrets.sh` 一次性创建或保留两套职责分离的部署级 Ed25519 密钥：`backup_manifest_*`
   只签备份 manifest，`restore_attestation_*` 只签恢复完成证明；私钥 `0400`、公钥 `0444`，父目录
   `0700`。四个文件都由独立 secret manager 备份，绝不复制进数据备份，也不得跨角色复用。
   可用 `AURIS_BACKUP_MANIFEST_SIGNING_PRIVATE_KEY_FILE`、
   `AURIS_BACKUP_MANIFEST_VERIFY_KEY_FILE`、
   `AURIS_RESTORE_ATTESTATION_SIGNING_PRIVATE_KEY_FILE` 与
   `AURIS_RESTORE_ATTESTATION_VERIFY_KEY_FILE` 指向部署方提供的绝对 secret-file。每次备份签名的
   v3 manifest 会委托恢复证明公钥的 key ID；只有外部 manifest 公钥复验成功、且委托 key ID 与
   外部恢复证明公钥一致，恢复才可继续。仅重算 `manifest.sha256` 和 artifact 自哈希不能伪造该
   信任链。
6. 备份目录不得放入 Git、制品镜像或普通日志。任何失败的 staging 目录也按敏感数据处理。

## 备份内容

- `mysql/all-databases.sql.gz`：`auris_flow`、`keycloak`、`dagster` 三个 schema 的 `mysqldump --single-transaction --quick` 转储，显式包含 schema、routines、triggers 和 events。
- `mysql/table-counts.tsv`：停写恢复点的全局表行数，不按 tenant/project 分组，避免清单泄漏租户画像。
- `minio/versions.json` 与 `minio/objects/**`：`auris-flow` bucket 的全部内容版本及 delete marker。停写复制完成后，工具从每个实际 artifact 计算 SHA-256 并回写 v2 权威计划；该计划和 artifact 再由 manifest 共同保护。恢复时先比较 key、代际顺序、大小、metadata、tags、storage class 与 delete marker，再把每个源 generation 映射到目标新 version ID，按该 ID 精确读回并验证大小和 SHA-256。ETag 只保留作诊断，不作为内容等价依据。
- `qdrant/snapshots.json` 与 `qdrant/snapshots/**`：单节点 collection snapshots、aliases，以及停写点逐 collection 全量 scroll 得到的 v2 语义证据。证据只保存顺序无关的逐点强 SHA-256 聚合指纹，以及一个确定性 probe 的 point ID、payload/vector hash 和 tenant/project scope；不保存原始 payload 或向量。Qdrant 官方要求源/目标使用相同 major/minor 版本，脚本会 fail closed。
- 可选 `redis/cache.rdb`：只用于故障诊断，不参与自动恢复，也不计入业务一致性。
- `metadata/release-metadata.json`、`metadata/release-metadata.sigstore.json`、`metadata/running-images.json`：签名部署包的 tag/commit、Compose、image-lock 与恢复兼容策略哈希，以及备份时全部正在运行的 release service 实际容器/RepoDigest 校验证据；MySQL、MinIO、Qdrant、Redis 始终为必选。
- `manifest.json`、`manifest.sha256`、`manifest.signature.json`：v3 manifest 强绑定上述 release
  metadata/运行镜像证据、UTC、每个文件的 SHA-256/大小、工具版本、deployment-wide counts、
  权威边界、固定恢复顺序及 `auris-flow.restore-attestation-delegation/v1` 恢复证明 key ID；独立 v1
  签名 envelope 绑定规范 manifest digest、manifest Ed25519 key identity、`backup_id`、UTC 与
  source commit。两种公钥都不来自备份目录，且 manifest 验签会拒绝两个角色使用同一个 key ID。
- 备份工具只接受 v3 release metadata，并保留其中按路径排序的完整 bundle 成员清单（SHA-256、
  `regular-file` 类型与精确 mode）；这不会替代恢复前对目标 release bundle 的独立 Sigstore 与
  全成员校验。

MinIO 官方说明普通 `mc mirror` 只复制当前对象、不保留版本历史，因此本实现不使用 mirror，而是逐代读取并重放版本。生产 Qdrant 写路径当前只支持未命名 dense vector，并要求每个非空 collection 的每个 point payload 都包含非空、非通配的 `tenant_id` 与 `project_id`；空 collection 以 `empty-collection` 显式建模。备份遇到 named/sparse vector、缺失 scope、重复 point ID、分页循环或数量漂移都会拒绝继续，不会静默降级。Qdrant collection snapshot 始终只是派生数据恢复加速器，不得替代 MySQL/MinIO 重建能力。

## 创建备份

### 1. 前置检查与停写窗口

先确认外部 secrets/TLS 有独立备份，并为输出卷预留至少“源数据估算的 2 倍 + 1 GiB”。容量阈值可通过 `AURIS_BACKUP_CAPACITY_PERCENT` 和 `AURIS_BACKUP_MIN_FREE_BYTES` 调高，不能用负数或非整数绕过。

进入维护窗口，停止所有写端；依赖层保持运行：

```bash
cd /opt/auris-flow
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml \
  stop edge worker bff dagster-daemon dagster-webserver dagster-code keycloak
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml ps
```

脚本会再次检查写端状态，任一写端仍运行就拒绝备份。这使 MySQL 行数、对象版本和 Qdrant snapshot 对应同一停写恢复点；`--single-transaction` 同时保证 InnoDB 逻辑转储自身一致。

### 2. 执行并离线验包

```bash
cd /opt/auris-flow
production/scripts/backup.sh \
  --output-root /mnt/encrypted-backups/auris-flow \
  --storage-boundary encrypted-external

production/scripts/verify-backup.sh \
  --backup /mnt/encrypted-backups/auris-flow/auris-flow-YYYYMMDDTHHMMSSZ-COMMIT
```

命令默认读取 `production/secrets/backup_manifest_signing_private_key.pem`、
`backup_manifest_signing_public_key.pem` 与仅用于写入委托 ID 的
`restore_attestation_signing_public_key.pem`；备份命令不读取恢复证明私钥。从外部 secret manager
注入时使用对应命令参数或上述环境变量，只传文件路径，不传 PEM 内容。只有看到外部 Ed25519
签名、manifest、所有 artifact SHA-256、MySQL gzip/结构以及 MinIO/Qdrant 元数据全部通过，才允许
退出维护窗口。随后把整个目录复制到外部加密存储，在外部位置连同**独立取得的 manifest 公钥**
再运行一次离线验包。不要只复制 SQL、manifest，或把任一公钥塞进备份后当成信任来源。

如果备份失败，脚本保留唯一的 `.auris-flow-backup.*` staging 目录并打印路径，便于诊断；确认不再需要后，由操作员只删除该确切目录，不使用未解析变量或通配符。

`backup.sh` 不读取 Git，也不接受 `AURIS_SOURCE_COMMIT`。它先用包内 Sigstore bundle 重新验证
release metadata，再核对当前 Compose project 中全部 running service 的配置引用与本地镜像
RepoDigest 均等于已签名 image lock；任何未知 service、可变 tag、stale container 或 metadata
漂移都会在写备份前失败。脚本固定 `default` Docker context 与 `auris-flow` project，拒绝环境变量
重定向。

## 恢复到空环境

### 1. 固定同一 Release

在替代宿主机安装 manifest 所指向的同一签名 deployment bundle，恢复 Compose `.env`、TLS 和
外部 secret files。部署包不含 `.git`；`restore.sh` 比较已安装
`production/release-metadata.json` 与备份 manifest，并验证权威服务的实际运行镜像 digest。

只启动空的强依赖层，不先启动迁移、Keycloak、Dagster、BFF 或 Worker：

```bash
cd /opt/auris-flow
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml pull mysql db-bootstrap redis minio minio-volume-init minio-bootstrap qdrant bff
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps minio-volume-init
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml up -d --no-deps --wait --wait-timeout 240 \
  mysql redis minio qdrant
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps db-bootstrap
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm --no-deps minio-bootstrap
```

volume init 与两个 bootstrap 是前台 one-shot，必须分别返回 0；不得与长期依赖一起交给 detached
`up --wait`。任一阶段失败都保留当前隔离 project 供诊断，不进入恢复步骤。
volume init 只对该 Compose project 的 `minio_data` named volume 根目录 `/data` 做非递归属主修正，
不会解析或触碰宿主机路径。

恢复脚本要求：MySQL 所有目标表总行数为零、MinIO bucket 没有任何版本、Qdrant 没有 collection、应用写端全部停止。不存在“强制覆盖非空目标”参数；如目标非空，应创建另一个全新 Compose project/卷，而不是清库后重试。

### 2. 校验并恢复权威数据

先离线验包，再使用 manifest 的完整 `backup_id` 二次确认：

```bash
cd /opt/auris-flow
production/scripts/verify-backup.sh --backup /mnt/restore/auris-flow-BACKUP_ID

production/scripts/restore.sh \
  --backup /mnt/restore/auris-flow-BACKUP_ID \
  --confirm auris-flow-BACKUP_ID \
  --qdrant-mode snapshot
```

只有目标 release 的已签 restore policy 精确列出旧 backup 的 tag、完整 commit 和 metadata SHA-256
三元组时，才允许跨 release 恢复；操作员还必须显式重复 manifest 中的完整旧 commit，例如
`--allow-release-migration-from <40-or-64-hex-commit>`。首发 policy 为空，因此所有跨 commit 恢复均
被拒绝。未来开放兼容窗口后，恢复成功必须立即执行目标 release 前向 migration。自由文本、短 SHA、
仅凭 commit 的确认或任意“忽略版本”开关都不被接受。

固定恢复顺序是：

1. 以 `O_NOFOLLOW` 将备份复制到权限收紧的私有 staging；在解压 gzip 或导入任何数据前，用部署
   外部公钥二次验证 snapshot 的 Ed25519 signature、key identity、`backup_id`、UTC、source
   commit、manifest 与所有 artifact SHA-256，后续只消费该 snapshot；
2. 恢复 MySQL 三个 schema；
3. 重放 MinIO 的内容版本与 delete marker；
4. 在权威数据成功后恢复兼容的 Qdrant 派生 snapshot/alias；
5. 比较 MySQL 全局表行数；校验 MinIO 每代 key/大小/属性/删除序列，并按目标 version ID 逐代读回验证 SHA-256；对 Qdrant 重做全量 scroll 指纹，再按 ID 精确读取 probe 校验 payload/vector hash，并使用该向量执行同时限定 tenant、project 与 probe ID 的真实 nearest-query，要求只命中自身且返回 payload 不跨 scope；
6. 明确丢弃 Redis cache。

任一步失败都会留下 TSV 诊断报告。MySQL 或 MinIO 成功、Qdrant 失败时，不回滚权威数据，也不伪装成完整成功；修复版本兼容问题后重做空环境恢复，或选择下述治理化重建。

### 3. 不使用 Qdrant snapshot 的重建路径

若 snapshot 缺失或选择从权威数据重建：

```bash
production/scripts/restore.sh \
  --backup /mnt/restore/auris-flow-BACKUP_ID \
  --confirm auris-flow-BACKUP_ID \
  --qdrant-mode rebuild-required
```

此模式把 Qdrant 留空，创建权限 `0600` 的 `*.state.json`，状态固定为
`pending-qdrant-rebuild`，并以退出码 **3** 结束；它不会写 `complete`，也不会声称 Qdrant 一致。
随后运行当前 release 的 migration，启动 BFF/Worker，再对 MySQL 中每个有效 knowledge index 提交
带 tenant/project、幂等键和 trace 的 `POST /api/v1/knowledge-indexes/{id}/build-runs`，并执行受治理
的 Outbox reconciliation。voiceprint 等其他派生 collection 同样通过其权威 MySQL/对象引用和
Outbox 重建，禁止手工拼装无审计向量。

全部 build 完成后，使用 restore 输出的精确 pending state 执行唯一的 finalize 路径：

```bash
production/scripts/finalize-restore.sh \
  --backup /mnt/restore/auris-flow-BACKUP_ID \
  --state /var/tmp/auris-flow-restore-reports/BACKUP_ID-TIMESTAMP.state.json \
  --confirm auris-flow-BACKUP_ID
```

finalizer 会先通过稳定文件描述符把 owner-only Compose `.env` 固定到私有快照，随后重新建立只读
私有 backup snapshot、验证外部 Ed25519 签名和 pending identity，并确认当前运行镜像属于已签
release。它先在 edge、OIDC 路径、Dagster daemon、Worker、BFF 与 code location 均完整运行时取得
严格 `/readyz` 证据，随后一次性停止 edge、Dagster daemon、BFF 与 code location，切断公网、调度、
回调和内部 API 新写入，仅保留 Worker 把既有 Outbox 的 `pending/processing` 递归排空；这避免停止
edge 后切断 OIDC discovery，也避免用已停止 daemon 的陈旧 heartbeat 冒充健康。Outbox 归零后再
停止 Worker 并终检仍为零，写入面从此持续保持围栏状态。最后对实时 Qdrant 重算所有 collection
的全量 fingerprint、scoped probe 与 alias，并在
签名前再次确认 Outbox 没有变化。readiness 取证后到入口冻结之间若发生写入，也必须被后续 Outbox
排空与双重终检吸收，否则 finalize 失败。

运行镜像、Qdrant 语义结果和规范化 readiness 响应不仅计算 SHA-256，还以限长、规范化正文嵌入
`complete` 状态；状态同时绑定证据 schema、大小、哈希与校验器，供离线审计复算。创建 pending
状态时工具内部生成 256-bit `restore_challenge`，并同时记录 manifest digest、manifest 签名 key ID
与被委托的 attestation key ID；调用方不能指定 challenge。操作员应在开始重建时把 pending 状态或
至少其 challenge 另存到受控审计记录，离线验收时据此拒绝整份旧 complete 状态的回放。

finalizer 在停止服务前验证独立恢复证明密钥对及其 key ID 与签名 manifest 的委托完全一致；它不
读取 manifest 私钥。最后一次 Qdrant/Outbox 验证后立即记录 `observed_at_utc`，要求
`pending_at_utc <= observed_at_utc <= completed_at_utc`，再用恢复证明私钥签署包含完整 unsigned
state 摘要、challenge、observed time 和 manifest provenance 的 domain-separated statement，并
立即用固定恢复证明公钥复验。状态文件通过独占锁、同目录原子替换和父目录 `fsync` 完成唯一转换。
只有全部检查通过，状态才转为带 `auris-flow.restore-complete-attestation/v2` 的 `complete`；错误
identity、角色密钥复用、错误委托、签名/challenge/时间篡改、并发或重复 finalize、证据篡改、
缺失/跨 scope point、alias 漂移或 readiness 失败都返回非零。恢复证明私钥不可用时必须从 secret
manager 恢复与 `attestation_key_id` 匹配的原密钥，不能使用 manifest 私钥或生成替代签名掩盖信任
链断裂。成功后 edge、Worker 与 Dagster daemon 仍保持停止；操作员必须先离线执行
`restore_state.py verify-complete`、比对预先归档的 challenge、归档签名状态，再按下一节显式恢复
写入面和入口流量。

### 4. 启动与业务校验

```bash
cd /opt/auris-flow
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml run --rm migrate
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml \
  up -d keycloak dagster-code dagster-webserver dagster-daemon bff worker edge
```

确认：

- `/healthz` 存活，`/readyz` 对 MySQL、Redis、MinIO、Qdrant、Dagster、OIDC 与 observability
  全部严格通过；observability 会实时探测 Collector、Tempo、Prometheus、Alertmanager 与
  node-exporter，而不是只检查应用内 SDK 标志；
- 核心 tenant/project 的资产数、对象逐代 SHA-256/大小、知识索引 point 数与备份报告一致；
- 新登录、任务提交、Outbox 投递和回调签名链路正常；
- 恢复报告进入受控审计存储，不写入普通应用日志。

## 空环境恢复演练

季度执行一次，并在每个 release candidate 上执行。命令使用全新随机 Compose project/volume，
先 `pull` 固定 digest 并验证实际镜像，不进行本地 `build`，也不触碰生产 project：

```bash
production/scripts/verify-backup.sh \
  --backup /mnt/restore/auris-flow-BACKUP_ID \
  --drill \
  --cleanup-on-success
```

失败时演练环境会保留，便于检查容器和卷；成功时只有显式 `--cleanup-on-success` 才会对脚本刚创建且名称通过校验的 project 执行 `down --volumes`。把实测数据量、备份耗时、恢复耗时和瓶颈记录到变更单，用它更新 RPO/RTO，不用“脚本返回 0”代替恢复能力证明。

## 已知限制

- 单机 Compose 无节点 HA；本地 named volume 不是备份。
- 该基线是完整停写恢复点，不提供 MySQL binlog 增量/PITR。需要低于 24 小时 RPO 时，应增加加密 binlog 外送和相应恢复演练。
- MinIO 恢复保留内容版本、删除语义、大小和属性；provider 分配的新 version ID 与 ETag 均不要求等于源值。恢复器以代际顺序建立源/目标 version 映射，并以实际内容 SHA-256 判定等价；应用不把 provider version ID 或 ETag 当作不可替代的唯一业务事实。
- Qdrant snapshot 只支持兼容版本；v2 以前缺少全量语义指纹和 scoped probe 的 metadata 会 fail closed，必须重新备份或从权威数据重建。任何 snapshot 都可以丢弃并从 MySQL/MinIO 重建。
- Redis RDB 不自动恢复。缓存预热时间应计入本环境 RTO。
