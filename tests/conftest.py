"""保留策略/冻结/归档接口测试的共享夹具。"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.pop("RETENTION_TEST_FAIL_OPERATION_IDS", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import main  # noqa: E402,F401  确保路由已注册
from app.database import Base, get_db  # noqa: E402
from app.models import (  # noqa: E402
    Annotation,
    Dataset,
    DatasetItem,
    DatasetVersion,
    OperationData,
    RobotModel,
    Scene,
    Skill,
)

UTC = timezone.utc


@pytest.fixture()
def db_engine():
    engine = create_engine(
        f"sqlite:///{_db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[get_db] = override_get_db
    yield SimpleNamespace(engine=engine, session_factory=TestingSession, path=_db_path)
    main.app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client(db_engine):
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def seed(db_engine):
    """提供基础资源与可定制作业/数据集的播种助手。"""
    session = db_engine.session_factory()

    robot = RobotModel(name="机型A", manufacturer="厂商A")
    scene_workshop = Scene(name="生产制造车间", category="制造")
    scene_retail = Scene(name="餐饮零售", category="零售")
    skill = Skill(name="抓取", category="抓取技能")
    session.add_all([robot, scene_workshop, scene_retail, skill])
    session.commit()
    for obj in (robot, scene_workshop, scene_retail, skill):
        session.refresh(obj)

    def make_operation(
        scene_id: int | None = None,
        *,
        age_days: float = 1.0,
        robot_serial: str | None = None,
        grade: str | None = "A",
        quality: float | None = 0.9,
        annotate: bool = True,
        is_success: bool = True,
        trajectory=None,
    ) -> OperationData:
        now = datetime.now(UTC)
        created = now - timedelta(days=age_days)
        op = OperationData(
            robot_model_id=robot.id,
            scene_id=scene_id if scene_id is not None else scene_workshop.id,
            skill_id=skill.id,
            robot_serial=robot_serial,
            motion_trajectory=trajectory if trajectory is not None else {"points": [[1, 2], [3, 4]]},
            perception_records={"frames": 10},
            grasp_result={"ok": True},
            environment_conditions={"temp": 25},
            hardware_status={"battery": 0.8},
            timestamp_start=created,
            timestamp_end=created + timedelta(minutes=5),
            duration_ms=300000,
            quality_score=quality,
            completeness_score=quality,
            data_grade=grade,
            created_at=created,
        )
        session.add(op)
        session.commit()
        session.refresh(op)
        if annotate:
            session.add(
                Annotation(
                    operation_data_id=op.id,
                    is_success=is_success,
                    failure_category=None if is_success else "感知异常",
                    review_status="approved",
                    annotation_quality_score=0.95,
                )
            )
            session.commit()
        return op

    def make_dataset(name: str, operation_ids: list[int], *, published: bool = False,
                     scene_id: int | None = None) -> Dataset:
        dataset = Dataset(
            name=name,
            robot_model_id=robot.id,
            scene_id=scene_id if scene_id is not None else scene_workshop.id,
            skill_id=skill.id,
            owner_team="数据团队",
            review_status="draft",
            is_published=False,
        )
        session.add(dataset)
        session.commit()
        session.refresh(dataset)
        session.add_all([
            DatasetItem(dataset_id=dataset.id, operation_data_id=op_id) for op_id in operation_ids
        ])
        session.commit()
        session.add(
            DatasetVersion(
                dataset_id=dataset.id,
                version_number=1,
                version_label="1.0",
                change_description="初始版本",
                total_items=len(operation_ids),
            )
        )
        dataset.total_items = len(operation_ids)
        session.commit()
        session.refresh(dataset)
        if published:
            dataset.review_status = "pending_review"
            dataset.is_published = False
            session.commit()
            dataset.review_status = "approved"
            dataset.is_published = True
            dataset.published_at = datetime.now(UTC)
            session.add(
                DatasetVersion(
                    dataset_id=dataset.id,
                    version_number=1,
                    version_label="1.0",
                    total_items=len(operation_ids),
                )
            )
            session.commit()
            session.refresh(dataset)
        return dataset

    helper = SimpleNamespace(
        session=session,
        robot_id=robot.id,
        workshop_scene_id=scene_workshop.id,
        retail_scene_id=scene_retail.id,
        skill_id=skill.id,
        make_operation=make_operation,
        make_dataset=make_dataset,
    )
    try:
        yield helper
    finally:
        session.close()
