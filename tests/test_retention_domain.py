"""保留领域：策略优先级、重叠冻结、解除后按原到期点判断。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.retention import (
    WILDCARD_CATEGORY,
    Hold,
    RecordState,
    RetentionCandidate,
    RetentionRule,
    decide_candidate,
    next_due_at,
    select_policy,
    validate_hold_intervals,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _rule(rule_id, category, days, priority=0):
    return RetentionRule(rule_id=rule_id, category=category, keep_for=timedelta(days=days), priority=priority)


def test_scene_policy_overrides_global_by_priority():
    rules = [
        _rule("global", WILDCARD_CATEGORY, 30, priority=0),
        _rule("scene-long", "7", 90, priority=0),
        _rule("scene-short", "7", 7, priority=10),
    ]
    # 高优先级场景短保留策略胜出
    rule, level = select_policy(rules, "7")
    assert rule.rule_id == "scene-short"
    assert level == "scene"

    # 全局策略优先级更高时覆盖场景策略
    rules2 = [
        _rule("global", WILDCARD_CATEGORY, 365, priority=100),
        _rule("scene-long", "7", 90, priority=0),
    ]
    rule, level = select_policy(rules2, "7")
    assert rule.rule_id == "global"
    assert level == "global"

    # 没有场景策略时使用全局兜底
    rule, level = select_policy(rules, "99")
    assert rule.rule_id == "global"
    assert level == "global"

    # 完全没有策略
    rule, level = select_policy([], "99")
    assert rule is None and level == "none"


def test_priority_tie_prefers_longer_retention_then_scene():
    created = T0
    candidate = RetentionCandidate("op:1", "7", created)
    rules = [
        _rule("global", WILDCARD_CATEGORY, 30, priority=0),
        _rule("scene", "7", 90, priority=0),
    ]
    # 同优先级：保留更久者胜（场景 90 天）
    decision = decide_candidate(candidate, rules, [], T0 + timedelta(days=40))
    assert decision.state == RecordState.ACTIVE  # 90 天尚未到期
    decision = decide_candidate(candidate, rules, [], T0 + timedelta(days=95))
    assert decision.state == RecordState.ELIGIBLE
    assert decision.rule_id == "scene"


def test_overlapping_holds_are_allowed_and_block():
    created = T0 - timedelta(days=400)
    candidate = RetentionCandidate("op:1", "7", created)
    rules = [_rule("r", "7", 30)]
    hold_a = Hold("case-a", "op:1", T0 - timedelta(days=10), "争议A")
    hold_b = Hold("case-b", "op:1", T0 - timedelta(days=5), "争议B")
    validate_hold_intervals([hold_a, hold_b])  # 重叠合法，不抛异常

    decision = decide_candidate(candidate, rules, [hold_a, hold_b], T0)
    assert decision.state == RecordState.HELD


def test_release_keeps_held_while_any_hold_remains():
    created = T0 - timedelta(days=400)
    candidate = RetentionCandidate("op:1", "7", created)
    rules = [_rule("r", "7", 30)]
    hold_a = Hold("case-a", "op:1", T0 - timedelta(days=10), "争议A")
    hold_b_open = Hold("case-b", "op:1", T0 - timedelta(days=5), "争议B")
    hold_b = Hold("case-b", "op:1", T0 - timedelta(days=5), "争议B", closed_at=T0 + timedelta(days=1))

    # 解除 B，但 A 仍有效 -> 继续冻结
    decision = decide_candidate(candidate, rules, [hold_a, hold_b], T0 + timedelta(days=2))
    assert decision.state == RecordState.HELD

    hold_a_closed = Hold("case-a", "op:1", T0 - timedelta(days=10), "争议A", closed_at=T0 + timedelta(days=3))
    # 全部解除后 -> 立即按原始到期点判定为可归档（不重新计算期限）
    decision = decide_candidate(candidate, rules, [hold_a_closed, hold_b], T0 + timedelta(days=4))
    assert decision.state == RecordState.ELIGIBLE
    assert next_due_at(candidate, rules) == created + timedelta(days=30)


def test_original_due_point_not_extended_by_hold():
    created = T0
    candidate = RetentionCandidate("op:1", "7", created)
    rules = [_rule("r", "7", 30)]
    original_due = next_due_at(candidate, rules)

    # 冻结覆盖整个到期点，第 60 天才解除
    hold = Hold("case-a", "op:1", T0 + timedelta(days=10), "争议", closed_at=T0 + timedelta(days=60))
    # 冻结中仍 HELD
    assert decide_candidate(candidate, rules, [hold], T0 + timedelta(days=40)).state == RecordState.HELD
    # 解除后从原到期点判断：立刻 ELIGIBLE，而不是再保留 30 天
    decision = decide_candidate(candidate, rules, [hold], T0 + timedelta(days=61))
    assert decision.state == RecordState.ELIGIBLE
    assert next_due_at(candidate, rules) == original_due == created + timedelta(days=30)
