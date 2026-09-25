"""保留策略、法律冻结与归档批次接口。"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    ArchivedRecord,
    LegalHold,
    OperationData,
    RetentionBatch,
)
from app.schemas.retention import (
    ArchiveBatchRequest,
    ArchivedRecordResponse,
    BatchDetailResponse,
    BatchResponse,
    HoldCreate,
    HoldRelease,
    HoldResponse,
    RetentionPolicyCreate,
    RetentionPolicyResponse,
    RetentionPolicyUpdate,
    RetentionStateResponse,
)
from app.services import retention_service as service
from app.services.retention import RetentionError

router = APIRouter()


def _raise_retention_error(exc: RetentionError) -> None:
    raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------


@router.post("/retention/policies", response_model=RetentionPolicyResponse, tags=["保留策略"])
def create_retention_policy(data: RetentionPolicyCreate, db: Session = Depends(get_db)):
    try:
        return service.create_policy(db, data.model_dump())
    except RetentionError as exc:
        _raise_retention_error(exc)


@router.get("/retention/policies", response_model=List[RetentionPolicyResponse], tags=["保留策略"])
def list_retention_policies(
    enabled_only: bool = Query(False, description="仅返回启用策略"),
    db: Session = Depends(get_db),
):
    return service.list_policies(db, enabled_only=enabled_only)


@router.put("/retention/policies/{rule_id}", response_model=RetentionPolicyResponse, tags=["保留策略"])
def update_retention_policy(rule_id: str, data: RetentionPolicyUpdate, db: Session = Depends(get_db)):
    try:
        return service.update_policy(db, rule_id, data.model_dump(exclude_unset=True))
    except RetentionError as exc:
        _raise_retention_error(exc)


# ---------------------------------------------------------------------------
# 法律冻结
# ---------------------------------------------------------------------------


@router.post("/retention/holds", response_model=HoldResponse, tags=["法律冻结"])
def open_legal_hold(data: HoldCreate, db: Session = Depends(get_db)):
    try:
        return service.open_hold(db, data.model_dump(exclude_unset=True))
    except RetentionError as exc:
        _raise_retention_error(exc)


@router.post("/retention/holds/{hold_id}/release", response_model=HoldResponse, tags=["法律冻结"])
def release_legal_hold(hold_id: str, data: HoldRelease, db: Session = Depends(get_db)):
    try:
        return service.release_hold(db, hold_id, data.model_dump(exclude_unset=True))
    except RetentionError as exc:
        _raise_retention_error(exc)


@router.get("/retention/holds", response_model=List[HoldResponse], tags=["法律冻结"])
def list_legal_holds(
    status: Optional[str] = Query(None, description="active / released"),
    scope: Optional[str] = Query(None, description="operation / dataset_version"),
    db: Session = Depends(get_db),
):
    holds = service.list_holds(db, status=status)
    if scope:
        holds = [hold for hold in holds if hold.scope == scope]
    return holds


@router.get("/retention/holds/{hold_id}", response_model=HoldResponse, tags=["法律冻结"])
def get_legal_hold(hold_id: str, db: Session = Depends(get_db)):
    hold = db.query(LegalHold).filter(LegalHold.hold_id == hold_id).first()
    if hold is None:
        raise HTTPException(status_code=404, detail="冻结不存在")
    return hold


# ---------------------------------------------------------------------------
# 归档批次
# ---------------------------------------------------------------------------


@router.post("/retention/archive-batches", response_model=BatchResponse, tags=["归档批次"])
def run_archive_batch(data: ArchiveBatchRequest, db: Session = Depends(get_db)):
    try:
        batch = service.run_archive_batch(
            db,
            batch_key=data.batch_key,
            dry_run=data.dry_run,
            scene_id=data.scene_id,
            trigger_by=data.trigger_by,
        )
    except RetentionError as exc:
        _raise_retention_error(exc)
    return batch


@router.get("/retention/archive-batches", response_model=List[BatchResponse], tags=["归档批次"])
def list_archive_batches(db: Session = Depends(get_db)):
    return (
        db.query(RetentionBatch)
        .order_by(RetentionBatch.started_at.desc(), RetentionBatch.id.desc())
        .all()
    )


@router.get("/retention/archive-batches/{batch_id}", response_model=BatchDetailResponse, tags=["归档批次"])
def get_archive_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(RetentionBatch).filter(RetentionBatch.id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


# ---------------------------------------------------------------------------
# 单条记录的保留状态与归档审计
# ---------------------------------------------------------------------------


@router.get("/operations/{operation_id}/retention", response_model=RetentionStateResponse, tags=["保留策略"])
def get_operation_retention_state(operation_id: int, db: Session = Depends(get_db)):
    operation = db.query(OperationData).filter(OperationData.id == operation_id).first()
    if operation is None:
        raise HTTPException(status_code=404, detail="作业数据不存在")
    try:
        projection = service.projected_state(db, operation)
    except RetentionError as exc:
        _raise_retention_error(exc)
    return RetentionStateResponse(
        operation_id=operation.id,
        stored_state=operation.retention_state,
        effective_state=projection["state"],
        rule_id=projection.get("rule_id"),
        reason=projection.get("reason"),
        hold_ids=projection.get("hold_ids", []),
        due_at=projection.get("due_at"),
        retention_paused_at=operation.retention_paused_at,
        archived_at=operation.archived_at,
    )


@router.get("/operations/{operation_id}/retention/explain", tags=["保留策略"])
def explain_operation_retention(operation_id: int, db: Session = Depends(get_db)):
    """解释某条作业为何仍存在：适用策略、冻结区间、暂停时长、到期点、数据集保护。"""
    try:
        return service.explain_operation(db, operation_id)
    except RetentionError as exc:
        _raise_retention_error(exc)


@router.get("/operations/{operation_id}/archive-record", response_model=ArchivedRecordResponse, tags=["保留策略"])
def get_archived_record(operation_id: int, db: Session = Depends(get_db)):
    record = (
        db.query(ArchivedRecord)
        .filter(ArchivedRecord.operation_data_id == operation_id)
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="该作业没有归档记录")
    return record
