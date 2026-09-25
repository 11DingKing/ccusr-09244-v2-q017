"""通过 HTTP 接口验证保留策略与法律冻结的端到端流程。"""

from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.services import retention_service as svc

from .conftest import make_base_resources, make_dataset, make_operation

API = "/api/v1"
MOMENT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _seed_scene_with_ops(num_old=2, num_fresh=1, in_active_dataset=False, review_status="draft"):
    db = SessionLocal()
    try:
        robot, scene, scene2, skill = make_base_resources(db)
        old = [make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=100 + i))
               for i in range(num_old)]
        fresh = [make_operation(db, robot, scene, skill, created_at=MOMENT - timedelta(days=2))
                 for _ in range(num_fresh)]
        dataset = None
        if in_active_dataset:
            dataset, _ = make_dataset(db, robot, scene, skill, old, review_status=review_status)
        db.commit()
        ids = {
            "scene_id": scene.id,
            "old": [op.id for op in old],
            "fresh": [op.id for op in fresh],
            "dataset_id": dataset.id if dataset else None,
        }
        return ids
    finally:
        db.close()


def test_policy_priority_and_archive_batch_via_api(client):
    ids = _seed_scene_with_ops(num_old=2, num_fresh=1)

    # 两条策略：全局长期 + 场景短期高优先级。
    assert client.post(f"{API}/retention/policies", json={
        "rule_id": "global-long", "name": "全局", "scene_id": None,
        "keep_days": 365, "priority": 0}).status_code == 200
    assert client.post(f"{API}/retention/policies", json={
        "rule_id": "scene-short", "name": "场景严格", "scene_id": ids["scene_id"],
        "keep_days": 30, "priority": 10}).status_code == 200

    # 重复 rule_id 被拒绝。
    dup = client.post(f"{API}/retention/policies", json={
        "rule_id": "scene-short", "name": "x", "scene_id": ids["scene_id"],
        "keep_days": 9, "priority": 0})
    assert dup.status_code == 400

    # dry-run 预演：2 条到期。
    dry = client.post(f"{API}/retention/archive-batches",
                      json={"batch_key": "dry", "dry_run": True}).json()
    assert dry["status"] == "dry_run"
    would = [it for it in client.get(f"{API}/retention/archive-batches/{dry['id']}").json()["items"]
             if it["state"] == "would_archive"]
    assert len(would) == 2

    # 正式执行（批次幂等：dry-run 键不能复用为正式执行）。
    clash = client.post(f"{API}/retention/archive-batches", json={"batch_key": "dry"})
    assert clash.status_code == 400

    batch = client.post(f"{API}/retention/archive-batches",
                        json={"batch_key": "run-1"}).json()
    assert batch["status"] == "completed"
    assert batch["succeeded"] == 2
    assert batch["failed"] == 0

    # 重复执行同批次：返回相同结果，不新增归档。
    again = client.post(f"{API}/retention/archive-batches", json={"batch_key": "run-1"}).json()
    assert again["id"] == batch["id"] and again["succeeded"] == 2

    # 高优先级策略生效；受限载荷已移除，摘要可查。
    state = client.get(f"{API}/operations/{ids['old'][0]}/retention").json()
    assert state["effective_state"] == "archived"
    assert state["rule_id"] == "scene-short"

    detail = client.get(f"{API}/operations/{ids['old'][0]}").json()
    assert detail["motion_trajectory"] is None

    archive = client.get(f"{API}/operations/{ids['old'][0]}/archive-record").json()
    assert archive["rule_id"] == "scene-short"
    assert any(d["field"] == "motion_trajectory" for d in archive["payload_digests"])

    # 未到期记录仍在，且能解释原因。
    explain = client.get(f"{API}/operations/{ids['fresh'][0]}/retention/explain").json()
    assert explain["state"] == "active"
    assert "尚未达到期限" in explain["reason"]
    assert explain["rule_id"] == "scene-short"


def test_overlapping_holds_block_until_both_released_via_api(client):
    ids = _seed_scene_with_ops(num_old=1, num_fresh=0)
    op_id = ids["old"][0]
    client.post(f"{API}/retention/policies", json={
        "rule_id": "p", "name": "p", "scene_id": ids["scene_id"], "keep_days": 30})

    # 单条作业冻结。
    h1 = client.post(f"{API}/retention/holds", json={
        "hold_id": "case-op", "scope": "operation", "operation_data_id": op_id,
        "reason": "争议A", "requested_by": "法务"}).json()
    assert h1["status"] == "active" and h1["covered_operation_ids"] == [op_id]

    # 叠加第二条作业级冻结（重叠）。
    client.post(f"{API}/retention/holds", json={
        "hold_id": "case-op-2", "scope": "operation", "operation_data_id": op_id,
        "reason": "争议B", "requested_by": "法务"})

    batch = client.post(f"{API}/retention/archive-batches",
                        json={"batch_key": "held"}).json()
    assert batch["skipped_held"] == 1 and batch["succeeded"] == 0

    # 解释接口应列出两条有效冻结。
    explain = client.get(f"{API}/operations/{op_id}/retention/explain").json()
    assert {h["hold_id"] for h in explain["active_holds"]} == {"case-op", "case-op-2"}
    assert explain["state"] == "held"

    # 解除一条仍被冻结。
    client.post(f"{API}/retention/holds/case-op/release", json={"released_by": "法务"})
    b2 = client.post(f"{API}/retention/archive-batches", json={"batch_key": "held-1"}).json()
    assert b2["skipped_held"] == 1

    # 重复解除幂等。
    rel_again = client.post(f"{API}/retention/holds/case-op/release", json={}).json()
    assert rel_again["status"] == "released"

    # 全部解除后归档。
    client.post(f"{API}/retention/holds/case-op-2/release", json={"released_by": "法务"})
    b3 = client.post(f"{API}/retention/archive-batches", json={"batch_key": "held-2"}).json()
    assert b3["succeeded"] == 1


