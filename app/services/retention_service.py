"""保留策略、法律冻结与归档的服务编排。

关键约定：
- 策略按场景选中候选；场景策略与全局兜底策略同时命中时按优先级解析。
- 法律冻结可覆盖单条作业或整个数据集（版本）；多条冻结允许重叠，只有
  全部解除后对象才恢复清理判定。
- 归档只移除受限载荷，保留统计摘要、载荷哈希等审计引用；已发布数据集
  的成员受到保护，不会被悄悄破坏。
- 冻结不顺延保留期限：解除后仍以“创建时间 + 保留期限”的原始到期点判断。
- 批次以幂等键去重，逐项使用保存点，允许部分失败并在重启后续跑。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    Annotation,
    ArchiveRecord,
    Dataset,
    DatasetItem,
    DatasetVersion,
    HoldTarget,
    LegalHold,
    OperationData,
    RetentionAuditEvent,
    RetentionBatch,
    RetentionBatchItem,
    RetentionPolicy,
    Scene,
)
from app.services.retention import (
    ARCHIVE_PURGE_PAYLOAD,
    SUBJECT_DATASET_VERSION,
    SUBJECT_OPERATION,
    RecordState,
    RetentionError,
    RetentionRule,
    select_policy,
    subject_key,
)

UTC = timezone.utc

# 归档时移除的受限载荷字段。
PAYLOAD_FIELDS = (
    "motion_trajectory",
    "perception_records",
    "grasp_result",
    "environment_conditions",
    "hardware_status",
)
# 非空约束的载荷字段用空对象占位，其余置空。
NON_NULLABLE_PAYLOAD = {"motion_trajectory", "perception_records"}

BATCH_RUNNING = "running"
BATCH_COMPLETED = "completed"
BATCH_PARTIAL = "partial_failed"

ITEM_PENDING = "pending"
ITEM_SUCCEEDED = "succeeded"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit(
    db: Session,
    event_type: str,
    *,
    subject_type: Optional[str] = None,
    subject_ref: Optional[str] = None,
    hold_key: Optional[str] = None,
    rule_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    actor: Optional[str] = None,
    detail: Optional[dict] = None,
) -> RetentionAuditEvent:
    event = RetentionAuditEvent(
        event_type=event_type,
        subject_type=subject_type,
        subject_ref=subject_ref,
        hold_key=hold_key,
        rule_id=rule_id,
        batch_id=batch_id,
        actor=actor,
        detail=detail,
    )
    db.add(event)
    return event


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #

def load_rules(db: Session) -> tuple[RetentionRule, ...]:
    policies = db.query(RetentionPolicy).filter(RetentionPolicy.enabled == True).all()  # noqa: E712
    rules = []
    for policy in policies:
        category = "*" if policy.scope == "global" or policy.scene_id is None else str(policy.scene_id)
        rules.append(
            RetentionRule(
                rule_id=policy.rule_id,
                category=category,
                keep_for=timedelta(days=policy.keep_days),
                priority=policy.priority,
                enabled=policy.enabled,
            )
        )
    return tuple(rules)


def create_policy(db: Session, payload, actor: Optional[str] = None) -> RetentionPolicy:
    scope = payload.scope
    scene_id = payload.scene_id
    if scope == "global":
        scene_id = None
    elif scene_id is None:
        raise RetentionError("场景策略必须指定 scene_id")

    if db.query(RetentionPolicy).filter(RetentionPolicy.rule_id == payload.rule_id).first():
        raise RetentionError("策略标识已存在")
    if scene_id is not None and db.query(Scene).filter(Scene.id == scene_id).first() is None:
        raise RetentionError("场景不存在")

    duplicate = (
        db.query(RetentionPolicy)
        .filter(
            RetentionPolicy.scope == scope,
            RetentionPolicy.scene_id == scene_id,
            RetentionPolicy.enabled == True,  # noqa: E712
        )
        .first()
    )
    if duplicate and payload.enabled:
        raise RetentionError("同一场景/全局层级已存在启用中的策略")

    policy = RetentionPolicy(
        rule_id=payload.rule_id,
        name=payload.name,
        scope=scope,
        scene_id=scene_id,
        keep_days=payload.keep_days,
        priority=payload.priority,
        enabled=payload.enabled,
        description=payload.description,
    )
    db.add(policy)
    _audit(db, "policy_created", rule_id=payload.rule_id, actor=actor, detail={"scope": scope, "scene_id": scene_id, "keep_days": payload.keep_days, "priority": payload.priority})
    db.commit()
    db.refresh(policy)
    return policy


def update_policy(db: Session, rule_id: str, payload, actor: Optional[str] = None) -> RetentionPolicy:
    policy = db.query(RetentionPolicy).filter(RetentionPolicy.rule_id == rule_id).first()
    if policy is None:
        raise RetentionError("策略不存在")
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(policy, field, value)
    _audit(db, "policy_updated", rule_id=rule_id, actor=actor, detail=changes)
    db.commit()
    db.refresh(policy)
    return policy


# --------------------------------------------------------------------------- #
# 法律冻结
# --------------------------------------------------------------------------- #

def _active_hold_joins(db: Session):
    return (
        db.query(HoldTarget, LegalHold)
        .join(LegalHold, HoldTarget.hold_id == LegalHold.id)
        .filter(LegalHold.status == "active", LegalHold.closed_at.is_(None))
    )


def active_hold_map(db: Session) -> dict[int, list[dict]]:
    """返回 operation_id -> 生效中的冻结列表（含数据集扩展）。"""
    result: dict[int, list[dict]] = {}

    direct = _active_hold_joins(db).filter(HoldTarget.subject_type == SUBJECT_OPERATION).all()
    for target, hold in direct:
        result.setdefault(target.operation_id, []).append(
            {"hold_id": hold.hold_id, "reason": hold.reason, "via": "operation"}
        )

    dataset_targets = (
        _active_hold_joins(db)
        .filter(HoldTarget.subject_type.in_([SUBJECT_DATASET_VERSION, "dataset"]))
        .all()
    )
    covered_dataset_ids = {target.dataset_id for target, _hold in dataset_targets if target.dataset_id is not None}
    if covered_dataset_ids:
        members = (
            db.query(DatasetItem.operation_data_id, DatasetItem.dataset_id)
            .filter(DatasetItem.dataset_id.in_(covered_dataset_ids))
            .all()
        )
        members_by_dataset: dict[int, set[int]] = {}
        for op_id, dataset_id in members:
            members_by_dataset.setdefault(dataset_id, set()).add(op_id)
        for target, hold in dataset_targets:
            label = "dataset_version" if target.subject_type == SUBJECT_DATASET_VERSION else "dataset"
            for op_id in members_by_dataset.get(target.dataset_id, set()):
                result.setdefault(op_id, []).append(
                    {
                        "hold_id": hold.hold_id,
                        "reason": hold.reason,
                        "via": label,
                        "dataset_id": target.dataset_id,
                        "dataset_version_id": target.dataset_version_id,
                    }
                )
    return result


def create_hold(db: Session, payload, actor: Optional[str] = None) -> LegalHold:
    if db.query(LegalHold).filter(LegalHold.hold_id == payload.hold_id).first():
        raise RetentionError("冻结案号已存在")

    now = utc_now()
    hold = LegalHold(
        hold_id=payload.hold_id,
        reason=payload.reason,
        requested_by=payload.requested_by,
        case_reference=payload.case_reference,
        status="active",
        opened_at=ensure_utc(payload.opened_at) if getattr(payload, "opened_at", None) else now,
    )
    db.add(hold)
    db.flush()

    expanded: list[dict] = []
    for spec in payload.targets:
        if spec.kind == SUBJECT_OPERATION:
            op = db.query(OperationData).filter(OperationData.id == spec.id).first()
            if op is None:
                raise RetentionError(f"作业 {spec.id} 不存在")
            target = HoldTarget(
                hold_id=hold.id,
                subject_type=SUBJECT_OPERATION,
                subject_ref=subject_key(SUBJECT_OPERATION, op.id),
                operation_id=op.id,
            )
        elif spec.kind == SUBJECT_DATASET_VERSION:
            version = (
                db.query(DatasetVersion).filter(DatasetVersion.id == spec.id).first()
            )
            if version is None:
                raise RetentionError(f"数据集版本 {spec.id} 不存在")
            target = HoldTarget(
                hold_id=hold.id,
                subject_type=SUBJECT_DATASET_VERSION,
                subject_ref=subject_key(SUBJECT_DATASET_VERSION, version.id),
                dataset_id=version.dataset_id,
                dataset_version_id=version.id,
            )
        else:  # dataset
            dataset = db.query(Dataset).filter(Dataset.id == spec.id).first()
            if dataset is None:
                raise RetentionError(f"数据集 {spec.id} 不存在")
            target = HoldTarget(
                hold_id=hold.id,
                subject_type="dataset",
                subject_ref=f"dataset:{dataset.id}",
                dataset_id=dataset.id,
            )
        db.add(target)
        expanded.append({"kind": spec.kind, "id": spec.id})

    _audit(
        db,
        "hold_opened",
        hold_key=payload.hold_id,
        actor=actor or payload.requested_by,
        detail={"reason": payload.reason, "case_reference": payload.case_reference, "targets": expanded},
    )
    db.commit()
    db.refresh(hold)
    return hold


def release_hold(db: Session, hold_id: str, payload, actor: Optional[str] = None) -> LegalHold:
    hold = db.query(LegalHold).filter(LegalHold.hold_id == hold_id).first()
    if hold is None:
        raise RetentionError("冻结不存在")
    if hold.status != "active":
        raise RetentionError("冻结已解除，不能重复解除")
    hold.status = "released"
    hold.closed_at = utc_now()
    hold.released_by = payload.released_by
    hold.release_note = payload.note
    _audit(
        db,
        "hold_released",
        hold_key=hold_id,
        actor=actor or payload.released_by,
        detail={"note": payload.note},
    )
    db.commit()
    db.refresh(hold)
    return hold


# --------------------------------------------------------------------------- #
# 候选选择与解释
# --------------------------------------------------------------------------- #

def _published_member_ids(db: Session) -> set[int]:
    """已发布（活动）数据集当前版本的成员，受保护不得清理载荷。"""
    published_dataset_ids = [
        row[0]
        for row in db.query(Dataset.id).filter(Dataset.is_published == True).all()  # noqa: E712
    ]
    if not published_dataset_ids:
        return set()
    rows = (
        db.query(DatasetItem.operation_data_id)
        .filter(DatasetItem.dataset_id.in_(published_dataset_ids))
        .all()
    )
    return {row[0] for row in rows}


def evaluate(db: Session, now: Optional[datetime] = None, scene_id: Optional[int] = None):
    """扫描作业并按保留策略/冻结/活动成员保护进行分区。"""
    now = ensure_utc(now) or utc_now()
    rules = load_rules(db)
    held_map = active_hold_map(db)
    protected = _published_member_ids(db)

    query = db.query(OperationData).filter(OperationData.retention_state != RecordState.ARCHIVED.value)
    if scene_id is not None:
        query = query.filter(OperationData.scene_id == scene_id)
    operations = query.order_by(OperationData.created_at.asc(), OperationData.id.asc()).all()

    eligible: list[dict] = []
    counts = {"scanned": 0, "held": 0, "active_member_protected": 0, "not_due": 0, "no_policy": 0}
    for op in operations:
        counts["scanned"] += 1
        if op.id in held_map:
            counts["held"] += 1
            continue
        if op.id in protected:
            counts["active_member_protected"] += 1
            continue
        rule, level = select_policy(rules, str(op.scene_id))
        if rule is None:
            counts["no_policy"] += 1
            continue
        created = ensure_utc(op.created_at)
        due_at = created + rule.keep_for
        if now < due_at:
            counts["not_due"] += 1
            continue
        eligible.append(
            {
                "operation": op,
                "rule_id": rule.rule_id,
                "policy_level": level,
                "due_at": due_at,
            }
        )
    return eligible, counts, rules, held_map, protected


def explain_operation(db: Session, operation_id: int, now: Optional[datetime] = None) -> dict:
    now = ensure_utc(now) or utc_now()
    op = db.query(OperationData).filter(OperationData.id == operation_id).first()
    if op is None:
        raise RetentionError("作业不存在")

    rules = load_rules(db)
    rule, level = select_policy(rules, str(op.scene_id))
    created = ensure_utc(op.created_at)
    due_at = created + rule.keep_for if rule else None
    overdue = bool(due_at and now >= due_at)

    held_map = active_hold_map(db)
    active_holds = held_map.get(op.id, [])

    memberships = (
        db.query(Dataset.id, Dataset.name, Dataset.is_published, Dataset.review_status, DatasetItem.dataset_id)
        .join(DatasetItem, DatasetItem.dataset_id == Dataset.id)
        .filter(DatasetItem.operation_data_id == op.id)
        .all()
    )
    membership_info = [
        {
            "dataset_id": ds_id,
            "name": name,
            "is_published": bool(published),
            "review_status": review_status,
        }
        for (ds_id, name, published, review_status, _join_id) in memberships
    ]

    archive = (
        db.query(ArchiveRecord)
        .filter(ArchiveRecord.subject_type == SUBJECT_OPERATION, ArchiveRecord.operation_id == op.id)
        .order_by(ArchiveRecord.id.desc())
        .first()
    )
    archive_info = None
    if archive is not None:
        archive_info = {
            "archive_ref": archive.archive_ref,
            "rule_id": archive.rule_id,
            "batch_id": archive.batch_id,
            "archived_at": ensure_utc(archive.archived_at).isoformat(),
            "summary": archive.summary,
            "payload_audit_ref": archive.payload_audit_ref,
        }

    payload_cleared = op.retention_state == RecordState.ARCHIVED.value
    if payload_cleared:
        why = f"已归档（{archive.archive_ref if archive else ''}），受限载荷已移除，仅保留统计摘要与审计引用"
        present_reason = "archived"
    elif active_holds:
        why = "处于法律冻结：" + "、".join(sorted({h["hold_id"] for h in active_holds}))
        present_reason = "held"
    elif any(item["is_published"] for item in membership_info):
        why = "属于已发布活动数据集的成员，冻结/保留清理不得破坏该版本"
        present_reason = "active_dataset_member"
    elif rule is None:
        why = "没有适用的保留策略，继续保留原始数据"
        present_reason = "no_policy"
    elif overdue:
        why = f"已于 {due_at.isoformat()} 到达保留期限（策略 {rule.rule_id}），等待下一批归档"
        present_reason = "due_pending_archive"
    else:
        why = f"按策略 {rule.rule_id} 保留至 {due_at.isoformat()}"
        present_reason = "within_retention"

    return {
        "operation_id": op.id,
        "retention_state": op.retention_state,
        "present": True,
        "why_present": why,
        "present_reason": present_reason,
        "rule_id": rule.rule_id if rule else None,
        "policy_level": level,
        "keep_days": rule.keep_for.days if rule else None,
        "created_at": created,
        "due_at": due_at,
        "overdue": overdue,
        "payload_cleared": payload_cleared,
        "active_holds": active_holds,
        "dataset_memberships": membership_info,
        "archive": archive_info,
    }


# --------------------------------------------------------------------------- #
# 归档动作
# --------------------------------------------------------------------------- #

def _canonical_payload(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _build_summary(db: Session, op: OperationData) -> dict:
    annotation = db.query(Annotation).filter(Annotation.operation_data_id == op.id).first()
    membership_count = (
        db.query(DatasetItem).filter(DatasetItem.operation_data_id == op.id).count()
    )
    return {
        "operation_id": op.id,
        "robot_model_id": op.robot_model_id,
        "scene_id": op.scene_id,
        "skill_id": op.skill_id,
        "robot_serial": op.robot_serial,
        "timestamp_start": ensure_utc(op.timestamp_start).isoformat() if op.timestamp_start else None,
        "timestamp_end": ensure_utc(op.timestamp_end).isoformat() if op.timestamp_end else None,
        "duration_ms": op.duration_ms,
        "quality_score": op.quality_score,
        "completeness_score": op.completeness_score,
        "data_grade": op.data_grade,
        "annotation": (
            {
                "is_success": annotation.is_success,
                "failure_category": annotation.failure_category,
                "failure_subcategory": annotation.failure_subcategory,
                "review_status": annotation.review_status,
                "annotation_quality_score": annotation.annotation_quality_score,
            }
            if annotation
            else None
        ),
        "dataset_membership_count": membership_count,
    }


def _fault_ids() -> set[int]:
    """仅用于测试：通过环境变量注入逐项故障以验证部分失败/恢复。"""
    raw = os.environ.get("RETENTION_TEST_FAIL_OPERATION_IDS", "")
    try:
        return {int(part) for part in raw.split(",") if part.strip()}
    except ValueError:
        return set()


def purge_operation_payload(
    db: Session,
    op: OperationData,
    rule_id: str,
    batch_id: Optional[str],
    now: datetime,
) -> ArchiveRecord:
    if op.id in _fault_ids():
        raise RuntimeError(f"注入故障：作业 {op.id} 归档失败")

    existing = (
        db.query(ArchiveRecord)
        .filter(ArchiveRecord.subject_type == SUBJECT_OPERATION, ArchiveRecord.operation_id == op.id)
        .first()
    )
    if existing is not None:
        return existing

    payload = {field: getattr(op, field) for field in PAYLOAD_FIELDS}
    digest = hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()
    summary = _build_summary(db, op)
    summary["payload_sha256"] = digest

    archive_ref = f"ARC-OP-{op.id}-{digest[:12]}"
    record = ArchiveRecord(
        subject_type=SUBJECT_OPERATION,
        operation_id=op.id,
        archive_ref=archive_ref,
        rule_id=rule_id,
        batch_id=batch_id,
        action=ARCHIVE_PURGE_PAYLOAD,
        archived_at=now,
        summary=summary,
        payload_audit_ref=f"sha256:{digest}",
        removed_fields=list(PAYLOAD_FIELDS),
    )
    db.add(record)

    for field in PAYLOAD_FIELDS:
        setattr(op, field, {} if field in NON_NULLABLE_PAYLOAD else None)
    op.retention_state = RecordState.ARCHIVED.value
    op.retention_rule_id = rule_id
    op.archived_at = now
    db.flush()
    return record


# --------------------------------------------------------------------------- #
# 批次执行（幂等、部分失败、重启恢复）
# --------------------------------------------------------------------------- #

def _get_or_create_batch(db: Session, payload, now: datetime) -> tuple[RetentionBatch, list[dict], dict, bool]:
    """返回 (批次, 候选, 计数, 是否新建)。命中幂等键的批次直接复用。

    新建批次会先落库并提交，使“待处理”项在进程崩溃后依然可见，可凭同一
    幂等键在重启后恢复续跑。
    """
    existing = None
    if payload.idempotency_key:
        existing = (
            db.query(RetentionBatch)
            .filter(RetentionBatch.idempotency_key == payload.idempotency_key)
            .first()
        )
    if existing is not None:
        counts = {
            "scanned": 0,
            "held": 0,
            "active_member_protected": 0,
            "not_due": 0,
            "no_policy": 0,
        }
        return existing, [], counts, False

    eligible, counts, _rules, _held, _protected = evaluate(
        db, now, scene_id=payload.scene_id
    )
    if payload.limit is not None:
        eligible = eligible[: payload.limit]

    batch = RetentionBatch(
        batch_id=f"BATCH-{uuid.uuid4().hex[:16]}",
        idempotency_key=payload.idempotency_key,
        status=BATCH_RUNNING,
        started_at=now,
        triggered_by=payload.triggered_by,
        total=len(eligible),
    )
    db.add(batch)
    db.flush()
    for entry in eligible:
        db.add(
            RetentionBatchItem(
                batch_pk=batch.id,
                subject_type=SUBJECT_OPERATION,
                operation_id=entry["operation"].id,
                status=ITEM_PENDING,
                rule_id=entry["rule_id"],
            )
        )
    _audit(
        db,
        "archive_batch_created",
        batch_id=batch.batch_id,
        actor=payload.triggered_by,
        detail={"total": len(eligible), "scene_id": payload.scene_id, "idempotency_key": payload.idempotency_key},
    )
    db.commit()
    db.refresh(batch)
    return batch, eligible, counts, True


def run_archive_batch(db: Session, payload, now: Optional[datetime] = None) -> tuple[RetentionBatch, dict, bool]:
    """执行归档批次；返回 (批次, 预览计数, resumed)。幂等重放不重复归档。"""
    now = ensure_utc(now) or utc_now()
    batch, eligible, counts, created = _get_or_create_batch(db, payload, now)

    # 已成功完成的批次直接幂等返回；运行中断或部分失败的批次可重跑恢复。
    if not created and batch.status == BATCH_COMPLETED:
        db.commit()
        return batch, counts, False

    resumed = not created

    items = (
        db.query(RetentionBatchItem)
        .filter(RetentionBatchItem.batch_pk == batch.id)
        .order_by(RetentionBatchItem.id.asc())
        .all()
    )
    # 重启恢复 / 部分失败重试：处理仍挂起或上次失败的项，已成功的不重复归档。
    pending = [item for item in items if item.status in (ITEM_PENDING, ITEM_FAILED)]

    # 恢复时补算扫描计数（仅供接口展示，不影响既有结果）。
    if resumed and not counts["scanned"]:
        _eligible, counts, _r, _h, _p = evaluate(db, now, scene_id=payload.scene_id)

    succeeded_delta = 0
    failed_delta = 0
    for item in pending:
        item.attempts += 1
        op = db.query(OperationData).filter(OperationData.id == item.operation_id).first()
        try:
            if op is None:
                raise RetentionError(f"作业 {item.operation_id} 已不存在")
            # 双重检查：冻结或活动成员身份可能在入队后变化。
            held_map = active_hold_map(db)
            protected = _published_member_ids(db)
            if op.id in held_map:
                item.status = ITEM_SKIPPED
                item.error = "入队后被法律冻结覆盖"
                item.processed_at = utc_now()
            elif op.id in protected:
                item.status = ITEM_SKIPPED
                item.error = "入队后成为活动数据集成员"
                item.processed_at = utc_now()
            else:
                try:
                    with db.begin_nested():  # 保存点：单项失败不影响整批
                        record = purge_operation_payload(db, op, item.rule_id, batch.batch_id, now)
                        item.status = ITEM_SUCCEEDED
                        item.archive_ref = record.archive_ref
                        item.error = None
                        item.processed_at = now
                except Exception as exc:  # 保存点已回滚该项改动
                    db.refresh(op)
                    raise exc
                succeeded_delta += 1
                _audit(
                    db,
                    "operation_archived",
                    subject_type=SUBJECT_OPERATION,
                    subject_ref=subject_key(SUBJECT_OPERATION, op.id),
                    rule_id=item.rule_id,
                    batch_id=batch.batch_id,
                    actor=payload.triggered_by,
                    detail={"archive_ref": record.archive_ref},
                )
        except Exception as exc:
            item.status = ITEM_FAILED
            item.error = str(exc)
            item.processed_at = utc_now()
            failed_delta += 1
            _audit(
                db,
                "archive_item_failed",
                subject_type=SUBJECT_OPERATION,
                subject_ref=subject_key(SUBJECT_OPERATION, item.operation_id),
                rule_id=item.rule_id,
                batch_id=batch.batch_id,
                actor=payload.triggered_by,
                detail={"error": str(exc), "attempts": item.attempts},
            )
        # 逐项提交：崩溃时已处理项不丢失，重启后只续跑剩余项。
        db.commit()

    final_items = (
        db.query(RetentionBatchItem)
        .filter(RetentionBatchItem.batch_pk == batch.id)
        .all()
    )
    # 计数器按最终逐项状态重算，保证部分失败重试后结果收敛。
    batch.succeeded = sum(1 for item in final_items if item.status == ITEM_SUCCEEDED)
    batch.failed = sum(1 for item in final_items if item.status == ITEM_FAILED)
    batch.skipped = sum(1 for item in final_items if item.status == ITEM_SKIPPED)
    has_failure = any(item.status == ITEM_FAILED for item in final_items)
    has_pending = any(item.status == ITEM_PENDING for item in final_items)
    if has_pending:
        batch.status = BATCH_RUNNING
        batch.finished_at = None
    else:
        batch.status = BATCH_PARTIAL if has_failure else BATCH_COMPLETED
        batch.finished_at = utc_now()
    _audit(
        db,
        "archive_batch_finished" if not has_pending else "archive_batch_interrupted",
        batch_id=batch.batch_id,
        actor=payload.triggered_by,
        detail={"status": batch.status, "succeeded": succeeded_delta, "failed": failed_delta, "resumed": resumed},
    )
    db.commit()
    db.refresh(batch)
    return batch, counts, resumed


def preview_archive(db: Session, payload, now: Optional[datetime] = None) -> dict:
    now = ensure_utc(now) or utc_now()
    eligible, counts, _rules, _held, _protected = evaluate(db, now, scene_id=payload.scene_id)
    if payload.limit is not None:
        eligible = eligible[: payload.limit]
    candidates = [
        {
            "operation_id": entry["operation"].id,
            "scene_id": entry["operation"].scene_id,
            "rule_id": entry["rule_id"],
            "policy_level": entry["policy_level"],
            "created_at": ensure_utc(entry["operation"].created_at),
            "due_at": entry["due_at"],
        }
        for entry in eligible
    ]
    counts["eligible"] = len(eligible)
    counts["candidates"] = candidates
    counts["dry_run"] = True
    return counts
