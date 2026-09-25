from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float, Boolean, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class RobotModel(Base):
    __tablename__ = "robot_models"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    manufacturer = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    capabilities = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    operations = relationship("OperationData", back_populates="robot_model")
    datasets = relationship("Dataset", back_populates="robot_model")


class Scene(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    environment_tags = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operations = relationship("OperationData", back_populates="scene")
    datasets = relationship("Dataset", back_populates="scene")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    operations = relationship("OperationData", back_populates="skill")


class OperationData(Base):
    __tablename__ = "operation_data"

    id = Column(Integer, primary_key=True, index=True)
    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=False, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=False, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False, index=True)
    robot_serial = Column(String(100), nullable=True, index=True)

    motion_trajectory = Column(JSON, nullable=False)
    perception_records = Column(JSON, nullable=False)
    grasp_result = Column(JSON, nullable=True)

    timestamp_start = Column(DateTime(timezone=True), nullable=False)
    timestamp_end = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=True)

    environment_conditions = Column(JSON, nullable=True)
    hardware_status = Column(JSON, nullable=True)

    quality_score = Column(Float, nullable=True)
    completeness_score = Column(Float, nullable=True)
    data_grade = Column(String(10), nullable=True, index=True)

    retention_state = Column(String(20), nullable=False, default="active", index=True)
    retention_rule_id = Column(String(64), nullable=True, index=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    robot_model = relationship("RobotModel", back_populates="operations")
    scene = relationship("Scene", back_populates="operations")
    skill = relationship("Skill", back_populates="operations")
    annotation = relationship("Annotation", back_populates="operation_data", uselist=False, cascade="all, delete-orphan")
    dataset_items = relationship("DatasetItem", back_populates="operation_data", cascade="all, delete-orphan")


class Annotation(Base):
    __tablename__ = "annotations"

    id = Column(Integer, primary_key=True, index=True)
    operation_data_id = Column(Integer, ForeignKey("operation_data.id"), nullable=False, unique=True, index=True)

    is_success = Column(Boolean, nullable=False, index=True)
    failure_category = Column(String(50), nullable=True, index=True)
    failure_subcategory = Column(String(100), nullable=True)
    failure_description = Column(Text, nullable=True)

    annotator = Column(String(100), nullable=True)
    annotation_time = Column(DateTime(timezone=True), server_default=func.now())
    review_status = Column(String(20), default="pending", index=True)
    reviewer = Column(String(100), nullable=True)
    review_notes = Column(Text, nullable=True)

    annotation_quality_score = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    operation_data = relationship("OperationData", back_populates="annotation")


class Dataset(Base):
    __tablename__ = "datasets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    description = Column(Text, nullable=True)
    version = Column(String(20), default="1.0")

    robot_model_id = Column(Integer, ForeignKey("robot_models.id"), nullable=False, index=True)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=False, index=True)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True, index=True)

    owner_team = Column(String(100), nullable=False)
    contact_person = Column(String(100), nullable=True)
    review_status = Column(String(20), default="draft", index=True)
    is_published = Column(Boolean, default=False, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)

    current_version = Column(Integer, default=1)

    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    annotation_complete_rate = Column(Float, default=0.0)
    average_quality_score = Column(Float, nullable=True)
    reuse_count = Column(Integer, default=0, index=True)

    data_grade = Column(String(10), nullable=True, index=True)
    tags = Column(JSON, nullable=True)
    license_info = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    robot_model = relationship("RobotModel", back_populates="datasets")
    scene = relationship("Scene", back_populates="datasets")
    items = relationship("DatasetItem", back_populates="dataset", cascade="all, delete-orphan")
    reuse_records = relationship("DatasetReuse", back_populates="dataset", cascade="all, delete-orphan")
    versions = relationship("DatasetVersion", back_populates="dataset", cascade="all, delete-orphan")
    reviews = relationship("DatasetReview", back_populates="dataset", cascade="all, delete-orphan")
    subscriptions = relationship("DatasetSubscription", back_populates="dataset", cascade="all, delete-orphan")


class DatasetItem(Base):
    __tablename__ = "dataset_items"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    operation_data_id = Column(Integer, ForeignKey("operation_data.id"), nullable=False, index=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="items")
    operation_data = relationship("OperationData", back_populates="dataset_items")


class DatasetReuse(Base):
    __tablename__ = "dataset_reuses"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    reusing_team = Column(String(100), nullable=False)
    purpose = Column(String(200), nullable=True)
    project_name = Column(String(200), nullable=True)
    reuse_date = Column(DateTime(timezone=True), server_default=func.now())
    notes = Column(Text, nullable=True)

    dataset = relationship("Dataset", back_populates="reuse_records")
    version = relationship("DatasetVersion", back_populates="reuse_records")


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    version_label = Column(String(20), nullable=False)
    change_description = Column(Text, nullable=True)

    total_items = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    failure_count = Column(Integer, default=0)
    annotation_complete_rate = Column(Float, default=0.0)
    average_quality_score = Column(Float, nullable=True)
    data_grade = Column(String(10), nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="versions")
    reuse_records = relationship("DatasetReuse", back_populates="version")


