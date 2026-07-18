# 版本与兼容策略

## 当前状态

Auris Flow 尚未发布正式版本。当前树是 `v1.0.0` 候选实现；在项目所有者完成许可权利主体签字、
`v1.0.0-rc.1` 真实发布演练、外部干净安装和所有 release gate 前，不存在受支持的 `v1.0.0`，
也不存在从旧正式版本升级的既成承诺。

## SemVer 与发行通道

正式版本采用 SemVer：

- `MAJOR`：公开 API、数据库/数据语义或生产配置出现不能由兼容窗口吸收的破坏性变化。
- `MINOR`：向后兼容的新 API、新资源或可选配置；允许先扩展 schema，不允许提前删除旧读写路径。
- `PATCH`：保持 API、配置与数据语义兼容的修复。
- `vX.Y.Z-rc.N`：候选版本，只用于安装、升级、回滚、安全与恢复验收，不承诺生产支持。

release 的源码、镜像 digest、SBOM、签名、checksum、`NOTICE`、迁移说明和 release notes 必须来自
同一 commit。生产安装引用 digest；可读 tag 只是辅助标识，禁止 `latest`。

## HTTP API 兼容

- 业务接口保持 `/api/v1/*`、复数资源和 kebab-case；错误响应保持稳定 error code、request ID 与
  trace ID，不暴露内部异常。
- `v1` 内可以增加可选请求字段、响应字段、资源和枚举能力，但客户端必须容忍未知响应字段。
- 不能在 `v1` 内改变既有字段类型/语义、把可选字段改成必填、复用旧 error code 表示新语义，或
  删除已有成功/失败状态。
- 弃用必须先在 CHANGELOG、OpenAPI 和迁移说明中公告；至少保留一个完整 MINOR 版本且不少于
  90 天。实现支持时同时返回标准 `Deprecation`、`Sunset` 和文档 `Link` 响应头。
- 安全漏洞可缩短窗口，但 release notes 必须说明威胁、替代路径、受影响版本和紧急时间线。
- 前端只能调用 BFF；MySQL、Redis、MinIO、Qdrant 与 Dagster 从来不是公共产品接口。

## 数据库迁移兼容窗口

从 `v1.0.0` 起，每个正式版本至少支持从最近一个正式版本（`N-1`）前向升级。跨多个版本必须逐
版本升级并在每一步运行对应验证；备份不能替代迁移测试。

破坏性 schema 变化采用 expand/migrate/contract：

1. **Expand**：先新增 nullable column/table/index 或双写能力；旧应用仍可运行。
2. **Migrate**：以有界批次回填并记录进度、tenant/project、幂等和 trace；验证新旧表示一致。
3. **Switch**：新应用切读新结构，保留旧结构和必要双写至少一个正式兼容窗口。
4. **Contract**：只有旧版本已超出支持窗口、备份与恢复演练通过后，才在后续版本删除旧结构。

同一 release 不得同时引入新结构并删除旧结构。迁移脚本必须可重复执行、对失败可诊断，并在
生产规模副本上测量锁等待和耗时。应用回滚优先依赖 expand 兼容 schema；不要把 Alembic
`downgrade` 当作生产数据回滚机制。若 contract 或不可逆数据转换已经发生，只能恢复到新的空
环境并按备份 manifest 固定旧 commit/digest。

`v1.0.0` 是首个目标正式版本，因此它没有 `N-1` 正式升级来源；RC 之间也不能被宣传为正式
兼容承诺，但仍须演练升级和恢复。

## 配置兼容

- MINOR/PATCH 可以增加有安全默认值的可选配置，不能静默改变现有默认值的业务/安全语义。
- 新的生产必填配置必须先在一个 MINOR 版本支持旧名/新名并发出显式弃用告警；删除旧名属于下一
  MAJOR，安全紧急变更除外。
- secret 值只通过 Docker secret file 或外部 secret reference 注入；release notes 只列 secret
  名称与轮换要求，绝不记录值。
- 配置解析在启动时 fail closed。未知/弱 demo credential、通配 CORS/TrustedHost、local/fake
  adapter 或缺失强依赖不得因兼容需要而降级放行。
- `production/.env.example` 是字段模板，不是可直接运行的生产配置；正式 release 的镜像锁定
  文件优先于示例中的 `:dev`。

## 数据与执行引擎边界

MySQL 是权威业务事实；MinIO 是权威对象内容。Redis 可以丢弃，Qdrant 必须可由 MySQL/MinIO
重建。Dagster schema 和运行元数据不替代 Auris Flow 的领域状态。升级 Qdrant、MinIO、MySQL、
Redis、Keycloak、Dagster、OTel 或 embedding 模型时，必须单独记录上游版本兼容、备份格式、
向量维度和索引重建要求。

embedding 模型或维度改变视为数据迁移：创建新 collection/index、后台重建、按质量门禁切换，
确认可回退后才删除旧索引；禁止原地混写不同模型或维度。

## 版本支持与安全修复

在 `v1.0.0` 发布前只支持当前开发主线，不提供生产 SLA。正式发布后，维护者必须在每个 release
notes 中声明支持窗口；没有明确声明时，不应推定无限期支持。高危安全问题按
[SECURITY.md](../../SECURITY.md) 私密处理，修复版本可能要求立即升级和密钥轮换。

## 发布说明最低内容

每个 RC/正式 release 必须列出：

- source commit、镜像 tag/digest、SBOM/checksum/signature 的对应关系；
- 新增、变更、弃用与删除的 API/config；
- migration head、支持的起始版本、预计停写/回填时长和 rollback boundary；
- 上游服务、embedding 模型/维度和数据重建影响；
- 已执行的干净安装、升级、回滚、恢复、安全与告警演练；
- 已知限制、未豁免高危问题和所有经过审批的例外。
