"""Data model (§6). Every table has id/created_at/updated_at; deletes are rare."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator

from app.db import Base
from app.utils import new_id, utcnow

JSONType = JSON().with_variant(JSONB, "postgresql")


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime that also behaves on SQLite.

    PostgreSQL stores ``timestamptz``; SQLite has no tz support, so values are
    stored as naive UTC and re-tagged on read. Comparisons in Python therefore
    work identically on both backends.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            from datetime import UTC

            value = value.replace(tzinfo=UTC)
        if dialect.name == "sqlite":
            return value.astimezone(__import__("datetime").UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            from datetime import UTC

            return value.replace(tzinfo=UTC)
        return value


class DateTimeTZ(UTCDateTime):
    """Alias used by the column definitions below."""

    cache_ok = True


class GUID(TypeDecorator):
    """UUID primary key: native uuid on PostgreSQL, CHAR(36) elsewhere."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=False))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return str(value)


def enum_col(*values: str, name: str):
    return Enum(*values, name=name, native_enum=False, validate_strings=True, length=32)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTimeTZ, default=utcnow, onupdate=utcnow, nullable=False
    )


class PkMixin(TimestampMixin):
    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=new_id)


# ---------------------------------------------------------------- users & auth

ROLE_VALUES = ("maker", "manager", "reviewer")


class User(PkMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    role: Mapped[str] = mapped_column(enum_col(*ROLE_VALUES, name="user_role"), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)

    jobs: Mapped[list["Job"]] = relationship(back_populates="maker", foreign_keys="Job.maker_id")


class RefreshToken(PkMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTimeTZ, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip: Mapped[str | None] = mapped_column(String(64))


# ------------------------------------------------------------------- profiles


class Profile(PkMixin, Base):
    __tablename__ = "profiles"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    description: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    theme_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("themes.id"))
    provider_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("llm_providers.id"))
    model: Mapped[str | None] = mapped_column(String(200))
    temperature: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[list | None] = mapped_column(JSONType, default=list)
    custom_fields: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    shared_fields: Mapped[list | None] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(
        enum_col("active", "archived", name="profile_status"), default="active", nullable=False
    )
    active_prompt_version_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("prompt_versions.id"))

    prompt_versions: Mapped[list["PromptVersion"]] = relationship(
        back_populates="profile", foreign_keys="PromptVersion.profile_id", order_by="PromptVersion.version_no"
    )


class PromptVersion(PkMixin, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("profile_id", "version_no", name="uq_prompt_version"),)

    profile_id: Mapped[str] = mapped_column(GUID, ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    change_note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))

    profile: Mapped[Profile] = relationship(back_populates="prompt_versions", foreign_keys=[profile_id])


class ProfileAssignment(PkMixin, Base):
    __tablename__ = "profile_assignments"
    __table_args__ = (
        Index(
            "ix_profile_assignments_active_maker",
            "maker_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
            sqlite_where=text("ended_at IS NULL"),
        ),
    )

    profile_id: Mapped[str] = mapped_column(GUID, ForeignKey("profiles.id"), index=True, nullable=False)
    maker_id: Mapped[str] = mapped_column(GUID, ForeignKey("users.id"), index=True, nullable=False)
    assigned_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)


# ------------------------------------------------------------------ providers


class LLMProvider(PkMixin, Base):
    __tablename__ = "llm_providers"
    __table_args__ = (UniqueConstraint("display_name", name="uq_provider_display_name"),)

    type: Mapped[str] = mapped_column(
        enum_col(
            "anthropic", "openai", "azure_openai", "google_gemini", "openrouter",
            "openai_compatible", "mock",
            name="provider_type",
        ),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    base_url: Mapped[str | None] = mapped_column(String(1000))
    default_model: Mapped[str | None] = mapped_column(String(200))
    max_concurrency: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    rpm: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    timeout_s: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    last_status: Mapped[str | None] = mapped_column(String(50))
    last_error: Mapped[str | None] = mapped_column(String(1000))


class Theme(PkMixin, Base):
    __tablename__ = "themes"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        enum_col("active", "archived", name="theme_status"), default="active", nullable=False
    )


# ------------------------------------------------------------- jobs & pipeline


