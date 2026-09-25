"""保留策略、法律冻结与归档批次的接口模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

PolicyScope = Literal["scene", "global"]
HoldTargetKind = Literal["operation", "dataset_version", "dataset"]
BatchItemStatus = Literal["pending", "succeeded", "failed", "skipped"]


class RetentionPolicyCreate(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$", description="策略标识")
    name: str = Field(..., min_length=1, max_length=200, description="策略名称")
    scope: PolicyScope = Field("scene", description="scene=场景专属，global=全局兜底")
    scene_id: Optional[int] = Field(None, description="场景ID；scope=scene 时必填")
    keep_days: int = Field(..., gt=0, description="保留天数，按创建时间计算")
    priority: int = Field(0, ge=0, description="优先级，场景与全局策略同时命中时取值高者")
    enabled: bool = Field(True, description="是否启用")
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
    scope: str
    scene_id: Optional[int] = None
    scene_name: Optional[str] = None
    keep_days: int
    priority: int
    enabled: bool
    description: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class HoldTargetSpec(BaseModel):
    kind: HoldTargetKind = Field(..., description="operation=单条作业，dataset_version=数据集版本，dataset=整个数据集的全部版本")
    id: int = Field(..., description="对应作业/版本/数据集的ID")


class HoldCreate(BaseModel):
    hold_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-.]+$", description="冻结案号，唯一")
    reason: str = Field(..., min_length=1, description="冻结原因/争议说明")
    requested_by: str = Field(..., min_length=1, max_length=100, description="申请人/案件负责人")
    case_reference: Optional[str] = Field(None, max_length=200, description="争议案件编号")
    targets: List[HoldTargetSpec] = Field(..., min_length=1, description="冻结对象列表")


class HoldRelease(BaseModel):
    released_by: str = Field(..., min_length=1, max_length=100)
    note: Optional[str] = None


class HoldTargetResponse(BaseModel):
    id: int
    subject_type: str
    subject_ref: str
    operation_id: Optional[int] = None
    dataset_id: Optional[int] = None
    dataset_version_id: Optional[int] = None
    version_number: Optional[int] = None

    class Config:
        from_attributes = True


class HoldResponse(BaseModel):
    id: int
    hold_id: str
    reason: str
    requested_by: str
    case_reference: Optional[str] = None
    status: str
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    released_by: Optional[str] = None
    release_note: Optional[str] = None
    targets: List[HoldTargetResponse] = []

    class Config:
        from_attributes = True


class EligibleItem(BaseModel):
    operation_id: int
    scene_id: int
    rule_id: str
    policy_level: str
    created_at: datetime
    due_at: datetime


class ArchiveBatchCreate(BaseModel):
    idempotency_key: Optional[str] = Field(None, max_length=128, description="幂等键，重复提交返回同一批次")
    scene_id: Optional[int] = Field(None, description="只处理指定场景")
    limit: Optional[int] = Field(None, ge=1, le=5000, description="本次最多处理条数")
    triggered_by: Optional[str] = Field(None, max_length=100)
    dry_run: bool = Field(False, description="只预览候选，不执行归档")


class BatchItemResponse(BaseModel):
    id: int
    subject_type: str
    operation_id: Optional[int] = None
    status: str
    rule_id: Optional[str] = None
    archive_ref: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    processed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ArchiveBatchResponse(BaseModel):
    batch_id: str
    idempotency_key: Optional[str] = None
    status: str
    total: int
    succeeded: int
    failed: int
    skipped: int
    triggered_by: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    items: List[BatchItemResponse] = []
    resumed: bool = False


class ArchivePreviewResponse(BaseModel):
    dry_run: bool
    scanned: int
    eligible: int
    held: int
    active_member_protected: int
    not_due: int
    no_policy: int
    candidates: List[EligibleItem] = []


class ExplainResponse(BaseModel):
    operation_id: int
    retention_state: str
    present: bool
    why_present: str
    present_reason: str = "unknown"
    rule_id: Optional[str] = None
    policy_level: str = "none"
    keep_days: Optional[int] = None
    created_at: Optional[datetime] = None
    due_at: Optional[datetime] = None
    overdue: bool = False
    payload_cleared: bool
    active_holds: List[Dict[str, Any]] = []
    dataset_memberships: List[Dict[str, Any]] = []
    archive: Optional[Dict[str, Any]] = None


class ArchiveRecordResponse(BaseModel):
    id: int
    subject_type: str
    operation_id: Optional[int] = None
    archive_ref: str
    rule_id: str
    batch_id: Optional[str] = None
    action: str
    archived_at: datetime
    summary: Dict[str, Any]
    payload_audit_ref: Optional[str] = None
    removed_fields: List[str] = []

    class Config:
        from_attributes = True
