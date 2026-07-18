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
5. `manifest.json` 的 SHA-256 用于完整性和误传检测，不等同于来源签名。正式 Release 应在外部备份系统对 `manifest.sha256` 再执行组织级签名。
6. 备份目录不得放入 Git、制品镜像或普通日志。任何失败的 staging 目录也按敏感数据处理。

## 备份内容

- `mysql/all-databases.sql.gz`：`auris_flow`、`keycloak`、`dagster` 三个 schema 的 `mysqldump --single-transaction --quick` 转储，显式包含 schema、routines、triggers 和 events。
- `mysql/table-counts.tsv`：停写恢复点的全局表行数，不按 tenant/project 分组，避免清单泄漏租户画像。
- `minio/versions.json` 与 `minio/objects/**`：`auris-flow` bucket 的全部内容版本及 delete marker，并记录可安全重放的 user metadata、tags 和 storage class；恢复后比较每代的 key、顺序、大小、ETag 与这些属性。目标 MinIO 会生成新的 version ID，因此业务不能把 provider version ID 当唯一事实。
- `qdrant/snapshots.json` 与 `qdrant/snapshots/**`：单节点 collection snapshots 和 aliases，仅用于加速派生索引恢复。Qdrant 官方要求源/目标使用相同 major/minor 版本，脚本会 fail closed。
- 可选 `redis/cache.rdb`：只用于故障诊断，不参与自动恢复，也不计入业务一致性。
- `metadata/release-metadata.json`、`metadata/release-metadata.sigstore.json`、`metadata/running-images.json`：签名部署包的 tag/commit、Compose、image-lock 与恢复兼容策略哈希，以及备份时全部正在运行的 release service 实际容器/RepoDigest 校验证据；MySQL、MinIO、Qdrant、Redis 始终为必选。
- `manifest.json`、`manifest.sha256`：v2 schema 强绑定上述 release metadata/运行镜像证据、UTC 时间、每个文件的 SHA-256/大小、工具版本、deployment-wide counts、权威边界和固定恢复顺序。

MinIO 官方说明普通 `mc mirror` 只复制当前对象、不保留版本历史，因此本实现不使用 mirror，而是逐代读取并重放版本。Qdrant collection snapshot 是派生数据恢复加速器，不得替代 MySQL/MinIO 重建能力。

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

只有看到 manifest、所有 artifact SHA-256、MySQL gzip/结构以及 MinIO/Qdrant 元数据全部通过，才允许退出维护窗口。随后把整个目录复制到外部加密存储，在外部位置再运行一次离线验包。不要只复制 SQL 文件或只复制 manifest。

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
  --file production/compose.yaml pull mysql db-bootstrap redis minio minio-bootstrap qdrant bff
docker --context default compose --project-name auris-flow \
  --project-directory production --env-file production/.env \
  --file production/compose.yaml up -d mysql db-bootstrap redis minio minio-bootstrap qdrant
```

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

1. 以 `O_NOFOLLOW` 将备份复制到权限收紧的私有 staging，二次验证 snapshot manifest 与所有 SHA-256，后续只消费该 snapshot；
2. 恢复 MySQL 三个 schema；
3. 重放 MinIO 的内容版本与 delete marker；
4. 在权威数据成功后恢复兼容的 Qdrant 派生 snapshot/alias；
5. 比较 MySQL 全局表行数、MinIO 每代 ETag/大小/删除序列、Qdrant collection/point counts；
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

此模式把 Qdrant 留空并在报告中记录 `action-required`。随后运行当前 release 的 migration，启动 BFF/Worker，再对 MySQL 中每个有效 knowledge index 提交带 tenant/project、幂等键和 trace 的 `POST /api/v1/knowledge-indexes/{id}/build-runs`，并执行受治理的 Outbox reconciliation。所有 build 完成、Qdrant point/payload scope 校验和 `/readyz` 通过前，不得开放入口流量。voiceprint 等其他派生 collection 同样通过其权威 MySQL/对象引用和 Outbox 重建，禁止手工拼装无审计向量。

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

- `/healthz` 存活，`/readyz` 对 MySQL、Redis、MinIO、Qdrant、Dagster、OIDC 全部严格通过；
- 核心 tenant/project 的资产数、对象 HEAD（size + ETag）、知识索引 point 数与备份报告一致；
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
- MinIO 恢复保留内容版本、删除语义、大小和 ETag，但 provider 分配的新 version ID 不保证与源相同；应用权威引用使用 bucket/key/size/strong ETag，不以 provider version ID 作为不可替代事实。
- Qdrant snapshot 只支持兼容版本；任何 snapshot 都可以丢弃并从 MySQL/MinIO 重建。
- Redis RDB 不自动恢复。缓存预热时间应计入本环境 RTO。