class Job(PkMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("maker_id", "submitted_date", "seq_no", name="uq_jobs_ordering_key"),
        UniqueConstraint("maker_id", "idempotency_key", name="uq_jobs_idempotency"),
        Index(
            "ix_jobs_pending_cursor",
            "maker_id",
            "submitted_date",
            "seq_no",
            postgresql_where=text("delivery_status = 'pending'"),
            sqlite_where=text("delivery_status = 'pending'"),
        ),
    )

    maker_id: Mapped[str] = mapped_column(GUID, ForeignKey("users.id"), index=True, nullable=False)
    profile_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("profiles.id"))
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    jd_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    duplicate_of: Mapped[str | None] = mapped_column(GUID, ForeignKey("jobs.id"))
    delivery_status: Mapped[str] = mapped_column(
        enum_col("pending", "released", "skipped", name="job_delivery_status"),
        default="pending",
        nullable=False,
        index=True,
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    released_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    skipped_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    skipped_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    initial_generation_id: Mapped[str | None] = mapped_column(GUID)
    jd_tsv: Mapped[str | None] = mapped_column(Text)

    maker: Mapped[User] = relationship(back_populates="jobs", foreign_keys=[maker_id])
    doc_set: Mapped["DocSet | None"] = relationship(back_populates="job", uselist=False)
    generations: Mapped[list["Generation"]] = relationship(
        back_populates="job", order_by="Generation.generation_no"
    )


class DocSet(PkMixin, Base):
    __tablename__ = "doc_sets"

    job_id: Mapped[str] = mapped_column(GUID, ForeignKey("jobs.id"), unique=True, nullable=False)
    candidate_name: Mapped[str | None] = mapped_column(String(300))
    company_name: Mapped[str | None] = mapped_column(String(300), index=True)
    job_title: Mapped[str | None] = mapped_column(String(300))
    slug: Mapped[str | None] = mapped_column(String(120))
    storage_dir: Mapped[str | None] = mapped_column(String(500))
    docx_basename: Mapped[str | None] = mapped_column(String(300))
    current_generation_id: Mapped[str | None] = mapped_column(GUID)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    selected_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    selected_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    keep: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    renamed_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    files_expired_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    #: RES-14: when the owning Maker last downloaded this set; ``None`` means "New".
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTimeTZ, index=True)
    search_tsv: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="doc_set")
    interviews: Mapped[list["Interview"]] = relationship(back_populates="doc_set")


class Generation(PkMixin, Base):
    __tablename__ = "generations"
    __table_args__ = (
        UniqueConstraint("job_id", "generation_no", name="uq_generation_no"),
        Index("ix_generations_dispatch_pending", "dispatch_state", "status"),
        Index("ix_generations_next_retry", "next_retry_at"),
        Index("ix_generations_lease", "lease_expires_at"),
        Index(
            "ix_generations_ready_initial",
            "job_id",
            postgresql_where=text("status = 'ready' AND kind = 'initial'"),
            sqlite_where=text("status = 'ready' AND kind = 'initial'"),
        ),
    )

    job_id: Mapped[str] = mapped_column(GUID, ForeignKey("jobs.id"), index=True, nullable=False)
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(
        enum_col("initial", "retry_after_skip", "regenerate", name="generation_kind"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        enum_col(
            "queued", "llm_running", "rendering", "retry_wait", "needs_attention", "ready", "cancelled",
            name="generation_status",
        ),
        default="queued",
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(enum_col("llm", "render", name="generation_stage"), default="llm", nullable=False)
    llm_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    render_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_budget_reset_at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    dispatch_state: Mapped[str] = mapped_column(
        enum_col("pending", "sent", name="dispatch_state"), default="pending", nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    claimed_by: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[str | None] = mapped_column(GUID)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    consecutive_provider_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(String(2000))

    prompt_version_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("prompt_versions.id"))
    provider_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("llm_providers.id"))
    model: Mapped[str | None] = mapped_column(String(200))
    llm_params: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    theme_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("themes.id"))
    theme_snapshot: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    storage_dir: Mapped[str | None] = mapped_column(String(500))
    ready_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    created_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    expired_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)

    job: Mapped[Job] = relationship(back_populates="generations")
    attempts: Mapped[list["GenerationAttempt"]] = relationship(
        back_populates="generation", order_by="GenerationAttempt.started_at"
    )
    files: Mapped[list["FileArtifact"]] = relationship(back_populates="generation")