def test_dataset_version_hold_covers_members_via_api(client):
    ids = _seed_scene_with_ops(num_old=2, num_fresh=0, in_active_dataset=True)
    client.post(f"{API}/retention/policies", json={
        "rule_id": "p", "name": "p", "scene_id": ids["scene_id"], "keep_days": 30})

    # 冻结整个数据集版本（不指定版本，取最新）。
    hold = client.post(f"{API}/retention/holds", json={
        "hold_id": "ds-case", "scope": "dataset_version",
        "dataset_id": ids["dataset_id"], "reason": "数据集争议",
        "requested_by": "法务"}).json()
    assert sorted(hold["covered_operation_ids"]) == sorted(ids["old"])
    assert hold["dataset_version_id"] is not None

    batch = client.post(f"{API}/retention/archive-batches", json={"batch_key": "ds-held"}).json()
    # 活动数据集成员本就受保护；冻结叠加时优先报告 held。
    assert batch["succeeded"] == 0
    assert batch["skipped_held"] + batch["skipped_dataset"] == 2

    # 冻结不存在目标时 400。
    bad = client.post(f"{API}/retention/holds", json={
        "scope": "operation", "operation_data_id": 999999,
        "reason": "x", "requested_by": "y"})
    assert bad.status_code == 400


def test_active_dataset_members_are_protected_via_api(client):
    ids = _seed_scene_with_ops(num_old=1, num_fresh=0, in_active_dataset=True,
                               review_status="pending_review")
    client.post(f"{API}/retention/policies", json={
        "rule_id": "p", "name": "p", "scene_id": ids["scene_id"], "keep_days": 30})
    batch = client.post(f"{API}/retention/archive-batches", json={"batch_key": "protect"}).json()
    assert batch["skipped_dataset"] == 1
    assert batch["succeeded"] == 0
    op_detail = client.get(f"{API}/operations/{ids['old'][0]}").json()
    assert op_detail["motion_trajectory"] is not None  # 成员未被破坏


def test_partial_failure_and_restart_recovery_services_visible_via_api(client):
    """通过 API 创建批次，再用服务层钩子模拟部分失败与崩溃，最后用同一批次键经 API 收敛。"""
    ids = _seed_scene_with_ops(num_old=3, num_fresh=0)
    client.post(f"{API}/retention/policies", json={
        "rule_id": "p", "name": "p", "scene_id": ids["scene_id"], "keep_days": 30})

    db = SessionLocal()
    try:
        # 模拟崩溃：第一项处理后中断，批次停留 running，检查点已落库。
        target = sorted(ids["old"])[1]

        def crash(op):
            if op.id == target:
                raise RuntimeError("进程崩溃")

        try:
            svc.run_archive_batch(db, batch_key="crash", crash_after=crash, moment=MOMENT)
        except RuntimeError:
            pass
    finally:
        db.close()

    # 重启后经 API 用同一批次键续跑：从检查点恢复并完成。
    recovered = client.post(f"{API}/retention/archive-batches",
                            json={"batch_key": "crash"}).json()
    assert recovered["status"] == "completed"
    assert recovered["succeeded"] == 3

    batches = client.get(f"{API}/retention/archive-batches").json()
    assert any(b["batch_key"] == "crash" for b in batches)


def test_partial_failure_visible_via_api_and_retry_converges(client):
    ids = _seed_scene_with_ops(num_old=3, num_fresh=0)
    client.post(f"{API}/retention/policies", json={
        "rule_id": "p", "name": "p", "scene_id": ids["scene_id"], "keep_days": 30})

    target = sorted(ids["old"])[1]
    db = SessionLocal()
    try:
        def fail_one(op):
            if op.id == target:
                raise RuntimeError("模拟存储故障")
        svc.run_archive_batch(db, batch_key="partial", item_failure=fail_one, moment=MOMENT)
    finally:
        db.close()

    # 批次与单项 FAILED 结果可通过接口查看。
    listing = client.get(f"{API}/retention/archive-batches").json()
    batch_id = next(b["id"] for b in listing if b["batch_key"] == "partial")
    detail = client.get(f"{API}/retention/archive-batches/{batch_id}").json()
    assert detail["status"] == "completed_with_errors"
    assert detail["succeeded"] == 2 and detail["failed"] == 1
    failed = [it for it in detail["items"] if it["state"] == "failed"]
    assert len(failed) == 1 and failed[0]["operation_data_id"] == target
    assert "模拟存储故障" in failed[0]["detail"]

    # 失败项载荷完好，且经接口用同批次键重试后收敛。
    assert client.get(f"{API}/operations/{target}").json()["motion_trajectory"] is not None
    recovered = client.post(f"{API}/retention/archive-batches",
                            json={"batch_key": "partial"}).json()
    assert recovered["status"] == "completed"
    assert recovered["succeeded"] == 3 and recovered["failed"] == 0


def test_hold_listing_and_404s(client):
    ids = _seed_scene_with_ops(num_old=1, num_fresh=0)
    assert client.post(f"{API}/retention/holds", json={
        "hold_id": "h", "scope": "operation", "operation_data_id": ids["old"][0],
        "reason": "r", "requested_by": "u"}).status_code == 200
    listing = client.get(f"{API}/retention/holds?status=active").json()
    assert any(item["hold_id"] == "h" for item in listing)
    assert client.get(f"{API}/retention/holds/missing").status_code == 404
    assert client.get(f"{API}/operations/999999/retention").status_code == 404
    assert client.get(f"{API}/operations/{ids['old'][0]}/archive-record").status_code == 404
