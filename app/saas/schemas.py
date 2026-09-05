"""Typed public contracts for the hosted AutoApply Cloud API.

No request model accepts ``user_id``. Tenant identity always comes from the verified
Supabase bearer token.
"""
from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .profile_urls import is_placeholder_profile_url


def _validated_http_url(value: str | None, label: str) -> str | None:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{label} must use http or https without embedded credentials")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AccountDeletionRequest(StrictModel):
    """Explicit acknowledgement required before irreversible account deletion."""

    confirmation: Literal["DELETE"]


class BrowserbaseLocalAbandonRequest(StrictModel):
    """Explicit acknowledgement for abandoning unreachable remote resources."""

    confirmation: Literal["ABANDON REMOTE BROWSER DATA"]


class GoogleOAuthClientUpsert(StrictModel):
    """A user's self-managed Google Web OAuth client credentials.

    These values are accepted only by the authenticated control plane and are
    encrypted before they reach the service-role-only database table.
    """

    client_id: str = Field(min_length=30, max_length=512)
    client_secret: str = Field(min_length=8, max_length=512, repr=False)

    @field_validator("client_id", "client_secret", mode="before")
    @classmethod
    def reject_implicit_trimming(cls, value: Any) -> Any:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Google OAuth credentials cannot contain surrounding whitespace")
        return value

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        if (
            not value.isascii()
            or any(character.isspace() or ord(character) < 33 for character in value)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*\.apps\.googleusercontent\.com",
                value,
            )
        ):
            raise ValueError("Enter a Google Web OAuth client ID")
        return value

    @field_validator("client_secret")
    @classmethod
    def validate_client_secret(cls, value: str) -> str:
        if not value.isascii() or any(
            character.isspace() or ord(character) < 33 or ord(character) > 126
            for character in value
        ):
            raise ValueError("Enter a valid Google OAuth client secret")
        return value


class GoogleOAuthStartRequest(StrictModel):
    """Choose which OAuth app should authorize the user's Gmail account."""

    credential_source: Literal["platform", "user"] = "platform"


class ProviderCredentialUpsert(StrictModel):
    """One tenant-owned provider credential accepted by the control plane.

    The route selects the provider from the URL.  ``project_id`` is required only
    for Browserbase; Groq rejects it.  Secret fields are deliberately
    omitted from model representations and validation errors are rendered by the
    application's non-reflecting validation handler.
    """

    api_key: str = Field(min_length=8, max_length=512, repr=False)
    project_id: str | None = Field(default=None, min_length=6, max_length=255)

    @field_validator("api_key", "project_id", mode="before")
    @classmethod
    def reject_implicit_credential_trimming(cls, value: Any) -> Any:
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Provider credentials cannot contain surrounding whitespace")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        if not value.isascii() or any(
            character.isspace() or ord(character) < 33 or ord(character) > 126
            for character in value
        ):
            raise ValueError("Enter a valid provider API key")
        return value

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.isascii() or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{5,254}", value
        ):
            raise ValueError("Enter a valid Browserbase project ID")
        return value


class ProfileUpdate(StrictModel):
    full_name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=60)
    location: str | None = Field(default=None, max_length=200)
    headline: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=5_000)
    years_experience: float | None = Field(default=None, ge=0, le=80)
    work_authorization: str | None = Field(default=None, max_length=500)
    notice_period: str | None = Field(default=None, max_length=200)
    college: str | None = Field(default=None, max_length=300)
    degree: str | None = Field(default=None, max_length=300)
    graduation_year: int | None = Field(default=None, ge=1950, le=2100)
    linkedin_url: str | None = Field(default=None, max_length=2_048)
    github_url: str | None = Field(default=None, max_length=2_048)
    portfolio_url: str | None = Field(default=None, max_length=2_048)
    resume_url: str | None = Field(default=None, max_length=2_048)
    education: list[dict[str, Any]] | None = None
    skills: list[str] | None = Field(default=None, max_length=200)
    preferences: dict[str, Any] | None = None
    onboarding_completed: bool | None = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid email address")
        return value

    @field_validator("linkedin_url", "github_url", "portfolio_url")
    @classmethod
    def validate_optional_url(cls, value: str | None) -> str | None:
        validated = _validated_http_url(value, "URL")
        if is_placeholder_profile_url(validated):
            raise ValueError("Enter your actual profile URL instead of a placeholder")
        return validated

    @field_validator("resume_url")
    @classmethod
    def validate_public_resume_url(cls, value: str | None) -> str | None:
        validated = _validated_http_url(value, "Public resume URL")
        if not validated:
            return validated
        parsed = urlsplit(validated)
        if parsed.scheme != "https":
            raise ValueError("Public resume URL must use https")
        if is_placeholder_profile_url(validated):
            raise ValueError("Enter your actual public resume URL instead of a placeholder")
        return validated