class GenerationAttempt(PkMixin, Base):
    __tablename__ = "generation_attempts"
    __table_args__ = (UniqueConstraint("generation_id", "stage", "attempt_no", name="uq_attempt_no"),)

    generation_id: Mapped[str] = mapped_column(GUID, ForeignKey("generations.id"), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(enum_col("llm", "render", name="attempt_stage"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(GUID)
    worker: Mapped[str | None] = mapped_column(String(200))
    provider_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("llm_providers.id"))
    model: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    outcome: Mapped[str | None] = mapped_column(
        enum_col("succeeded", "failed", "timed_out", "superseded", name="attempt_outcome")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_cached: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    artifacts_dir: Mapped[str | None] = mapped_column(String(500))

    generation: Mapped[Generation] = relationship(back_populates="attempts")


class PipelineEvent(PkMixin, Base):
    __tablename__ = "pipeline_events"

    job_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("jobs.id"), index=True)
    generation_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("generations.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(50))
    to_state: Mapped[str | None] = mapped_column(String(50))
    stage: Mapped[str | None] = mapped_column(String(20))
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)


class EventOutbox(PkMixin, Base):
    __tablename__ = "event_outbox"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at"),)

    audience: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class FileArtifact(PkMixin, Base):
    __tablename__ = "files"
    __table_args__ = (Index("ix_files_generation_kind", "generation_id", "kind"),)

    generation_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("generations.id"), index=True)
    doc_set_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("doc_sets.id"), index=True)
    kind: Mapped[str] = mapped_column(
        enum_col("pdf", "docx", "txt", "llm_json", "meta", "jd", name="file_kind"), nullable=False
    )
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    filename: Mapped[str] = mapped_column(String(300), nullable=False, default="document")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    expired_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)

    generation: Mapped[Generation | None] = relationship(back_populates="files")


# ---------------------------------------------------------------- interviews


class InterviewTemplate(PkMixin, Base):
    __tablename__ = "interview_templates"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    fields: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))


class Interview(PkMixin, Base):
    __tablename__ = "interviews"

    doc_set_id: Mapped[str] = mapped_column(GUID, ForeignKey("doc_sets.id"), index=True, nullable=False)
    generation_id: Mapped[str] = mapped_column(GUID, ForeignKey("generations.id"), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"), index=True)
    template_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("interview_templates.id"))
    template_snapshot: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    values: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    meeting_at: Mapped[datetime | None] = mapped_column(DateTimeTZ, index=True)
    meeting_tz: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        enum_col("scheduled", "completed", "cancelled", "no_show", name="interview_status"),
        default="scheduled",
        nullable=False,
        index=True,
    )
    created_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    seen_by_reviewer_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)

    doc_set: Mapped[DocSet] = relationship(back_populates="interviews")
    feedback: Mapped[list["Feedback"]] = relationship(
        back_populates="interview", order_by="Feedback.version_no"
    )


class InterviewEvent(PkMixin, Base):
    __tablename__ = "interview_events"

    interview_id: Mapped[str] = mapped_column(GUID, ForeignKey("interviews.id"), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))
    details: Mapped[dict | None] = mapped_column(JSONType, default=dict)
    at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)


class Feedback(PkMixin, Base):
    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("interview_id", "version_no", name="uq_feedback_version"),)

    interview_id: Mapped[str] = mapped_column(GUID, ForeignKey("interviews.id"), index=True, nullable=False)
    author_id: Mapped[str] = mapped_column(GUID, ForeignKey("users.id"), nullable=False)
    outcome: Mapped[str | None] = mapped_column(
        enum_col("pass", "fail", "hold", "no_show", name="feedback_outcome")
    )
    rating: Mapped[int | None] = mapped_column(Integer)
    strengths: Mapped[str | None] = mapped_column(Text)
    concerns: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    version_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    interview: Mapped[Interview] = relationship(back_populates="feedback")


# --------------------------------------------------------------- ops & stats


class Setting(PkMixin, Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[Any] = mapped_column(JSONType)
    updated_by: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"))


class AuditLog(PkMixin, Base):
    __tablename__ = "audit_log"

    actor_id: Mapped[str | None] = mapped_column(GUID, ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSONType)
    after: Mapped[dict | None] = mapped_column(JSONType)
    ip: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)


class DailyMakerStat(PkMixin, Base):
    __tablename__ = "daily_maker_stats"
    __table_args__ = (UniqueConstraint("maker_id", "date", name="uq_daily_maker_stat"),)

    maker_id: Mapped[str] = mapped_column(GUID, ForeignKey("users.id"), index=True, nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    submitted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ready: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_ready_ms: Mapped[int | None] = mapped_column(Integer)
    avg_llm_attempts: Mapped[float | None] = mapped_column(Float)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class WorkerHeartbeat(PkMixin, Base):
    """SET-14: which workers are alive and when they last reported in."""

    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    queues: Mapped[list | None] = mapped_column(JSONType)
    concurrency: Mapped[int | None] = mapped_column(Integer)
    pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTimeTZ, default=utcnow, nullable=False)


class SystemMetric(PkMixin, Base):
    """SET-14: small cumulative counters (sweeps, re-dispatches, expired leases, bytes freed)."""

    __tablename__ = "system_metrics"

    key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    value_int: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
