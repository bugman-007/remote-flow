"""Request models (Pydantic v2). Responses are produced by ``app.serializers``."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Model):
    email: str
    password: str
    as_admin: bool = False


class ChangePasswordRequest(Model):
    current_password: str
    new_password: str = Field(min_length=10)


class SubmitJobRequest(Model):
    jd_text: str


class RetryRequest(Model):
    mode: Literal["render_only", "new_llm_call"]


class BulkSelectRequest(Model):
    ids: list[str]
    selected: bool = True


class ZipRequest(Model):
    ids: list[str]
    generation: str | None = None


class UseGenerationRequest(Model):
    generation_id: str


class PromptVersionRequest(Model):
    body: str
    change_note: str | None = None


class ProfileRequest(Model):
    name: str = Field(min_length=1, max_length=200)
    url: str | None = None
    description: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    theme_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None
    shared_fields: list[str] | None = None
    prompt_body: str | None = None
    prompt_change_note: str | None = None


class ProfileUpdate(Model):
    name: str | None = None
    url: str | None = None
    description: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    theme_id: str | None = None
    provider_id: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None
    shared_fields: list[str] | None = None


class AssignmentRequest(Model):
    maker_ids: list[str]


PROVIDER_TYPE_VALUES = (
    "anthropic", "openai", "azure_openai", "google_gemini", "openrouter", "openai_compatible", "mock"
)


class ProviderRequest(Model):
    type: Literal[
        "anthropic", "openai", "azure_openai", "google_gemini", "openrouter", "openai_compatible", "mock"
    ]
    display_name: str
    api_key: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    max_concurrency: int = Field(default=8, ge=1)
    #: ``None`` means "no cap"; the router stores the 60 rpm default.
    rpm: int | None = Field(default=None, ge=1)
    timeout_s: int = Field(default=600, ge=5)
    is_enabled: bool = True


class ProviderUpdate(Model):
    type: Literal[
        "anthropic", "openai", "azure_openai", "google_gemini", "openrouter", "openai_compatible", "mock"
    ] | None = None
    display_name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    max_concurrency: int | None = Field(default=None, ge=1)
    rpm: int | None = Field(default=None, ge=1)
    timeout_s: int | None = Field(default=None, ge=5)
    is_enabled: bool | None = None


class ThemeRequest(Model):
    name: str
    description: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class TaxonomyRequest(Model):
    """INT-14: a manager-defined interview step or status label."""

    name: str = Field(min_length=1, max_length=120)
    color: str | None = None


class TaxonomyUpdate(Model):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    color: str | None = None
    position: int | None = None
    is_active: bool | None = None


class StepRecordRequest(Model):
    """INT-15: add a step hop to an interview."""

    step_id: str | None = None
    reviewer_id: str | None = None
    note: str | None = None


class StepRecordUpdate(Model):
    reviewer_id: str | None = None
    done: bool | None = None
    rejected: bool | None = None
    note: str | None = None


class ThemePreviewRequest(Model):
    """SET-10: preview unsaved theme params inside the theme dialog."""

    params: dict[str, Any] = Field(default_factory=dict)


class ThemeUpdate(Model):
    name: str | None = None
    description: str | None = None
    params: dict[str, Any] | None = None
    status: Literal["active", "archived"] | None = None


class ThemeAssignRequest(Model):
    profile_ids: list[str]


class UserRequest(Model):
    name: str
    email: str
    role: Literal["maker", "manager", "reviewer"]
    daily_limit: int | None = None
    profile_id: str | None = None
    password: str | None = None
    must_change_password: bool = True
    #: USR-9: free-form Manager note shown on the Users page card.
    info: str | None = None


class UserUpdate(Model):
    name: str | None = None
    email: str | None = None
    role: Literal["maker", "manager", "reviewer"] | None = None
    daily_limit: int | None = None
    is_active: bool | None = None
    profile_id: str | None = None
    must_change_password: bool | None = None
    info: str | None = None


class SettingsUpdate(Model):
    values: dict[str, Any]


class InterviewTemplateRequest(Model):
    name: str
    fields: list[dict[str, Any]] = Field(default_factory=list)
    is_default: bool = False


class InterviewRequest(Model):
    """INT-3/INT-14/INT-16: schedule an existing doc set or a hand-made interview."""

    doc_set_id: str | None = None
    reviewer_id: str | None = None
    template_id: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    meeting_at: datetime | None = None
    meeting_tz: str | None = None
    step_id: str | None = None
    status_id: str | None = None
    tech_stack: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    candidate_name: str | None = None
    attachment_ids: list[str] = Field(default_factory=list)


class InterviewUpdate(Model):
    reviewer_id: str | None = None
    values: dict[str, Any] | None = None
    meeting_at: datetime | None = None
    meeting_tz: str | None = None
    status: Literal["scheduled", "completed", "cancelled", "no_show"] | None = None
    template_id: str | None = None
    status_id: str | None = None
    tech_stack: str | None = None
    company_name: str | None = None
    job_title: str | None = None


class FeedbackRequest(Model):
    outcome: Literal["pass", "fail", "hold", "no_show"]
    rating: int | None = Field(default=None, ge=1, le=5)
    strengths: str | None = None
    concerns: str | None = None
    notes: str | None = None


class DuplicateInterviewRequest(Model):
    reviewer_id: str


class TestPromptRequest(Model):
    jd_text: str
    #: When set, test this text instead of the saved active prompt.
    prompt_body: str | None = None


class DocSetPatch(Model):
    company_name: str | None = None
    job_title: str | None = None
    keep: bool | None = None


class CsvImportRequest(Model):
    csv: str


class SelectRequest(Model):
    selected: bool = True
