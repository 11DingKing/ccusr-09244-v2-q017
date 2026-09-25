# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集和分析接口。
- `app/services`：评分、统计、策略目录、时间窗口、保留策略与法律冻结领域逻辑/服务。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 保留策略与法律冻结

- **保留策略**：按场景（可叠加机型/技能限定）配置保留天数与优先级；同场景多条策略按优先级（其次保留期更长者）生效，期限从作业创建时间起算。
- **法律冻结**：可冻结单条作业或整个数据集版本（开启时快照版本成员，事后数据集改动不影响冻结范围）；支持多重/重叠冻结，争议调查中的记录一律暂停清理。
- **暂停计时**：冻结期间保留计时暂停，重叠冻结区间取并集、不重复计时；解除冻结后从**原到期点顺延**继续判断，而不是重新计算期限。
- **归档动作**：仅对“到期、无有效冻结、且不属于活动数据集版本（draft/pending_review/approved）”的记录归档；归档移除受限原始载荷（轨迹、感知、抓取、环境、硬件），保留统计摘要、各载荷 SHA256 指纹与审计引用（`archived_records` 表），并保留数据集成员关系，活动数据集成员不会被悄悄破坏。
- **批次执行**：归档以批次运行，相同 `batch_key` 幂等；逐项提交检查点，支持单项失败（批次标记 `completed_with_errors`，同键重试收敛）与进程崩溃后的重启续跑；支持 `dry_run` 预演。
- **可解释**：`GET /api/v1/operations/{id}/retention/explain` 返回适用策略、到期点、有效冻结、暂停时长与数据集保护原因，用于说明某条数据为何仍存在。

主要接口（`/api/v1` 前缀）：策略 `POST/GET /retention/policies`、`PUT /retention/policies/{rule_id}`；冻结 `POST /retention/holds`（`scope=operation|dataset_version`）、`POST /retention/holds/{hold_id}/release`、`GET /retention/holds`；归档 `POST /retention/archive-batches`（可带 `batch_key`、`dry_run`、`scene_id`）、`GET /retention/archive-batches[/{id}]`；状态 `GET /operations/{id}/retention`、`.../retention/explain`、`.../archive-record`。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。接口测试需要开发依赖，可执行 `pip install -r requirements-dev.txt`。