class DatasetReview(Base):
    __tablename__ = "dataset_reviews"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    action = Column(String(20), nullable=False, index=True)
    reviewer = Column(String(100), nullable=True)
    review_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="reviews")
    version = relationship("DatasetVersion")


class DatasetSubscription(Base):
    __tablename__ = "dataset_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=False, index=True)
    subscriber_team = Column(String(100), nullable=False)
    contact_person = Column(String(100), nullable=True)
    notify_on_new_version = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    dataset = relationship("Dataset", back_populates="subscriptions")


class RetentionPolicy(Base):
    """按场景配置的保留策略；scene_id 为空表示全局策略。"""

    __tablename__ = "retention_policies"

    id = Column(Integer, primary_key=True, index=True)
    rule_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    scene_id = Column(Integer, ForeignKey("scenes.id"), nullable=True, index=True)
    scope = Column(String(20), nullable=False, default="scene", index=True)
    keep_days = Column(Integer, nullable=False)
    priority = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    scene = relationship("Scene")


class LegalHold(Base):
    """法律冻结（争议调查）。可直接冻结作业，或冻结数据集/数据集版本。"""

    __tablename__ = "legal_holds"

    id = Column(Integer, primary_key=True, index=True)
    hold_id = Column(String(64), unique=True, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    requested_by = Column(String(100), nullable=False)
    case_reference = Column(String(200), nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)

    opened_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)
    released_by = Column(String(100), nullable=True)
    release_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    targets = relationship("HoldTarget", back_populates="hold", cascade="all, delete-orphan")


class HoldTarget(Base):
    """冻结覆盖的对象：单条作业或整个数据集版本。"""

    __tablename__ = "hold_targets"

    id = Column(Integer, primary_key=True, index=True)
    hold_id = Column(Integer, ForeignKey("legal_holds.id"), nullable=False, index=True)
    subject_type = Column(String(32), nullable=False, index=True)
    subject_ref = Column(String(64), nullable=False, index=True)
    operation_id = Column(Integer, ForeignKey("operation_data.id"), nullable=True, index=True)
    dataset_id = Column(Integer, ForeignKey("datasets.id"), nullable=True, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    hold = relationship("LegalHold", back_populates="targets")


class ArchiveRecord(Base):
    """归档结果：移除受限载荷后保留的统计摘要与审计引用。"""

    __tablename__ = "archive_records"

    id = Column(Integer, primary_key=True, index=True)
    subject_type = Column(String(32), nullable=False, index=True)
    operation_id = Column(Integer, ForeignKey("operation_data.id"), nullable=True, index=True)
    dataset_version_id = Column(Integer, ForeignKey("dataset_versions.id"), nullable=True, index=True)

    archive_ref = Column(String(80), unique=True, nullable=False, index=True)
    rule_id = Column(String(64), nullable=False, index=True)
    batch_id = Column(String(64), nullable=True, index=True)
    action = Column(String(32), nullable=False, default="purge_payload")
    archived_at = Column(DateTime(timezone=True), nullable=False)

    summary = Column(JSON, nullable=False)
    payload_audit_ref = Column(String(200), nullable=True)
    removed_fields = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RetentionBatch(Base):
    """归档批次：幂等键唯一，状态可在重启后续跑。"""

    __tablename__ = "retention_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(String(64), unique=True, nullable=False, index=True)
    idempotency_key = Column(String(128), unique=True, nullable=True, index=True)
    status = Column(String(20), nullable=False, default="running", index=True)

    requested_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    triggered_by = Column(String(100), nullable=True)

    total = Column(Integer, nullable=False, default=0)
    succeeded = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)

    items = relationship("RetentionBatchItem", back_populates="batch", cascade="all, delete-orphan")


class RetentionBatchItem(Base):
    """批次内单个对象的处理结果，支撑部分失败与重启恢复。"""

    __tablename__ = "retention_batch_items"

    id = Column(Integer, primary_key=True, index=True)
    batch_pk = Column(Integer, ForeignKey("retention_batches.id"), nullable=False, index=True)
    subject_type = Column(String(32), nullable=False)
    operation_id = Column(Integer, nullable=True, index=True)
    dataset_version_id = Column(Integer, nullable=True, index=True)

    status = Column(String(20), nullable=False, index=True)
    rule_id = Column(String(64), nullable=True)
    archive_ref = Column(String(80), nullable=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    batch = relationship("RetentionBatch", back_populates="items")


class RetentionAuditEvent(Base):
    """保留/冻结/归档全流程的审计事件。"""

    __tablename__ = "retention_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    subject_type = Column(String(32), nullable=True, index=True)
    subject_ref = Column(String(64), nullable=True, index=True)
    hold_key = Column(String(64), nullable=True, index=True)
    rule_id = Column(String(64), nullable=True, index=True)
    batch_id = Column(String(64), nullable=True, index=True)
    actor = Column(String(100), nullable=True)
    detail = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
