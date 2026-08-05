# 平台音频导入真实栈验收门

`scripts/verify_audio_import_stack.sh` 验证平台音频导入的真实纵向链，不把
generic Dagster ACK、本地对象存储或脚本拼装结果当成成功。

## 覆盖范围

验收门在独立 Docker Compose 项目内启动：

- 带临时 CA 和主机名证书的 HTTPS 平台 URL API，提供 3 条可分页录音清单和
  3 个确定性 WAV；
- MySQL、Redis、启用对象版本的 MinIO；
- 固定白名单 job `auris_flow_audio_import_v1` 的真实 Dagster code server、
  webserver 和 daemon；
- BFF、outbox worker，以及只通过 BFF 业务 API 操作的验收客户端。

客户端依次完成连接器创建、真实连通性测试、3 条预览、TaskVersion 发布、
`production` TaskRun 创建与幂等重放、ImportBatch 回读、会话列表/详情回读、
播放授权，以及全量和 Range 音频播放。

同一验收门随后启动生产模式 Vite，由 Playwright 通过真实页面点击再完成一轮
独立数据集：

- 数据资产 → 音频数据 → 连接器导入；
- 选择平台连接、填写平台范围与 URL API；
- 测试连接、预览 3 条源记录、配置字段映射；
- 保存草稿、发布、立即拉取；
- 等待真实 ImportBatch 物化，刷新页面并从 BFF 恢复状态；
- 进入新会话并由 `<audio>` 发起带 Range 的播放；
- 在租户页“拉取一次”复用同一个已发布 TaskVersion，且不调用旧
  `/platform-sync-jobs`。

API 验收使用 `/v1/recordings`，浏览器验收使用
`/v1/browser-recordings`；两组 external record ID 和 WAV 内容互相独立，避免
第二轮被第一轮去重命中后产生假阳性。

最终断言还会从数据库核对：

- Dagster run 为固定 job 且状态为 `SUCCESS`；
- 完成回执经过 `hmac-sha256` 签名认证并已物化；
- MinIO 中存在 3 个 WAV 和 1 个不可变 manifest，均具有非空精确
  `version_id`、ETag 和 SHA-256；
- 批次 3 条成功、0 条失败，且每条音频对象版本与会话、`root_trace_id`
  完整绑定；
- BFF 播放字节与平台源 WAV 完全一致。

## 运行

前提是 Docker Engine 可用，且仓库依赖镜像能够构建：

```bash
bash scripts/verify_audio_import_stack.sh
```

脚本使用唯一且可校验的 Compose project name。无论成功、失败还是中断，退出
trap 都只对该项目执行 `down --volumes --remove-orphans`，不清理其他 Compose
项目。

以下环境变量只用于调整超时：

- `AURIS_AUDIO_IMPORT_GATE_BUILD_TIMEOUT`
- `AURIS_AUDIO_IMPORT_GATE_WAIT_TIMEOUT`
- `AURIS_AUDIO_IMPORT_GATE_RUN_TIMEOUT`
- `AURIS_AUDIO_IMPORT_GATE_BROWSER_TIMEOUT`
- `AURIS_AUDIO_IMPORT_GATE_CLEANUP_TIMEOUT`

`AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE=1` 会被明确拒绝。

## CI 与发布门禁

- 修改音频导入、Dagster、对象存储或播放链相关路径的 Pull Request，会触发
  `.github/workflows/audio-import-real-stack.yml`；
- 该 workflow 每日夜间执行一次，并在 GitHub Release 发布时执行；
- `scripts/verify_release.sh` 也会在生成供应链清单前直接执行本验收门；
- 普通 `scripts/verify_all.sh` 不启动该重栈，开发者仍可运行快速验证；
- CI、nightly 和 release 都不得设置
  `AURIS_SKIP_AUDIO_IMPORT_REAL_STACK_GATE=1`，成功证据必须绑定当前 commit。

## 证据

成功后写入：

`build/release-evidence/audio-import-real-stack-gate.json`

以及浏览器交互证据：

`build/release-evidence/audio-import-browser-e2e.json`

证据 schema 为 `auris.audio-import-real-stack-gate.v1`，包含连接器、TaskVersion、
TaskRun、`completion_pending` 状态历史、异步 materialization outbox、Dagster run、
签名回执、ImportBatch、MinIO 精确对象版本和播放回读摘要。
浏览器证据 schema 为 `auris.audio-import-browser-e2e.v1`，包含 UI 创建的连接器、
不可变 TaskVersion、production TaskRun、ImportBatch、新会话、刷新恢复、
trace、201 播放授权、206 Range 播放和租户页复用结果。证据不包含平台凭证、
播放 grant 或下载 URL。

如果运行时工作树有未提交改动，证据会写入 `source_tree_dirty: true`，终端也会
明确警告；这种产物可用于本地联调验收，但不能冒充绑定干净 commit 的发布证据。
