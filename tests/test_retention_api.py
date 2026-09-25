"""保留策略/法律冻结/归档的端到端接口验证。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.schemas.retention import ArchiveBatchCreate
from app.services import retention_service as service


def _policy(client, rule_id, scope, keep_days, scene_id=None, priority=0, enabled=True):
    return client.post(
        "/api/v1/retention/policies",
        json={
            "rule_id": rule_id,
            "name": rule_id,
            "scope": scope,
            "scene_id": scene_id,
            "keep_days": keep_days,
            "priority": priority,
            "enabled": enabled,
        },
    )


def _hold(client, hold_id, targets, reason="争议调查", requested_by="法务"):
    return client.post(
        "/api/v1/retention/holds",
        json={
            "hold_id": hold_id,
            "reason": reason,
            "requested_by": requested_by,
            "targets": targets,
        },
    )


def _run(client, **kwargs):
    return client.post("/api/v1/retention/archive-batches", json=kwargs)


def _preview(client, **kwargs):
    payload = {"dry_run": True}
    payload.update(kwargs)
    return client.post("/api/v1/retention/archive-preview", json=payload)


def test_policy_priority_scene_vs_global(client, seed):
    old_ws = seed.make_operation(scene_id=seed.workshop_scene_id, age_days=100)
    old_retail = seed.make_operation(scene_id=seed.retail_scene_id, age_days=100)
    fresh_ws = seed.make_operation(scene_id=seed.workshop_scene_id, age_days=2)

    # 全局 30 天（低优先级），车间场景 7 天（高优先级）
    assert _policy(client, "global-30", "global", 30, priority=0).status_code == 200
    assert _policy(client, "workshop-7", "scene", 7, scene_id=seed.workshop_scene_id, priority=10).status_code == 200

    res = _preview(client)
    assert res.status_code == 200
    data = res.json()
    by_op = {c["operation_id"]: c for c in data["candidates"]}
    # 车间老数据命中场景短策略
    assert by_op[old_ws.id]["rule_id"] == "workshop-7"
    assert by_op[old_ws.id]["policy_level"] == "scene"
    # 零售场景无专属策略，落到全局兜底
    assert by_op[old_retail.id]["rule_id"] == "global-30"
    assert by_op[old_retail.id]["policy_level"] == "global"
    # 未到期的数据不入选
    assert fresh_ws.id not in by_op
    assert data["not_due"] >= 1


def test_overlapping_holds_block_until_all_released(client, seed):
    op = seed.make_operation(age_days=200)
    _policy(client, "global-30", "global", 30)

    assert _hold(client, "case-a", [{"kind": "operation", "id": op.id}]).status_code == 200
    assert _hold(client, "case-b", [{"kind": "operation", "id": op.id}]).status_code == 200

    preview = _preview(client).json()
    assert preview["held"] == 1
    assert preview["eligible"] == 0

    # 只解除一条，重叠的另一条仍在 -> 继续冻结
    r = client.post("/api/v1/retention/holds/case-a/release", json={"released_by": "法务"})
    assert r.status_code == 200
    assert _preview(client).json()["held"] == 1

    explain = client.get(f"/api/v1/retention/operations/{op.id}/explain").json()
    assert explain["present_reason"] == "held"
    assert {h["hold_id"] for h in explain["active_holds"]} == {"case-b"}

    # 全部解除后，按原始到期点立即进入候选，不重新计算 30 天
    client.post("/api/v1/retention/holds/case-b/release", json={"released_by": "法务"})
    preview = _preview(client).json()
    assert preview["eligible"] == 1
    assert preview["candidates"][0]["operation_id"] == op.id
    assert preview["held"] == 0

    # 解除后实际归档成功，且期限未重新计算（无需再等 30 天）
    run = _run(client, idempotency_key="after-release").json()
    assert run["status"] == "completed" and run["succeeded"] == 1
    explain = client.get(f"/api/v1/retention/operations/{op.id}/explain").json()
    assert explain["payload_cleared"] is True
    assert explain["overdue"] is True


def test_dataset_version_hold_covers_members(client, seed):
    op1 = seed.make_operation(age_days=200)
    op2 = seed.make_operation(age_days=200)
    dataset = seed.make_dataset("版本争议数据集", [op1.id, op2.id])
    version_id = 1  # make_dataset 创建的版本
    _policy(client, "global-30", "global", 30)

    res = _hold(client, "case-ver", [{"kind": "dataset_version", "id": version_id}])
    assert res.status_code == 200
    body = res.json()
    assert body["targets"][0]["subject_type"] == "dataset_version"
    assert body["targets"][0]["dataset_id"] == dataset.id

    preview = _preview(client).json()
    assert preview["held"] == 2 and preview["eligible"] == 0

    # 查询覆盖单条作业的冻结（含版本扩展）
    covers = client.get("/api/v1/retention/holds", params={"operation_id": op1.id}).json()
    assert {h["hold_id"] for h in covers} == {"case-ver"}


def test_dataset_wide_hold_covers_version_members(client, seed):
    op1 = seed.make_operation(age_days=200)
    op2 = seed.make_operation(age_days=200)
    standalone = seed.make_operation(age_days=200)
    dataset = seed.make_dataset("争议数据集", [op1.id, op2.id])
    _policy(client, "global-30", "global", 30)

    # 冻结整个数据集（其当前版本的成员全部被覆盖）
    res = _hold(client, "case-ds", [{"kind": "dataset", "id": dataset.id}])
    assert res.status_code == 200

    preview = _preview(client).json()
    assert preview["held"] == 2
    assert {c["operation_id"] for c in preview["candidates"]} == {standalone.id}

    explain = client.get(f"/api/v1/retention/operations/{op1.id}/explain").json()
    assert explain["present_reason"] == "held"
    assert explain["active_holds"][0]["hold_id"] == "case-ds"

    client.post("/api/v1/retention/holds/case-ds/release", json={"released_by": "法务"})
    preview = _preview(client).json()
    assert preview["held"] == 0
    assert preview["eligible"] == 3


def test_active_dataset_members_are_protected(client, seed):
    member = seed.make_operation(age_days=200, trajectory={"secret": "原始轨迹"})
    standalone = seed.make_operation(age_days=200)
    seed.make_dataset("已发布数据集", [member.id], published=True)
    _policy(client, "global-30", "global", 30)

    preview = _preview(client).json()
    assert preview["active_member_protected"] == 1
    assert {c["operation_id"] for c in preview["candidates"]} == {standalone.id}

    run = _run(client, idempotency_key="protect-1").json()
    assert run["succeeded"] == 1
    # 成员载荷未被破坏
    protected = client.get(f"/api/v1/operations/{member.id}").json()
    assert protected["motion_trajectory"] == {"secret": "原始轨迹"}

    explain = client.get(f"/api/v1/retention/operations/{member.id}/explain").json()
    assert explain["present_reason"] == "active_dataset_member"
    assert any(m["is_published"] for m in explain["dataset_memberships"])


def test_batch_partial_failure_then_resume(client, seed, monkeypatch):
    op1 = seed.make_operation(age_days=60)
    op2 = seed.make_operation(age_days=50)
    op3 = seed.make_operation(age_days=40)
    _policy(client, "global-30", "global", 30)

    # 注入 op2 归档故障：保存点隔离，其余成功
    monkeypatch.setenv("RETENTION_TEST_FAIL_OPERATION_IDS", str(op2.id))
    run = _run(client, idempotency_key="partial-1").json()
    assert run["status"] == "partial_failed"
    assert run["succeeded"] == 2
    assert run["failed"] == 1
    failed_item = [i for i in run["items"] if i["operation_id"] == op2.id][0]
    assert failed_item["status"] == "failed" and failed_item["attempts"] == 1

    # op2 载荷未被动到；op1/op3 已归档
    seed.session.expire_all()
    explain2 = client.get(f"/api/v1/retention/operations/{op2.id}/explain").json()
    assert explain2["payload_cleared"] is False

    # 故障排除后用同一幂等键重试：只续跑失败项
    monkeypatch.delenv("RETENTION_TEST_FAIL_OPERATION_IDS", raising=False)
    rerun = _run(client, idempotency_key="partial-1").json()
    assert rerun["batch_id"] == run["batch_id"]
    assert rerun["status"] == "completed"
    assert rerun["succeeded"] == 3
    assert rerun["failed"] == 0
    retried = [i for i in rerun["items"] if i["operation_id"] == op2.id][0]
    assert retried["attempts"] == 2

    # 每条作业只有一条归档记录（没有重复归档）
    records = client.get("/api/v1/retention/archive-records").json()
    assert len(records) == 3


def test_batch_idempotent_replay(client, seed):
    ops = [seed.make_operation(age_days=40 + i) for i in range(3)]
    _policy(client, "global-30", "global", 30)

    first = _run(client, idempotency_key="same-key").json()
    assert first["status"] == "completed"
    assert first["succeeded"] == 3

    second = _run(client, idempotency_key="same-key").json()
    assert second["batch_id"] == first["batch_id"]
    assert second["succeeded"] == 3
    assert second["resumed"] is False
    assert len(client.get("/api/v1/retention/archive-records").json()) == 3


def test_restart_recovery_after_crash(db_engine, seed, monkeypatch):
    monkeypatch.delenv("RETENTION_TEST_FAIL_OPERATION_IDS", raising=False)
    ops = [seed.make_operation(age_days=40 + i) for i in range(3)]
    _policy_seed = None
    session = db_engine.session_factory()
    from app.models import RetentionPolicy
    session.add(RetentionPolicy(rule_id="global-30", name="g", scope="global", scene_id=None, keep_days=30, priority=0, enabled=True))
    session.commit()
    session.close()

    # 模拟崩溃：只落库批次和 pending 项，未执行任何归档就结束会话
    crashed = db_engine.session_factory()
    payload = ArchiveBatchCreate(idempotency_key="crash-1")
    batch, _eligible, _counts, created = service._get_or_create_batch(crashed, payload, datetime.now(timezone.utc))
    assert created is True
    crash_batch_id = batch.batch_id
    crashed.close()

    # 重启后用同一幂等键续跑
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as client:
        res = client.post("/api/v1/retention/archive-batches", json={"idempotency_key": "crash-1"})
        data = res.json()
        assert data["batch_id"] == crash_batch_id
        assert data["resumed"] is True
        assert data["status"] == "completed"
        assert data["succeeded"] == 3
        records = client.get("/api/v1/retention/archive-records").json()
        assert {r["operation_id"] for r in records} == {op.id for op in ops}


def test_archive_removes_payload_keeps_summary_and_audit(client, seed):
    op = seed.make_operation(age_days=200, trajectory={"waypoints": "受限原始载荷"})
    _policy(client, "global-30", "global", 30)

    run = _run(client, idempotency_key="purge-1").json()
    assert run["status"] == "completed"

    detail = client.get(f"/api/v1/operations/{op.id}").json()
    # 受限载荷已移除（非空约束字段为占位空对象）
    assert detail["motion_trajectory"] == {}
    assert detail["perception_records"] == {}
    assert detail["grasp_result"] is None

    explain = client.get(f"/api/v1/retention/operations/{op.id}/explain").json()
    assert explain["payload_cleared"] is True
    assert explain["archive"] is not None
    assert explain["archive"]["payload_audit_ref"].startswith("sha256:")
    # 统计所需摘要保留
    summary = explain["archive"]["summary"]
    assert summary["scene_id"] == seed.workshop_scene_id
    assert summary["data_grade"] == "A"
    assert summary["annotation"]["review_status"] == "approved"
    assert "payload_sha256" in summary

    records = client.get(f"/api/v1/retention/archive-records").json()
    assert records[0]["removed_fields"] == [
        "motion_trajectory", "perception_records", "grasp_result",
        "environment_conditions", "hardware_status",
    ]
    assert records[0]["action"] == "purge_payload"

    # 已归档作业不再进入候选扫描
    preview = _preview(client).json()
    assert preview["scanned"] == 0
    assert preview["eligible"] == 0

    # 审计链完整
    events = client.get("/api/v1/retention/audit", params={"batch_id": run["batch_id"]}).json()
    kinds = {e["event_type"] for e in events}
    assert "archive_batch_created" in kinds
    assert "operation_archived" in kinds
