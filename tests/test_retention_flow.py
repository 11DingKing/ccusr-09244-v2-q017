"""保留/冻结服务层流程测试：优先级、重叠冻结、部分失败、解除续期、重启恢复。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models import (
    Annotation,
    ArchivedRecord,
    DatasetItem,
    RetentionBatch,
    RetentionBatchItem,
)
from app.services import retention_service as svc
from app.services.retention import BatchItemState, RecordState

from .conftest import make_base_resources, make_dataset, make_operation

MOMENT = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
def resources(db):
    robot, scene, scene2, skill = make_base_resources(db)
    db.commit()
    return robot, scene, scene2, skill


def _policy(db, scene, days, priority=0, rule_id="r1", **extra):
    return svc.create_policy(db, {
        "rule_id": rule_id, "name": rule_id, "scene_id": scene.id,
        "keep_days": days, "priority": priority, **extra,
    })


# ---------------------------------------------------------------------------
# 1. 策略优先级
# ---------------------------------------------------------------------------


def test_policy_priority_selects_higher_priority_rule(db, resources):
    robot, scene, scene2, skill = resources
    # 低优先级长期保留 + 高优先级短期保留，高优先级生效。
    _policy(db, scene, 365, priority=0, rule_id="baseline")
    _policy(db, scene, 30, priority=10, rule_id="privacy-strict")
    old = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    fresh = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=5))
    db.commit()

    batch = svc.run_archive_batch(db, batch_key="b1", moment=MOMENT)
    assert batch.status == "completed"
    assert batch.succeeded == 1  # 仅 100 天前的到期（30 天高优先级策略）

    db.refresh(old)
    db.refresh(fresh)
    assert old.retention_state == RecordState.ARCHIVED.value
    assert old.retention_rule_id == "privacy-strict"
    assert fresh.retention_state == RecordState.ACTIVE.value

    projection = svc.projected_state(db, fresh, MOMENT)
    assert projection["rule_id"] == "privacy-strict"


def test_global_policy_used_when_no_scene_specific(db, resources):
    robot, scene, scene2, skill = resources
    svc.create_policy(db, {"rule_id": "global", "name": "global", "scene_id": None,
                           "keep_days": 10, "priority": 0})
    old = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=40))
    db.commit()
    batch = svc.run_archive_batch(db, batch_key="g1", moment=MOMENT)
    assert batch.succeeded == 1
    db.refresh(old)
    assert old.retention_rule_id == "global"


# ---------------------------------------------------------------------------
# 2. 重叠冻结：单条作业冻结 + 数据集版本冻结
# ---------------------------------------------------------------------------


def test_overlapping_holds_until_all_released(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    old = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    db.commit()

    # 数据集版本冻结覆盖该作业（数据集本身已驳回，非活动，隔离“冻结”这一变量）。
    dataset, version = make_dataset(db, robot, scene, skill, [old], review_status="rejected")
    db.commit()
    ds_hold = svc.open_hold(db, {"scope": "dataset_version", "dataset_id": dataset.id,
                                 "dataset_version_id": version.id, "reason": "数据集争议",
                                 "requested_by": "法务甲", "hold_id": "hold-ds",
                                 "opened_at": MOMENT - timedelta(days=10)})
    # 单条作业冻结叠加（重叠）。
    svc.open_hold(db, {"scope": "operation", "operation_data_id": old.id,
                       "reason": "单条争议", "requested_by": "法务乙",
                       "hold_id": "hold-op",
                       "opened_at": MOMENT - timedelta(days=5)})
    assert old.id in ds_hold.covered_operation_ids

    batch = svc.run_archive_batch(db, batch_key="h1", moment=MOMENT)
    assert batch.skipped_held == 1
    assert batch.succeeded == 0
    db.refresh(old)
    assert old.motion_trajectory is not None  # 载荷未被清理

    # 只解除数据集冻结：仍被单条冻结覆盖。
    svc.release_hold(db, "hold-ds", {"released_by": "法务甲", "closed_at": MOMENT})
    batch = svc.run_archive_batch(db, batch_key="h2", moment=MOMENT)
    assert batch.skipped_held == 1 and batch.succeeded == 0

    # 解除全部冻结后可归档。
    svc.release_hold(db, "hold-op", {"released_by": "法务乙", "closed_at": MOMENT})
    batch = svc.run_archive_batch(db, batch_key="h3", moment=MOMENT)
    assert batch.succeeded == 1


def test_dataset_version_freeze_snapshot_is_stable(db, resources):
    """冻结开启后新增的数据集成员不受该冻结影响（快照语义）。"""
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    member = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    later = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    dataset, version = make_dataset(db, robot, scene, skill, [member], review_status="approved",
                                    published=True)
    db.commit()
    hold = svc.open_hold(db, {"scope": "dataset_version", "dataset_id": dataset.id,
                              "reason": "调查", "requested_by": "法务", "hold_id": "snap"})
    # 冻结后把 later 加入数据集（新版本）。
    db.add(DatasetItem(dataset_id=dataset.id, operation_data_id=later.id))
    db.commit()
    assert member.id in hold.covered_operation_ids
    assert later.id not in hold.covered_operation_ids


# ---------------------------------------------------------------------------
# 活动数据集成员保护
# ---------------------------------------------------------------------------


def test_active_dataset_member_is_not_silently_broken(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    op = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    make_dataset(db, robot, scene, skill, [op], review_status="draft")
    db.commit()

    batch = svc.run_archive_batch(db, batch_key="ds-protect", moment=MOMENT)
    assert batch.skipped_dataset == 1
    assert batch.succeeded == 0
    db.refresh(op)
    assert op.motion_trajectory is not None  # 成员载荷完好
    assert op.retention_state == RecordState.ACTIVE.value

    # 数据集成员关系仍然存在
    assert db.query(DatasetItem).filter(DatasetItem.operation_data_id == op.id).count() == 1


# ---------------------------------------------------------------------------
# 3. 批次部分失败
# ---------------------------------------------------------------------------


def test_batch_partial_failure_then_retry_converges(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    ops = [make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100 + i))
           for i in range(3)]
    db.commit()
    target = ops[1].id

    def fail_middle(op):
        if op.id == target:
            raise RuntimeError("模拟存储故障")

    batch = svc.run_archive_batch(db, batch_key="partial", moment=MOMENT, item_failure=fail_middle)
    assert batch.status == "completed_with_errors"
    assert batch.succeeded == 2
    assert batch.failed == 1

    failed_item = (
        db.query(RetentionBatchItem)
        .filter(RetentionBatchItem.batch_id == batch.id,
                RetentionBatchItem.operation_data_id == target)
        .one()
    )
    assert failed_item.state == BatchItemState.FAILED.value
    db.refresh(ops[1])
    assert ops[1].motion_trajectory is not None  # 失败项未被破坏
    for other in (ops[0], ops[2]):
        db.refresh(other)
        assert other.motion_trajectory is None  # 其余项正常归档

    # 同一批次重跑：失败项重试，批次收敛为 completed。
    batch = svc.run_archive_batch(db, batch_key="partial", moment=MOMENT)
    assert batch.status == "completed"
    assert batch.succeeded == 3
    assert batch.failed == 0
    # 没有重复归档记录
    assert db.query(ArchivedRecord).count() == 3


# ---------------------------------------------------------------------------
# 4. 解除冻结后从原到期点继续（不重新计算期限）
# ---------------------------------------------------------------------------


def test_release_resumes_from_original_due_point_not_restarted(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    created = MOMENT - timedelta(days=100)
    op = make_operation(db, robot, scene, skill, created_at=created)
    db.commit()

    hold = svc.open_hold(db, {"scope": "operation", "operation_data_id": op.id,
                              "reason": "调查", "requested_by": "法务", "hold_id": "pause",
                              "opened_at": MOMENT - timedelta(days=20)})
    db.commit()

    # 冻结中：不可归档。
    assert svc.run_archive_batch(db, batch_key="p1", moment=MOMENT).skipped_held == 1

    svc.release_hold(db, "pause", {"released_by": "法务", "closed_at": MOMENT})
    db.refresh(hold)
    assert hold.status == "released" and hold.closed_at is not None

    explanation = svc.explain_operation(db, op.id, MOMENT)
    # 原到期点 = created+30（约 70 天前），叠加 20 天暂停 => 约 50 天前到期，
    # 而不是从解除时刻重新给 30 天（那样会落到未来）。
    due = datetime.fromisoformat(explanation["due_at"])
    days_overdue = (MOMENT - due).total_seconds() / 86400
    assert 49 < days_overdue < 51
    assert explanation["paused_seconds"] == int(timedelta(days=20).total_seconds())
    assert explanation["state"] == "eligible"

    batch = svc.run_archive_batch(db, batch_key="p2", moment=MOMENT)
    assert batch.succeeded == 1
    db.refresh(op)
    assert op.retention_state == RecordState.ARCHIVED.value


def test_hold_release_is_idempotent(db, resources):
    robot, scene, scene2, skill = resources
    op = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    db.commit()
    svc.open_hold(db, {"scope": "operation", "operation_data_id": op.id,
                       "reason": "x", "requested_by": "y", "hold_id": "idem"})
    first = svc.release_hold(db, "idem")
    second = svc.release_hold(db, "idem")
    assert first.closed_at == second.closed_at


# ---------------------------------------------------------------------------
# 5. 重启恢复 + 批次幂等
# ---------------------------------------------------------------------------


def test_crash_recovery_resumes_from_checkpoint(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    ops = [make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=200 + i))
           for i in range(3)]
    db.commit()
    target = ops[1].id
    calls = {"n": 0}

    def crash_on_second(op):
        if op.id == target:
            calls["n"] += 1
            raise RuntimeError("进程崩溃")

    # 第一次运行：归档第 2 项的检查点已提交后崩溃。
    with pytest.raises(RuntimeError):
        svc.run_archive_batch(db, batch_key="restart", moment=MOMENT, crash_after=crash_on_second)
    db.close()

    # 模拟重启：使用全新会话续跑同一批次键。
    db2 = SessionLocal()
    try:
        stale = db2.query(RetentionBatch).filter(RetentionBatch.batch_key == "restart").one()
        assert stale.status == "running"
        assert stale.finished_at is None

        recovered = svc.run_archive_batch(db2, batch_key="restart", moment=MOMENT)
        assert recovered.status == "completed"
        assert recovered.succeeded == 3
        assert recovered.failed == 0
        # 崩溃后只重试后续/未完成项，已提交的第一项不重复归档。
        assert db2.query(ArchivedRecord).count() == 3
        assert calls["n"] == 1  # 重跑时第一项被检查点跳过，崩溃钩子不再触发
    finally:
        db2.close()


def test_completed_batch_replay_is_idempotent(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    op = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    db.commit()

    first = svc.run_archive_batch(db, batch_key="once", moment=MOMENT)
    assert first.succeeded == 1
    db.refresh(op)
    assert op.motion_trajectory is None

    # 再次执行同批次：直接返回既有结果，不产生新记录。
    second = svc.run_archive_batch(db, batch_key="once", moment=MOMENT)
    assert second.id == first.id
    assert second.status == "completed"
    assert db.query(ArchivedRecord).count() == 1
    items = db.query(RetentionBatchItem).filter(RetentionBatchItem.batch_id == first.id).count()
    assert items == 1


def test_dry_run_does_not_touch_payloads(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    op = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    db.commit()
    batch = svc.run_archive_batch(db, batch_key="dry", moment=MOMENT, dry_run=True)
    assert batch.status == "dry_run"
    item = db.query(RetentionBatchItem).filter(RetentionBatchItem.batch_id == batch.id).one()
    assert item.state == BatchItemState.WOULD_ARCHIVE.value
    db.refresh(op)
    assert op.retention_state == RecordState.ACTIVE.value
    assert op.motion_trajectory is not None


# ---------------------------------------------------------------------------
# 归档内容：摘要/指纹/审计引用，受限载荷移除
# ---------------------------------------------------------------------------


def test_archive_keeps_summary_digests_and_audit_ref(db, resources):
    robot, scene, scene2, skill = resources
    _policy(db, scene, 30)
    op = make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100))
    db.add(Annotation(operation_data_id=op.id, is_success=False, failure_category="抓取异常",
                      failure_subcategory="物体滑脱", review_status="approved"))
    db.commit()
    svc.run_archive_batch(db, batch_key="arc", moment=MOMENT)

    record = db.query(ArchivedRecord).filter(ArchivedRecord.operation_data_id == op.id).one()
    assert record.archive_ref.startswith(f"arc-{op.id}-")
    assert record.rule_id == "r1"
    digest_fields = {d["field"] for d in record.payload_digests}
    assert {"motion_trajectory", "perception_records"} <= digest_fields
    assert record.summary["annotation"]["failure_category"] == "抓取异常"
    assert record.summary["perception_counts"]["detection_count"] == 1

    db.refresh(op)
    assert op.motion_trajectory is None
    assert op.perception_records is None
    assert op.grasp_result is None
    # 统计/元数据保留
    assert op.quality_score == 0.9 and op.data_grade == "A"
