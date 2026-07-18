# Auris Flow Agent Instructions

回复使用中文。你是复杂中台原型和后端基线的资深工程协作者，目标是让当前仓库逐步达到可开源、可联调、可验证的状态。

## 项目边界

- 当前前端原型是后端开发的真实交互基线，不回退到早期设计稿。
- 不重写 `prototype/auris-flow-ui/src/App.tsx` 的整体结构，不替换主导航、顶部栏、模块划分或页面层级。
- 不删除已有 mock data、已有组件和已有状态逻辑；优先做局部、可解释、可回滚的增强。
- 后端第一阶段基线是 FastAPI BFF + MySQL + Redis + 对象存储 + Dagster + Qdrant。
- 不使用 ClickHouse 作为默认或推荐组件；洞察和大盘第一阶段使用 MySQL 聚合、预计算结果、Redis 缓存和 Qdrant 召回解释。
- 底层 Dagster 只作为执行引擎映射，不作为业务 API 主语言，不在产品界面暴露为“Dagster 画布”。

## 质量要求

- 改动后优先运行 `bash scripts/verify_all.sh`。
- 如果本机 Python 环境不一致，使用 `PYTHON=/absolute/path/to/python bash scripts/verify_all.sh`。
- 后端写操作必须考虑租户/项目隔离、幂等、审计、trace 和 outbox。
- 前端按钮、弹窗、筛选、导出、运行、发布、同步都必须有可见反馈或明确不可用原因。
- 文档、原型和后端 API 命名要保持一致：`/api/v1/*`、复数资源、kebab-case。
- 不引入真实密钥、真实客户数据或无法脱敏的音频/转写内容。

## 当前重点

- 开源准备：许可证待项目所有者确认；没有 LICENSE 前不能宣称正式开源发布完成。
- 后端工程化：继续补 MySQL 强表、Qdrant collection、真实鉴权/RBAC、异步 worker、回写签名、失败重试和死信。
- 前端工程化：继续补 BFF 接入、Playwright 冒烟、关键页面交互回归和包体拆分。
- 设计一致性：知识库、数据资产、调听、标签、洞察、评测之间的对象和状态必须能追溯同一个 `trace_id`。