class UserSettingsUpdate(StrictModel):
    daily_send_cap: int | None = Field(default=None, ge=0, le=150)
    duplicate_window_days: int | None = Field(default=None, ge=1, le=90)
    require_review: bool | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("Enter a valid IANA timezone") from exc
        return value


class ResumeRegister(StrictModel):
    storage_path: str = Field(min_length=3, max_length=1_024)
    original_name: str = Field(min_length=1, max_length=255)
    mime_type: Literal["application/pdf"] = "application/pdf"
    size_bytes: int = Field(gt=0, le=6_291_456)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")

    @field_validator("storage_path")
    @classmethod
    def safe_storage_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/") or "\\" in value:
            raise ValueError("Invalid storage object path")
        return value

    @field_validator("original_name")
    @classmethod
    def pdf_name(cls, value: str) -> str:
        if (
            not value.lower().endswith(".pdf")
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("Résumé filename must end in .pdf")
        return value


class JobCreate(StrictModel):
    source: str = Field(default="manual", min_length=1, max_length=60)
    external_id: str | None = Field(default=None, max_length=255)
    apply_url: str | None = Field(default=None, max_length=2_048)
    title: str = Field(min_length=1, max_length=240)
    company: str = Field(min_length=1, max_length=240)
    location: str | None = Field(default=None, max_length=240)
    description: str = Field(min_length=20, max_length=25_000)
    contact_email: str | None = Field(default=None, max_length=320)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("apply_url")
    @classmethod
    def validate_apply_url(cls, value: str | None) -> str | None:
        return _validated_http_url(value, "Apply URL")

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid contact email")
        return value


class JobUpdate(StrictModel):
    apply_url: str | None = Field(default=None, max_length=2_048)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    company: str | None = Field(default=None, min_length=1, max_length=240)
    location: str | None = Field(default=None, max_length=240)
    description: str | None = Field(default=None, min_length=20, max_length=25_000)
    contact_email: str | None = Field(default=None, max_length=320)
    status: Literal["saved", "drafting", "ready", "applied", "archived"] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("apply_url")
    @classmethod
    def validate_apply_url(cls, value: str | None) -> str | None:
        return _validated_http_url(value, "Apply URL")

    @field_validator("contact_email")
    @classmethod
    def validate_contact_email(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid contact email")
        return value


class ApplicationCreate(StrictModel):
    job_id: UUID | None = None
    channel: Literal["email", "manual", "ats"] = "email"
    recipient: str | None = Field(default=None, max_length=320)
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("recipient")
    @classmethod
    def validate_recipient(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid recipient email")
        return value


class ApplicationUpdate(StrictModel):
    recipient: str | None = Field(default=None, max_length=320)
    subject: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=20_000)
    status: Literal[
        "draft_pending", "drafted", "approved", "manual", "applied",
        "rejected", "interview", "archived"
    ] | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("recipient")
    @classmethod
    def validate_recipient(cls, value: str | None) -> str | None:
        if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Enter a valid recipient email")
        return value


class SendApplicationRequest(StrictModel):
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    attach_resume: bool = True


class SendApplicationBatchRequest(StrictModel):
    """Queue a small, explicitly selected batch for the persistent Gmail worker."""

    application_ids: list[UUID] = Field(min_length=1, max_length=30)
    attach_resume: bool = True
    idempotency_key: str = Field(
        min_length=8, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )

    @field_validator("application_ids")
    @classmethod
    def unique_application_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Choose each application only once")
        return value


class OutreachResearchPromptRequest(StrictModel):
    """Optional overrides for the copy/paste external research brief."""

    target_role: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=160)
    remote_only: bool = False


class ApproveApplicationRequest(StrictModel):
    """Approve exactly the content revision the browser displayed to the user."""

    expected_revision: int = Field(ge=1)


class ReferralDigestIngest(StrictModel):
    text: str = Field(min_length=20, max_length=100_000)


class ResumeGuidedDiscoveryRequest(StrictModel):
    """Bounded inputs for deriving public-job searches from the active resume."""

    location: str | None = Field(default=None, max_length=120)
    remote_only: bool = False
    linkedin_limit: int = Field(default=20, ge=1, le=25)
    feed_limit: int = Field(default=60, ge=1, le=200)
    # These are intentionally optional for backwards compatibility with older
    # clients. The deployed UI sends them explicitly so one run has a clear
    # result budget and worker deadline.
    max_jobs: int | None = Field(default=None, ge=2, le=50)
    timeout_seconds: int | None = Field(default=None, ge=15, le=120)
    idempotency_key: str = Field(
        min_length=8, max_length=180, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )


class PublicFeedDiscoveryRequest(StrictModel):
    source_ids: list[str] = Field(default_factory=list, max_length=30)
    limit: int = Field(default=60, ge=1, le=200)
    timeout_seconds: int | None = Field(default=None, ge=15, le=120)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        clean = list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", item) for item in clean):
            raise ValueError("Choose valid discovery sources")
        return clean


class LinkedInDiscoveryRequest(StrictModel):
    keywords: str = Field(min_length=2, max_length=100)
    location: str | None = Field(default=None, max_length=120)
    remote_only: bool = False
    limit: int = Field(default=20, ge=1, le=25)
    timeout_seconds: int | None = Field(default=None, ge=15, le=120)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )


