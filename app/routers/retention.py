"""保留策略、法律冻结与归档批次接口。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    ArchiveRecord,
    LegalHold,
    RetentionAuditEvent,
    RetentionBatch,
    RetentionPolicy,
    Scene,
)
from app.schemas.retention import (
    ArchiveBatchCreate,
    ArchiveBatchResponse,
    ArchivePreviewResponse,
    ArchiveRecordResponse,
    BatchItemResponse,
    EligibleItem,
    ExplainResponse,
    HoldCreate,
    HoldRelease,
    HoldResponse,
    RetentionPolicyCreate,
    RetentionPolicyResponse,
    RetentionPolicyUpdate,
)
from app.services import retention_service as service
from app.services.retention import RetentionError

router = APIRouter()


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------- #
# 保留策略
# --------------------------------------------------------------------------- #

def _policy_to_response(db: Session, policy: RetentionPolicy) -> RetentionPolicyResponse:
    scene_name = None
    if policy.scene_id is not None:
        scene = db.query(Scene).filter(Scene.id == policy.scene_id).first()
        scene_name = scene.name if scene else None
    return RetentionPolicyResponse(
        id=policy.id,
        rule_id=policy.rule_id,
        name=policy.name,
        scope=policy.scope,
        scene_id=policy.scene_id,
        scene_name=scene_name,
        keep_days=policy.keep_days,
        priority=policy.priority,
        enabled=policy.enabled,
        description=policy.description,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


@router.get("/retention/policies", response_model=List[RetentionPolicyResponse], tags=["保留策略"])
def list_policies(
    scene_id: Optional[int] = Query(None, description="按场景过滤"),
    scope: Optional[str] = Query(None, description="scene/global"),
    enabled: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(RetentionPolicy)
    if scene_id is not None:
        query = query.filter(RetentionPolicy.scene_id == scene_id)
    if scope is not None:
        query = query.filter(RetentionPolicy.scope == scope)
    if enabled is not None:
        query = query.filter(RetentionPolicy.enabled == enabled)
    policies = query.order_by(RetentionPolicy.priority.desc(), RetentionPolicy.id.asc()).all()
    return [_policy_to_response(db, policy) for policy in policies]


@router.post("/retention/policies", response_model=RetentionPolicyResponse, tags=["保留策略"])
def create_policy(payload: RetentionPolicyCreate, db: Session = Depends(get_db)):
    try:
        policy = service.create_policy(db, payload)
    except RetentionError as exc:
        raise _bad(exc)
    return _policy_to_response(db, policy)


@router.get("/retention/policies/{rule_id}", response_model=RetentionPolicyResponse, tags=["保留策略"])
def get_policy(rule_id: str, db: Session = Depends(get_db)):
    policy = db.query(RetentionPolicy).filter(RetentionPolicy.rule_id == rule_id).first()
    if policy is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    return _policy_to_response(db, policy)


@router.patch("/retention/policies/{rule_id}", response_model=RetentionPolicyResponse, tags=["保留策略"])
def update_policy(rule_id: str, payload: RetentionPolicyUpdate, db: Session = Depends(get_db)):
    try:
        policy = service.update_policy(db, rule_id, payload)
    except RetentionError as exc:
        raise _bad(exc)
    return _policy_to_response(db, policy)


# --------------------------------------------------------------------------- #
# 法律冻结
# --------------------------------------------------------------------------- #

def _hold_to_response(hold: LegalHold) -> HoldResponse:
    targets = []
    for target in hold.targets:
        targets.append(
            {
                "id": target.id,
                "subject_type": target.subject_type,
                "subject_ref": target.subject_ref,
                "operation_id": target.operation_id,
                "dataset_id": target.dataset_id,
                "dataset_version_id": target.dataset_version_id,
            }
        )
    return HoldResponse(
        id=hold.id,
        hold_id=hold.hold_id,
        reason=hold.reason,
        requested_by=hold.requested_by,
        case_reference=hold.case_reference,
        status=hold.status,
        opened_at=hold.opened_at,
        closed_at=hold.closed_at,
        released_by=hold.released_by,
        release_note=hold.release_note,
        targets=targets,
    )


@router.post("/retention/holds", response_model=HoldResponse, tags=["法律冻结"])
def create_hold(payload: HoldCreate, db: Session = Depends(get_db)):
    try:
        hold = service.create_hold(db, payload)
    except RetentionError as exc:
        db.rollback()
        raise _bad(exc)
    return _hold_to_response(hold)


@router.get("/retention/holds", response_model=List[HoldResponse], tags=["法律冻结"])
def list_holds(
    status: Optional[str] = Query(None, description="active/released"),
    operation_id: Optional[int] = Query(None, description="覆盖该作业的冻结（含数据集扩展）"),
    db: Session = Depends(get_db),
):
    if operation_id is not None:
        hold_rows = service.active_hold_map(db).get(operation_id, [])
        hold_ids = {row["hold_id"] for row in hold_rows}
        if not hold_ids:
            return []
        holds = db.query(LegalHold).filter(LegalHold.hold_id.in_(hold_ids)).all()
        return [_hold_to_response(hold) for hold in holds]

    query = db.query(LegalHold)
    if status is not None:
        query = query.filter(LegalHold.status == status)
    holds = query.order_by(LegalHold.opened_at.desc()).all()
    return [_hold_to_response(hold) for hold in holds]


@router.get("/retention/holds/{hold_id}", response_model=HoldResponse, tags=["法律冻结"])
def get_hold(hold_id: str, db: Session = Depends(get_db)):
    hold = db.query(LegalHold).filter(LegalHold.hold_id == hold_id).first()
    if hold is None:
        raise HTTPException(status_code=404, detail="冻结不存在")
    return _hold_to_response(hold)


@router.post("/retention/holds/{hold_id}/release", response_model=HoldResponse, tags=["法律冻结"])
def release_hold(hold_id: str, payload: HoldRelease, db: Session = Depends(get_db)):
    try:
        hold = service.release_hold(db, hold_id, payload)
    except RetentionError as exc:
        raise _bad(exc)
    return _hold_to_response(hold)


# --------------------------------------------------------------------------- #
# 归档批次
# --------------------------------------------------------------------------- #

def _batch_to_response(batch: RetentionBatch, resumed: bool = False) -> ArchiveBatchResponse:
    items = [
        BatchItemResponse(
            id=item.id,
            subject_type=item.subject_type,
            operation_id=item.operation_id,
            status=item.status,
            rule_id=item.rule_id,
            archive_ref=item.archive_ref,
            error=item.error,
            attempts=item.attempts,
            processed_at=item.processed_at,
        )
        for item in sorted(batch.items, key=lambda value: value.id)
    ]
    return ArchiveBatchResponse(
        batch_id=batch.batch_id,
        idempotency_key=batch.idempotency_key,
        status=batch.status,
        total=batch.total,
        succeeded=batch.succeeded,
        failed=batch.failed,
        skipped=batch.skipped,
        triggered_by=batch.triggered_by,
        started_at=batch.started_at,
        finished_at=batch.finished_at,
        items=items,
        resumed=resumed,
    )


@router.post("/retention/archive-batches", response_model=ArchiveBatchResponse, tags=["归档批次"])
def create_archive_batch(payload: ArchiveBatchCreate, db: Session = Depends(get_db)):
    if payload.dry_run:
        raise HTTPException(status_code=400, detail="预览请使用 /retention/archive-preview")
    try:
        batch, _counts, resumed = service.run_archive_batch(db, payload)
    except RetentionError as exc:
        db.rollback()
        raise _bad(exc)
    return _batch_to_response(batch, resumed=resumed)


@router.get("/retention/archive-batches", response_model=List[ArchiveBatchResponse], tags=["归档批次"])
def list_archive_batches(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
):
    query = db.query(RetentionBatch)
    if status_filter is not None:
        query = query.filter(RetentionBatch.status == status_filter)
    batches = query.order_by(RetentionBatch.id.desc()).all()
    return [_batch_to_response(batch) for batch in batches]


@router.get("/retention/archive-batches/{batch_id}", response_model=ArchiveBatchResponse, tags=["归档批次"])
def get_archive_batch(batch_id: str, db: Session = Depends(get_db)):
    batch = db.query(RetentionBatch).filter(RetentionBatch.batch_id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return _batch_to_response(batch)


@router.post("/retention/archive-preview", response_model=ArchivePreviewResponse, tags=["归档批次"])
def preview_archive_batch(payload: ArchiveBatchCreate, db: Session = Depends(get_db)):
    result = service.preview_archive(db, payload)
    return ArchivePreviewResponse(
        dry_run=True,
        scanned=result["scanned"],
        eligible=result["eligible"],
        held=result["held"],
        active_member_protected=result["active_member_protected"],
        not_due=result["not_due"],
        no_policy=result["no_policy"],
        candidates=[
            EligibleItem(
                operation_id=entry["operation_id"],
                scene_id=entry["scene_id"],
                rule_id=entry["rule_id"],
                policy_level=entry["policy_level"],
                created_at=entry["created_at"],
                due_at=entry["due_at"],
            )
            for entry in result["candidates"]
        ],
    )


@router.get("/retention/archive-records", response_model=List[ArchiveRecordResponse], tags=["归档批次"])
def list_archive_records(
    operation_id: Optional[int] = Query(None),
    batch_id: Optional[str] = Query(None),
    rule_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(ArchiveRecord)
    if operation_id is not None:
        query = query.filter(ArchiveRecord.operation_id == operation_id)
    if batch_id is not None:
        query = query.filter(ArchiveRecord.batch_id == batch_id)
    if rule_id is not None:
        query = query.filter(ArchiveRecord.rule_id == rule_id)
    return query.order_by(ArchiveRecord.id.desc()).all()


# --------------------------------------------------------------------------- #
# 存在性解释与审计
# --------------------------------------------------------------------------- #

@router.get("/retention/operations/{operation_id}/explain", response_model=ExplainResponse, tags=["保留解释"])
def explain_operation(operation_id: int, db: Session = Depends(get_db)):
    try:
        result = service.explain_operation(db, operation_id)
    except RetentionError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


@router.get("/retention/audit", tags=["保留审计"])
def list_audit_events(
    subject_ref: Optional[str] = Query(None),
    hold_id: Optional[str] = Query(None),
    batch_id: Optional[str] = Query(None),
    rule_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(RetentionAuditEvent)
    if subject_ref is not None:
        query = query.filter(RetentionAuditEvent.subject_ref == subject_ref)
    if hold_id is not None:
        query = query.filter(RetentionAuditEvent.hold_key == hold_id)
    if batch_id is not None:
        query = query.filter(RetentionAuditEvent.batch_id == batch_id)
    if rule_id is not None:
        query = query.filter(RetentionAuditEvent.rule_id == rule_id)
    if event_type is not None:
        query = query.filter(RetentionAuditEvent.event_type == event_type)
    rows = query.order_by(RetentionAuditEvent.id.desc()).limit(limit).all()
    return [
        {
            "id": row.id,
            "event_type": row.event_type,
            "subject_type": row.subject_type,
            "subject_ref": row.subject_ref,
            "hold_id": row.hold_key,
            "rule_id": row.rule_id,
            "batch_id": row.batch_id,
            "actor": row.actor,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in rows
    ]
