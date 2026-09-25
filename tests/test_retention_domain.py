"""保留/冻结纯领域逻辑测试。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.retention import (
    BatchItemState,
    Hold,
    RecordState,
    RetentionError,
    RetentionRule,
    SubjectContext,
    build_archive_summary,
    build_payload_digests,
    choose_rule,
    evaluate_subject,
    explain_subject,
    is_past_due,
    merge_intervals,
    paused_duration,
    plan_batch,
    redact_payload,
    resume_due_at,
    validate_hold_intervals,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def rule(rule_id: str, keep_days: int, priority: int = 0, category: str = "scene-1"):
    return RetentionRule(rule_id=rule_id, category=category, keep_for=timedelta(days=keep_days), priority=priority)


# ---------------------------------------------------------------------------
# 策略优先级
# ---------------------------------------------------------------------------


def test_higher_priority_rule_wins_even_with_shorter_retention():
    rules = [rule("baseline", keep_days=365, priority=0), rule("privacy", keep_days=30, priority=10)]
    chosen = choose_rule(rules, "scene-1")
    assert chosen.rule_id == "privacy"


def test_disabled_rule_never_chosen_and_tie_prefers_longer():
    rules = [
        RetentionRule("off", "scene-1", timedelta(days=1), priority=100, enabled=False),
        rule("a", keep_days=10, priority=5),
        rule("b", keep_days=20, priority=5),
    ]
    assert choose_rule(rules, "scene-1").rule_id == "b"


def test_rule_validation():
    with pytest.raises(RetentionError):
        RetentionRule("x", "c", timedelta(days=0)).validate()
    with pytest.raises(RetentionError):
        RetentionRule("x", "c", timedelta(days=1), priority=-1).validate()


# ---------------------------------------------------------------------------
# 冻结区间与暂停计时（重叠不重复计算）
# ---------------------------------------------------------------------------


def test_overlapping_holds_are_merged_not_double_counted():
    holds = [
        Hold("h1", "op-1", T0 + timedelta(days=5), "案件A", closed_at=T0 + timedelta(days=20)),
        Hold("h2", "op-1", T0 + timedelta(days=10), "案件B", closed_at=T0 + timedelta(days=25)),
    ]
    # 区间并集为 [day5, day25)，共 20 天，而不是 15+15=30 天。
    assert paused_duration(holds, T0, T0 + timedelta(days=40)) == timedelta(days=20)


def test_merge_intervals_handles_adjacent_and_nested():
    intervals = [
        (T0 + timedelta(days=1), T0 + timedelta(days=3)),
        (T0 + timedelta(days=2), T0 + timedelta(days=4)),
        (T0 + timedelta(days=10), T0 + timedelta(days=11)),
    ]
    merged = merge_intervals(intervals)
    assert merged == [(T0 + timedelta(days=1), T0 + timedelta(days=4)),
                      (T0 + timedelta(days=10), T0 + timedelta(days=11))]


def test_resume_continues_from_original_due_point_not_restarted():
    """保留期 30 天，冻结暂停 20 天（重叠并集），到期点顺延为 day50 而非重新起算。"""
    holds = [
        Hold("h1", "op-1", T0 + timedelta(days=5), "案件A", closed_at=T0 + timedelta(days=20)),
        Hold("h2", "op-1", T0 + timedelta(days=10), "案件B", closed_at=T0 + timedelta(days=25)),
    ]
    due = resume_due_at(T0, timedelta(days=30), holds, T0 + timedelta(days=40))
    assert due == T0 + timedelta(days=50)
    # day45：45 - 20 = 25 < 30，尚未到期
    assert is_past_due(T0, timedelta(days=30), holds, T0 + timedelta(days=45)) is False
    # day50：50 - 20 = 30，到期
    assert is_past_due(T0, timedelta(days=30), holds, T0 + timedelta(days=50)) is True


def test_due_point_unknown_while_hold_still_active():
    holds = [Hold("h1", "op-1", T0 + timedelta(days=5), "案件A")]
    assert resume_due_at(T0, timedelta(days=30), holds, T0 + timedelta(days=40)) is None
    assert is_past_due(T0, timedelta(days=30), holds, T0 + timedelta(days=400)) is False


def test_hold_opened_after_natural_expiry_blocks_archive():
    """记录自然到期后才开启的冻结仍然阻止归档。"""
    ctx = SubjectContext("op-1", "scene-1", T0)
    holds = [Hold("h1", "op-1", T0 + timedelta(days=100), "争议调查")]
    decision = evaluate_subject(ctx, [rule("r", 30)], holds, T0 + timedelta(days=200))
    assert decision.state == RecordState.HELD
    assert decision.hold_ids == ("h1",)


def test_overlapping_hold_intervals_are_valid():
    # 多重冻结（区间重叠）是合法场景，旧版校验会拒绝，现必须放行。
    holds = [
        Hold("h1", "op-1", T0, "案件A", closed_at=T0 + timedelta(days=10)),
        Hold("h2", "op-1", T0 + timedelta(days=5), "案件B", closed_at=T0 + timedelta(days=15)),
    ]
    validated = validate_hold_intervals(holds)
    assert {hold.hold_id for hold in validated} == {"h1", "h2"}


def test_hold_requires_timezone_and_order():
    with pytest.raises(RetentionError):
        Hold("h", "op", datetime(2026, 1, 1), "原因").active_at(T0)
    with pytest.raises(RetentionError):
        Hold("h", "op", T0 + timedelta(days=10), "原因", closed_at=T0).validate()


# ---------------------------------------------------------------------------
# 数据集保护与批次计划
# ---------------------------------------------------------------------------


def test_active_dataset_member_is_protected_even_when_expired():
    ctx = SubjectContext(
        "op-1", "scene-1", T0,
        active_dataset_refs=frozenset({"dataset:7@v1"}),
    )
    decision = evaluate_subject(ctx, [rule("r", 30)], [], T0 + timedelta(days=100))
    assert decision.state == RecordState.ACTIVE
    assert "活动数据集" in decision.reason


def test_plan_batch_partitions_all_outcomes():
    contexts = [
        SubjectContext("op-held", "scene-1", T0),
        SubjectContext("op-ds", "scene-1", T0, active_dataset_refs=frozenset({"d:1@v1"})),
        SubjectContext("op-expired", "scene-1", T0),
        SubjectContext("op-fresh", "scene-1", T0 + timedelta(days=90)),
        SubjectContext("op-archived", "scene-1", T0, already_archived=True),
    ]
    holds = [Hold("h1", "op-held", T0 - timedelta(days=1), "争议")]
    outcomes = plan_batch(contexts, [rule("r", 30)], holds, T0 + timedelta(days=100))
    by_id = {item.subject_id: item.state for item in outcomes}
    assert by_id["op-held"] == BatchItemState.HELD
    assert by_id["op-ds"] == BatchItemState.SKIPPED_DATASET
    assert by_id["op-expired"] == BatchItemState.ARCHIVED
    assert by_id["op-fresh"] == BatchItemState.ACTIVE
    assert by_id["op-archived"] == BatchItemState.ARCHIVED


# ---------------------------------------------------------------------------
# 归档载荷：摘要/指纹/移除
# ---------------------------------------------------------------------------

PAYLOAD = {
    "motion_trajectory": {"waypoints": [{"x": 1}, {"x": 2}], "joint_angles": [[1, 2]]},
    "perception_records": {"camera_images_captured": 9, "depth_frames": 4,
                           "detections": [{"object_id": "a"}, {"object_id": "b"}]},
    "grasp_result": {"success": False},
    "environment_conditions": {"temperature_c": 41.2},
    "hardware_status": {"cpu_usage_percent": 70},
    "robot_model_id": 3,
    "scene_id": 1,
    "skill_id": 2,
    "duration_ms": 5000,
    "quality_score": 0.8,
    "data_grade": "B",
    "created_at": T0.isoformat(),
    "timestamp_start": T0.isoformat(),
    "timestamp_end": (T0 + timedelta(seconds=5)).isoformat(),
}


def test_redaction_removes_restricted_payload_but_keeps_metadata():
    redacted = redact_payload(PAYLOAD)
    for field_name in ("motion_trajectory", "perception_records", "grasp_result",
                       "environment_conditions", "hardware_status"):
        assert redacted[field_name] is None
    assert redacted["quality_score"] == 0.8
    assert redacted["scene_id"] == 1


def test_digests_are_stable_and_summary_has_stats_without_raw_payload():
    digests = build_payload_digests(PAYLOAD)
    again = build_payload_digests(PAYLOAD)
    assert [d.digest for d in digests] == [d.digest for d in again]
    assert {d.field for d in digests} >= {"motion_trajectory", "perception_records"}
    assert all(d.byte_size > 0 and d.algorithm == "sha256" for d in digests)

    summary = build_archive_summary(PAYLOAD, {"is_success": False, "failure_category": "抓取异常",
                                             "failure_subcategory": "物体滑脱",
                                             "review_status": "approved",
                                             "annotation_quality_score": 0.9})
    assert summary["trajectory_waypoint_count"] == 2
    assert summary["perception_counts"]["detection_count"] == 2
    assert summary["grasp_success"] is False
    assert summary["annotation"]["failure_category"] == "抓取异常"
    # 摘要中不得包含原始轨迹/感知载荷
    assert "waypoints" not in summary
    assert "joint_angles" not in summary
    assert "camera_images_captured" not in summary


def test_explain_reports_why_record_still_exists():
    ctx = SubjectContext("op-1", "scene-1", T0, active_dataset_refs=frozenset({"d:9@v2"}))
    explanation = explain_subject(ctx, [rule("r", 30)], [], T0 + timedelta(days=100))
    assert explanation["state"] == "active"
    assert explanation["rule_id"] == "r"
    assert explanation["active_dataset_refs"] == ["d:9@v2"]
    assert explanation["due_at"] is not None
