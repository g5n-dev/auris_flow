# Auris Flow 系统架构

[返回项目首页](../../README.md#architecture) ·
[查看浅色原图](../assets/architecture-light.svg) ·
[查看深色原图](../assets/architecture-dark.svg)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../assets/architecture-dark.svg">
  <img src="../assets/architecture-light.svg" alt="Auris Flow 横向三层系统架构：产品体验、领域控制、数据与基础设施" width="100%">
</picture>

这张蓝图只表达稳定边界，不把底层组件名称当作产品语言。所有业务操作都沿
`tenant_id · project_id · trace_id` 贯穿三层。

<a id="boundaries"></a>

## 三层边界

| 层 | 负责什么 | 不能做什么 |
| --- | --- | --- |
| 产品体验 | React 工作台承载首页、数据资产、调听、标签、知识、评测、洞察、设置与发布 | 浏览器不得直连数据库、对象存储、Redis、Qdrant 或执行引擎 |
| 领域控制 | FastAPI BFF 统一认证授权、领域规则、幂等、审计、Trace 与异步调度 | Dagster 不能成为业务 API、产品画布或授权事实源 |
| 数据与基础设施 | MySQL 保存权威业务事实；对象存储保存权威音频和证据；Redis/Qdrant 提供可重建能力 | Redis/Qdrant 不得保存不可恢复的唯一业务事实 |

<a id="flows"></a>

## 关键链路

<details open>
<summary><strong>登录与业务请求</strong></summary>

1. 通用 OIDC IdP 完成 Authorization Code + PKCE。
2. BFF 建立不透明浏览器会话，并实时校验租户、项目与角色。
3. 工作台只调用 `/api/v1/*`；所有响应和审计继续携带 `trace_id`。

</details>

<details>
<summary><strong>音频播放与证据读取</strong></summary>

1. 工作台向 BFF 申请短期 playback grant。
2. 原生媒体元素携带 grant 发起 HTTP Range 请求。
3. BFF 重新校验会话、项目成员关系和对象版本，再代理权威对象存储的字节范围。

[查看 HTTP Range 契约中的“数据管理”章节](../backend-spec/api-contract.md#36-数据管理)

</details>

<details>
<summary><strong>异步任务与状态回写</strong></summary>

1. 领域写入和 Outbox 在同一 MySQL 事务中落盘。
2. Worker 通过租约、fencing、退避和死信机制调度任务。
3. Dagster 仅作为内部执行适配器；签名回调经 HMAC、时间窗、幂等和重放保护回写 BFF。

</details>

<details>
<summary><strong>派生索引与可观测性</strong></summary>

- Redis 和 Qdrant 可以从权威数据重建；短时不可用只让相关能力进入明确降级状态。
- API、Worker、执行适配器和回调共享业务 `trace_id` 与 OpenTelemetry trace/span。
- 遥测系统故障需要独立告警，但不应成为 `/readyz` 的业务强依赖。

</details>

## 图形维护

唯一拓扑源是 [`auris-flow-system.mmd`](auris-flow-system.mmd)。浅色与深色 SVG 由固定版本
Mermaid CLI 从同一源生成：

```bash
bash scripts/render_readme_architecture.sh
bash scripts/render_readme_architecture.sh --check
```

不要手工编辑生成的 SVG。校验会阻止脚本、`foreignObject`、外部链接、base64 内容、个人绝对路径、
主题拓扑漂移和退化为纵向长图。