class PublicAtsDiscoveryRequest(StrictModel):
    urls: list[str] = Field(min_length=1, max_length=100)

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: list[str]) -> list[str]:
        clean: list[str] = []
        for item in value:
            validated = _validated_http_url(item, "ATS URL")
            if validated and validated not in clean:
                clean.append(validated)
        if not clean:
            raise ValueError("Add at least one public ATS URL")
        return clean


class PublicAtsBoardDiscoveryRequest(StrictModel):
    urls: list[str] = Field(min_length=1, max_length=8)
    limit: int = Field(default=100, ge=1, le=200)
    timeout_seconds: int | None = Field(default=None, ge=15, le=120)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: list[str]) -> list[str]:
        clean: list[str] = []
        for item in value:
            validated = _validated_http_url(item, "ATS board URL")
            if validated and validated not in clean:
                clean.append(validated)
        if not clean:
            raise ValueError("Add at least one public ATS board URL")
        return clean


class DiscoveryPreferencesUpdate(StrictModel):
    enabled_sources: list[str] | None = Field(default=None, max_length=30)
    keywords: list[str] | None = Field(default=None, max_length=50)
    excluded_keywords: list[str] | None = Field(default=None, max_length=50)
    locations: list[str] | None = Field(default=None, max_length=30)
    remote_only: bool | None = None
    schedule_enabled: bool | None = None
    schedule_interval_minutes: int | None = Field(default=None, ge=15, le=1_440)
    max_results_per_run: int | None = Field(default=None, ge=1, le=200)
    feed_urls: list[str] | None = Field(default=None, max_length=8)
    metadata: dict[str, Any] | None = None

    @field_validator("locations", "keywords", "excluded_keywords")
    @classmethod
    def validate_short_lists(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        clean = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if any(len(item) > 160 for item in clean):
            raise ValueError("Discovery preference values must be 160 characters or fewer")
        return clean

    @field_validator("enabled_sources")
    @classmethod
    def validate_enabled_sources(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        clean = list(dict.fromkeys(item.strip().lower() for item in value if item.strip()))
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", item) for item in clean):
            raise ValueError("Choose valid discovery sources")
        return clean

    @field_validator("feed_urls")
    @classmethod
    def validate_feed_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        clean: list[str] = []
        for item in value:
            validated = _validated_http_url(item, "Feed URL")
            if validated and validated not in clean:
                clean.append(validated)
        return clean


class YcApplicationPreferencesUpdate(StrictModel):
    """Optional local matching controls; this contract never starts YC discovery."""

    query: str | None = Field(default=None, max_length=160)
    remote_only: bool | None = None
    limit: int | None = Field(default=None, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = " ".join(value.split())
        if not clean:
            return None
        if any(ord(character) < 32 for character in clean):
            raise ValueError("YC matching query cannot contain control characters")
        return clean


class ApplicationFormApprovalRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    schema_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    answers: dict[str, Any] = Field(default_factory=dict)


class ApplicationFormSubmissionResolutionRequest(StrictModel):
    """A user's explicit resolution of an uncertain provider submission."""

    outcome: Literal["submitted", "not_submitted"]
    expected_revision: int = Field(ge=1)
    schema_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class ApplicationStageRequest(StrictModel):
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    form_revision_id: UUID | None = None


class AutomationJobCreate(StrictModel):
    kind: Literal[
        "manual_handoff",
        "ats_prepare",
        "connection_check",
        "discover_public_feeds",
        "discover_linkedin_guest",
        "discover_public_ats",
        "application_scan",
        "application_prefill",
        "application_submit",
    ]
    provider: str | None = Field(default=None, max_length=80)
    application_id: UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", clean):
            raise ValueError("Choose a valid provider")
        return clean


def normalized_http_url(value: str | None) -> str | None:
    """Normalize a user URL for tenant-local deduplication without fetching it."""
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        parsed_port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    host = parsed.hostname.lower()
    port = f":{parsed_port}" if parsed_port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), host + port, path, parsed.query, ""))
