# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集、分析与保留策略接口。
- `app/services`：评分、统计、策略目录、时间窗口，以及保留/冻结/归档（`retention.py` 为纯领域判定，`retention_service.py` 为数据库编排）。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 保留策略与法律冻结

隐私/法务保留能力在 `/api/v1/retention` 下提供：

- **保留策略**：按场景配置保留天数与优先级；`scope=global` 的策略兜底所有场景，场景策略与全局策略同时命中时取优先级高者（平局保留更久者，再平局场景优先）。候选按“场景 + 创建时间”选中。
- **法律冻结**：`POST /retention/holds` 可冻结单条作业（`operation`）、整个数据集版本（`dataset_version`）或整个数据集（`dataset`，覆盖当前成员）。多条冻结允许重叠，只有全部解除后对象才恢复清理判定。
- **解除冻结**：解除不重新计算期限，仍以“创建时间 + 保留期限”的原始到期点判断；已逾期的记录在下次批次立即归档。
- **归档动作**：仅移除受限原始载荷（轨迹、感知、抓取结果、环境与硬件状态），保留统计摘要、标注结论和载荷 `sha256` 审计引用（`archive_records`），并写入审计事件。
- **活动数据集保护**：已发布数据集的成员不会被归档清除载荷，避免活动版本被悄悄破坏。
- **批次语义**：`POST /retention/archive-batches` 支持 `idempotency_key`，重复提交返回同一批次；逐项使用保存点，允许部分失败并可用同一键重试剩余项；批次与待处理项先落库，进程重启后凭幂等键续跑。
- **存在性解释**：`GET /retention/operations/{id}/explain` 说明一条数据为何仍存在（冻结 / 活动成员 / 未到期 / 无策略 / 已归档）及其到期点。
- **预览与审计**：`POST /retention/archive-preview` 只返回候选与分区计数；`GET /retention/audit` 查询全流程审计事件。

> 说明：数据集版本不单独保存历史成员快照，因此“版本冻结”按该数据集当前成员展开覆盖。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
