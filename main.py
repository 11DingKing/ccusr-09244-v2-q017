from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.config import settings
from app.database import engine, Base
from app.routers import common, operation, dataset, analytics, retention


# 已存在的 SQLite 库不会因 create_all 自动获得新列，这里做幂等补齐。
_OPERATION_COLUMN_DEFAULTS = {
    "retention_state": "TEXT NOT NULL DEFAULT 'active'",
    "retention_rule_id": "TEXT",
    "retention_due_at": "DATETIME",
    "retention_paused_at": "DATETIME",
    "archived_at": "DATETIME",
}


def ensure_operation_columns():
    inspector = inspect(engine)
    if "operation_data" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("operation_data")}
    with engine.begin() as conn:
        for name, ddl_type in _OPERATION_COLUMN_DEFAULTS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE operation_data ADD COLUMN {name} {ddl_type}"))


def create_tables():
    Base.metadata.create_all(bind=engine)
    ensure_operation_columns()


create_tables()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="""
# 机器人真机作业数据回流后端服务

## 功能模块：

### 基础资源管理
- **机型管理**：管理机器人型号信息
- **场景管理**：生产制造、餐饮零售等作业场景
- **技能管理**：抓取、装配、焊接等作业技能

### 作业数据采集
- 按机型、场景、作业技能归类入库
- 动作轨迹、感知记录、抓取成败数据采集

### 人工标注
- 成功/失败标注
- 失败分类（感知异常、运动控制异常、环境干扰等）
- 标注审核流程

### 数据集管理
- 数据集打包与发布
- 优质数据集公开供其他团队复用
- 复用次数统计（关联具体版本）

### 数据集审核
- 提交审核 → 审核（通过/驳回） → 发布
- 审核状态：draft / pending_review / approved / rejected
- 支持撤回已提交的审核

### 数据集版本
- 每次修改自动创建新版本快照
- 版本历史可追溯、可查询
- 复用方明确使用的版本

### 数据集订阅
- 团队可订阅感兴趣的数据集
- 数据集发布新版本时通知订阅方

### 数据质量分级
- 按完整度和标注质量自动评分分级（A/B/C/D）

### 统计分析
- 按机型、场景统计数据量
- 标注完成率、复用率
- 失败原因分析
- 按审核状态统计（待审/已发布等）

### 保留策略与法律冻结
- 按场景（可叠加机型/技能）配置保留期限与优先级
- 法律冻结可覆盖单条作业或整个数据集版本，支持重叠冻结
- 冻结暂停计时，解除后从原到期点继续判断
- 归档移除受限载荷但保留统计摘要、指纹与审计引用
- 活动数据集成员受保护，批次幂等、可部分失败重试与重启恢复
    """,
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_prefix = settings.API_V1_PREFIX

app.include_router(common.router, prefix=api_prefix)
app.include_router(operation.router, prefix=api_prefix)
app.include_router(dataset.router, prefix=api_prefix)
app.include_router(analytics.router, prefix=api_prefix)
app.include_router(retention.router, prefix=api_prefix)


@app.get("/", tags=["首页"])
def root():
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
        "api_prefix": api_prefix,
        "message": "机器人真机作业数据回流服务已启动"
    }


@app.get("/health", tags=["首页"])
def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
