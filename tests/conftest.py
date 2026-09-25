import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# 必须在导入应用前指定临时数据库。
_DB_DIR = tempfile.mkdtemp(prefix="retention-test-")
_DB_PATH = os.path.join(_DB_DIR, "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401  # 确保模型已注册
import main  # noqa: E402

UTC = timezone.utc


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def make_base_resources(db):
    robot = models.RobotModel(name="RM-X", manufacturer="Acme")
    scene = models.Scene(name="测试场景", category="生产制造")
    scene2 = models.Scene(name="另一场景", category="餐饮零售")
    skill = models.Skill(name="抓取", category="操作")
    db.add_all([robot, scene, scene2, skill])
    db.flush()
    return robot, scene, scene2, skill


def make_operation(db, robot, scene, skill, *, created_at, payload_marker="full"):
    op = models.OperationData(
        robot_model_id=robot.id,
        scene_id=scene.id,
        skill_id=skill.id,
        robot_serial="SN-1",
        motion_trajectory={"waypoints": [{"x": 1, "y": 2}], "joint_angles": [[0, 1]]},
        perception_records={"camera_images_captured": 3, "depth_frames": 2,
                            "detections": [{"object_id": "o1"}]},
        grasp_result={"success": True},
        environment_conditions={"temperature_c": 25.0},
        hardware_status={"cpu_usage_percent": 40},
        timestamp_start=created_at,
        timestamp_end=created_at + timedelta(seconds=10),
        duration_ms=10000,
        quality_score=0.9,
        completeness_score=1.0,
        data_grade="A",
        created_at=created_at,
    )
    db.add(op)
    db.flush()
    return op


def make_dataset(db, robot, scene, skill, operations, *, review_status="draft", published=False):
    dataset = models.Dataset(
        name="DS",
        robot_model_id=robot.id,
        scene_id=scene.id,
        skill_id=skill.id,
        owner_team="team-a",
        review_status=review_status,
        is_published=published,
        current_version=1,
        version="1.0",
        total_items=len(operations),
    )
    db.add(dataset)
    db.flush()
    for op in operations:
        db.add(models.DatasetItem(dataset_id=dataset.id, operation_data_id=op.id))
    version = models.DatasetVersion(
        dataset_id=dataset.id,
        version_number=1,
        version_label="1.0",
        total_items=len(operations),
    )
    db.add(version)
    db.flush()
    return dataset, version
