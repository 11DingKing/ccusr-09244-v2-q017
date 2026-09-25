"""数据保留、法律冻结和归档判定的纯领域实现。

本模块不依赖数据库或 Web 框架，所有判定都是确定性的纯函数，便于单测和审计。

关键语义：
- 策略按场景（category）选择，priority 高者优先，同优先级取保留期更长者；
- 冻结可以覆盖单条作业，也可以覆盖整个数据集版本（由服务层展开到成员）；
- 冻结会暂停保留计时，解除后从原到期点继续，重叠冻结不重复计时；
- 归档只允许作用于“到期、无有效冻结、且不属于活动数据集版本”的记录。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping


class RetentionError(ValueError):
    """保留策略参数不合法。"""


class RecordState(str, Enum):
    ACTIVE = "active"
    HELD = "held"
    ELIGIBLE = "eligible"
    ARCHIVED = "archived"


# 批次内单条记录的处理结果（比 RecordState 更细，包含跳过原因）。
class BatchItemState(str, Enum):
    ARCHIVED = "archived"
    WOULD_ARCHIVE = "would_archive"
    HELD = "held"
    SKIPPED_DATASET = "skipped_dataset"
    ACTIVE = "active"
    FAILED = "failed"


SCOPE_OPERATION = "operation"
SCOPE_DATASET_VERSION = "dataset_version"

VALID_SCOPES = {SCOPE_OPERATION, SCOPE_DATASET_VERSION}

# 归档时必须移除的受限载荷字段。
RESTRICTED_PAYLOAD_FIELDS: tuple[str, ...] = (
    "motion_trajectory",
    "perception_records",
    "grasp_result",
    "environment_conditions",
    "hardware_status",
)

# 归档后仍保留的统计/元数据字段。
SUMMARY_METADATA_FIELDS: tuple[str, ...] = (
    "robot_model_id",
    "scene_id",
    "skill_id",
    "robot_serial",
    "timestamp_start",
    "timestamp_end",
    "duration_ms",
    "quality_score",
    "completeness_score",
    "data_grade",
    "created_at",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise RetentionError("时间必须带时区")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class RetentionRule:
    rule_id: str
    category: str
    keep_for: timedelta
    priority: int = 0
    enabled: bool = True

    def validate(self) -> "RetentionRule":
        if not self.rule_id.strip() or not self.category.strip():
            raise RetentionError("规则标识和类别不能为空")
        if self.keep_for <= timedelta(0):
            raise RetentionError("保留时长必须为正")
        if self.priority < 0:
            raise RetentionError("优先级不能为负")
        return self


@dataclass(frozen=True)
class Hold:
    """一次法律冻结。

    subject_id 是被覆盖对象的稳定标识；scope/target_ref 描述冻结来源
    （单条作业或某个数据集版本，后者会覆盖版本下全部成员）。
    """

    hold_id: str
    subject_id: str
    opened_at: datetime
    reason: str
    closed_at: datetime | None = None
    scope: str = SCOPE_OPERATION
    target_ref: str | None = None

    def active_at(self, moment: datetime) -> bool:
        current = _utc(moment)
        opened = _utc(self.opened_at)
        closed = _utc(self.closed_at) if self.closed_at else None
        return opened <= current and (closed is None or current < closed)

    def validate(self) -> "Hold":
        if not self.hold_id.strip() or not self.subject_id.strip():
            raise RetentionError("冻结需要标识和对象")
        if not self.reason.strip():
            raise RetentionError("冻结原因不能为空")
        if self.scope not in VALID_SCOPES:
            raise RetentionError("冻结范围无效")
        _utc(self.opened_at)
        if self.closed_at:
            if _utc(self.closed_at) < _utc(self.opened_at):
                raise RetentionError("冻结结束时间早于开始时间")
        return self


@dataclass(frozen=True)
class RetentionCandidate:
    subject_id: str
    category: str
    created_at: datetime
    state: RecordState = RecordState.ACTIVE
    rule_id: str | None = None
    hold_ids: tuple[str, ...] = ()

    def age_at(self, moment: datetime) -> timedelta:
        return _utc(moment) - _utc(self.created_at)


@dataclass(frozen=True)
class ArchiveDecision:
    subject_id: str
    state: RecordState
    rule_id: str | None
    reason: str
    effective_at: datetime
    hold_ids: tuple[str, ...] = ()
    due_at: datetime | None = None


@dataclass(frozen=True)
class SubjectContext:
    """评估单条记录所需的全部运行时上下文。"""

    subject_id: str
    category: str
    created_at: datetime
    already_archived: bool = False
    # 该记录所属的“活动数据集版本”标识集合（草稿/待审/已发布且数据集存活）。
    active_dataset_refs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class BatchItemOutcome:
    subject_id: str
    state: BatchItemState
    reason: str
    rule_id: str | None = None
    hold_ids: tuple[str, ...] = ()
    due_at: datetime | None = None


@dataclass(frozen=True)
class PayloadDigest:
    field: str
    algorithm: str
    digest: str
    byte_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "algorithm": self.algorithm,
            "digest": self.digest,
            "byte_size": self.byte_size,
        }


def validate_rules(rules: Iterable[RetentionRule]) -> tuple[RetentionRule, ...]:
    checked = tuple(rule.validate() for rule in rules)
    if len({rule.rule_id for rule in checked}) != len(checked):
        raise RetentionError("规则标识必须唯一")
    return checked


def choose_rule(rules: Iterable[RetentionRule], category: str) -> RetentionRule | None:
    """同类别允许配置多条策略：priority 高者优先，其次保留期更长者。"""
    candidates = [rule for rule in rules if rule.enabled and rule.category == category]
    return max(candidates, key=lambda rule: (rule.priority, rule.keep_for), default=None)


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """对带时区的闭开区间做并集，重叠或相邻区间合并。"""
    ordered = sorted((_utc(start), _utc(end)) for start, end in intervals)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def holds_for_subject(holds: Iterable[Hold], subject_id: str) -> list[Hold]:
    return sorted(
        (hold for hold in holds if hold.subject_id == subject_id),
        key=lambda item: (item.opened_at, item.hold_id),
    )


def active_holds_for(holds: Iterable[Hold], subject_id: str, moment: datetime) -> list[Hold]:
    current = _utc(moment)
    return [hold for hold in holds_for_subject(holds, subject_id) if hold.active_at(current)]


def paused_duration(
    holds: Iterable[Hold],
    start: datetime,
    moment: datetime,
) -> timedelta:
    """返回 [start, moment] 内被冻结覆盖（区间取并集）的总时长。

    重叠冻结只计一次，避免重复暂停计时。
    """
    start_utc = _utc(start)
    end_utc = _utc(moment)
    if end_utc <= start_utc:
        return timedelta(0)
    intervals: list[tuple[datetime, datetime]] = []
    for hold in holds:
        opened = _utc(hold.opened_at)
        closed = _utc(hold.closed_at) if hold.closed_at else end_utc
        left = max(opened, start_utc)
        right = min(closed, end_utc)
        if right > left:
            intervals.append((left, right))
    return sum((right - left for left, right in merge_intervals(intervals)), timedelta(0))


def is_past_due(
    created_at: datetime,
    keep_for: timedelta,
    holds: Iterable[Hold],
    moment: datetime,
) -> bool:
    """扣除冻结暂停时间后，判断保留期是否已届满。"""
    created = _utc(created_at)
    current = _utc(moment)
    elapsed = current - created
    if elapsed < timedelta(0):
        return False
    effective = elapsed - paused_duration(holds, created, current)
    return effective >= keep_for


def resume_due_at(
    created_at: datetime,
    keep_for: timedelta,
    holds: Iterable[Hold],
    moment: datetime,
) -> datetime | None:
    """解除冻结后继续计时的到期点。

    仍存在有效（未关闭）冻结时返回 None；否则在原到期点上叠加历次冻结
    （含区间重叠，按并集计算）占用的时长。
    """
    current = _utc(moment)
    subject_holds = list(holds)
    if any(hold.active_at(current) for hold in subject_holds):
        return None
    created = _utc(created_at)
    total_pause = paused_duration(subject_holds, created, current)
    return created + keep_for + total_pause


def evaluate_subject(
    ctx: SubjectContext,
    rules: Iterable[RetentionRule],
    holds: Iterable[Hold],
    moment: datetime,
) -> ArchiveDecision:
    """对单条记录做完整的保留判定（优先级、冻结、数据集保护、到期）。"""
    current = _utc(moment)
    if ctx.already_archived:
        return ArchiveDecision(ctx.subject_id, RecordState.ARCHIVED, None, "已归档", current)

    subject_holds = holds_for_subject(holds, ctx.subject_id)
    active = [hold for hold in subject_holds if hold.active_at(current)]
    if active:
        return ArchiveDecision(
            ctx.subject_id,
            RecordState.HELD,
            None,
            "存在有效法律冻结",
            current,
            hold_ids=tuple(hold.hold_id for hold in active),
        )

    rule = choose_rule(rules, ctx.category)
    if rule is None:
        return ArchiveDecision(ctx.subject_id, RecordState.ACTIVE, None, "没有适用策略", current)

    due = resume_due_at(ctx.created_at, rule.keep_for, subject_holds, current)
    if ctx.active_dataset_refs:
        return ArchiveDecision(
            ctx.subject_id,
            RecordState.ACTIVE,
            rule.rule_id,
            "属于活动数据集版本，受保护",
            current,
            due_at=due,
        )
    if is_past_due(ctx.created_at, rule.keep_for, subject_holds, current):
        return ArchiveDecision(
            ctx.subject_id,
            RecordState.ELIGIBLE,
            rule.rule_id,
            "达到保留期限",
            current,
            due_at=due,
        )
    return ArchiveDecision(
        ctx.subject_id,
        RecordState.ACTIVE,
        rule.rule_id,
        "尚未达到期限",
        current,
        due_at=due,
    )


def decide_candidate(
    candidate: RetentionCandidate,
    rules: Iterable[RetentionRule],
    holds: Iterable[Hold],
    moment: datetime,
) -> ArchiveDecision:
    """向后兼容的便捷封装：不带数据集保护上下文。"""
    if moment.tzinfo is None:
        raise RetentionError("判断时间必须带时区")
    ctx = SubjectContext(
        subject_id=candidate.subject_id,
        category=candidate.category,
        created_at=candidate.created_at,
        already_archived=candidate.state == RecordState.ARCHIVED,
    )
    return evaluate_subject(ctx, rules, holds, moment)


def plan_batch(
    contexts: Iterable[SubjectContext],
    rules: Iterable[RetentionRule],
    holds: Iterable[Hold],
    moment: datetime,
) -> list[BatchItemOutcome]:
    """对一批记录生成确定性的处理计划（不执行任何副作用）。"""
    rules = tuple(rules)
    holds = tuple(holds)
    outcomes: list[BatchItemOutcome] = []
    for ctx in contexts:
        decision = evaluate_subject(ctx, rules, holds, moment)
        if decision.state == RecordState.ARCHIVED:
            outcomes.append(BatchItemOutcome(ctx.subject_id, BatchItemState.ARCHIVED, "已归档", decision.rule_id))
        elif decision.state == RecordState.HELD:
            outcomes.append(BatchItemOutcome(
                ctx.subject_id, BatchItemState.HELD, decision.reason,
                hold_ids=decision.hold_ids, due_at=decision.due_at,
            ))
        elif decision.state == RecordState.ELIGIBLE:
            outcomes.append(BatchItemOutcome(
                ctx.subject_id, BatchItemState.ARCHIVED, decision.reason,
                rule_id=decision.rule_id, due_at=decision.due_at,
            ))
        elif ctx.active_dataset_refs:
            outcomes.append(BatchItemOutcome(
                ctx.subject_id, BatchItemState.SKIPPED_DATASET,
                "属于活动数据集版本，跳过清理",
                rule_id=decision.rule_id, due_at=decision.due_at,
            ))
        else:
            outcomes.append(BatchItemOutcome(
                ctx.subject_id, BatchItemState.ACTIVE, decision.reason,
                rule_id=decision.rule_id, due_at=decision.due_at,
            ))
    return sorted(outcomes, key=lambda item: item.subject_id)


# ---------------------------------------------------------------------------
# 载荷归档：摘要 + 指纹 + 审计引用
# ---------------------------------------------------------------------------


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_payload(field: str, value: Any) -> PayloadDigest:
    raw = _canonical_bytes(value)
    return PayloadDigest(
        field=field,
        algorithm="sha256",
        digest=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )


def build_payload_digests(payload: Mapping[str, Any]) -> tuple[PayloadDigest, ...]:
    """为每个受限载荷生成移除前指纹，供归档后审计核验。"""
    return tuple(
        digest_payload(field, payload[field])
        for field in RESTRICTED_PAYLOAD_FIELDS
        if payload.get(field) is not None
    )


def _annotation_summary(annotation: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not annotation:
        return None
    return {
        "is_success": annotation.get("is_success"),
        "failure_category": annotation.get("failure_category"),
        "failure_subcategory": annotation.get("failure_subcategory"),
        "review_status": annotation.get("review_status"),
        "annotation_quality_score": annotation.get("annotation_quality_score"),
    }


def build_archive_summary(
    payload: Mapping[str, Any],
    annotation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """归档后保留的统计所需摘要：元数据、评分、计数，不含原始受限载荷。"""
    summary = {field: payload.get(field) for field in SUMMARY_METADATA_FIELDS if field in payload}
    perception = payload.get("perception_records")
    if isinstance(perception, Mapping):
        summary["perception_counts"] = {
            "camera_images_captured": perception.get("camera_images_captured"),
            "depth_frames": perception.get("depth_frames"),
            "detection_count": len(perception.get("detections") or []),
        }
    trajectory = payload.get("motion_trajectory")
    if isinstance(trajectory, Mapping):
        waypoints = trajectory.get("waypoints") or []
        summary["trajectory_waypoint_count"] = len(waypoints)
    grasp = payload.get("grasp_result")
    if isinstance(grasp, Mapping):
        summary["grasp_success"] = grasp.get("success")
    summary["annotation"] = _annotation_summary(annotation)
    return summary


def redact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """返回受限字段被清空后的载荷（由服务层持久化）。"""
    redacted = dict(payload)
    for field_name in RESTRICTED_PAYLOAD_FIELDS:
        redacted[field_name] = None
    return redacted


def archive_ref_for(subject_id: str, archived_at: datetime) -> str:
    moment = _utc(archived_at).strftime("%Y%m%dT%H%M%S")
    return f"arc-{subject_id}-{moment}"


# ---------------------------------------------------------------------------
# 内存台账（供轻量场景/测试复用）
# ---------------------------------------------------------------------------


class RetentionLedger:
    """在内存中维护策略、冻结和决策，供服务层复用。"""

    def __init__(self, rules: Iterable[RetentionRule] = ()) -> None:
        self.rules = list(validate_rules(rules))
        self.holds: dict[str, Hold] = {}
        self.decisions: dict[tuple[str, str], ArchiveDecision] = {}

    def replace_rules(self, rules: Iterable[RetentionRule]) -> None:
        self.rules = list(validate_rules(rules))

    def open_hold(self, hold: Hold) -> Hold:
        hold.validate()
        existing = self.holds.get(hold.hold_id)
        if existing and existing != hold:
            raise RetentionError("冻结标识已被其他内容使用")
        self.holds[hold.hold_id] = hold
        return hold

    def close_hold(self, hold_id: str, closed_at: datetime) -> Hold:
        current = _utc(closed_at)
        hold = self.holds.get(hold_id)
        if hold is None:
            raise RetentionError("冻结不存在")
        if current < _utc(hold.opened_at):
            raise RetentionError("解除时间不能早于冻结时间")
        updated = Hold(
            hold.hold_id, hold.subject_id, hold.opened_at, hold.reason, current,
            scope=hold.scope, target_ref=hold.target_ref,
        )
        self.holds[hold_id] = updated
        return updated

    def decide(self, candidate: RetentionCandidate, moment: datetime) -> ArchiveDecision:
        decision = decide_candidate(candidate, self.rules, self.holds.values(), moment)
        key = (candidate.subject_id, decision.effective_at.isoformat())
        self.decisions[key] = decision
        return decision

    def batch_decide(self, candidates: Iterable[RetentionCandidate], moment: datetime) -> list[ArchiveDecision]:
        ordered = sorted(candidates, key=lambda item: (item.created_at, item.subject_id))
        return [self.decide(candidate, moment) for candidate in ordered]

    def active_holds_for(self, subject_id: str, moment: datetime) -> list[Hold]:
        return active_holds_for(self.holds.values(), subject_id, moment)

    def explain(self, subject_id: str, moment: datetime) -> dict[str, object]:
        holds = self.active_holds_for(subject_id, moment)
        decisions = [value for value in self.decisions.values() if value.subject_id == subject_id]
        latest = max(decisions, key=lambda value: value.effective_at, default=None)
        return {
            "subject_id": subject_id,
            "holds": [hold.hold_id for hold in holds],
            "latest": latest.reason if latest else None,
            "state": latest.state.value if latest else RecordState.ACTIVE.value,
        }


@dataclass(frozen=True)
class RetentionSummary:
    """一批保留决策的稳定摘要。"""

    active: int
    held: int
    eligible: int
    archived: int
    by_rule: Mapping[str, int]

    @property
    def total(self) -> int:
        return self.active + self.held + self.eligible + self.archived

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "held": self.held,
            "eligible": self.eligible,
            "archived": self.archived,
            "total": self.total,
            "by_rule": dict(sorted(self.by_rule.items())),
        }


def summarize_decisions(decisions: Iterable[ArchiveDecision]) -> RetentionSummary:
    counters = {state: 0 for state in RecordState}
    by_rule: dict[str, int] = {}
    for decision in decisions:
        counters[decision.state] += 1
        if decision.rule_id:
            by_rule[decision.rule_id] = by_rule.get(decision.rule_id, 0) + 1
    return RetentionSummary(
        active=counters[RecordState.ACTIVE],
        held=counters[RecordState.HELD],
        eligible=counters[RecordState.ELIGIBLE],
        archived=counters[RecordState.ARCHIVED],
        by_rule=by_rule,
    )


def summarize_outcomes(outcomes: Iterable[BatchItemOutcome]) -> dict[str, int]:
    counters = {state: 0 for state in BatchItemState}
    for outcome in outcomes:
        counters[outcome.state] += 1
    return {state.value: count for state, count in counters.items()}


def next_due_at(candidate: RetentionCandidate, rules: Iterable[RetentionRule]) -> datetime | None:
    rule = choose_rule(rules, candidate.category)
    if rule is None or candidate.state == RecordState.ARCHIVED:
        return None
    return candidate.created_at.astimezone(timezone.utc) + rule.keep_for


def partition_candidates(
    candidates: Iterable[RetentionCandidate],
    rules: Iterable[RetentionRule],
    holds: Iterable[Hold],
    moment: datetime,
) -> dict[RecordState, list[RetentionCandidate]]:
    result = {state: [] for state in RecordState}
    rules = tuple(rules)
    holds = tuple(holds)
    for candidate in candidates:
        decision = decide_candidate(candidate, rules, holds, moment)
        result[decision.state].append(candidate)
    for values in result.values():
        values.sort(key=lambda item: (item.created_at, item.subject_id))
    return result


def validate_hold_intervals(holds: Iterable[Hold]) -> tuple[Hold, ...]:
    """校验冻结区间。同一对象允许区间重叠（多重冻结合法），但时间必须合法。"""
    ordered = sorted(holds, key=lambda item: (item.subject_id, item.opened_at, item.hold_id))
    seen: set[str] = set()
    for hold in ordered:
        hold.validate()
        if hold.hold_id in seen:
            raise RetentionError("冻结标识重复")
        seen.add(hold.hold_id)
    return tuple(ordered)


def retention_headers(summary: RetentionSummary) -> tuple[tuple[str, str], ...]:
    """为接口导出生成稳定的统计表头。"""
    return (
        ("active", str(summary.active)),
        ("held", str(summary.held)),
        ("eligible", str(summary.eligible)),
        ("archived", str(summary.archived)),
        ("total", str(summary.total)),
    )


def is_deletable(decision: ArchiveDecision) -> bool:
    """只有已确认可归档且没有冻结的记录才允许清理原始载荷。"""
    return decision.state == RecordState.ELIGIBLE and decision.rule_id is not None and not decision.hold_ids


def decision_key(decision: ArchiveDecision) -> tuple[str, str]:
    """返回可用于幂等存储的决策键。"""
    return decision.subject_id, decision.effective_at.astimezone(timezone.utc).isoformat()


def sort_decisions(decisions: Iterable[ArchiveDecision]) -> list[ArchiveDecision]:
    """按对象和生效时间稳定排序。"""
    return sorted(decisions, key=lambda item: decision_key(item))


def state_counts(decisions: Iterable[ArchiveDecision]) -> dict[str, int]:
    """将决策数量转换为接口可直接序列化的映射。"""
    summary = summarize_decisions(decisions)
    return {key: int(value) for key, value in retention_headers(summary)}


def can_release(subject_id: str, holds: Iterable[Hold], moment: datetime) -> bool:
    """判断对象在指定时点是否没有有效冻结。"""
    if not subject_id.strip():
        raise RetentionError("对象标识不能为空")
    _utc(moment)
    return not any(hold.subject_id == subject_id and hold.active_at(moment) for hold in holds)


def active_rule_ids(rules: Iterable[RetentionRule]) -> tuple[str, ...]:
    """返回已启用规则的稳定标识。"""
    return tuple(sorted(rule.rule_id for rule in rules if rule.enabled))


def rule_count(rules: Iterable[RetentionRule]) -> int:
    """返回有效规则数量。"""
    return len(active_rule_ids(rules))


def explain_subject(
    ctx: SubjectContext,
    rules: Iterable[RetentionRule],
    holds: Iterable[Hold],
    moment: datetime,
) -> dict[str, Any]:
    """生成可向隐私专员解释“某条数据为何仍存在”的结构化说明。"""
    current = _utc(moment)
    rule = choose_rule(rules, ctx.category)
    subject_holds = holds_for_subject(holds, ctx.subject_id)
    active = [hold for hold in subject_holds if hold.active_at(current)]
    decision = evaluate_subject(ctx, rules, holds, current)
    return {
        "subject_id": ctx.subject_id,
        "state": decision.state.value,
        "reason": decision.reason,
        "rule_id": rule.rule_id if rule else None,
        "keep_for_days": rule.keep_for.days if rule else None,
        "created_at": _utc(ctx.created_at).isoformat(),
        "due_at": decision.due_at.isoformat() if decision.due_at else None,
        "paused_seconds": int(paused_duration(subject_holds, ctx.created_at, current).total_seconds()),
        "active_holds": [
            {
                "hold_id": hold.hold_id,
                "scope": hold.scope,
                "target_ref": hold.target_ref,
                "reason": hold.reason,
                "opened_at": _utc(hold.opened_at).isoformat(),
            }
            for hold in active
        ],
        "active_dataset_refs": sorted(ctx.active_dataset_refs),
        "archived": ctx.already_archived,
    }
