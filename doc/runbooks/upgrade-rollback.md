# 单机 Compose 升级与回滚 Runbook

## 适用范围

本 Runbook 只适用于 Auris Flow Linux 单机 Docker Compose 基线。它不是滚动、蓝绿或高可用部署；
升级需要计划停写窗口。当前仓库仍是 `v1.0.0` 候选，下面流程必须先在隔离 RC 环境演练，不能被
解释为已经完成正式升级认证。

版本/API/配置/数据库规则以
[版本与兼容策略](../release/versioning-and-compatibility.md)为准，数据保护以
[备份恢复 Runbook](backup-restore.md)为准。

## 变更单必须固定的对象

升级前记录且由第二位维护者复核：

- 当前与目标 release、source commit、每个镜像 digest、SBOM、签名和 checksum；
- 当前 Alembic head、目标 head、迁移是否仅 expand/migrate，是否包含 contract；
- `.env` 的非敏感 diff、需要轮换的 secret 名称，以及 OIDC/回调/embedding 兼容变化；
- 数据规模、预计迁移/回填时间、维护窗口、回滚决策人和最晚回滚点；
- 最近一次已验证 backup ID 与外部 secret/TLS 备份位置；
- 关键业务 smoke 的 tenant/project、预期 trace 和验收人，不记录客户内容或 secret。

发现 `latest`、可变 tag、无 digest、签名/SBOM/commit 不一致、未说明的破坏性迁移、未验证备份或
未完成安全公告时，停止升级。

## 升级前演练

1. 在新的 Compose project 与空卷上安装目标版本，执行 OIDC、核心业务、真实 Dagster、embedding、
   回调、metrics 和告警 smoke。
2. 从生产数据的脱敏副本或最近备份恢复到隔离环境；按真实数据量测量迁移、启动和校验时长。
3. 从当前版本升级到目标版本后验证，再把应用镜像切回当前版本，证明 expand schema 仍兼容。
4. 完成空环境恢复演练；若目标 release 含 contract，必须另行证明恢复旧 backup 到旧 release。
5. 把成功/失败、耗时、镜像 digest、migration head、backup ID 和关键 trace 记入变更单。

RC 演练不能以 SQLite、fake Dagster、确定性测试向量或本地 callback receipt 替代生产路径。

## 生产升级

以下示例从仓库根目录执行，并显式使用候选配置：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml config --quiet

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml ps
```

### 1. 进入停写窗口并备份

阻止新流量，等待进行中的任务到安全终态或显式取消，然后停止所有写端：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml \
  stop edge worker bff dagster-daemon dagster-webserver dagster-code keycloak
```

按 [备份恢复 Runbook](backup-restore.md)创建并离线验证 quiesced backup。备份脚本会拒绝写端仍在
运行的环境。只有外部副本验包成功后才能继续。

### 2. 固定并验证目标制品

将目标 release 的 `.env`/镜像锁定文件放到受控路径，复核所有引用为预期 digest，再按 release
提供的签名策略验证。不要在生产主机现场重写 digest，也不要在此步骤生成新镜像。

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml pull
```

如果 registry 返回的 digest 与审批记录不同，停止并隔离已拉取制品。

### 3. 执行 expand/migrate

保持 edge/BFF/Worker/Dagster/Keycloak 停止，只启动强依赖并运行一次目标 migration：

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml \
  up -d --wait mysql db-bootstrap redis minio minio-bootstrap qdrant

docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml run --rm migrate
```

确认 migration head、表/索引约束和回填账本与 release notes 一致。大数据回填必须由可恢复、有界批次
的 migrate 阶段执行；不在同步 DDL 中做无界全表业务转换。任何 contract 都应位于旧版本已退出
支持窗口后的单独 release，不应出现在普通可回滚升级里。

### 4. 启动并观察

```bash
docker compose \
  --project-directory production \
  --env-file production/.env \
  -f production/compose.yaml up -d --wait
```

先保持外部流量关闭，至少确认：

- `/healthz` 返回 200；`/readyz` 的 auth、database、redis、object_storage、qdrant、dagster 全部
  `ok`；
- OIDC 新登录、已有 session、用户禁用和权限变化符合预期；
- 一个合成业务操作贯穿 API、MySQL、Worker、真实 Dagster、Outbox 和签名 callback，业务
  `trace_id` 可与 OTel trace 关联；
- MinIO Range、语义 embedding/Qdrant 查询、死信/积压指标和 Grafana dashboard 正常；
- 没有 5xx/auth failure/callback failure 激增，数据库连接池、磁盘和 Outbox 延迟在基线内。

观察期达到变更单要求后再开放 edge 流量。发现数据语义不一致、跨租户可见性、无法追踪写入、
大量重试或安全边界退化时立即停止流量并进入回滚决策。

## 回滚决策树

### 仅应用/配置失败，schema 仍是兼容 expand 状态

1. 停止 edge、BFF、Worker、Dagster 和 Keycloak 写端。
2. 将镜像引用恢复为变更单记录的旧 digest，并恢复旧版兼容 `.env`；secret 值从 secret manager
   取回，不从日志/backup 中提取。
3. **不执行生产 Alembic downgrade。** 用旧镜像在扩展后的 schema 上启动。
4. 验证 `/readyz`、OIDC、核心 smoke、Outbox 和 callback，再开放流量。
5. 保留新 column/table，后续通过修复版继续 migrate 或在独立 contract release 清理。

### 数据回填已开始但仍可双读/双写

停止回填，记录最后 checkpoint，切回旧应用并保持扩展 schema。验证旧读路径未被切断、旧字段仍
完整，随后由维护者决定恢复回填还是发布修复版。禁止手工删新数据以“回到原样”。

### 已执行 contract、不可逆转换或权威数据被污染

原地回滚不安全。保持入口关闭，创建新的空 Compose project/卷，固定旧 release commit/digest，
按 [备份恢复 Runbook](backup-restore.md)恢复升级前 backup。不要清空原卷后重试；保留失败环境供
取证与一致性比较。恢复后重新生成 Qdrant 派生索引并完成业务校验。

### 安全事件触发回滚

先按 [安全事件响应 Runbook](security-incident-response.md)保全证据和吊销凭据，再决定回滚。
单纯切回镜像不会撤销泄漏的 session、OIDC key、callback key 或数据库凭据。

## 升级后收尾

- 保存变更单、backup ID、source commit、镜像 digest、migration head、SLO 截图/查询和 smoke
  trace；不得保存 secret 或客户原始音频。
- 在一个完整兼容窗口和经审批的 contract release 前，不删除旧镜像、旧 schema 或旧索引。
- 24 小时内执行并验包一次新版本 backup，确认 `AurisBackupStale` 已恢复。
- 更新 CHANGELOG、已知限制和实测 RTO；失败升级必须写无责 postmortem。

## 禁止事项

- 禁止 `docker compose down --volumes`、清空数据库或删除对象作为普通回滚。
- 禁止把本地源码 build、`latest` 或未签名镜像临时顶替正式 digest。
- 禁止在没有停写、backup 和兼容证明时执行 destructive DDL。
- 禁止因 `/healthz` 正常就开放流量；必须以严格 `/readyz` 和业务 smoke 为准。
