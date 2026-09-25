"""保留策略、法律冻结与归档执行的数据库服务层。

设计要点：
- 纯判定在 app.services.retention 中完成，本层只负责装载数据、落库和事务边界；
- 数据集版本冻结在开启时快照成员（covered_operation_ids），事后数据集改动不影响冻结范围；
- 归档批次逐项提交，batch_key 相同即幂等；中断后用同一 batch_key 续跑，
  已处理项（batch_items）作为检查点跳过；
- 归档只清空受限载荷并写摘要/指纹/审计引用，记录行与数据集成员关系保留。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import (
    Annotation,
    Dataset,
    DatasetItem,
    DatasetVersion,
    LegalHold,
    OperationData,
    ArchivedRecord,
    RetentionBatch,
    RetentionBatchItem,
    RetentionPolicy,
    Scene,
)
from app.services.retention import (
    RESTRICTED_PAYLOAD_FIELDS,
    SCOPE_DATASET_VERSION,
    SCOPE_OPERATION,
    BatchItemState,
    Hold,
    RecordState,
    RetentionError,
    RetentionRule,
    SubjectContext,
    active_holds_for,
    build_archive_summary,
    build_payload_digests,
    choose_rule,
    evaluate_subject,
    explain_subject as domain_explain,
    redact_payload,
    resume_due_at,
)

# 视为“活动数据集”的审核状态：其成员受保护，不得清理原始载荷。
ACTIVE_DATASET_STATUSES = ("draft", "pending_review", "approved")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


def _rule_from_model(policy: RetentionPolicy, category: str | None = None) -> RetentionRule:
    return RetentionRule(
        rule_id=policy.rule_id,
        category=category if category is not None else (
            str(policy.scene_id) if policy.scene_id is not None else "*"
        ),
        keep_for=timedelta(days=policy.keep_days),
        priority=policy.priority,
        enabled=bool(policy.enabled),
    )


def create_policy(db: Session, data: dict[str, Any]) -> RetentionPolicy:
    scene_id = data.get("scene_id")
    if scene_id is not None and not db.query(Scene.id).filter(Scene.id == scene_id).first():
        raise RetentionError("场景不存在")
    if not data.get("rule_id", "").strip() or not data.get("name", "").strip():
        raise RetentionError("策略标识和名称不能为空")
    if int(data.get("keep_days", 0)) <= 0:
        raise RetentionError("保留天数必须为正")
    if int(data.get("priority", 0)) < 0:
        raise RetentionError("优先级不能为负")
    if db.query(RetentionPolicy).filter(RetentionPolicy.rule_id == data["rule_id"]).first():
        raise RetentionError("策略标识已存在")
    policy = RetentionPolicy(
        rule_id=data["rule_id"].strip(),
        name=data["name"].strip(),
        scene_id=scene_id,
        skill_id=data.get("skill_id"),
        robot_model_id=data.get("robot_model_id"),
        keep_days=int(data["keep_days"]),
        priority=int(data.get("priority", 0)),
        enabled=bool(data.get("enabled", True)),
        description=data.get("description"),
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def update_policy(db: Session, rule_id: str, changes: dict[str, Any]) -> RetentionPolicy:
    policy = db.query(RetentionPolicy).filter(RetentionPolicy.rule_id == rule_id).first()
    if policy is None:
        raise RetentionError("策略不存在")
    for field_name in ("name", "keep_days", "priority", "enabled", "description",
                       "scene_id", "skill_id", "robot_model_id"):
        if field_name in changes and changes[field_name] is not None:
            if field_name == "keep_days" and int(changes[field_name]) <= 0:
                raise RetentionError("保留天数必须为正")
            setattr(policy, field_name, changes[field_name])
    db.commit()
    db.refresh(policy)
    return policy


def list_policies(db: Any, enabled_only: bool = False) -> list[RetentionPolicy]:
    query = db.query(RetentionPolicy)
    if enabled_only:
        query = query.filter(RetentionPolicy.enabled == True)  # noqa: E712
    return query.order_by(RetentionPolicy.priority.desc(), RetentionPolicy.rule_id).all()


def _rules_for_operation(db: Session, operation: OperationData) -> tuple[RetentionRule, ...]:
    """返回适用于某条作业的全部策略。

    场景维度：精确场景或全局策略（scene_id 为空）；
    机型/技能维度：策略留空表示不限定，否则必须与作业一致。
    最终由领域层按 priority、保留期决定优先级。
    """
    query = db.query(RetentionPolicy).filter(RetentionPolicy.enabled == True)  # noqa: E712
    query = query.filter(
        (RetentionPolicy.scene_id == operation.scene_id) | (RetentionPolicy.scene_id.is_(None))
    )
    policies = query.all()
    rules = []
    for policy in policies:
        if policy.skill_id is not None and policy.skill_id != operation.skill_id:
            continue
        if policy.robot_model_id is not None and policy.robot_model_id != operation.robot_model_id:
            continue
        rules.append(_rule_from_model(policy, category=str(operation.scene_id)))
    return tuple(rules)


# ---------------------------------------------------------------------------
# 法律冻结
# ---------------------------------------------------------------------------


def _snapshot_version_members(db: Session, dataset_id: int, dataset_version_id: int | None) -> list[int]:
    version = None
    if dataset_version_id is not None:
        version = (
            db.query(DatasetVersion)
            .filter(DatasetVersion.id == dataset_version_id, DatasetVersion.dataset_id == dataset_id)
            .first()
        )
        if version is None:
            raise RetentionError("指定的数据集版本不存在")
    members = [
        row[0]
        for row in db.query(DatasetItem.operation_data_id)
        .filter(DatasetItem.dataset_id == dataset_id)
        .all()
    ]
    return sorted(set(members))


def open_hold(db: Session, data: dict[str, Any]) -> LegalHold:
    scope = data.get("scope", SCOPE_OPERATION)
    if scope not in (SCOPE_OPERATION, SCOPE_DATASET_VERSION):
        raise RetentionError("冻结范围无效，仅支持 operation / dataset_version")
    if not data.get("reason", "").strip() or not data.get("requested_by", "").strip():
        raise RetentionError("冻结必须提供原因和申请人")

    hold_id = (data.get("hold_id") or f"hold-{uuid.uuid4().hex[:16]}").strip()
    existing = db.query(LegalHold).filter(LegalHold.hold_id == hold_id).first()
    if existing is not None:
        # 幂等：相同冻结重复开启直接返回既有记录。
        return existing

    operation_data_id = data.get("operation_data_id")
    dataset_id = data.get("dataset_id")
    dataset_version_id = data.get("dataset_version_id")
    covered: list[int] = []

    if scope == SCOPE_OPERATION:
        if operation_data_id is None:
            raise RetentionError("作业级冻结必须提供 operation_data_id")
        op = db.query(OperationData.id).filter(OperationData.id == operation_data_id).first()
        if op is None:
            raise RetentionError("作业数据不存在")
        covered = [operation_data_id]
    else:
        if dataset_id is None:
            raise RetentionError("数据集版本冻结必须提供 dataset_id")
        dataset = db.query(Dataset.id).filter(Dataset.id == dataset_id).first()
        if dataset is None:
            raise RetentionError("数据集不存在")
        if dataset_version_id is None:
            dataset_version_id = (
                db.query(DatasetVersion.id)
                .filter(DatasetVersion.dataset_id == dataset_id)
                .order_by(DatasetVersion.version_number.desc())
                .first()
            )
            dataset_version_id = dataset_version_id[0] if dataset_version_id else None
        covered = _snapshot_version_members(db, dataset_id, dataset_version_id)

    hold = LegalHold(
        hold_id=hold_id,
        scope=scope,
        operation_data_id=operation_data_id if scope == SCOPE_OPERATION else None,
        dataset_id=dataset_id if scope == SCOPE_DATASET_VERSION else None,
        dataset_version_id=dataset_version_id if scope == SCOPE_DATASET_VERSION else None,
        reason=data["reason"].strip(),
        case_reference=data.get("case_reference"),
        requested_by=data["requested_by"].strip(),
        status="active",
        opened_at=ensure_utc(data.get("opened_at")) or utc_now(),
        covered_operation_ids=covered,
    )
    db.add(hold)
    db.flush()
    # 标记进入暂停的作业（已处于其它冻结中的不覆盖首个暂停时点）。
    if covered:
        (
            db.query(OperationData)
            .filter(OperationData.id.in_(covered))
            .filter(OperationData.retention_paused_at.is_(None))
            .filter(OperationData.retention_state != RecordState.ARCHIVED.value)
            .update({OperationData.retention_paused_at: hold.opened_at}, synchronize_session=False)
        )
    db.commit()
    db.refresh(hold)
    return hold


def release_hold(db: Session, hold_id: str, data: dict[str, Any] | None = None) -> LegalHold:
    hold = db.query(LegalHold).filter(LegalHold.hold_id == hold_id).first()
    if hold is None:
        raise RetentionError("冻结不存在")
    if hold.status != "active":
        # 幂等解除：重复调用返回既有结果。
        return hold
    hold.status = "released"
    hold.closed_at = ensure_utc((data or {}).get("closed_at")) or utc_now()
    hold.released_by = (data or {}).get("released_by")
    hold.release_notes = (data or {}).get("release_notes")
    db.flush()

    # 解除后从原到期点继续：按完整冻结区间重算每个覆盖作业的到期点。
    # 仍存在其它有效冻结的作业保持暂停状态。
    covered = list(hold.covered_operation_ids or [])
    if covered:
        all_holds = _load_domain_holds(db)
        current = ensure_utc(hold.closed_at)
        operations = (
            db.query(OperationData)
            .filter(OperationData.id.in_(covered))
            .filter(OperationData.retention_state != RecordState.ARCHIVED.value)
            .all()
        )
        for op in operations:
            subject_holds = [item for item in all_holds if item.subject_id == str(op.id)]
            if active_holds_for(subject_holds, str(op.id), current):
                continue  # 仍有其它重叠冻结，保持暂停。
            chosen = choose_rule(_rules_for_operation(db, op), str(op.scene_id))
            if chosen is not None:
                due = resume_due_at(
                    ensure_utc(op.created_at), chosen.keep_for, subject_holds, current
                )
                op.retention_due_at = due
            op.retention_paused_at = None
    db.commit()
    db.refresh(hold)
    return hold


def list_holds(db: Session, status: str | None = None) -> list[LegalHold]:
    query = db.query(LegalHold)
    if status:
        query = query.filter(LegalHold.status == status)
    return query.order_by(LegalHold.opened_at.desc(), LegalHold.hold_id).all()


def _load_domain_holds(db: Session) -> list[Hold]:
    """把所有冻结（含已解除）转为领域对象，暂停计时依赖完整区间。"""
    holds: list[Hold] = []
    for row in db.query(LegalHold).all():
        target_ref = None
        if row.scope == SCOPE_DATASET_VERSION:
            target_ref = f"dataset:{row.dataset_id}@version:{row.dataset_version_id}"
        else:
            target_ref = f"operation:{row.operation_data_id}"
        for op_id in row.covered_operation_ids or []:
            holds.append(Hold(
                hold_id=row.hold_id,
                subject_id=str(op_id),
                opened_at=ensure_utc(row.opened_at),  # type: ignore[arg-type]
                reason=row.reason,
                closed_at=ensure_utc(row.closed_at),
                scope=row.scope,
                target_ref=target_ref,
            ))
    return holds


def _active_dataset_refs_map(db: Session, op_ids: Iterable[int]) -> dict[str, frozenset[str]]:
    ids = list(op_ids)
    result: dict[str, set[str]] = {str(op_id): set() for op_id in ids}
    if not ids:
        return {key: frozenset() for key, value in result.items()}
    rows = (
        db.query(DatasetItem.operation_data_id, Dataset.id, Dataset.current_version)
        .join(Dataset, DatasetItem.dataset_id == Dataset.id)
        .filter(DatasetItem.operation_data_id.in_(ids))
        .filter(Dataset.review_status.in_(ACTIVE_DATASET_STATUSES))
        .all()
    )
    for op_id, dataset_id, version_number in rows:
        result[str(op_id)].add(f"dataset:{dataset_id}@v{version_number or 1}")
    return {key: frozenset(value) for key, value in result.items()}


# ---------------------------------------------------------------------------
# 作业状态投影
# ---------------------------------------------------------------------------


def projected_state(
    db: Session,
    operation: OperationData,
    moment: datetime | None = None,
) -> dict[str, Any]:
    """返回一条作业当前的有效保留状态（含运行时冻结判断）。"""
    current = ensure_utc(moment or utc_now())
    if operation.retention_state == RecordState.ARCHIVED.value:
        return {"state": RecordState.ARCHIVED.value, "rule_id": operation.retention_rule_id}
    holds = [hold for hold in _load_domain_holds(db) if hold.subject_id == str(operation.id)]
    active = active_holds_for(holds, str(operation.id), current)
    if active:
        return {"state": RecordState.HELD.value, "hold_ids": [hold.hold_id for hold in active]}
    refs_map = _active_dataset_refs_map(db, [operation.id])
    ctx = SubjectContext(
        subject_id=str(operation.id),
        category=str(operation.scene_id),
        created_at=ensure_utc(operation.created_at),  # type: ignore[arg-type]
        already_archived=False,
        active_dataset_refs=refs_map[str(operation.id)],
    )
    decision = evaluate_subject(ctx, _rules_for_operation(db, operation), holds, current)
    return {
        "state": decision.state.value,
        "rule_id": decision.rule_id,
        "reason": decision.reason,
        "due_at": decision.due_at.isoformat() if decision.due_at else None,
    }


def explain_operation(db: Session, operation_id: int, moment: datetime | None = None) -> dict[str, Any]:
    operation = db.query(OperationData).filter(OperationData.id == operation_id).first()
    if operation is None:
        raise RetentionError("作业数据不存在")
    current = ensure_utc(moment or utc_now())
    holds = [hold for hold in _load_domain_holds(db) if hold.subject_id == str(operation.id)]
    refs_map = _active_dataset_refs_map(db, [operation.id])
    ctx = SubjectContext(
        subject_id=str(operation.id),
        category=str(operation.scene_id),
        created_at=ensure_utc(operation.created_at),  # type: ignore[arg-type]
        already_archived=operation.retention_state == RecordState.ARCHIVED.value,
        active_dataset_refs=refs_map[str(operation.id)],
    )
    explanation = domain_explain(ctx, _rules_for_operation(db, operation), holds, current)
    explanation["operation_id"] = operation.id
    explanation["stored_state"] = operation.retention_state
    return explanation


# ---------------------------------------------------------------------------
# 归档批次
# ---------------------------------------------------------------------------


def _operation_payload(operation: OperationData) -> dict[str, Any]:
    return {
        "motion_trajectory": operation.motion_trajectory,
        "perception_records": operation.perception_records,
        "grasp_result": operation.grasp_result,
        "environment_conditions": operation.environment_conditions,
        "hardware_status": operation.hardware_status,
        "robot_model_id": operation.robot_model_id,
        "scene_id": operation.scene_id,
        "skill_id": operation.skill_id,
        "robot_serial": operation.robot_serial,
        "timestamp_start": ensure_utc(operation.timestamp_start).isoformat(),
        "timestamp_end": ensure_utc(operation.timestamp_end).isoformat(),
        "duration_ms": operation.duration_ms,
        "quality_score": operation.quality_score,
        "completeness_score": operation.completeness_score,
        "data_grade": operation.data_grade,
        "created_at": ensure_utc(operation.created_at).isoformat(),
    }


def _build_contexts(db: Session, moment: datetime) -> tuple[list[SubjectContext], list[OperationData]]:
    operations = (
        db.query(OperationData)
        .filter(OperationData.retention_state != RecordState.ARCHIVED.value)
        .order_by(OperationData.created_at.asc(), OperationData.id.asc())
        .all()
    )
    refs_map = _active_dataset_refs_map(db, [op.id for op in operations])
    contexts = [
        SubjectContext(
            subject_id=str(op.id),
            category=str(op.scene_id),
            created_at=ensure_utc(op.created_at),  # type: ignore[arg-type]
            already_archived=False,
            active_dataset_refs=refs_map[str(op.id)],
        )
        for op in operations
    ]
    return contexts, operations


def run_archive_batch(
    db: Session,
    *,
    batch_key: str | None = None,
    dry_run: bool = False,
    scene_id: int | None = None,
    trigger_by: str | None = None,
    moment: datetime | None = None,
    item_failure: Any = None,
    crash_after: Any = None,
) -> RetentionBatch:
    """执行（或断点续跑）一次归档批次。

    - 同一 batch_key 重复调用保持幂等：已结束批次直接返回既有结果；
      running 批次（进程中断）从检查点 RetentionBatchItem 续跑；
    - 单项失败只记录该条 FAILED 并继续，批次整体标记 completed_with_errors；
    - dry_run 只生成 would_archive 计划，不动任何载荷；
    - item_failure(operation) 抛异常模拟单项失败（被捕获）；
    - crash_after(operation) 抛异常模拟进程崩溃（异常上抛，批次保持 running，
      已提交的检查点保留，供重启后续跑验证）。
    """
    current = ensure_utc(moment or utc_now())
    batch_key = batch_key or f"batch-{current.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"

    batch = db.query(RetentionBatch).filter(RetentionBatch.batch_key == batch_key).first()
    if batch is None:
        batch = RetentionBatch(
            batch_key=batch_key,
            status="dry_run" if dry_run else "running",
            dry_run=dry_run,
            started_at=current,
            trigger_by=trigger_by,
        )
        db.add(batch)
        db.commit()
        db.refresh(batch)
    else:
        if batch.dry_run != dry_run:
            raise RetentionError("同一批次键的预演与正式执行模式不一致，请使用新的批次键")
        if batch.status in ("completed", "dry_run"):
            # 完全成功或预演批次重复执行：直接返回既有结果，不做任何变更。
            return batch
        if batch.status == "completed_with_errors":
            # 部分失败批次用同键重跑：重新打开，仅重试此前 FAILED 项（成功项有检查点）。
            batch.status = "running"
            batch.finished_at = None
            db.commit()

    # 续跑时沿用批次开始时刻，保证同一批次的判定确定性。
    plan_moment = ensure_utc(batch.started_at)
    contexts, operations = _build_contexts(db, plan_moment)
    if scene_id is not None:
        contexts = [ctx for ctx in contexts if ctx.category == str(scene_id)]
        keep = {ctx.subject_id for ctx in contexts}
        operations = [op for op in operations if str(op.id) in keep]

    holds = _load_domain_holds(db)

    # 检查点：已成功/跳过（终态）的项崩溃或重复调用时跳过；
    # 之前 FAILED 的项在续跑时删除旧记录并重试，使批次可收敛。
    checkpoint_states = (
        BatchItemState.ARCHIVED.value,
        BatchItemState.WOULD_ARCHIVE.value,
        BatchItemState.HELD.value,
        BatchItemState.SKIPPED_DATASET.value,
        BatchItemState.ACTIVE.value,
    )
    done_ids = {
        row[0]
        for row in db.query(RetentionBatchItem.operation_data_id)
        .filter(RetentionBatchItem.batch_id == batch.id)
        .filter(RetentionBatchItem.state.in_(checkpoint_states))
        .all()
    }
    (
        db.query(RetentionBatchItem)
        .filter(RetentionBatchItem.batch_id == batch.id)
        .filter(RetentionBatchItem.state == BatchItemState.FAILED.value)
        .delete(synchronize_session=False)
    )
    db.commit()

    op_by_id = {op.id: op for op in operations}
    for ctx in contexts:
        op_id = int(ctx.subject_id)
        op = op_by_id[op_id]
        if op_id in done_ids:
            continue
        rules = _rules_for_operation(db, op)
        decision = evaluate_subject(ctx, rules, holds, plan_moment)

        if decision.state == RecordState.HELD:
            _record_item(db, batch, op_id, BatchItemState.HELD.value, decision.reason)
            continue
        if ctx.active_dataset_refs and decision.state != RecordState.ELIGIBLE:
            _record_item(db, batch, op_id, BatchItemState.SKIPPED_DATASET.value, decision.reason)
            continue
        if decision.state != RecordState.ELIGIBLE:
            _record_item(db, batch, op_id, BatchItemState.ACTIVE.value, decision.reason)
            continue

        if dry_run:
            _record_item(db, batch, op_id, BatchItemState.WOULD_ARCHIVE.value, decision.reason)
            continue

        # 单项钩子：模拟该项处理失败（被捕获，记录 FAILED 后继续）。
        if item_failure is not None:
            try:
                item_failure(op)
            except Exception as exc:
                _record_item(db, batch, op_id, BatchItemState.FAILED.value, str(exc)[:500])
                continue

        try:
            # 归档变更与成功检查点在同一事务提交，杜绝“已归档无检查点”的中间态。
            _stage_archive(db, batch, op, decision.rule_id, holds, current)
            db.add(RetentionBatchItem(
                batch_id=batch.id,
                operation_data_id=op_id,
                state=BatchItemState.ARCHIVED.value,
                detail=decision.reason,
                processed_at=current,
            ))
            db.commit()
        except Exception as exc:  # 单项失败不影响批次其余部分
            db.rollback()
            batch = db.query(RetentionBatch).filter(RetentionBatch.id == batch.id).first()
            _record_item(db, batch, op_id, BatchItemState.FAILED.value, str(exc)[:500])
            continue

        # 崩溃钩子：在成功提交检查点之后模拟进程中断，异常上抛。
        if crash_after is not None:
            crash_after(op)

    return _finalize_batch(db, batch, current, dry_run)


def _finalize_batch(db: Session, batch: RetentionBatch, current: datetime, dry_run: bool) -> RetentionBatch:
    all_items = (
        db.query(RetentionBatchItem.state)
        .filter(RetentionBatchItem.batch_id == batch.id)
        .all()
    )
    states = [row[0] for row in all_items]
    batch.total = len(states)
    batch.succeeded = states.count(BatchItemState.ARCHIVED.value)
    batch.failed = states.count(BatchItemState.FAILED.value)
    batch.skipped_held = states.count(BatchItemState.HELD.value)
    batch.skipped_dataset = states.count(BatchItemState.SKIPPED_DATASET.value)
    batch.finished_at = current
    batch.last_error = f"{batch.failed} 项处理失败" if batch.failed else None
    batch.status = "dry_run" if dry_run else ("completed_with_errors" if batch.failed else "completed")
    db.commit()
    db.refresh(batch)
    return batch


def _record_item(db: Session, batch: RetentionBatch, op_id: int, state: str, detail: str) -> None:
    db.add(RetentionBatchItem(
        batch_id=batch.id,
        operation_data_id=op_id,
        state=state,
        detail=detail,
        processed_at=utc_now(),
    ))
    db.commit()


def _stage_archive(
    db: Session,
    batch: RetentionBatch,
    operation: OperationData,
    rule_id: str | None,
    holds: list[Hold],
    moment: datetime,
) -> None:
    """暂存单条作业的归档变更（不提交）：摘要/指纹/审计引用 + 清空受限载荷。

    提交由调用方与成功检查点在同一事务内完成。
    """
    existing = (
        db.query(ArchivedRecord)
        .filter(ArchivedRecord.operation_data_id == operation.id)
        .first()
    )
    current = ensure_utc(moment)
    if existing is not None:
        # 已归档（可能由其它批次完成），保持幂等，仅补齐状态。
        operation.retention_state = RecordState.ARCHIVED.value
        operation.archived_at = ensure_utc(existing.archived_at)
        return

    payload = _operation_payload(operation)
    annotation = (
        db.query(Annotation)
        .filter(Annotation.operation_data_id == operation.id)
        .first()
    )
    annotation_data = None
    if annotation is not None:
        annotation_data = {
            "is_success": annotation.is_success,
            "failure_category": annotation.failure_category,
            "failure_subcategory": annotation.failure_subcategory,
            "review_status": annotation.review_status,
            "annotation_quality_score": annotation.annotation_quality_score,
        }

    digests = tuple(build_payload_digests(payload))
    summary = build_archive_summary(payload, annotation_data)
    active = active_holds_for(holds, str(operation.id), current)
    record = ArchivedRecord(
        operation_data_id=operation.id,
        archive_ref=f"arc-{operation.id}-{current.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        rule_id=rule_id,
        batch_id=batch.id,
        hold_ids=[hold.hold_id for hold in active],
        payload_digests=[digest.as_dict() for digest in digests],
        summary=summary,
        archived_at=current,
    )
    db.add(record)

    redacted = redact_payload(payload)
    for field_name in RESTRICTED_PAYLOAD_FIELDS:
        setattr(operation, field_name, redacted[field_name])
    operation.retention_state = RecordState.ARCHIVED.value
    operation.retention_rule_id = rule_id
    operation.archived_at = current
