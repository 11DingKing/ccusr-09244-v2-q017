from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------


class RetentionPolicyCreate(BaseModel):
    rule_id: str = Field(..., max_length=64, description="策略唯一标识")
    name: str = Field(..., max_length=200, description="策略名称")
    scene_id: Optional[int] = Field(None, description="适用场景ID，留空表示全局策略")
    skill_id: Optional[int] = Field(None, description="限定技能ID，留空不限定")
    robot_model_id: Optional[int] = Field(None, description="限定机型ID，留空不限定")
    keep_days: int = Field(..., gt=0, description="保留天数，从作业创建时间起算")
    priority: int = Field(0, ge=0, description="优先级，数值越大越优先")
    enabled: bool = True
    description: Optional[str] = None


class RetentionPolicyUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    keep_days: Optional[int] = Field(None, gt=0)
    priority: Optional[int] = Field(None, ge=0)
    enabled: Optional[bool] = None
    description: Optional[str] = None


class RetentionPolicyResponse(BaseModel):
    id: int
    rule_id: str
    name: str
    scene_id: Optional[int] = None
    skill_id: Optional[int] = None
    robot_model_id: Optional[int] = None
    keep_days: int
    priority: int
    enabled: bool
    description: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# 法律冻结
# ---------------------------------------------------------------------------


class HoldCreate(BaseModel):
    hold_id: Optional[str] = Field(None, max_length=64, description="冻结标识，留空自动生成；重复提交相同标识保持幂等")
    scope: str = Field("operation", description="冻结范围：operation / dataset_version")
    operation_data_id: Optional[int] = Field(None, description="单条作业冻结的作业ID")
    dataset_id: Optional[int] = Field(None, description="数据集版本冻结的数据集ID")
    dataset_version_id: Optional[int] = Field(None, description="数据集版本ID，留空取最新版本")
    reason: str = Field(..., min_length=1, description="冻结原因/争议案号说明")
    case_reference: Optional[str] = Field(None, max_length=200)
    requested_by: str = Field(..., max_length=100, description="申请人")


class HoldRelease(BaseModel):
    released_by: Optional[str] = Field(None, max_length=100)
    release_notes: Optional[str] = None


class HoldResponse(BaseModel):
    id: int
    hold_id: str
    scope: str
    operation_data_id: Optional[int] = None
    dataset_id: Optional[int] = None
    dataset_version_id: Optional[int] = None
    covered_operation_ids: List[int] = []
    reason: str
    case_reference: Optional[str] = None
    requested_by: str
    status: str
    opened_at: datetime
    closed_at: Optional[datetime] = None
    released_by: Optional[str] = None
    release_notes: Optional[str] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# 归档批次
# ---------------------------------------------------------------------------


class ArchiveBatchRequest(BaseModel):
    batch_key: Optional[str] = Field(None, max_length=100, description="批次键，相同键重复执行幂等")
    dry_run: bool = Field(False, description="仅预演到期对象，不移除载荷")
    scene_id: Optional[int] = Field(None, description="仅处理指定场景")
    trigger_by: Optional[str] = Field(None, max_length=100)


class BatchItemResponse(BaseModel):
    operation_data_id: int
    state: str
    detail: Optional[str] = None
    processed_at: datetime

    class Config:
        from_attributes = True


class BatchResponse(BaseModel):
    id: int
    batch_key: str
    status: str
    dry_run: bool
    total: int
    succeeded: int
    failed: int
    skipped_held: int
    skipped_dataset: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    trigger_by: Optional[str] = None

    class Config:
        from_attributes = True


class BatchDetailResponse(BatchResponse):
    items: List[BatchItemResponse] = []


# ---------------------------------------------------------------------------
# 状态与解释
# ---------------------------------------------------------------------------


class RetentionStateResponse(BaseModel):
    operation_id: int
    stored_state: str
    effective_state: str
    rule_id: Optional[str] = None
    reason: Optional[str] = None
    hold_ids: List[str] = []
    due_at: Optional[str] = None
    retention_paused_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class ArchivedRecordResponse(BaseModel):
    id: int
    operation_data_id: int
    archive_ref: str
    rule_id: Optional[str] = None
    batch_id: Optional[int] = None
    hold_ids: Optional[List[str]] = None
    payload_digests: Optional[List[Dict[str, Any]]] = None
    summary: Dict[str, Any]
    archived_at: datetime

    class Config:
        from_attributes = True
