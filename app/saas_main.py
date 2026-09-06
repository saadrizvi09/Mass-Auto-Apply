"""AutoApply Cloud's stateless FastAPI control plane.

This module is the only Vercel entrypoint. It deliberately does not import the legacy
SQLite, scheduler, desktop OAuth, or Playwright application.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse

from app.saas.auth import AuthUser, SupabaseAuth
from app.saas.browser import BrowserbaseClient, BrowserbaseError
from app.saas.config import Settings, get_settings
from app.saas.crypto import TokenCipher, TokenCipherError
from app.saas.errors import ApiError, install_exception_handlers
from app.saas.discovery import (
    DEFAULT_RSS_FEEDS,
    DEFAULT_TELEGRAM_CHANNELS,
    MAX_PUBLIC_ATS_BOARDS,
    MAX_PUBLIC_ATS_RESULTS,
    canonical_public_ats_board_url,
    detect_provider,
    discover_provider_urls,
    parse_referral_digest,
    referral_digest_summary,
    parse_spreadsheet_bytes,
    public_company_form_target,
)
from app.saas.gmail import (
    GMAIL_SEND_SCOPE,
    GoogleProviderError,
    build_google_authorization_url,
    exchange_google_code,
    get_google_userinfo,
    revoke_google_token,
)
from app.saas.groq import (
    GroqProviderError,
    analyze_resume_profile,
    generate_application_draft,
    generate_form_answer_suggestions,
    profile_form_answers,
    validate_groq_key,
)
from app.saas.matching import enrich_jobs_with_fit, recommended_roles
from app.saas.providers import (
    browser_provider_allowed,
    canonical_yc_job_url,
    get_provider,
    provider_catalog,
)
from app.saas.public_contacts import public_contact_candidates
from app.saas.outreach_prompt import build_research_prompt
from app.saas.resume import ResumeParseError, extract_pdf_text, profile_suggestions
from app.saas.schemas import (
    AccountDeletionRequest,
    BrowserbaseLocalAbandonRequest,
    ApplicationFormApprovalRequest,
    ApplicationFormSubmissionResolutionRequest,
    ApplicationStageRequest,
    ApproveApplicationRequest,
    ApplicationCreate,
    ApplicationUpdate,
    DraftApplicationRequest,
    AutomationJobCreate,
    DiscoveryPreferencesUpdate,
    GoogleOAuthClientUpsert,
    GoogleOAuthStartRequest,
    JobCreate,
    JobUpdate,
    LinkedInDiscoveryRequest,
    ProviderCredentialUpsert,
    PublicAtsBoardDiscoveryRequest,
    PublicAtsDiscoveryRequest,
    PublicContactDiscoveryRequest,
    PublicFeedDiscoveryRequest,
    ReferralDigestIngest,
    ResumeGuidedDiscoveryRequest,
    ProfileUpdate,
    ResumeRegister,
    SendApplicationRequest,
    SendApplicationBatchRequest,
    OutreachResearchPromptRequest,
    UserSettingsUpdate,
    YcApplicationPreferencesUpdate,
    normalized_http_url,
)
from app.saas.store import StoreClient, SupabaseStore


VERSION = "2.0.0"
PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"
PUBLIC_ASSET_DIR = (PUBLIC_DIR / "assets").resolve()
RESUME_BUCKET = "resumes"
RESUME_ANALYSIS_TIMEOUT_SECONDS = 50
STORAGE_LIST_PAGE_SIZE = 1000
MAX_ACCOUNT_STORAGE_ENTRIES = 20_000
MAX_JOB_IMPORT_BYTES = 4 * 1024 * 1024
MANAGED_BROWSER_LIFECYCLE_PROVIDERS = frozenset(
    {
        "google_forms",
        "greenhouse",
        "lever",
        "ashby",
        "yc",
        "wellfound",
        "cutshort",
        "instahyre",
    }
)
APPLICATION_AUTOMATION_PROVIDERS = MANAGED_BROWSER_LIFECYCLE_PROVIDERS | {
    "company_form"
}
COMPANY_FORM_METADATA_KEYS = frozenset(
    {
        "company_form_host",
        "company_form_target_url",
    }
)
YC_TARGET_METADATA_KEYS = frozenset({"yc_job_target_url"})
PERSISTENT_CONTEXT_REQUIRED_PROVIDERS = frozenset(
    {"yc", "wellfound", "cutshort", "instahyre"}
)
ACCOUNT_DELETION_REAUTH_WINDOW = timedelta(minutes=10)
ACCOUNT_DELETION_CLOCK_SKEW = timedelta(minutes=2)
# Interactive provider login is the longest Browserbase flow.  Sessions are
# released as soon as the user completes login; this is only a hard ceiling for
# abandoned tabs so they cannot consume five full minutes per attempt.
MANAGED_BROWSER_LOGIN_TIMEOUT_SECONDS = 180
STORED_PROVIDER_IDS = ("groq", "browserbase")
PROVIDER_CREDENTIAL_LINKS: dict[str, dict[str, str]] = {
    "groq": {"key_url": "https://console.groq.com/keys"},
    "browserbase": {
        "key_url": "https://www.browserbase.com/settings",
        "signup_url": "https://www.browserbase.com/sign-up",
        "project_url": "https://www.browserbase.com/overview",
    },
}


@dataclass(frozen=True, slots=True, repr=False)
class GoogleOAuthCredentials:
    """A server-only OAuth client selected for one Google authorization flow."""

    source: Literal["platform", "user"]
    client_id: str
    client_secret: str
    credential_generation: int | None = None


@dataclass(frozen=True, slots=True, repr=False)
class StoredProviderCredential:
    """A decrypted tenant credential that never crosses an API response boundary."""

    provider: Literal["groq", "browserbase"]
    api_key: str
    project_id: str | None
    verification_status: Literal["verified", "unverified", "invalid"]
    verification_code: str | None
    generation: int
    binding_fingerprint: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class BrowserbaseCredentials:
    """The Browserbase account selected consistently for one tenant."""

    source: Literal["platform", "user"]
    api_key: str
    project_id: str
    generation: int | None = None
    project_fingerprint: str = ""
    epoch: int = 0


def _browserbase_project_fingerprint(project_id: str) -> str:
    """Return a domain-separated non-reversible binding for one project ID."""

    return hashlib.sha256(
        b"autoapply.browserbase.project.v1\x00" + project_id.encode("utf-8")
    ).hexdigest()


def _first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else None
    return value if isinstance(value, dict) else None


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _browser_lifecycle_snapshot(value: Any, *, include_reuse: bool = False) -> dict[str, Any]:
    """Validate a service-role lifecycle RPC response before reusing its values."""

    if not isinstance(value, Mapping):
        raise ApiError(
            503,
            "data_store_invalid_response",
            "The data service returned an invalid browser lifecycle response.",
        )
    generation = _positive_integer(value.get("generation"))
    connection_id = value.get("connection_id")
    if connection_id is not None:
        try:
            connection_id = str(UUID(str(connection_id)))
        except (TypeError, ValueError, AttributeError):
            connection_id = None
            generation = None
    result: dict[str, Any] = {
        "generation": generation,
        "connection_id": connection_id,
    }
    for key in ("context_ciphertext", "session_ciphertext"):
        ciphertext = value.get(key)
        if ciphertext is not None and (
            not isinstance(ciphertext, str) or not ciphertext or len(ciphertext) > 16_384
        ):
            generation = None
        result[key] = ciphertext
    # These fields describe an already-persisted context.  Start responses also
    # carry a separate active credential binding below; keeping the names
    # distinct prevents an old context from being mistaken for the credential
    # selected for a new Browserbase session.
    credential_source = value.get("context_credential_source")
    if credential_source is None:
        credential_source = value.get("credential_source")
    if credential_source is not None and credential_source not in {"platform", "user"}:
        generation = None
    credential_generation = value.get("context_credential_generation")
    if credential_generation is None:
        credential_generation = value.get("credential_generation")
    if credential_generation is not None:
        credential_generation = _positive_integer(credential_generation)
        if credential_generation is None:
            generation = None
    project_fingerprint = value.get("context_project_fingerprint")
    if project_fingerprint is None:
        project_fingerprint = value.get("project_fingerprint")
    if project_fingerprint is not None and (
        not isinstance(project_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", project_fingerprint)
    ):
        generation = None
    result.update(
        {
            "credential_source": credential_source,
            "credential_generation": credential_generation,
            "project_fingerprint": project_fingerprint,
        }
    )
    credential_epoch = value.get("context_credential_epoch")
    if credential_epoch is None and not include_reuse:
        credential_epoch = value.get("credential_epoch")
    if credential_epoch is not None:
        credential_epoch = _nonnegative_integer(credential_epoch)
        if credential_epoch is None:
            generation = None
    result["context_credential_epoch"] = credential_epoch
    if not include_reuse:
        result["credential_epoch"] = credential_epoch
    if include_reuse:
        reuse_context = value.get("reuse_context")
        if not isinstance(reuse_context, bool):
            generation = None
        result["reuse_context"] = reuse_context
        active_epoch = _nonnegative_integer(value.get("credential_epoch"))
        active_source = value.get("active_credential_source")
        active_generation = value.get("active_credential_generation")
        active_fingerprint = value.get("active_project_fingerprint")
        if active_source not in {"platform", "user"} or active_epoch is None:
            generation = None
        if active_generation is not None:
            active_generation = _positive_integer(active_generation)
            if active_generation is None:
                generation = None
        if active_fingerprint is not None and (
            not isinstance(active_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", active_fingerprint)
        ):
            generation = None
        if active_source == "user" and (
            active_generation is None
            or active_generation != active_epoch
            or active_fingerprint is None
        ):
            generation = None
        if active_source == "platform" and active_generation is not None:
            generation = None
        result.update(
            {
                "active_credential_source": active_source,
                "active_credential_generation": active_generation,
                "active_project_fingerprint": active_fingerprint,
                "credential_epoch": active_epoch,
            }
        )
    if generation is None:
        raise ApiError(
            503,
            "data_store_invalid_response",
            "The data service returned an invalid browser lifecycle response.",
        )
    result["generation"] = generation
    return result


def _data(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"data": dict(value)}


def _items(value: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": value, "count": len(value)}


def _owned(user: AuthUser, **filters: Any) -> dict[str, Any]:
    return {"user_id": str(user.user_id), **filters}


def _model_changes(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_unset=True)


def _company_form_metadata(
    value: Any, target: Mapping[str, str] | None
) -> dict[str, Any]:
    """Strip client-supplied binding fields, then add one server-derived marker."""

    metadata = dict(value) if isinstance(value, Mapping) else {}
    for key in COMPANY_FORM_METADATA_KEYS:
        metadata.pop(key, None)
    if metadata.get("application_provider") == "company_form":
        metadata.pop("application_provider", None)
    if target is not None:
        metadata.update(
            {
                "application_provider": "company_form",
                "company_form_host": target["host"],
                "company_form_target_url": target["target_url"],
            }
        )
    return metadata


def _yc_target_metadata(value: Any, target_url: str | None) -> dict[str, Any]:
    """Remove client-spoofable YC authority and add the server-derived target."""

    metadata = dict(value) if isinstance(value, Mapping) else {}
    for key in YC_TARGET_METADATA_KEYS:
        metadata.pop(key, None)
    if metadata.get("application_provider") == "yc":
        metadata.pop("application_provider", None)
    if target_url is not None:
        metadata.update(
            {
                "application_provider": "yc",
                "yc_job_target_url": target_url,
            }
        )
    return metadata


def _bounded_text_list(
    value: Any,
    *,
    limit: int,
    max_length: int,
) -> list[str]:
    """Keep a stable, case-insensitively unique list from model/user output."""

    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        clean = " ".join(item.split())[:max_length].strip()
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def _nested_google_form_urls(value: Any, *, max_values: int = 2_000) -> list[str]:
    """Find bounded Google Form URLs already present in tenant-owned metadata."""

    found: list[str] = []
    pending: list[tuple[Any, int]] = [(value, 0)]
    visited = 0
    while pending and visited < max_values:
        item, depth = pending.pop()
        visited += 1
        if isinstance(item, str):
            if detect_provider(item) == "google_forms" and normalized_http_url(item):
                found.append(item.strip())
            continue
        if depth >= 4:
            continue
        if isinstance(item, Mapping):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item[:500])
    return list(dict.fromkeys(found))


def _public_automation_job(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only fields useful for following a newly queued run."""

    allowed = (
        "id",
        "kind",
        "provider",
        "status",
        "idempotency_key",
        "progress",
        "created_at",
        "updated_at",
    )
    return {key: value.get(key) for key in allowed if key in value}


def _public_contact_view(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose contact evidence without returning tenant/database internals."""

    email = str(value.get("email") or "").strip().lower()
    result: dict[str, Any] = {
        "email": email,
        "name": value.get("person_name") or value.get("name"),
        "position": value.get("person_title") or value.get("position"),
        "verification_status": value.get(
            "email_verification_status", value.get("verification_status")
        ),
    }
    for key in (
        "person_name",
        "person_title",
        "contact_type",
        "company_key",
        "contact_source",
        "source_date",
        "source_url",
        "linkedin_url",
        "domain",
        "source",
        "confidence",
    ):
        if key in value:
            result[key] = value.get(key)
    return result


def _contact_company_key(value: Any) -> str:
    """Match the worker's stable company key for cross-role review."""

    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return normalized[:160] or "unknown employer"


def _required_answer_preflight(revision: Mapping[str, Any]) -> dict[str, Any]:
    """Build bounded, non-secret submit preflight metadata for the worker."""

    schema = revision.get("question_schema")
    answers = revision.get("answers")
    questions = schema if isinstance(schema, list) else []
    approved_answers = answers if isinstance(answers, Mapping) else {}
    required_keys: list[str] = []
    missing_keys: list[str] = []
    missing_labels: list[str] = []
    resume_keys: list[str] = []

    def normalized_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()

    normalized_answers = {
        normalized_key(key): value
        for key, value in approved_answers.items()
        if isinstance(key, str) and normalized_key(key)
    }

    def answer_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, Mapping)):
            return bool(value)
        return True

    for index, item in enumerate(questions[:200]):
        if not isinstance(item, Mapping) or item.get("required") is not True:
            continue
        key_value = item.get("key") or item.get("id") or item.get("name")
        key = " ".join(str(key_value or f"field_{index + 1}").split())[:160]
        if not key:
            key = f"field_{index + 1}"
        label_value = item.get("label") or item.get("title") or item.get("text") or key
        label = " ".join(str(label_value).split())[:160] or key
        required_keys.append(key)
        field_type = str(item.get("type") or item.get("kind") or "").lower()
        # Only a captured file control may be satisfied by the worker's private
        # active PDF.  A text/URL field labelled "Resume Link" still requires a
        # user-approved public URL; never substitute a private storage path.
        if field_type == "file" and item.get("accepts_resume") is True:
            resume_keys.append(key)
            continue
        answer = normalized_answers.get(normalized_key(key))
        if answer is None:
            answer = normalized_answers.get(normalized_key(label))
        if not answer_present(answer):
            missing_keys.append(key)
            missing_labels.append(label)

    return {
        "required_count": len(required_keys),
        "answered_count": len(required_keys) - len(missing_keys),
        "missing_count": len(missing_keys),
        "missing_keys": missing_keys,
        "missing_labels": missing_labels,
        "resume_upload_keys": resume_keys,
        "complete": not missing_keys,
    }


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_return_path(value: str | None) -> str:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        return "/"
    return value[:1024]


def _redirect_with_status(settings: Settings, path: str, **params: str) -> RedirectResponse:
    base = (settings.site_url or "/").rstrip("/")
    safe_path = _safe_return_path(path)
    target = f"{base}{safe_path}" if base else safe_path
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}{urlencode(params)}", status_code=303)


def _groq_error(error: GroqProviderError) -> ApiError:
    if error.code == "groq_invalid_key":
        status_code = 400
    elif error.code == "groq_rate_limited":
        status_code = 429
    elif error.code in {"groq_unavailable", "groq_timeout"}:
        status_code = 503
    else:
        status_code = 422
    return ApiError(status_code, error.code, str(error))


def _google_error(error: GoogleProviderError) -> ApiError:
    if error.code in {"google_rate_limited", "gmail_rate_limited"}:
        status_code = 429
    elif error.code in {
        "google_unavailable", "google_oauth_timeout", "google_timeout",
        "gmail_unavailable", "google_revoke_timeout",
    }:
        status_code = 503
    elif error.code == "gmail_send_ambiguous":
        status_code = 409
    elif error.code in {"gmail_reauthorization_required", "google_oauth_rejected"}:
        status_code = 409
    else:
        status_code = 422
    return ApiError(status_code, error.code, str(error))


async def _revoke_google_grant_best_effort(token: Mapping[str, Any] | None) -> bool:
    """Revoke any grant issued during OAuth without leaking provider failures.

    Refresh tokens revoke the durable grant; an access token is still useful when
    Google did not issue a refresh token.  Callers use the boolean only for honest
    UX and never treat a revocation failure as authorization to retain local tokens.
    """

    if not isinstance(token, Mapping):
        return False
    candidate = token.get("refresh_token") or token.get("access_token")
    if not isinstance(candidate, str) or not candidate:
        return False
    try:
        await run_in_threadpool(revoke_google_token, candidate)
    except GoogleProviderError:
        return False
    return True


def _cipher(settings: Settings) -> TokenCipher:
    if not settings.token_encryption_key:
        raise ApiError(
            503, "token_encryption_unavailable", "Provider connections are not configured."
        )
    try:
        return TokenCipher.from_settings(settings)
    except TokenCipherError as exc:
        raise ApiError(
            503, "token_encryption_unavailable", "Provider connections are not configured."
        ) from exc


def _google_client_id_hint(value: str) -> str:
    """Return a useful identifier without reflecting the complete client ID."""

    suffix = ".apps.googleusercontent.com"
    stem = value[: -len(suffix)] if value.endswith(suffix) else value
    if len(stem) <= 12:
        masked_stem = f"{stem[:4]}…"
    else:
        masked_stem = f"{stem[:8]}…{stem[-4:]}"
    return f"{masked_stem}{suffix}" if value.endswith(suffix) else masked_stem


def _provider_id(value: str) -> Literal["groq", "browserbase"]:
    clean = value.strip().lower() if isinstance(value, str) else ""
    if clean not in STORED_PROVIDER_IDS:
        raise ApiError(404, "provider_credential_unknown", "Choose a supported credential provider.")
    return clean  # type: ignore[return-value]


def _validated_provider_body(
    provider: Literal["groq", "browserbase"],
    body: ProviderCredentialUpsert,
) -> ProviderCredentialUpsert:
    if provider == "browserbase":
        if body.project_id is None:
            raise ApiError(
                422,
                "browserbase_project_required",
                "Enter the Browserbase project ID shown beside your API key.",
            )
    elif body.project_id is not None:
        raise ApiError(
            422,
            "provider_credential_invalid",
            "A project ID is supported only for Browserbase.",
        )
    return body


def _provider_credential_plaintext(
    provider: Literal["groq", "browserbase"],
    body: ProviderCredentialUpsert,
) -> str:
    envelope: dict[str, Any] = {
        "version": 1,
        "provider": provider,
        "api_key": body.api_key,
    }
    if provider == "browserbase":
        envelope["project_id"] = body.project_id
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


def _credential_hint(value: str) -> str:
    if len(value) <= 8:
        return f"••••{value[-2:]}"
    prefix = value.split("_", 1)[0]
    visible_prefix = f"{prefix}_" if value.startswith(f"{prefix}_") else ""
    return f"{visible_prefix}••••{value[-4:]}"


def _identifier_hint(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 12:
        return f"{value[:4]}…"
    return f"{value[:8]}…{value[-4:]}"


async def _required_row(
    client: StoreClient, table: str, user: AuthUser, resource_id: UUID | str
) -> dict[str, Any]:
    row = await client.fetch_one(
        table,
        filters=_owned(user, id=str(resource_id)),
        required=True,
    )
    assert row is not None
    return row


def _storage_child_name(value: Any) -> str:
    """Validate a single child name returned by the trusted Storage API."""

    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("invalid Storage listing entry")
    return value


async def _list_owned_resume_objects(
    client: StoreClient, user_id: UUID
) -> list[str]:
    """Enumerate every object below the user's private résumé prefix.

    Listing Storage, rather than only the ``resumes`` table, also cleans uploads
    that reached Storage but were never registered after an interrupted request.
    """

    owner_prefix = str(user_id)
    pending_folders = [owner_prefix]
    visited_folders: set[str] = set()
    object_paths: list[str] = []
    entries_seen = 0

    while pending_folders:
        folder = pending_folders.pop()
        if folder in visited_folders:
            continue
        if folder != owner_prefix and not folder.startswith(f"{owner_prefix}/"):
            raise ValueError("Storage listing escaped the owner prefix")
        visited_folders.add(folder)
        offset = 0
        while True:
            entries = await client.list_objects(
                RESUME_BUCKET,
                prefix=folder,
                limit=STORAGE_LIST_PAGE_SIZE,
                offset=offset,
            )
            entries_seen += len(entries)
            if entries_seen > MAX_ACCOUNT_STORAGE_ENTRIES:
                raise ValueError("Storage cleanup entry limit exceeded")
            for entry in entries:
                name = _storage_child_name(entry.get("name"))
                path = f"{folder}/{name}"
                if entry.get("id") is None:
                    pending_folders.append(path)
                else:
                    object_paths.append(path)
            if len(entries) < STORAGE_LIST_PAGE_SIZE:
                break
            offset += len(entries)
    return object_paths


def create_app(
    *,
    settings: Settings | None = None,
    auth: Any | None = None,
    store: Any | None = None,
) -> FastAPI:
    """Create an app with injectable auth/store services for tenant-boundary tests."""

    runtime_settings = settings or get_settings()
    auth_service = auth or SupabaseAuth(runtime_settings)
    store_service = store or SupabaseStore(runtime_settings)

    application = FastAPI(
        title="AutoApply Cloud API",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    application.state.settings = runtime_settings
    application.state.auth_service = auth_service
    application.state.store_service = store_service
    install_exception_handlers(application)

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    async def authenticated_user(user: AuthUser = Depends(auth_service)) -> AuthUser:
        return user

    async def current_user(user: AuthUser = Depends(authenticated_user)) -> AuthUser:
        profile = await store_service.user(user.access_token).fetch_one(
            "profiles",
            columns="user_id,account_status",
            filters={"user_id": str(user.user_id)},
        )
        if profile and profile.get("account_status") == "deleting":
            raise ApiError(
                409,
                "account_deletion_in_progress",
                "Account deletion is in progress. Retry deletion or contact support.",
            )
        return user

    application.state.current_user_dependency = current_user

    async def load_user_google_oauth_credentials(
        user_id: UUID | str,
        *,
        expected_generation: int | None = None,
    ) -> GoogleOAuthCredentials:
        """Load one tenant's encrypted OAuth app through the service-role client."""

        if not runtime_settings.google_byoc_ready:
            raise ApiError(
                503,
                "gmail_user_oauth_unavailable",
                "User-managed Google OAuth apps are not configured for this deployment.",
            )
        row = await store_service.secret().fetch_one(
            "user_google_oauth_clients",
            columns=(
                "user_id,client_id_ciphertext,client_secret_ciphertext,"
                "generation,created_at,updated_at"
            ),
            filters={"user_id": str(user_id)},
        )
        if row is None:
            raise ApiError(
                409,
                "google_oauth_client_not_configured",
                "Save your Google OAuth app before connecting Gmail with it.",
            )
        generation = _positive_integer(row.get("generation"))
        if generation is None or (
            expected_generation is not None and generation != expected_generation
        ):
            raise ApiError(
                409,
                "google_oauth_client_stale",
                "The Google OAuth app changed. Start the Gmail connection again.",
            )
        try:
            cipher = _cipher(runtime_settings)
            validated = GoogleOAuthClientUpsert.model_validate(
                {
                    "client_id": cipher.decrypt(row["client_id_ciphertext"]),
                    "client_secret": cipher.decrypt(row["client_secret_ciphertext"]),
                }
            )
        except (ApiError, KeyError, TokenCipherError, TypeError, ValueError) as exc:
            raise ApiError(
                409,
                "google_oauth_client_reconfiguration_required",
                "Re-save your Google OAuth app before connecting Gmail.",
            ) from exc
        return GoogleOAuthCredentials(
            source="user",
            client_id=validated.client_id,
            client_secret=validated.client_secret,
            credential_generation=generation,
        )

    async def resolve_google_oauth_credentials(
        source: str,
        user_id: UUID | str,
        *,
        expected_generation: int | None = None,
    ) -> GoogleOAuthCredentials:
        """Resolve only the credential source bound to the flow or connection."""

        if source == "platform":
            if expected_generation is not None or not runtime_settings.google_configured:
                raise ApiError(
                    503,
                    "gmail_platform_oauth_unavailable",
                    "The platform Google OAuth app is not available.",
                )
            return GoogleOAuthCredentials(
                source="platform",
                client_id=runtime_settings.google_client_id,
                client_secret=runtime_settings.google_client_secret,
            )
        if source == "user":
            return await load_user_google_oauth_credentials(
                user_id, expected_generation=expected_generation
            )
        raise ApiError(
            409,
            "google_oauth_client_stale",
            "The Google OAuth app selection is invalid. Start the Gmail connection again.",
        )

    async def load_user_provider_credential(
        user_id: UUID | str,
        provider: Literal["groq", "browserbase"],
        *,
        required: bool = False,
    ) -> StoredProviderCredential | None:
        """Decrypt one tenant credential without exposing it to user-scoped REST.

        A present-but-corrupt row fails closed.  In particular, falling back to
        the platform Browserbase account after a tenant row becomes unreadable
        could try to reuse a context ID in the wrong Browserbase project.
        """

        if not runtime_settings.provider_credential_store_ready:
            if required:
                raise ApiError(
                    503,
                    "provider_credential_store_unavailable",
                    "Saved provider credentials are not available for this deployment.",
                )
            return None
        row = await store_service.secret().fetch_one(
            "user_provider_credentials",
            columns=(
                "user_id,provider,credential_ciphertext,verification_status,"
                "verification_code,generation,binding_fingerprint,verified_at,"
                "created_at,updated_at"
            ),
            filters={"user_id": str(user_id), "provider": provider},
        )
        if row is None:
            if required:
                raise ApiError(
                    409,
                    f"{provider}_credential_required",
                    f"Save your {provider.title()} credential before using this feature.",
                )
            return None
        try:
            plaintext = _cipher(runtime_settings).decrypt(row["credential_ciphertext"])
            envelope = json.loads(plaintext)
            if not isinstance(envelope, dict):
                raise ValueError("invalid provider credential envelope")
            expected_keys = {"version", "provider", "api_key"}
            if provider == "browserbase":
                expected_keys.add("project_id")
            if set(envelope) != expected_keys:
                raise ValueError("invalid provider credential envelope")
            if envelope.get("version") != 1 or envelope.get("provider") != provider:
                raise ValueError("invalid provider credential envelope")
            body = _validated_provider_body(
                provider,
                ProviderCredentialUpsert.model_validate(
                    {
                        "api_key": envelope.get("api_key"),
                        "project_id": envelope.get("project_id"),
                    }
                ),
            )
            generation = _positive_integer(row.get("generation"))
            verification_status = row.get("verification_status")
            verification_code = row.get("verification_code")
            binding_fingerprint = row.get("binding_fingerprint")
            if generation is None or verification_status not in {
                "verified",
                "unverified",
                "invalid",
            }:
                raise ValueError("invalid provider credential metadata")
            if verification_code is not None and (
                not isinstance(verification_code, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", verification_code)
            ):
                raise ValueError("invalid provider verification code")
            if provider == "browserbase":
                expected_fingerprint = _browserbase_project_fingerprint(
                    body.project_id or ""
                )
                if (
                    not isinstance(binding_fingerprint, str)
                    or not secrets.compare_digest(
                        binding_fingerprint, expected_fingerprint
                    )
                ):
                    raise ValueError("invalid Browserbase project binding")
            elif binding_fingerprint is not None:
                raise ValueError("unexpected provider credential binding")
        except (
            ApiError,
            KeyError,
            TokenCipherError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ApiError(
                409,
                f"{provider}_credential_reconfiguration_required",
                f"Re-save your {provider.title()} credential before using this feature.",
            ) from exc
        return StoredProviderCredential(
            provider=provider,
            api_key=body.api_key,
            project_id=body.project_id,
            verification_status=verification_status,
            verification_code=verification_code,
            generation=generation,
            binding_fingerprint=binding_fingerprint,
        )

    def validated_header_key(value: str | None, provider: str) -> str | None:
        if value is None:
            return None
        try:
            return ProviderCredentialUpsert(api_key=value).api_key
        except ValueError as exc:
            raise ApiError(
                400,
                f"{provider}_key_required",
                f"Enter a valid {provider.title()} API key.",
            ) from exc

    async def resolve_groq_key(value: str | None, user: AuthUser) -> str:
        header_key = validated_header_key(value, "groq")
        if header_key is not None:
            return header_key
        stored = await load_user_provider_credential(user.user_id, "groq")
        if stored is None:
            raise ApiError(400, "groq_key_required", "Save a valid Groq API key first.")
        if stored.verification_status == "invalid":
            raise ApiError(
                409,
                "groq_credential_reconfiguration_required",
                "The saved Groq key was rejected. Replace it before continuing.",
            )
        return stored.api_key

    async def load_browserbase_epoch(user_id: UUID | str) -> int:
        if not runtime_settings.provider_credential_store_ready:
            return 0
        value = await store_service.secret().rpc(
            "get_browserbase_credential_state", {"user_id_input": str(user_id)}
        )
        if isinstance(value, Mapping):
            value = value.get("epoch")
        epoch = _nonnegative_integer(value)
        if epoch is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid Browserbase credential state.",
            )
        return epoch

    async def resolve_browserbase_credentials(
        user_id: UUID | str,
        *,
        expected_epoch: int | None = None,
    ) -> BrowserbaseCredentials:
        epoch = (
            expected_epoch
            if expected_epoch is not None
            else await load_browserbase_epoch(user_id)
        )
        if _nonnegative_integer(epoch) is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid Browserbase credential state.",
            )
        stored = await load_user_provider_credential(user_id, "browserbase")
        if stored is not None:
            if stored.verification_status != "verified" or not stored.project_id:
                raise ApiError(
                    409,
                    "browserbase_credential_reconfiguration_required",
                    "Validate or replace your saved Browserbase credential before continuing.",
                )
            if stored.generation != epoch:
                raise ApiError(
                    409,
                    "browserbase_credential_reconfiguration_required",
                    "The saved Browserbase credential is stale. Re-save it before continuing.",
                )
            return BrowserbaseCredentials(
                source="user",
                api_key=stored.api_key,
                project_id=stored.project_id,
                generation=stored.generation,
                project_fingerprint=stored.binding_fingerprint or "",
                epoch=epoch,
            )
        if runtime_settings.browserbase_configured:
            return BrowserbaseCredentials(
                source="platform",
                api_key=runtime_settings.browserbase_api_key,
                project_id=runtime_settings.browserbase_project_id,
                project_fingerprint=_browserbase_project_fingerprint(
                    runtime_settings.browserbase_project_id
                ),
                epoch=epoch,
            )
        raise ApiError(
            409,
            "browserbase_credential_required",
            "Add and validate your Browserbase API key and project ID first.",
        )

    async def browserbase_client_for(user_id: UUID | str) -> BrowserbaseClient:
        credentials = await resolve_browserbase_credentials(user_id)
        return BrowserbaseClient(credentials.api_key, credentials.project_id)

    def assert_browserbase_binding(
        credentials: BrowserbaseCredentials,
        snapshot: Mapping[str, Any],
        *,
        allow_unbound_legacy_cleanup: bool = False,
    ) -> None:
        """Fail closed when a context is not bound to the selected account/project."""

        bound_source = snapshot.get("credential_source") or snapshot.get(
            "browser_credential_source"
        )
        bound_generation = snapshot.get("credential_generation")
        if bound_generation is None:
            bound_generation = snapshot.get("browser_credential_generation")
        bound_fingerprint = snapshot.get("project_fingerprint") or snapshot.get(
            "browser_project_fingerprint"
        )
        bound_epoch = snapshot.get("context_credential_epoch")
        if bound_epoch is None:
            bound_epoch = snapshot.get("credential_epoch")
        if bound_epoch is None:
            bound_epoch = snapshot.get("browser_credential_epoch")
        if bound_source is None and bound_generation is None and bound_fingerprint is None:
            if allow_unbound_legacy_cleanup:
                # A user-requested disconnect may make one best-effort cleanup
                # attempt. Start/complete must never adopt or rebind an old
                # context whose Browserbase project cannot be proven.
                return
            raise ApiError(
                409,
                "browserbase_credential_binding_stale",
                "The saved browser context predates project binding. Disconnect it or "
                "explicitly abandon it before starting again.",
            )
        valid = (
            bound_source == credentials.source
            and isinstance(bound_fingerprint, str)
            and secrets.compare_digest(
                bound_fingerprint, credentials.project_fingerprint
            )
            and (
                (credentials.source == "user" and bound_generation == credentials.generation)
                or (credentials.source == "platform" and bound_generation is None)
            )
            and (
                bound_epoch is None or bound_epoch == credentials.epoch
            )
        )
        if not valid:
            raise ApiError(
                409,
                "browserbase_credential_binding_stale",
                "The saved browser context belongs to a different Browserbase project.",
            )

    def assert_active_browserbase_binding(
        credentials: BrowserbaseCredentials, snapshot: Mapping[str, Any]
    ) -> None:
        """Match credentials resolved after ``begin_browser_start`` to its CAS."""

        source = snapshot.get("active_credential_source")
        generation = snapshot.get("active_credential_generation")
        fingerprint = snapshot.get("active_project_fingerprint")
        epoch = snapshot.get("credential_epoch")
        valid = (
            source == credentials.source
            and epoch == credentials.epoch
            and (
                (
                    source == "user"
                    and generation == credentials.generation
                    and isinstance(fingerprint, str)
                    and secrets.compare_digest(
                        fingerprint, credentials.project_fingerprint
                    )
                )
                or (
                    source == "platform"
                    and generation is None
                    and fingerprint is None
                )
            )
        )
        if not valid:
            raise ApiError(
                409,
                "browserbase_credential_binding_stale",
                "The Browserbase credential changed while the login was starting.",
            )

    async def disconnect_managed_browser_connection(
        provider_id: str,
        user: AuthUser,
        *,
        missing_ok: bool = False,
        browserbase_credentials: BrowserbaseCredentials | None = None,
    ) -> dict[str, bool]:
        """Remove one remote context before discarding its encrypted handle."""

        provider_id = provider_id.strip().lower()
        if provider_id not in MANAGED_BROWSER_LIFECYCLE_PROVIDERS:
            raise ApiError(
                409,
                "provider_connection_unavailable",
                "This provider does not support a managed-browser connection.",
            )
        if missing_ok:
            connection = await store_service.user(user.access_token).fetch_one(
                "connections",
                columns="id,provider,mode,status",
                filters={"user_id": str(user.user_id), "provider": provider_id},
            )
            if connection is None:
                return {"ok": True}

        server = store_service.secret()
        lifecycle = _browser_lifecycle_snapshot(
            await server.rpc(
                "begin_browser_disconnect",
                {"user_id_input": str(user.user_id), "provider_input": provider_id},
            )
        )
        context_ciphertext = lifecycle["context_ciphertext"]
        session_ciphertext = lifecycle["session_ciphertext"]
        if context_ciphertext or session_ciphertext:
            try:
                cipher = _cipher(runtime_settings)
                context_id = cipher.decrypt_optional(context_ciphertext)
                session_id = cipher.decrypt_optional(session_ciphertext)
                credentials = browserbase_credentials or await resolve_browserbase_credentials(
                    user.user_id
                )
                assert_browserbase_binding(
                    credentials,
                    lifecycle,
                    allow_unbound_legacy_cleanup=True,
                )
                browser = BrowserbaseClient(credentials.api_key, credentials.project_id)
                if session_id:
                    await run_in_threadpool(browser.release_session, session_id)
                if context_id:
                    await run_in_threadpool(browser.delete_context, context_id)
            except (ApiError, BrowserbaseError, TokenCipherError) as exc:
                raise ApiError(
                    503,
                    "provider_disconnect_failed",
                    "The remote browser context could not be removed. Try again.",
                ) from exc
        finished = await server.rpc(
            "finish_browser_disconnect",
            {
                "user_id_input": str(user.user_id),
                "provider_input": provider_id,
                "expected_generation_input": lifecycle["generation"],
                "expected_connection_id_input": lifecycle["connection_id"],
            },
        )
        if finished is not True:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid browser lifecycle response.",
            )
        return {"ok": True}

    async def disconnect_all_managed_browser_connections(
        user: AuthUser,
        *,
        browserbase_credentials: BrowserbaseCredentials | None = None,
    ) -> None:
        rows = await store_service.user(user.access_token).fetch_many(
            "connections",
            columns="id,provider,mode,status",
            filters={"user_id": str(user.user_id), "mode": "managed_browser"},
            limit=100,
        )
        for row in rows:
            provider = row.get("provider")
            if isinstance(provider, str) and provider in MANAGED_BROWSER_LIFECYCLE_PROVIDERS:
                await disconnect_managed_browser_connection(
                    provider,
                    user,
                    missing_ok=True,
                    browserbase_credentials=browserbase_credentials,
                )

    async def ingest_discovered_jobs(
        jobs: list[Mapping[str, Any]], user: AuthUser
    ) -> dict[str, Any]:
        """Validate and persist one bounded tenant-owned discovery batch."""

        if len(jobs) > 200:
            raise ApiError(413, "discovery_batch_too_large", "At most 200 jobs can be saved per batch.")
        rows: list[dict[str, Any]] = []
        for raw in jobs:
            try:
                model = JobCreate.model_validate(dict(raw))
            except (TypeError, ValueError) as exc:
                raise ApiError(422, "discovery_result_invalid", "A discovered job was invalid.") from exc
            row = model.model_dump(mode="json")
            row["normalized_url"] = normalized_http_url(model.apply_url)
            row["status"] = "saved"
            row["metadata"] = {**row.get("metadata", {}), "discovered": True}
            rows.append(row)
        if not rows:
            return {"items": [], "count": 0, "inserted": 0, "updated": 0}
        if len(json.dumps(rows, default=str, separators=(",", ":"))) > 1_500_000:
            raise ApiError(413, "discovery_batch_too_large", "The discovery batch is too large.")
        result = await store_service.user(user.access_token).rpc(
            "ingest_discovered_jobs", {"jobs_input": rows}
        )
        if isinstance(result, Mapping):
            items = result.get("items")
            if not isinstance(items, list):
                items = []
            count = result.get("count")
            return {
                "items": [dict(item) for item in items if isinstance(item, Mapping)],
                "count": count if isinstance(count, int) and count >= 0 else len(items),
                "inserted": result.get("inserted", 0),
                "updated": result.get("updated", 0),
            }
        if isinstance(result, list):
            items = [dict(item) for item in result if isinstance(item, Mapping)]
            return {"items": items, "count": len(items), "inserted": len(items), "updated": 0}
        raise ApiError(503, "data_store_invalid_response", "Discovered jobs could not be saved.")

    async def enqueue_job(
        *,
        user: AuthUser,
        kind: str,
        provider: str | None,
        application_id: UUID | str | None,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        row = _first(
            await client.rpc(
                "enqueue_automation_job",
                {
                    "kind_input": kind,
                    "provider_input": provider,
                    "application_id_input": str(application_id) if application_id else None,
                    "payload_input": dict(payload),
                    "idempotency_key_input": idempotency_key,
                },
            )
        )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "The job could not be queued.")
        return row

    async def load_company_form_binding(
        user: AuthUser, job: Mapping[str, Any]
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        """Resolve the service-owned binding for one explicitly saved company form."""

        job_id = job.get("id")
        source_url = job.get("apply_url")
        if not job_id or not isinstance(source_url, str):
            return None
        target = public_company_form_target(source_url)
        if target is None or detect_provider(source_url) is not None:
            return None
        binding = await store_service.secret().fetch_one(
            "company_form_targets",
            filters={"job_id": str(job_id), "user_id": str(user.user_id)},
        )
        if (
            binding is None
            or binding.get("source_url") != source_url
            or binding.get("exact_host") != target["host"]
            or binding.get("target_url") != target["target_url"]
        ):
            return None
        return target, binding

    async def save_company_form_binding(
        user: AuthUser,
        job: Mapping[str, Any],
        source_url: str,
        target: Mapping[str, str],
    ) -> None:
        job_id = job.get("id")
        if not job_id:
            raise ApiError(503, "data_store_invalid_response", "The job could not be bound.")
        await store_service.secret().upsert(
            "company_form_targets",
            {
                "job_id": str(job_id),
                "user_id": str(user.user_id),
                "source_url": source_url,
                "target_url": target["target_url"],
                "exact_host": target["host"],
            },
            on_conflict="job_id",
            returning=False,
        )

    async def delete_company_form_binding(user: AuthUser, job_id: UUID | str) -> None:
        await store_service.secret().delete(
            "company_form_targets",
            filters={"job_id": str(job_id), "user_id": str(user.user_id)},
            returning=False,
        )

    async def load_yc_job_binding(
        user: AuthUser, job: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """Resolve the service-owned authority for one explicitly saved YC job."""

        job_id = job.get("id")
        source_url = job.get("apply_url")
        target_url = canonical_yc_job_url(source_url)
        if not job_id or target_url is None or source_url != target_url:
            return None
        binding = await store_service.secret().fetch_one(
            "yc_application_targets",
            filters={"job_id": str(job_id), "user_id": str(user.user_id)},
        )
        if binding is None or binding.get("target_url") != target_url:
            return None
        return target_url, binding

    async def save_yc_job_binding(
        user: AuthUser, job: Mapping[str, Any], target_url: str
    ) -> None:
        job_id = job.get("id")
        if not job_id or canonical_yc_job_url(target_url) != target_url:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The exact YC job could not be bound.",
            )
        await store_service.secret().upsert(
            "yc_application_targets",
            {
                "job_id": str(job_id),
                "user_id": str(user.user_id),
                "target_url": target_url,
            },
            on_conflict="job_id",
            returning=False,
        )

    async def delete_yc_job_binding(user: AuthUser, job_id: UUID | str) -> None:
        await store_service.secret().delete(
            "yc_application_targets",
            filters={"job_id": str(job_id), "user_id": str(user.user_id)},
            returning=False,
        )

    async def require_managed_connection(
        provider: str,
        user: AuthUser,
        *,
        operation: Literal["scan", "prefill", "submit"],
    ) -> dict[str, Any]:
        capability = get_provider(
            provider,
            runtime_settings.allowed_browser_providers,
            google_configured=runtime_settings.google_configured,
            browserbase_configured=runtime_settings.managed_browser_available,
        )
        capability_key = {
            "scan": "can_scan",
            "prefill": "can_prefill",
            "submit": "can_auto_apply",
        }[operation]
        if (
            provider not in APPLICATION_AUTOMATION_PROVIDERS
            or capability is None
            or not capability[capability_key]
            or not browser_provider_allowed(provider, runtime_settings.allowed_browser_providers)
        ):
            raise ApiError(
                409,
                "provider_automation_unavailable",
                f"Managed-browser {operation} support is not enabled for this provider.",
            )
        await resolve_browserbase_credentials(user.user_id)
        connection = await store_service.user(user.access_token).fetch_one(
            "connections",
            filters={"user_id": str(user.user_id), "provider": provider},
        )
        if provider in PERSISTENT_CONTEXT_REQUIRED_PROVIDERS and (
            connection is None
            or connection.get("status") not in {"active", "connected", "needs_attention"}
        ):
            raise ApiError(
                409,
                "provider_connection_required",
                "Connect this provider in an isolated browser before queueing application work.",
            )
        return connection or {}

    @application.get("/api/v1/config", tags=["public"])
    async def public_config() -> dict[str, object]:
        return runtime_settings.public_config()

    @application.get("/api/v1/health", tags=["public"])
    async def health() -> dict[str, object]:
        checks = runtime_settings.readiness()
        return {
            "status": (
                "ready"
                if checks["supabase"] and checks["server_store"] and checks["captcha"]
                else "setup_required"
            ),
            "version": VERSION,
            "checks": checks,
        }

    @application.get("/api/v1/providers", tags=["public"])
    async def providers() -> dict[str, Any]:
        catalog = provider_catalog(
            runtime_settings.allowed_browser_providers,
            google_configured=runtime_settings.gmail_connection_available,
            browserbase_configured=runtime_settings.managed_browser_available,
        )
        return _items(catalog)

    def yc_preferences_response(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "user_id": str(row.get("user_id") or ""),
            "provider": "yc",
            "query": row.get("query") if isinstance(row.get("query"), str) else None,
            "remote_only": bool(row.get("remote_only", False)),
            "limit": int(row.get("result_limit") or 10),
            **(
                {"created_at": row["created_at"]}
                if row.get("created_at") is not None
                else {}
            ),
            **(
                {"updated_at": row["updated_at"]}
                if row.get("updated_at") is not None
                else {}
            ),
        }

    @application.get("/api/v1/providers/yc/preferences", tags=["applications"])
    async def get_yc_application_preferences(
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        row = await client.fetch_one(
            "provider_application_preferences",
            filters={"user_id": str(user.user_id), "provider": "yc"},
        )
        if row is None:
            row = _first(
                await client.upsert(
                    "provider_application_preferences",
                    {
                        "user_id": str(user.user_id),
                        "provider": "yc",
                        "remote_only": False,
                        "result_limit": 10,
                    },
                    on_conflict="user_id,provider",
                )
            )
        if row is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "YC application preferences could not be loaded.",
            )
        return _data(yc_preferences_response(row))

    @application.patch("/api/v1/providers/yc/preferences", tags=["applications"])
    async def patch_yc_application_preferences(
        body: YcApplicationPreferencesUpdate,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        changes = _model_changes(body)
        if "limit" in changes:
            changes["result_limit"] = changes.pop("limit")
        client = store_service.user(user.access_token)
        row = _first(
            await client.upsert(
                "provider_application_preferences",
                {
                    "user_id": str(user.user_id),
                    "provider": "yc",
                    **changes,
                },
                on_conflict="user_id,provider",
            )
        )
        if row is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "YC application preferences could not be saved.",
            )
        return _data(yc_preferences_response(row))

    @application.get("/api/v1/discovery/sources", tags=["discovery"])
    async def discovery_sources(
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        del user
        return _items(
            [
                {
                    "id": "telegram",
                    "label": "Telegram public channels",
                    "credential_required": False,
                    "bounded_sources": len(DEFAULT_TELEGRAM_CHANNELS),
                },
                {
                    "id": "rss",
                    "label": "Public RSS feeds",
                    "credential_required": False,
                    "bounded_sources": len(DEFAULT_RSS_FEEDS),
                },
                {
                    "id": "linkedin_guest",
                    "label": "LinkedIn guest jobs",
                    "credential_required": False,
                    "unofficial": True,
                    "limit": 25,
                    "easy_apply": False,
                },
                {"id": "referral_digest", "label": "Pasted referral digest", "credential_required": False},
                {"id": "csv", "label": "CSV import", "credential_required": False},
                {"id": "xlsx", "label": "XLSX import", "credential_required": False},
                {
                    "id": "public_ats",
                    "label": "Public ATS links and board discovery",
                    "credential_required": False,
                    "providers": ["greenhouse", "lever", "ashby"],
                    "max_boards": MAX_PUBLIC_ATS_BOARDS,
                    "limit": MAX_PUBLIC_ATS_RESULTS,
                },
            ]
        )

    @application.get("/api/v1/discovery/preferences", tags=["discovery"])
    async def get_discovery_preferences(
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        row = await client.fetch_one(
            "discovery_preferences", filters={"user_id": str(user.user_id)}
        )
        if row is None:
            row = _first(
                await client.upsert(
                    "discovery_preferences",
                    {"user_id": str(user.user_id)},
                    on_conflict="user_id",
                )
            )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "Discovery preferences could not be loaded.")
        return _data(row)

    @application.patch("/api/v1/discovery/preferences", tags=["discovery"])
    async def patch_discovery_preferences(
        body: DiscoveryPreferencesUpdate,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        values = _model_changes(body)
        allowed_sources = {
            "telegram", "rss", "referral_digest", "csv", "xlsx", "public_ats", "linkedin_guest"
        }
        if any(source not in allowed_sources for source in values.get("enabled_sources", [])):
            raise ApiError(422, "discovery_source_invalid", "Choose a supported discovery source.")
        if any(url not in DEFAULT_RSS_FEEDS for url in values.get("feed_urls", [])):
            raise ApiError(422, "discovery_feed_not_allowed", "Choose a feed from the deployment catalog.")
        client = store_service.user(user.access_token)
        row = _first(
            await client.upsert(
                "discovery_preferences",
                {"user_id": str(user.user_id), **values},
                on_conflict="user_id",
            )
        )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "Discovery preferences could not be saved.")
        return _data(row)

    @application.post("/api/v1/discovery/referrals", status_code=201, tags=["discovery"])
    async def ingest_referrals(
        body: ReferralDigestIngest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            jobs = await run_in_threadpool(parse_referral_digest, body.text, limit=200)
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "referral_digest_invalid", str(exc)) from exc
        result = await ingest_discovered_jobs(jobs, user)
        summary = referral_digest_summary(body.text, jobs)
        summary["saved"] = result.get("count", 0)
        return {**result, "summary": summary}

    @application.post("/api/v1/discovery/import", status_code=201, tags=["discovery"])
    async def import_discovery_file(
        file: UploadFile = File(...),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        filename = (file.filename or "").strip()
        try:
            payload = await file.read(MAX_JOB_IMPORT_BYTES + 1)
        finally:
            await file.close()
        if len(payload) > MAX_JOB_IMPORT_BYTES:
            raise ApiError(413, "job_import_too_large", "The spreadsheet must be 4 MB or smaller.")
        try:
            jobs = await run_in_threadpool(
                parse_spreadsheet_bytes, payload, filename, max_rows=200
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ApiError(422, "job_import_invalid", str(exc)) from exc
        return await ingest_discovered_jobs(jobs, user)

    @application.post("/api/v1/discovery/ats", status_code=201, tags=["discovery"])
    async def ingest_public_ats_urls(
        body: PublicAtsDiscoveryRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        jobs = await run_in_threadpool(
            discover_provider_urls, "\n".join(body.urls), limit=100
        )
        if not jobs:
            raise ApiError(
                422,
                "public_ats_not_detected",
                "No supported public application URL was detected.",
            )
        return await ingest_discovered_jobs(jobs, user)

    @application.post(
        "/api/v1/discovery/ats/boards",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["discovery"],
    )
    async def queue_public_ats_board_discovery(
        body: PublicAtsBoardDiscoveryRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        canonical_urls: list[str] = []
        try:
            for url in body.urls:
                canonical = canonical_public_ats_board_url(url)
                if canonical not in canonical_urls:
                    canonical_urls.append(canonical)
        except ValueError as exc:
            raise ApiError(422, "public_ats_board_invalid", str(exc)) from exc
        row = await enqueue_job(
            user=user,
            kind="discover_public_ats",
            provider="public_ats",
            application_id=None,
            payload={
                "board_urls": canonical_urls,
                "limit": body.limit,
                **(
                    {"timeout_seconds": body.timeout_seconds}
                    if body.timeout_seconds is not None
                    else {}
                ),
            },
            idempotency_key=body.idempotency_key,
        )
        return _data(row)

    @application.post(
        "/api/v1/discovery/public-feeds",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["discovery"],
    )
    async def queue_public_feed_discovery(
        body: PublicFeedDiscoveryRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        requested = body.source_ids or ["telegram", "rss"]
        if any(source not in {"telegram", "rss"} for source in requested):
            raise ApiError(422, "discovery_source_invalid", "Public feed runs support Telegram and RSS only.")
        row = await enqueue_job(
            user=user,
            kind="discover_public_feeds",
            provider="public_feeds",
            application_id=None,
            payload={
                "source_ids": requested,
                "limit": body.limit,
                **(
                    {"timeout_seconds": body.timeout_seconds}
                    if body.timeout_seconds is not None
                    else {}
                ),
            },
            idempotency_key=body.idempotency_key,
        )
        return _data(row)

    @application.post(
        "/api/v1/discovery/linkedin",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["discovery"],
    )
    async def queue_linkedin_discovery(
        body: LinkedInDiscoveryRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        row = await enqueue_job(
            user=user,
            kind="discover_linkedin_guest",
            provider="linkedin",
            application_id=None,
            payload={
                "keywords": body.keywords,
                "location": body.location or "India",
                "remote": body.remote_only,
                "limit": body.limit,
                **(
                    {"timeout_seconds": body.timeout_seconds}
                    if body.timeout_seconds is not None
                    else {}
                ),
            },
            idempotency_key=body.idempotency_key,
        )
        return _data(row)

    @application.post(
        "/api/v1/discovery/resume-guided",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["discovery", "groq"],
    )
    async def queue_resume_guided_discovery(
        body: ResumeGuidedDiscoveryRequest,
        groq_key: str | None = Header(default=None, alias="X-Groq-Api-Key"),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Derive a bounded public-source search plan from the active résumé."""

        groq_key = await resolve_groq_key(groq_key, user)
        client = store_service.user(user.access_token)
        resume = await client.fetch_one(
            "resumes",
            columns="id,parse_status,parsed_text",
            filters={"user_id": str(user.user_id), "is_active": True},
        )
        if (
            resume is None
            or resume.get("parse_status") != "parsed"
            or not isinstance(resume.get("parsed_text"), str)
            or not resume["parsed_text"].strip()
        ):
            raise ApiError(
                409,
                "resume_not_parsed",
                "Upload and parse an active résumé before searching from it.",
            )
        profile = await client.fetch_one(
            "profiles",
            columns="skills,preferences,location",
            filters={"user_id": str(user.user_id)},
        ) or {}
        preferences = await client.fetch_one(
            "discovery_preferences",
            columns="locations",
            filters={"user_id": str(user.user_id)},
        ) or {}

        await client.rpc("reserve_groq_request", {"operation_input": "generate"})
        try:
            analysis = await asyncio.wait_for(
                run_in_threadpool(
                    analyze_resume_profile,
                    groq_key,
                    runtime_settings.groq_model,
                    resume["parsed_text"],
                ),
                timeout=RESUME_ANALYSIS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise _groq_error(
                GroqProviderError(
                    "groq_timeout",
                    "Groq took too long to analyze the résumé. Try again later.",
                )
            ) from exc
        except GroqProviderError as exc:
            raise _groq_error(exc) from exc

        profile_preferences = profile.get("preferences")
        saved_roles = _bounded_text_list(
            profile_preferences.get("target_roles")
            if isinstance(profile_preferences, Mapping)
            else None,
            limit=5,
            max_length=100,
        )
        analyzed_roles = _bounded_text_list(
            analysis.get("target_roles"), limit=5, max_length=100
        )
        fallback_roles = _bounded_text_list(
            recommended_roles(profile, resume["parsed_text"]),
            limit=5,
            max_length=100,
        )
        roles = saved_roles or analyzed_roles or fallback_roles
        analyzed_skills = _bounded_text_list(
            analysis.get("skills"), limit=12, max_length=80
        )
        saved_skills = _bounded_text_list(
            profile.get("skills"), limit=12, max_length=80
        )
        keywords = _bounded_text_list(
            [*analyzed_skills, *saved_skills], limit=12, max_length=80
        )
        search_terms = _bounded_text_list(
            [*roles, *keywords], limit=20, max_length=100
        )
        if not roles or not search_terms:
            raise ApiError(
                422,
                "resume_search_terms_missing",
                "The résumé did not contain enough evidence to derive a job search.",
            )

        saved_locations = _bounded_text_list(
            preferences.get("locations"), limit=1, max_length=120
        )
        profile_location = profile.get("location")
        location = (
            body.location
            or (saved_locations[0] if saved_locations else None)
            or (" ".join(profile_location.split())[:120] if isinstance(profile_location, str) else None)
            or "India"
        )
        linkedin_query = roles[0][:100]
        linkedin_limit = body.linkedin_limit
        feed_limit = body.feed_limit
        if body.max_jobs is not None:
            # Keep the combined two-collector run within the user's requested
            # result budget while giving both public sources a chance to return
            # matches. The minimum of two jobs is intentional because the run
            # always dispatches LinkedIn and feeds separately.
            linkedin_limit = min(25, (body.max_jobs + 1) // 2)
            feed_limit = max(1, body.max_jobs - linkedin_limit)
        timeout_payload = (
            {"timeout_seconds": body.timeout_seconds}
            if body.timeout_seconds is not None
            else {}
        )
        linkedin_job = await enqueue_job(
            user=user,
            kind="discover_linkedin_guest",
            provider="linkedin",
            application_id=None,
            payload={
                "keywords": linkedin_query,
                "location": location,
                "remote": body.remote_only,
                "limit": linkedin_limit,
                **timeout_payload,
            },
            idempotency_key=f"{body.idempotency_key}:linkedin",
        )
        feed_job = await enqueue_job(
            user=user,
            kind="discover_public_feeds",
            provider="public_feeds",
            application_id=None,
            payload={
                "source_ids": ["telegram", "rss"],
                "limit": feed_limit,
                "search_terms": search_terms,
                **timeout_payload,
            },
            idempotency_key=f"{body.idempotency_key}:feeds",
        )
        return _data(
            {
                "plan": {
                    "roles": roles,
                    "keywords": keywords,
                    "search_terms": search_terms,
                    "linkedin_query": linkedin_query,
                    "location": location,
                    "remote_only": body.remote_only,
                    "sources": ["linkedin_guest", "telegram", "rss"],
                },
                "automation_jobs": [
                    _public_automation_job(linkedin_job),
                    _public_automation_job(feed_job),
                ],
            }
        )

    @application.get("/api/v1/discovery/google-forms", tags=["discovery", "applications"])
    async def list_google_form_queue(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """List direct and metadata-discovered Google Forms for review."""

        client = store_service.user(user.access_token)
        jobs = await client.fetch_many(
            "jobs",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=1_000,
        )
        if len(jobs) == 1_000:
            jobs.extend(
                await client.fetch_many(
                    "jobs",
                    filters={"user_id": str(user.user_id)},
                    order="created_at.desc",
                    limit=1_000,
                    offset=1_000,
                )
            )
        applications = await client.fetch_many(
            "applications",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=1_000,
        )
        if len(applications) == 1_000:
            applications.extend(
                await client.fetch_many(
                    "applications",
                    filters={"user_id": str(user.user_id)},
                    order="created_at.desc",
                    limit=1_000,
                    offset=1_000,
                )
            )
        application_by_job: dict[str, dict[str, Any]] = {}
        for application_row in applications:
            if application_row.get("channel") != "ats":
                continue
            job_id = application_row.get("job_id")
            if job_id is not None and str(job_id) not in application_by_job:
                application_by_job[str(job_id)] = dict(application_row)

        by_url: dict[str, dict[str, Any]] = {}
        for job in jobs:
            apply_url = job.get("apply_url")
            if not isinstance(apply_url, str) or detect_provider(apply_url) != "google_forms":
                continue
            normalized = normalized_http_url(apply_url)
            if not normalized:
                continue
            job_id = str(job.get("id"))
            by_url[normalized] = {
                "id": f"job:{job_id}",
                "job_id": job_id,
                "parent_job_id": job_id,
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "source": job.get("source"),
                "created_at": job.get("created_at"),
                "apply_url": apply_url,
                "saved": True,
                "application": application_by_job.get(job_id),
            }

        for job in jobs:
            for form_url in _nested_google_form_urls(job.get("metadata")):
                normalized = normalized_http_url(form_url)
                if not normalized or normalized in by_url:
                    continue
                parent_job_id = str(job.get("id"))
                digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
                by_url[normalized] = {
                    "id": f"form:{digest}",
                    "job_id": None,
                    "parent_job_id": parent_job_id,
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "location": job.get("location"),
                    "source": job.get("source"),
                    "created_at": job.get("created_at"),
                    "apply_url": form_url,
                    "saved": False,
                    "application": None,
                }

        entries = list(by_url.values())
        page = entries[offset : offset + limit]
        return {
            "items": page,
            "count": len(page),
            "total": len(entries),
            "has_more": offset + len(page) < len(entries),
        }

    @application.get("/api/v1/profile", tags=["profile"])
    async def get_profile(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        row = await client.fetch_one("profiles", filters={"user_id": str(user.user_id)})
        if row is None:
            metadata_name = user.user_metadata.get("full_name") or user.user_metadata.get("name")
            values = {
                "user_id": str(user.user_id),
                "email": user.email,
                "full_name": metadata_name if isinstance(metadata_name, str) else None,
            }
            row = _first(
                await store_service.secret().upsert(
                    "profiles", values, on_conflict="user_id"
                )
            )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "The profile could not be loaded.")
        return _data(row)

    @application.patch("/api/v1/profile", tags=["profile"])
    async def patch_profile(
        changes: ProfileUpdate, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        values = _model_changes(changes)
        if values:
            row = _first(
                await client.update(
                    "profiles", values, filters={"user_id": str(user.user_id)}
                )
            )
        else:
            row = await client.fetch_one("profiles", filters={"user_id": str(user.user_id)})
        if row is None:
            raise ApiError(404, "profile_not_found", "The profile was not found.")
        # Refresh only deterministic answers in current, non-terminal form
        # snapshots. The database creates a new unapproved revision atomically;
        # an active, confirmed, failed, or uncertain submit attempt is never
        # touched. Profile saving itself remains successful if an older database
        # deployment has not installed this optional synchronization RPC yet.
        revisions = await client.fetch_many(
            "application_form_revisions",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=50,
        )
        seen_applications: set[str] = set()
        for revision in revisions:
            application_id = str(revision.get("application_id") or "")
            if not application_id or application_id in seen_applications:
                continue
            seen_applications.add(application_id)
            questions = revision.get("question_schema")
            if (
                revision.get("status") not in {"scanned", "prefilled", "approved"}
                or not isinstance(questions, list)
                or not isinstance(revision.get("answers"), Mapping)
            ):
                continue
            profile_answers = profile_form_answers(row, questions)
            if not profile_answers:
                continue
            merged_answers = dict(revision["answers"])
            merged_answers.update(profile_answers)
            if merged_answers == revision["answers"]:
                continue
            try:
                await store_service.secret().rpc(
                    "refresh_application_form_profile_answers_for_user",
                    {
                        "user_id_input": str(user.user_id),
                        "revision_id_input": str(revision["id"]),
                        "expected_revision_input": int(revision.get("revision") or 0),
                        "expected_schema_hash_input": revision.get("schema_hash"),
                        "answers_input": merged_answers,
                    },
                )
            except (TypeError, ValueError):
                # A malformed historical snapshot is skipped. Database/API
                # failures must remain visible; silently swallowing them would
                # make the Profile look saved while prepared forms stay stale.
                continue
        return _data(row)

    @application.get("/api/v1/settings", tags=["profile"])
    async def get_user_settings(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        row = await client.fetch_one("user_settings", filters={"user_id": str(user.user_id)})
        if row is None:
            row = _first(
                await client.upsert(
                    "user_settings",
                    {
                        "user_id": str(user.user_id),
                        "daily_send_cap": runtime_settings.default_daily_send_cap,
                    },
                    on_conflict="user_id",
                )
            )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "Settings could not be loaded.")
        return _data(row)

    @application.patch("/api/v1/settings", tags=["profile"])
    async def patch_user_settings(
        changes: UserSettingsUpdate, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        values = _model_changes(changes)
        # Review-before-send is a launch invariant, not a user-disableable preference.
        if values.get("require_review") is False:
            raise ApiError(422, "review_required", "Review before sending cannot be disabled.")
        if values:
            row = _first(
                await client.update(
                    "user_settings", values, filters={"user_id": str(user.user_id)}
                )
            )
        else:
            row = await client.fetch_one(
                "user_settings", filters={"user_id": str(user.user_id)}
            )
        if row is None:
            raise ApiError(404, "settings_not_found", "Settings were not found.")
        return _data(row)

    @application.delete("/api/v1/account", tags=["account"])
    async def delete_account(
        body: AccountDeletionRequest,
        user: AuthUser = Depends(authenticated_user),
    ) -> dict[str, Any]:
        """Delete provider state, private objects, database rows, and the Auth user."""

        signed_in_at = user.last_sign_in_at
        sign_in_age = _now() - signed_in_at if signed_in_at is not None else None
        if (
            sign_in_age is None
            or sign_in_age < -ACCOUNT_DELETION_CLOCK_SKEW
            or sign_in_age > ACCOUNT_DELETION_REAUTH_WINDOW
        ):
            raise ApiError(
                403,
                "recent_authentication_required",
                "Sign out and sign in again before permanently deleting this account.",
            )

        client = store_service.user(user.access_token)
        server = store_service.secret()
        deletion_ready = await client.rpc(
            "begin_account_deletion", {"confirmation_input": body.confirmation}
        )
        if deletion_ready is not True:
            # The RPC commits cancellation requests and keeps the account in its
            # deletion-only state. A later authenticated retry may proceed only
            # after every worker has stopped holding tenant plaintext/browser
            # credentials.
            raise ApiError(
                409,
                "account_automation_jobs_running",
                "Account deletion requested cancellation of active work. Retry after the running worker stops.",
            )
        connections = await client.fetch_many(
            "connections",
            filters={"user_id": str(user.user_id)},
            limit=100,
        )

        # Remote browser contexts can retain login cookies.  Do not discard the only
        # encrypted cleanup handle unless the provider confirms cleanup (404 is an
        # idempotent success in the adapter).
        for connection in connections:
            secret_row = await server.fetch_one(
                "connection_secrets",
                filters={
                    "connection_id": connection["id"],
                    "user_id": str(user.user_id),
                },
            )
            if not secret_row:
                continue
            if connection.get("provider") == "gmail":
                try:
                    cipher = _cipher(runtime_settings)
                    token = cipher.decrypt_optional(
                        secret_row.get("refresh_token_ciphertext")
                    ) or cipher.decrypt_optional(secret_row.get("access_token_ciphertext"))
                    if token:
                        await run_in_threadpool(revoke_google_token, token)
                except (ApiError, GoogleProviderError, TokenCipherError):
                    # Deletion of the local encrypted token remains possible.  The
                    # privacy page also explains Google-account-side revocation.
                    pass

        remote_browser_cleanup_confirmed = True
        if any(
            connection.get("mode") == "managed_browser"
            for connection in connections
        ):
            try:
                old_credentials = await resolve_browserbase_credentials(user.user_id)
                await disconnect_all_managed_browser_connections(
                    user, browserbase_credentials=old_credentials
                )
            except (ApiError, BrowserbaseError, TokenCipherError):
                # Total account deletion is already explicitly confirmed and the
                # profile is in `deleting`. If a revoked/corrupt key makes remote
                # cleanup impossible, record the loss of remote confirmation and
                # remove the local handles so Auth deletion cannot strand PII.
                abandoned = await server.rpc(
                    "abandon_browserbase_resources",
                    {
                        "user_id_input": str(user.user_id),
                        "confirmation_input": "ABANDON REMOTE BROWSER DATA",
                    },
                )
                if abandoned is not True:
                    raise ApiError(
                        503,
                        "account_remote_cleanup_failed",
                        "A managed-browser login could not be removed. Try account deletion again.",
                    )
                remote_browser_cleanup_confirmed = False

        try:
            object_paths = await _list_owned_resume_objects(server, user.user_id)
        except ValueError as exc:
            raise ApiError(
                503,
                "account_storage_cleanup_failed",
                "Private résumé storage could not be enumerated safely.",
            ) from exc
        for offset in range(0, len(object_paths), 100):
            await server.delete_objects(RESUME_BUCKET, object_paths[offset : offset + 100])

        await server.delete_auth_user(user.user_id, should_soft_delete=False)
        response: dict[str, Any] = {"ok": True}
        if not remote_browser_cleanup_confirmed:
            response.update(
                {
                    "remote_browser_cleanup_confirmed": False,
                    "browserbase_dashboard_url": PROVIDER_CREDENTIAL_LINKS[
                        "browserbase"
                    ]["project_url"],
                }
            )
        return response

    @application.post("/api/v1/resumes/register", status_code=201, tags=["resumes"])
    async def register_resume(
        body: ResumeRegister, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        expected_prefix = f"{user.user_id}/"
        parts = body.storage_path.split("/")
        if (
            not body.storage_path.startswith(expected_prefix)
            or len(parts) != 2
            or parts[1].lower() not in {f"resume-{slot}.pdf" for slot in range(1, 6)}
        ):
            raise ApiError(
                403,
                "resume_path_forbidden",
                "The résumé path must belong to the signed-in user.",
            )
        client = store_service.user(user.access_token)
        row = _first(
            await client.rpc(
                "register_resume",
                {
                    "storage_path_input": body.storage_path,
                    "original_name_input": body.original_name,
                    "mime_type_input": body.mime_type,
                    "size_bytes_input": body.size_bytes,
                    "sha256_input": body.sha256.lower() if body.sha256 else None,
                },
            )
        )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "The résumé could not be registered.")
        return _data(row)

    @application.get("/api/v1/resumes", tags=["resumes"])
    async def list_resumes(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        rows = await client.fetch_many(
            "resumes",
            columns=(
                "id,user_id,storage_path,original_name,mime_type,size_bytes,sha256,"
                "parse_status,parse_error,is_active,created_at,updated_at"
            ),
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=20,
        )
        return _items(rows)

    @application.post("/api/v1/resumes/{resume_id}/parse", tags=["resumes"])
    async def parse_resume(
        resume_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        server = store_service.secret()
        row = await _required_row(client, "resumes", user, resume_id)
        await client.rpc("reserve_resume_parse", {"resume_id_input": str(resume_id)})
        await server.update(
            "resumes",
            {"parse_status": "parsing", "parse_error": None},
            filters=_owned(user, id=str(resume_id)),
            returning=False,
        )
        try:
            pdf_bytes = await client.download_object(RESUME_BUCKET, row["storage_path"])
            if len(pdf_bytes) != row.get("size_bytes"):
                raise ResumeParseError(
                    "resume_metadata_mismatch",
                    "The uploaded résumé size does not match its registered metadata.",
                )
            actual_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
            if row.get("sha256") and not secrets.compare_digest(
                str(row["sha256"]).lower(), actual_sha256
            ):
                raise ResumeParseError(
                    "resume_metadata_mismatch",
                    "The uploaded résumé does not match its registered checksum.",
                )
            text = await run_in_threadpool(
                extract_pdf_text, pdf_bytes, max_bytes=runtime_settings.max_resume_bytes
            )
        except ResumeParseError as exc:
            await server.update(
                "resumes",
                {"parse_status": "failed", "parse_error": exc.code},
                filters=_owned(user, id=str(resume_id)),
                returning=False,
            )
            raise ApiError(422, exc.code, exc.message) from exc
        updated = _first(
            await server.update(
                "resumes",
                {
                    "parsed_text": text,
                    "parse_status": "parsed",
                    "parse_error": None,
                    "sha256": actual_sha256,
                },
                filters=_owned(user, id=str(resume_id)),
            )
        )
        if updated is None:
            raise ApiError(404, "resume_not_found", "The résumé was not found.")
        public_resume = dict(updated)
        public_resume.pop("parsed_text", None)
        return {"data": public_resume, "suggestions": profile_suggestions(text)}

    @application.post("/api/v1/resumes/{resume_id}/analyze", tags=["resumes", "groq"])
    async def analyze_resume(
        resume_id: UUID,
        groq_key: str | None = Header(default=None, alias="X-Groq-Api-Key"),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Return reviewed profile suggestions without mutating the user's profile."""

        groq_key = await resolve_groq_key(groq_key, user)
        client = store_service.user(user.access_token)
        resume = await _required_row(client, "resumes", user, resume_id)
        resume_text = resume.get("parsed_text")
        if resume.get("parse_status") != "parsed" or not isinstance(resume_text, str):
            raise ApiError(409, "resume_not_parsed", "Parse this résumé before analyzing it.")
        await client.rpc("reserve_groq_request", {"operation_input": "generate"})
        try:
            suggestions = await run_in_threadpool(
                analyze_resume_profile,
                groq_key,
                runtime_settings.groq_model,
                resume_text,
            )
        except GroqProviderError as exc:
            raise _groq_error(exc) from exc
        # Exact contact/link values found in the PDF text win over model output.
        suggestions.update(profile_suggestions(resume_text))
        return _data(
            {
                "resume_id": str(resume_id),
                "suggestions": suggestions,
                "analysis": "groq",
            }
        )

    @application.delete("/api/v1/resumes/{resume_id}", tags=["resumes"])
    async def delete_resume(
        resume_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, bool]:
        client = store_service.user(user.access_token)
        server = store_service.secret()
        row = await _required_row(client, "resumes", user, resume_id)
        await server.delete_object(RESUME_BUCKET, row["storage_path"])
        await server.delete(
            "resumes", filters=_owned(user, id=str(resume_id)), returning=False
        )
        return {"ok": True}

    @application.get("/api/v1/provider-credentials", tags=["connections"])
    async def list_provider_credentials(
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Return tenant-safe credential state; never return encrypted or plaintext values."""

        rows: list[dict[str, Any]] = []
        if runtime_settings.provider_credential_store_ready:
            rows = await store_service.secret().fetch_many(
                "user_provider_credentials",
                columns=(
                    "provider,verification_status,verification_code,verified_at,"
                    "created_at,updated_at"
                ),
                filters={"user_id": str(user.user_id)},
                limit=len(STORED_PROVIDER_IDS),
            )
        by_provider = {
            row.get("provider"): row
            for row in rows
            if row.get("provider") in STORED_PROVIDER_IDS
        }
        items: list[dict[str, Any]] = []
        for provider_value in STORED_PROVIDER_IDS:
            provider = _provider_id(provider_value)
            row = by_provider.get(provider)
            key_hint: str | None = None
            project_id_hint: str | None = None
            requires_reconfiguration = False
            if row is not None:
                try:
                    stored = await load_user_provider_credential(
                        user.user_id, provider, required=True
                    )
                    assert stored is not None
                    key_hint = _credential_hint(stored.api_key)
                    project_id_hint = _identifier_hint(stored.project_id)
                except ApiError as exc:
                    if exc.code.endswith("_reconfiguration_required"):
                        requires_reconfiguration = True
                    else:
                        raise
            items.append(
                {
                    "provider": provider,
                    "configured": row is not None,
                    "verification_status": (
                        row.get("verification_status") if row is not None else "missing"
                    ),
                    "verification_code": (
                        row.get("verification_code") if row is not None else None
                    ),
                    "verified_at": row.get("verified_at") if row is not None else None,
                    "updated_at": row.get("updated_at") if row is not None else None,
                    "key_hint": key_hint,
                    "project_id_hint": project_id_hint,
                    "requires_reconfiguration": requires_reconfiguration,
                    **PROVIDER_CREDENTIAL_LINKS[provider],
                }
            )
        return {
            "items": items,
            "count": len(items),
            "store_available": runtime_settings.provider_credential_store_ready,
            "platform_browserbase_available": runtime_settings.browserbase_configured,
        }

    @application.put("/api/v1/provider-credentials/{provider_id}", tags=["connections"])
    async def save_provider_credential(
        provider_id: str,
        body: ProviderCredentialUpsert,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        if not runtime_settings.provider_credential_store_ready:
            raise ApiError(
                503,
                "provider_credential_store_unavailable",
                "Saved provider credentials are not configured for this deployment.",
            )
        provider = _provider_id(provider_id)
        validated = _validated_provider_body(provider, body)
        server = store_service.secret()
        existing = await server.fetch_one(
            "user_provider_credentials",
            columns=(
                "provider,verification_status,verification_code,generation,"
                "binding_fingerprint"
            ),
            filters={"user_id": str(user.user_id), "provider": provider},
        )
        verification: dict[str, Any]
        expected_browserbase_epoch: int | None = None
        old_browserbase_credentials: BrowserbaseCredentials | None = None

        if provider == "browserbase":
            expected_browserbase_epoch = await load_browserbase_epoch(user.user_id)
            if existing is not None or runtime_settings.browserbase_configured:
                # Capture the old account/project before validating the
                # replacement. Any stored contexts must be removed with the
                # credential that created them, never with the candidate key.
                old_browserbase_credentials = await resolve_browserbase_credentials(
                    user.user_id, expected_epoch=expected_browserbase_epoch
                )

        if provider == "groq":
            try:
                await store_service.user(user.access_token).rpc(
                    "reserve_groq_request", {"operation_input": "validate"}
                )
            except ApiError as exc:
                if exc.status_code != 429:
                    raise
                verification = {
                    "valid": False,
                    "status": "groq_validation_deferred",
                    "message": "The key was saved. Validate it after the temporary limit clears.",
                }
            else:
                verification = await run_in_threadpool(
                    validate_groq_key, validated.api_key, runtime_settings.groq_model
                )
        else:
            try:
                verification = await run_in_threadpool(
                    BrowserbaseClient(validated.api_key, validated.project_id or "").validate_project
                )
            except BrowserbaseError as exc:
                verification = {
                    "valid": False,
                    "status": exc.code,
                    "message": str(exc),
                }

            # The Browserbase project ID is itself account configuration.  The
            # caller already receives a masked hint, so keep only explicitly
            # safe validation diagnostics even when a test/different adapter
            # returns additional provider fields.
            verification = {
                key: verification[key]
                for key in (
                    "valid",
                    "status",
                    "message",
                    "project_name",
                    "concurrency",
                    "default_timeout",
                )
                if key in verification
            }

        valid = verification.get("valid") is True
        raw_code = verification.get("status")
        verification_code = (
            raw_code
            if isinstance(raw_code, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", raw_code)
            and raw_code != "ready"
            else None
        )
        if valid:
            verification_status: Literal["verified", "unverified", "invalid"] = "verified"
        elif provider == "groq" and verification_code == "groq_invalid_key":
            verification_status = "invalid"
        elif provider == "browserbase" and verification_code not in {
            "browserbase_timeout",
            "browserbase_unavailable",
            "browserbase_rate_limited",
        }:
            verification_status = "invalid"
        else:
            # Rate limits, model permissions, and provider outages must not make a
            # syntactically valid credential impossible to save for later use.
            verification_status = "unverified"

        if provider == "browserbase":
            if verification_status != "verified":
                raise ApiError(
                    503 if verification_status == "unverified" else 422,
                    verification_code or "browserbase_validation_failed",
                    str(
                        verification.get("message")
                        or "Browserbase could not validate that key and project."
                    ),
                )
            # Context IDs are scoped to the project that created them.  Remove all
            # old remote handles with the captured old client before changing the
            # project-bound credential. Invalid/outage candidates never reach here.
            await disconnect_all_managed_browser_connections(
                user, browserbase_credentials=old_browserbase_credentials
            )
        elif (
            existing is not None
            and existing.get("verification_status") == "verified"
            and verification_status != "verified"
        ):
            # A bad replacement must not downgrade a key that is already known
            # to work. First-time transient credentials may still be saved as
            # unverified so the user can validate later.
            raise ApiError(
                503 if verification_status == "unverified" else 422,
                verification_code or f"{provider}_validation_failed",
                str(
                    verification.get("message")
                    or f"{provider.title()} could not validate the replacement key."
                ),
            )

        encrypted = _cipher(runtime_settings).encrypt(
            _provider_credential_plaintext(provider, validated)
        )
        saved = _first(
            await server.rpc(
                "save_user_provider_credential",
                {
                    "user_id_input": str(user.user_id),
                    "provider_input": provider,
                    "credential_ciphertext_input": encrypted,
                    "verification_status_input": verification_status,
                    "verification_code_input": verification_code,
                    "verified_at_input": (
                        datetime.now(UTC).isoformat() if verification_status == "verified" else None
                    ),
                    "binding_fingerprint_input": (
                        _browserbase_project_fingerprint(validated.project_id or "")
                        if provider == "browserbase"
                        else None
                    ),
                    "expected_browserbase_epoch_input": expected_browserbase_epoch,
                },
            )
        )
        if saved is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The provider credential could not be saved.",
            )
        return {
            "data": {
                "provider": provider,
                "configured": True,
                "verification_status": verification_status,
                "verification_code": verification_code,
                "verified_at": saved.get("verified_at"),
                "updated_at": saved.get("updated_at"),
                "key_hint": _credential_hint(validated.api_key),
                "project_id_hint": _identifier_hint(validated.project_id),
                "requires_reconfiguration": False,
                **PROVIDER_CREDENTIAL_LINKS[provider],
            },
            "verification": verification,
        }

    @application.delete(
        "/api/v1/provider-credentials/{provider_id}", tags=["connections"]
    )
    async def delete_provider_credential(
        provider_id: str,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, bool]:
        if not runtime_settings.provider_credential_store_ready:
            raise ApiError(
                503,
                "provider_credential_store_unavailable",
                "Saved provider credentials are not configured for this deployment.",
            )
        provider = _provider_id(provider_id)
        server = store_service.secret()
        existing = await server.fetch_one(
            "user_provider_credentials",
            columns="provider,verification_status,generation,binding_fingerprint",
            filters={"user_id": str(user.user_id), "provider": provider},
        )
        expected_browserbase_epoch: int | None = None
        if provider == "browserbase":
            if existing is None:
                return {"ok": True}
            expected_browserbase_epoch = await load_browserbase_epoch(user.user_id)
            old_credentials = await resolve_browserbase_credentials(
                user.user_id, expected_epoch=expected_browserbase_epoch
            )
            await disconnect_all_managed_browser_connections(
                user, browserbase_credentials=old_credentials
            )
        deleted = await server.rpc(
            "delete_user_provider_credential",
            {
                "user_id_input": str(user.user_id),
                "provider_input": provider,
                "expected_browserbase_epoch_input": expected_browserbase_epoch,
            },
        )
        if deleted is not True:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The provider credential could not be deleted.",
            )
        return {"ok": True}

    @application.post(
        "/api/v1/provider-credentials/browserbase/abandon",
        tags=["connections"],
    )
    async def abandon_browserbase_resources_locally(
        body: BrowserbaseLocalAbandonRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Explicitly abandon handles only when remote cleanup is impossible."""

        if not runtime_settings.provider_credential_store_ready:
            raise ApiError(
                503,
                "provider_credential_store_unavailable",
                "Saved provider credentials are not configured for this deployment.",
            )
        abandoned = await store_service.secret().rpc(
            "abandon_browserbase_resources",
            {
                "user_id_input": str(user.user_id),
                "confirmation_input": body.confirmation,
            },
        )
        if abandoned is not True:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "Browserbase resources could not be abandoned locally.",
            )
        return {
            "ok": True,
            "remote_cleanup_confirmed": False,
            "browserbase_dashboard_url": PROVIDER_CREDENTIAL_LINKS["browserbase"][
                "project_url"
            ],
        }

    @application.post("/api/v1/groq/validate", tags=["groq"])
    async def validate_groq(
        groq_key: str | None = Header(default=None, alias="X-Groq-Api-Key"),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        groq_key = await resolve_groq_key(groq_key, user)
        await store_service.user(user.access_token).rpc(
            "reserve_groq_request", {"operation_input": "validate"}
        )
        return await run_in_threadpool(
            validate_groq_key, groq_key, runtime_settings.groq_model
        )

    @application.get("/api/v1/jobs", tags=["jobs"])
    async def list_jobs(
        job_status: str | None = Query(default=None, alias="status", max_length=40),
        limit: int = Query(default=50, ge=1, le=50),
        offset: int = Query(default=0, ge=0, le=10_000),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        filters: dict[str, Any] = {"user_id": str(user.user_id)}
        if job_status:
            filters["status"] = job_status
        rows = await client.fetch_many(
            "jobs", filters=filters, order="created_at.desc", limit=limit, offset=offset
        )
        profile = await client.fetch_one(
            "profiles",
            columns="skills,preferences",
            filters={"user_id": str(user.user_id)},
        ) or {}
        resume = await client.fetch_one(
            "resumes",
            columns="parse_status,parsed_text",
            filters={"user_id": str(user.user_id), "is_active": True},
        ) or {}
        resume_text = (
            resume.get("parsed_text")
            if resume.get("parse_status") == "parsed"
            and isinstance(resume.get("parsed_text"), str)
            else None
        )
        enriched = enrich_jobs_with_fit(rows, profile=profile, resume_text=resume_text)
        scores = [
            item["fit"]["score"]
            for item in enriched
            if item.get("fit", {}).get("evaluated")
            and isinstance(item.get("fit", {}).get("score"), int)
        ]
        return {
            **_items(enriched),
            "fit_summary": {
                "evaluated": bool(resume_text or profile.get("skills")),
                "recommended_roles": recommended_roles(profile, resume_text),
                "top_score": max(scores) if scores else None,
                "resume_status": resume.get("parse_status") or "missing",
            },
        }

    @application.post("/api/v1/jobs", status_code=201, tags=["jobs"])
    async def create_job(
        body: JobCreate, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        values = body.model_dump(mode="json")
        company_target: dict[str, str] | None = None
        yc_target_url = canonical_yc_job_url(body.apply_url)
        # Generic company forms are automation-eligible only after an authenticated
        # user explicitly saves the URL. Discovery/import paths never create this
        # marker and fixed providers retain their immutable detection rules.
        if yc_target_url is not None:
            values["apply_url"] = yc_target_url
        elif body.apply_url and detect_provider(body.apply_url) is None:
            company_target = public_company_form_target(body.apply_url)
        values["metadata"] = _yc_target_metadata(
            _company_form_metadata(values.get("metadata"), company_target),
            yc_target_url,
        )
        values |= {
            "user_id": str(user.user_id),
            "normalized_url": normalized_http_url(values.get("apply_url")),
            "status": "saved",
        }
        row = _first(await store_service.user(user.access_token).insert("jobs", values))
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "The job could not be saved.")
        if company_target is not None and body.apply_url is not None:
            await save_company_form_binding(
                user, row, body.apply_url, company_target
            )
        if yc_target_url is not None:
            await save_yc_job_binding(user, row, yc_target_url)
        return _data(row)

    @application.get("/api/v1/jobs/{job_id}", tags=["jobs"])
    async def get_job(job_id: UUID, user: AuthUser = Depends(current_user)) -> dict[str, Any]:
        return _data(await _required_row(store_service.user(user.access_token), "jobs", user, job_id))

    @application.post("/api/v1/jobs/{job_id}/contacts/public", tags=["jobs"])
    async def find_job_public_contacts(
        job_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        """Extract contact leads already present in the owned job record.

        This is intentionally a zero-credential operation. It never crawls an
        arbitrary site, guesses a person from a domain, sends a verification email,
        or performs SMTP probing. Public/user-supplied candidates remain explicitly
        unverified until the user reviews them.
        """

        job = await _required_row(
            store_service.user(user.access_token), "jobs", user, job_id
        )
        contacts = public_contact_candidates(job)
        return _data(
            {
                "job_id": str(job_id),
                "company": job.get("company"),
                "contacts": contacts,
                "verification": {
                    "status": "syntax_only",
                    "message": (
                        "No email was sent. Candidates come only from the saved job "
                        "record and require your review before drafting or sending."
                    ),
                },
            }
        )

    @application.get("/api/v1/jobs/{job_id}/contacts/public", tags=["jobs"])
    async def get_job_public_contacts(
        job_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        """Return persisted crawler evidence plus legacy imported candidates."""

        client = store_service.user(user.access_token)
        job = await _required_row(client, "jobs", user, job_id)
        persisted = await client.fetch_many(
            "job_contacts",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=2_000,
        )
        contacts: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_contact(value: Mapping[str, Any]) -> None:
            email = str(value.get("email") or "").strip().lower()
            if not email or email in seen:
                return
            seen.add(email)
            contacts.append(_public_contact_view(value))

        for row in persisted:
            if (
                isinstance(row, Mapping)
                and row.get("status") != "rejected"
                and (
                    str(row.get("job_id") or "") == str(job_id)
                    or row.get("company_key") == _contact_company_key(job.get("company"))
                )
            ):
                add_contact(row)
        for candidate in public_contact_candidates(job):
            add_contact(candidate)
        return _data(
            {
                "job_id": str(job_id),
                "company": job.get("company"),
                "contacts": contacts[:50],
                "verification": {
                    "status": "source_evidence" if persisted else "syntax_only",
                    "message": (
                        "Crawler contacts include the public page where the address appeared. "
                        "They are not SMTP or deliverability verified; review before drafting."
                        if persisted
                        else "Imported candidates come from the saved job record and require review."
                    ),
                },
            }
        )

    @application.post(
        "/api/v1/contacts/discover",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["contacts", "automation"],
    )
    async def queue_public_contact_discovery(
        body: PublicContactDiscoveryRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Queue one bounded, same-site public contact crawl for up to 30 jobs."""

        client = store_service.user(user.access_token)
        requested_ids = [str(job_id) for job_id in body.job_ids]
        owned_jobs = await client.fetch_many(
            "jobs",
            filters={"user_id": str(user.user_id)},
            limit=2_000,
        )
        owned_by_id = {
            str(row.get("id")): row
            for row in owned_jobs
            if isinstance(row, Mapping) and row.get("id")
        }
        if any(job_id not in owned_by_id for job_id in requested_ids):
            raise ApiError(404, "job_not_found", "One or more selected jobs were not found.")

        queued = _first(
            await client.rpc(
                "enqueue_public_contact_discovery",
                {
                    "job_ids_input": requested_ids,
                    "idempotency_key_input": body.idempotency_key,
                    "max_contacts_input": body.max_contacts_per_job,
                    "max_pages_input": body.max_pages_per_job,
                    "timeout_seconds_input": body.timeout_seconds,
                },
            )
        )
        if queued is None:
            raise ApiError(503, "data_store_invalid_response", "Contact discovery could not be queued.")

        existing_rows = await client.fetch_many(
            "job_contacts",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=2_000,
        )
        existing: dict[str, list[dict[str, Any]]] = {job_id: [] for job_id in requested_ids}
        for row in existing_rows:
            if not isinstance(row, Mapping):
                continue
            if row.get("status") == "rejected":
                continue
            job_id = str(row.get("job_id") or "")
            if job_id not in existing:
                continue
            email = str(row.get("email") or "").strip().lower()
            if not email or any(item.get("email") == email for item in existing[job_id]):
                continue
            existing[job_id].append(_public_contact_view(row))
        return _data(
            {
                "automation_job": _public_automation_job(queued),
                "job_ids": requested_ids,
                "contacts": existing,
                "status": "queued",
                "message": (
                    "Contact discovery is running in the persistent worker. "
                    "Refresh this page when the leads are ready."
                ),
            }
        )

    @application.post("/api/v1/outreach/research-prompt", tags=["outreach", "resumes"])
    async def create_outreach_research_prompt(
        body: OutreachResearchPromptRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Create a bounded brief for a user-run external AI web search."""

        client = store_service.user(user.access_token)
        profile = await client.fetch_one("profiles", filters={"user_id": str(user.user_id)}) or {}
        resume = await client.fetch_one(
            "resumes",
            columns="parse_status,parsed_text",
            filters={"user_id": str(user.user_id), "is_active": True},
        )
        resume_text = resume.get("parsed_text") if isinstance(resume, Mapping) else None
        if not isinstance(resume_text, str) or not resume_text.strip() or resume.get("parse_status") != "parsed":
            raise ApiError(409, "resume_not_parsed", "Upload and parse an active résumé before generating the research prompt.")
        result = build_research_prompt(
            profile,
            resume_text,
            target_role=body.target_role,
            location=body.location,
            remote_only=body.remote_only,
        )
        return _data(result)

    @application.patch("/api/v1/jobs/{job_id}", tags=["jobs"])
    async def patch_job(
        job_id: UUID, body: JobUpdate, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        existing = await _required_row(client, "jobs", user, job_id)
        values = _model_changes(body)
        company_target: dict[str, str] | None = None
        yc_target_url: str | None = None
        if "apply_url" in values:
            apply_url = values["apply_url"]
            yc_target_url = canonical_yc_job_url(apply_url)
            if yc_target_url is not None:
                values["apply_url"] = yc_target_url
                apply_url = yc_target_url
            elif apply_url and detect_provider(apply_url) is None:
                company_target = public_company_form_target(apply_url)
            values["normalized_url"] = normalized_http_url(apply_url)
            values["metadata"] = _yc_target_metadata(
                _company_form_metadata(
                    values.get("metadata", existing.get("metadata")), company_target
                ),
                yc_target_url,
            )
        elif "metadata" in values:
            existing_binding = await load_company_form_binding(user, existing)
            trusted_target = existing_binding[0] if existing_binding is not None else None
            existing_yc_binding = await load_yc_job_binding(user, existing)
            trusted_yc_target = (
                existing_yc_binding[0] if existing_yc_binding is not None else None
            )
            values["metadata"] = _yc_target_metadata(
                _company_form_metadata(values.get("metadata"), trusted_target),
                trusted_yc_target,
            )
        if values.get("status") == "archived":
            values["archived_at"] = _iso(_now())
        elif "status" in values:
            values["archived_at"] = None
        if values:
            row = _first(await client.update("jobs", values, filters=_owned(user, id=str(job_id))))
        else:
            row = await _required_row(client, "jobs", user, job_id)
        if row is None:
            raise ApiError(404, "job_not_found", "The job was not found.")
        if "apply_url" in values:
            apply_url = values.get("apply_url")
            if company_target is not None and isinstance(apply_url, str):
                await save_company_form_binding(user, row, apply_url, company_target)
            else:
                await delete_company_form_binding(user, job_id)
            if yc_target_url is not None:
                await save_yc_job_binding(user, row, yc_target_url)
            else:
                await delete_yc_job_binding(user, job_id)
        return _data(row)

    @application.delete("/api/v1/jobs/{job_id}", tags=["jobs"])
    async def delete_job(job_id: UUID, user: AuthUser = Depends(current_user)) -> dict[str, bool]:
        client = store_service.user(user.access_token)
        await _required_row(client, "jobs", user, job_id)
        await client.delete("jobs", filters=_owned(user, id=str(job_id)), returning=False)
        return {"ok": True}

    @application.post("/api/v1/jobs/{job_id}/draft", tags=["groq", "applications"])
    async def draft_job_application(
        job_id: UUID,
        body: DraftApplicationRequest | None = None,
        groq_key: str | None = Header(default=None, alias="X-Groq-Api-Key"),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        groq_key = await resolve_groq_key(groq_key, user)
        client = store_service.user(user.access_token)
        job = await _required_row(client, "jobs", user, job_id)
        recipient = body.recipient if body and body.recipient else job.get("contact_email")
        if not isinstance(recipient, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
            raise ApiError(
                422,
                "recipient_required",
                "Choose an email lead before creating a draft.",
            )
        await client.rpc("reserve_groq_request", {"operation_input": "generate"})
        writer = store_service.secret()
        profile = await client.fetch_one("profiles", filters={"user_id": str(user.user_id)}) or {}
        resume = await client.fetch_one(
            "resumes", filters={"user_id": str(user.user_id), "is_active": True}
        )
        if not resume or resume.get("parse_status") != "parsed" or not resume.get("parsed_text"):
            raise ApiError(409, "resume_not_parsed", "Upload and parse an active résumé first.")
        await client.update(
            "jobs", {"status": "drafting"}, filters=_owned(user, id=str(job_id)), returning=False
        )
        try:
            draft = await run_in_threadpool(
                generate_application_draft,
                groq_key,
                runtime_settings.groq_model,
                profile,
                job,
                resume["parsed_text"],
            )
        except GroqProviderError as exc:
            await client.update(
                "jobs", {"status": "saved"}, filters=_owned(user, id=str(job_id)), returning=False
            )
            raise _groq_error(exc) from exc
        existing = await client.fetch_one(
            "applications",
            filters={"user_id": str(user.user_id), "job_id": str(job_id), "status": "drafted"},
        )
        values = {
            "recipient": recipient,
            "subject": draft["subject"],
            "body": draft["body"],
            "status": "drafted",
            "approved_at": None,
        }
        if existing:
            row = _first(
                await writer.update(
                    "applications", values, filters=_owned(user, id=existing["id"])
                )
            )
        else:
            row = _first(
                await writer.insert(
                    "applications",
                    values
                    | {
                        "user_id": str(user.user_id),
                        "job_id": str(job_id),
                        "channel": "email",
                    },
                )
            )
        await client.update(
            "jobs", {"status": "ready"}, filters=_owned(user, id=str(job_id)), returning=False
        )
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "The draft could not be saved.")
        return _data(row)

    @application.post(
        "/api/v1/jobs/{job_id}/application/scan",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["applications", "automation"],
    )
    async def scan_job_application(
        job_id: UUID,
        body: ApplicationStageRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        if body.form_revision_id is not None:
            raise ApiError(422, "form_revision_not_expected", "A new scan cannot reuse an older revision.")
        client = store_service.user(user.access_token)
        job = await _required_row(client, "jobs", user, job_id)
        provider = detect_provider(job.get("apply_url"))
        company_binding: tuple[dict[str, str], dict[str, Any]] | None = None
        yc_binding: tuple[str, dict[str, Any]] | None = None
        yc_target_url = canonical_yc_job_url(job.get("apply_url"))
        if yc_target_url is not None:
            provider = "yc"
        if provider == "yc":
            yc_binding = await load_yc_job_binding(user, job)
            if yc_binding is None:
                raise ApiError(
                    409,
                    "yc_exact_job_url_required",
                    "Save or update this exact YC job-detail URL before scanning it. YC search and listing pages are not supported.",
                )
        if provider is None:
            company_binding = await load_company_form_binding(user, job)
            if company_binding is not None:
                provider = "company_form"
        if provider not in APPLICATION_AUTOMATION_PROVIDERS:
            raise ApiError(
                409,
                "application_provider_unsupported",
                "This URL is not a supported managed application form.",
            )
        await require_managed_connection(provider, user, operation="scan")
        active_resume = await client.fetch_one(
            "resumes", filters={"user_id": str(user.user_id), "is_active": True}
        )
        if active_resume is None:
            raise ApiError(409, "active_resume_required", "Upload and activate a résumé before scanning an application form.")
        application_row = await client.fetch_one(
            "applications",
            filters={
                "user_id": str(user.user_id),
                "job_id": str(job_id),
                "channel": "ats",
            },
        )
        if application_row is None:
            application_row = _first(
                await store_service.secret().insert(
                    "applications",
                    {
                        "user_id": str(user.user_id),
                        "job_id": str(job_id),
                        "channel": "ats",
                        "status": "draft_pending",
                        "metadata": {"provider": provider, "form_url": job.get("apply_url")},
                    },
                )
            )
        if application_row is None:
            raise ApiError(503, "data_store_invalid_response", "The application review could not be created.")
        scan_payload: dict[str, Any] = {"job_id": str(job_id)}
        if provider == "company_form":
            if company_binding is None:
                raise ApiError(
                    409,
                    "company_form_target_invalid",
                    "Save a public HTTPS company application URL before scanning.",
                )
            company_target = company_binding[0]
            scan_payload.update(
                {
                    "company_form_host": company_target["host"],
                    "company_form_target_url": company_target["target_url"],
                }
            )
        elif provider == "yc":
            if yc_binding is None:
                raise ApiError(
                    409,
                    "yc_exact_job_url_required",
                    "Save or update this exact YC job-detail URL before scanning it.",
                )
            scan_payload["yc_job_target_url"] = yc_binding[0]
        queued = await enqueue_job(
            user=user,
            kind="application_scan",
            provider=provider,
            application_id=application_row["id"],
            payload=scan_payload,
            idempotency_key=body.idempotency_key,
        )
        return {
            "data": {
                "application": application_row,
                "application_id": application_row["id"],
                "automation_job": queued,
            }
        }

    @application.get("/api/v1/applications", tags=["applications"])
    async def list_applications(
        application_status: str | None = Query(default=None, alias="status", max_length=40),
        channel: Literal["email", "ats", "manual"] | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=50),
        offset: int = Query(default=0, ge=0, le=10_000),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {"user_id": str(user.user_id)}
        if application_status:
            filters["status"] = application_status
        if channel:
            filters["channel"] = channel
        rows = await store_service.user(user.access_token).fetch_many(
            "applications", filters=filters, order="created_at.desc", limit=limit, offset=offset
        )
        return _items(rows)

    @application.post("/api/v1/applications", status_code=201, tags=["applications"])
    async def create_application(
        body: ApplicationCreate, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        writer = store_service.secret()
        if body.job_id is not None:
            await _required_row(client, "jobs", user, body.job_id)
        values = body.model_dump(mode="json") | {
            "user_id": str(user.user_id),
            "status": "drafted" if body.subject and body.body else "draft_pending",
        }
        row = _first(await writer.insert("applications", values))
        if row is None:
            raise ApiError(503, "data_store_invalid_response", "The application could not be saved.")
        return _data(row)

    @application.post(
        "/api/v1/applications/send-batch",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["applications", "automation"],
    )
    async def queue_application_send_batch(
        body: SendApplicationBatchRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Reserve and queue up to 30 reviewed emails for the persistent worker.

        The database function performs the approval, duplicate, Gmail-account, and
        daily-cap checks transactionally with the queue insert.  No Gmail call is
        made from this Vercel request.
        """

        client = store_service.user(user.access_token)
        queued: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for application_id in body.application_ids:
            item_key = f"{body.idempotency_key}-{application_id.hex[:24]}"
            try:
                row = _first(
                    await client.rpc(
                        "enqueue_email_send",
                        {
                            "application_id_input": str(application_id),
                            "idempotency_key_input": item_key,
                            "attach_resume_input": body.attach_resume,
                        },
                    )
                )
                if row is None:
                    raise ApiError(503, "email_queue_unavailable", "The email could not be queued.")
                queued.append(row)
            except ApiError as exc:
                rejected.append(
                    {
                        "application_id": str(application_id),
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
        return {
            "queued": queued,
            "rejected": rejected,
            "count": len(queued),
            "requested": len(body.application_ids),
            "daily_app_cap": 150,
        }

    @application.get("/api/v1/applications/{application_id}", tags=["applications"])
    async def get_application(
        application_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        return _data(
            await _required_row(
                store_service.user(user.access_token), "applications", user, application_id
            )
        )

    @application.patch("/api/v1/applications/{application_id}", tags=["applications"])
    async def patch_application(
        application_id: UUID,
        body: ApplicationUpdate,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        writer = store_service.secret()
        existing = await _required_row(client, "applications", user, application_id)
        values = _model_changes(body)
        if values.get("status") == "approved":
            raise ApiError(422, "approval_endpoint_required", "Use the review approval action.")
        content_changed = any(field in values for field in ("recipient", "subject", "body"))
        if existing.get("status") == "queued" and values:
            raise ApiError(409, "send_in_progress", "This application has a send in progress.")
        if content_changed:
            if existing.get("status") in {"queued", "sent"}:
                raise ApiError(
                    409,
                    "application_content_locked",
                    "Queued or sent message content cannot be changed.",
                )
            values["approved_at"] = None
            values["approved_revision"] = None
            values["content_revision"] = int(existing.get("content_revision") or 1) + 1
            values["status"] = "drafted"
        requested_status = values.get("status")
        allowed_transitions: dict[str, set[str]] = {
            "draft_pending": {"drafted", "manual", "archived"},
            "drafted": {"drafted", "manual", "archived"},
            "approved": {"drafted", "archived"},
            "failed": {"drafted", "archived"},
            "manual": {"applied", "archived"},
            "applied": {"interview", "rejected", "archived"},
            "sent": {"interview", "rejected", "archived"},
            "interview": {"rejected", "archived"},
            "rejected": {"archived"},
            "archived": set(),
        }
        if not content_changed and requested_status and requested_status not in allowed_transitions.get(
            str(existing.get("status")), set()
        ):
            raise ApiError(
                409,
                "application_status_conflict",
                "That application status transition is not permitted.",
            )
        if values:
            filters = _owned(user, id=str(application_id), status=existing.get("status"))
            if existing.get("updated_at"):
                filters["updated_at"] = existing["updated_at"]
            row = _first(
                await writer.update(
                    "applications", values, filters=filters
                )
            )
        else:
            row = existing
        if row is None:
            raise ApiError(404, "application_not_found", "The application was not found.")
        return _data(row)

    @application.post("/api/v1/applications/{application_id}/approve", tags=["applications"])
    async def approve_application(
        application_id: UUID,
        body: ApproveApplicationRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        updated = _first(
            await client.rpc(
                "approve_application_revision",
                {
                    "application_id_input": str(application_id),
                    "expected_revision_input": body.expected_revision,
                },
            )
        )
        if updated is None:
            raise ApiError(404, "application_not_found", "The application was not found.")
        return _data(updated)

    @application.get(
        "/api/v1/applications/{application_id}/form-revisions",
        tags=["applications"],
    )
    async def list_application_form_revisions(
        application_id: UUID,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        await _required_row(client, "applications", user, application_id)
        rows = await client.fetch_many(
            "application_form_revisions",
            filters={"user_id": str(user.user_id), "application_id": str(application_id)},
            order="created_at.desc",
            limit=50,
        )
        # Profile facts are read at review time rather than copied into the
        # worker's immutable scan. This lets an already prepared, but unsealed,
        # form pick up a newly saved public resume URL (and other deterministic
        # facts) without adding duplicate inputs to Form Pilot or requiring a
        # Groq round trip. Never overlay a sealed revision: the values displayed
        # for an approval must remain byte-for-byte aligned with the worker's
        # approved snapshot.
        profile = await client.fetch_one(
            "profiles", filters={"user_id": str(user.user_id)}
        ) or {}
        hydrated: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            questions = row.get("question_schema")
            unsealed = (
                row.get("approved_at") is None
                and row.get("status") in {"scanned", "prefilled"}
                and isinstance(questions, list)
            )
            row["profile_answers"] = (
                profile_form_answers(profile, questions) if unsealed else {}
            )
            hydrated.append(row)
        return _items(hydrated)

    @application.post(
        "/api/v1/application-form-revisions/{revision_id}/suggest",
        tags=["applications", "groq"],
    )
    async def suggest_application_form_answers(
        revision_id: UUID,
        groq_key: str | None = Header(default=None, alias="X-Groq-Api-Key"),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        revision = await _required_row(
            client, "application_form_revisions", user, revision_id
        )
        if revision.get("approved_at") or revision.get("status") in {"approved", "submitted"}:
            raise ApiError(409, "form_revision_sealed", "Run a new scan before changing a sealed revision.")
        application_id = revision.get("application_id")
        job_id = revision.get("job_id")
        resume_id = revision.get("resume_id")
        if not application_id or not job_id or not resume_id:
            raise ApiError(409, "form_revision_invalid", "The captured form is incomplete.")
        await _required_row(client, "applications", user, application_id)
        job = await _required_row(client, "jobs", user, job_id)
        resume = await _required_row(client, "resumes", user, resume_id)
        if resume.get("parse_status") != "parsed" or not resume.get("parsed_text"):
            raise ApiError(409, "resume_not_parsed", "Parse the résumé linked to this form revision first.")
        question_schema = revision.get("question_schema")
        if not isinstance(question_schema, list):
            raise ApiError(409, "form_revision_invalid", "The captured form schema is invalid.")
        profile = await client.fetch_one(
            "profiles", filters={"user_id": str(user.user_id)}
        ) or {}
        deterministic_answers = profile_form_answers(profile, question_schema)
        allowed_questions = [
            item
            for item in question_schema[:150]
            if isinstance(item, Mapping)
            and isinstance(item.get("key") or item.get("id") or item.get("name"), str)
            and isinstance(item.get("label") or item.get("title") or item.get("text"), str)
        ]
        resolved_keys = set(deterministic_answers)
        unresolved = [
            item
            for item in allowed_questions
            if str(item.get("key") or item.get("id") or item.get("name")) not in resolved_keys
        ]
        if not unresolved:
            return {
                "data": {
                    "revision_id": str(revision_id),
                    "revision": revision.get("revision"),
                    "schema_hash": revision.get("schema_hash"),
                    "answers": deterministic_answers,
                    "source": "profile",
                }
            }
        try:
            groq_key = await resolve_groq_key(groq_key, user)
        except ApiError as exc:
            if exc.code != "groq_key_required":
                raise
            # Deterministic profile facts remain useful without AI. Unknown/open
            # questions stay blank for the user instead of blocking known fields.
            return {
                "data": {
                    "revision_id": str(revision_id),
                    "revision": revision.get("revision"),
                    "schema_hash": revision.get("schema_hash"),
                    "answers": deterministic_answers,
                    "source": "profile_partial",
                }
            }
        await client.rpc("reserve_groq_request", {"operation_input": "generate"})
        try:
            answers = await run_in_threadpool(
                generate_form_answer_suggestions,
                groq_key,
                runtime_settings.groq_model,
                profile,
                job,
                resume["parsed_text"],
                question_schema,
            )
        except GroqProviderError as exc:
            raise _groq_error(exc) from exc
        return {
            "data": {
                "revision_id": str(revision_id),
                "revision": revision.get("revision"),
                "schema_hash": revision.get("schema_hash"),
                "answers": answers,
                "source": "groq",
            }
        }

    @application.post(
        "/api/v1/application-form-revisions/{revision_id}/approve",
        tags=["applications"],
    )
    async def approve_application_form_revision(
        revision_id: UUID,
        body: ApplicationFormApprovalRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        revision = await _required_row(
            client, "application_form_revisions", user, revision_id
        )
        if int(revision.get("revision") or 0) != body.expected_revision:
            raise ApiError(409, "form_revision_changed", "The captured form changed. Review the latest revision.")
        if revision.get("schema_hash") != body.schema_hash:
            raise ApiError(409, "form_schema_changed", "The form schema changed. Scan and review it again.")
        if len(json.dumps(body.answers, default=str, separators=(",", ":"))) > 32_000:
            raise ApiError(413, "form_answers_too_large", "The reviewed answers are too large.")
        updated = _first(
            await client.rpc(
                "approve_application_form_revision",
                {
                    "revision_id_input": str(revision_id),
                    "revision_input": body.expected_revision,
                    "schema_hash_input": body.schema_hash,
                    "answers_input": body.answers,
                },
            )
        )
        if updated is None:
            updated = await client.fetch_one(
                "application_form_revisions", filters=_owned(user, id=str(revision_id))
            )
        if updated is None:
            raise ApiError(409, "form_revision_changed", "The captured form changed. Review it again.")
        return _data(updated)

    @application.post(
        "/api/v1/application-form-revisions/{revision_id}/resolve-submission",
        tags=["applications"],
    )
    async def resolve_application_form_submission(
        revision_id: UUID,
        body: ApplicationFormSubmissionResolutionRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Resolve an uncertain form submit without ever reopening its snapshot."""

        client = store_service.user(user.access_token)
        revision = await _required_row(
            client, "application_form_revisions", user, revision_id
        )
        try:
            revision_number = int(revision.get("revision") or 0)
        except (TypeError, ValueError):
            revision_number = 0
        if revision_number != body.expected_revision:
            raise ApiError(
                409,
                "form_revision_changed",
                "The captured form changed. Review the latest revision.",
            )
        if revision.get("schema_hash") != body.schema_hash:
            raise ApiError(
                409,
                "form_schema_changed",
                "The form schema changed. Review the latest revision.",
            )
        if revision.get("status") not in {
            "approved",
            "needs_attention",
            "submitted",
            "superseded",
        }:
            raise ApiError(
                409,
                "form_submission_not_uncertain",
                "This form submission no longer needs an outcome decision.",
            )

        resolved = _first(
            await client.rpc(
                "resolve_application_form_submission",
                {
                    "revision_id_input": str(revision_id),
                    "expected_revision_input": body.expected_revision,
                    "expected_schema_hash_input": body.schema_hash,
                    "outcome_input": body.outcome,
                },
            )
        )
        if resolved is None:
            raise ApiError(
                409,
                "form_submission_resolution_stale",
                "The form state changed while resolving the submission. Refresh and review it again.",
            )
        # A not-submitted resolution returns its fresh retry revision. That
        # retry may itself be submitted by the time an idempotent HTTP request
        # is replayed, so its *status* cannot describe the original decision.
        # Revision identity is stable: only a submitted/provider-confirmed
        # resolution returns the requested immutable revision itself.
        actual_outcome = (
            "submitted"
            if str(resolved.get("id") or "") == str(revision_id)
            else "not_submitted"
        )
        return {
            "data": resolved,
            "resolution": {
                "outcome": actual_outcome,
                "retry_created": actual_outcome == "not_submitted",
                "rescan_required": actual_outcome == "not_submitted",
            },
        }

    async def queue_application_revision_stage(
        revision_id: UUID,
        body: ApplicationStageRequest,
        stage: str,
        user: AuthUser,
    ) -> dict[str, Any]:
        if body.form_revision_id is not None and body.form_revision_id != revision_id:
            raise ApiError(422, "form_revision_mismatch", "The requested form revision does not match the URL.")
        client = store_service.user(user.access_token)
        revision = await _required_row(
            client, "application_form_revisions", user, revision_id
        )
        try:
            revision_number = int(revision.get("revision") or 0)
        except (TypeError, ValueError):
            revision_number = 0
        approval_is_exact = (
            revision.get("status") == "approved"
            and revision.get("approved_at") is not None
            and revision_number >= 1
            and revision.get("approved_revision") == revision_number
            and revision.get("approved_schema_hash") == revision.get("schema_hash")
        )
        if not approval_is_exact:
            raise ApiError(409, "form_revision_not_approved", "Approve this exact form revision first.")
        provider = str(revision.get("provider") or "").lower()
        yc_target_url: str | None = None
        if provider == "company_form":
            form_target = public_company_form_target(revision.get("form_url"))
            if form_target is None:
                raise ApiError(
                    409,
                    "company_form_target_invalid",
                    "The approved company form no longer has a valid public HTTPS target.",
                )
            application_id = revision.get("application_id")
            if not application_id:
                raise ApiError(
                    409,
                    "form_revision_invalid",
                    "The form revision is not linked to an application.",
                )
            application_row = await _required_row(
                client, "applications", user, application_id
            )
            application_job_id = application_row.get("job_id")
            if (
                not application_job_id
                or str(revision.get("job_id") or "") != str(application_job_id)
            ):
                raise ApiError(
                    409,
                    "form_revision_invalid",
                    "The form revision is not linked to this saved job.",
                )
            bound_job = await _required_row(
                client, "jobs", user, application_job_id
            )
            company_binding = await load_company_form_binding(user, bound_job)
            if (
                company_binding is None
                or company_binding[0]["host"] != form_target["host"]
            ):
                raise ApiError(
                    409,
                    "company_form_target_changed",
                    "The saved company form host changed. Scan and review it again.",
                )
        elif provider == "yc":
            application_id = revision.get("application_id")
            if not application_id:
                raise ApiError(
                    409,
                    "form_revision_invalid",
                    "The YC form revision is not linked to an application.",
                )
            application_row = await _required_row(
                client, "applications", user, application_id
            )
            application_job_id = application_row.get("job_id")
            if (
                not application_job_id
                or str(revision.get("job_id") or "") != str(application_job_id)
            ):
                raise ApiError(
                    409,
                    "form_revision_invalid",
                    "The YC form revision is not linked to this saved job.",
                )
            bound_job = await _required_row(client, "jobs", user, application_job_id)
            yc_binding = await load_yc_job_binding(user, bound_job)
            if yc_binding is None:
                raise ApiError(
                    409,
                    "yc_exact_job_url_changed",
                    "The saved YC job URL changed. Scan and review it again.",
                )
            yc_target_url = yc_binding[0]
        submit_preflight: dict[str, Any] | None = None
        if stage == "submit":
            submit_preflight = _required_answer_preflight(revision)
            if not submit_preflight["complete"]:
                labels = submit_preflight["missing_labels"]
                visible = ", ".join(str(label) for label in labels[:5])[:180]
                remaining = int(submit_preflight["missing_count"]) - len(labels[:5])
                suffix = f" and {remaining} more" if remaining > 0 else ""
                raise ApiError(
                    409,
                    "form_required_answers_missing",
                    f"Complete these required approved answers before submitting: {visible}{suffix}.",
                )
        await require_managed_connection(provider, user, operation=stage)
        application_id = revision.get("application_id")
        if not application_id:
            raise ApiError(409, "form_revision_invalid", "The form revision is not linked to an application.")
        await _required_row(client, "applications", user, application_id)
        queue_payload: dict[str, Any] = {"form_revision_id": str(revision_id)}
        if provider == "company_form":
            queue_payload.update(
                {
                    "company_form_host": form_target["host"],
                    "company_form_target_url": form_target["target_url"],
                }
            )
        elif provider == "yc":
            if yc_target_url is None:
                raise ApiError(
                    409,
                    "yc_exact_job_url_changed",
                    "The saved YC job URL changed. Scan and review it again.",
                )
            queue_payload["yc_job_target_url"] = yc_target_url
        if stage == "submit":
            # This is derived only from the already approved immutable snapshot.
            # The worker also resolves that revision through a tenant-bound RPC;
            # no answer values are copied into the queue payload.
            queue_payload["required_answer_preflight"] = submit_preflight
        queued = await enqueue_job(
            user=user,
            kind=f"application_{stage}",
            provider=provider,
            application_id=application_id,
            payload=queue_payload,
            idempotency_key=body.idempotency_key,
        )
        return _data(queued)

    @application.post(
        "/api/v1/application-form-revisions/{revision_id}/prefill",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["applications", "automation"],
    )
    async def prefill_application_form_revision(
        revision_id: UUID,
        body: ApplicationStageRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        return await queue_application_revision_stage(revision_id, body, "prefill", user)

    @application.post(
        "/api/v1/application-form-revisions/{revision_id}/submit",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["applications", "automation"],
    )
    async def submit_application_form_revision(
        revision_id: UUID,
        body: ApplicationStageRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        return await queue_application_revision_stage(revision_id, body, "submit", user)

    @application.post(
        "/api/v1/applications/{application_id}/reconcile", tags=["applications"]
    )
    async def reconcile_application_send(
        application_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        event = _first(
            await client.rpc(
                "reconcile_stale_application_send",
                {"application_id_input": str(application_id)},
            )
        )
        if event is None:
            raise ApiError(404, "send_reservation_not_found", "No send reservation was found.")
        row = await _required_row(client, "applications", user, application_id)
        return {"data": row, "send_event": event}

    @application.post(
        "/api/v1/applications/{application_id}/send",
        status_code=status.HTTP_202_ACCEPTED,
        tags=["applications", "automation"],
    )
    async def send_application(
        application_id: UUID,
        body: SendApplicationRequest,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        """Queue one approved email; delivery happens in the persistent worker.

        Keep this compatibility endpoint asynchronous too. Older clients used
        this route directly and must not bring the Gmail request back into a
        Vercel function just because they have not adopted the batch endpoint.
        """
        client = store_service.user(user.access_token)
        queued = _first(
            await client.rpc(
                "enqueue_email_send",
                {
                    "application_id_input": str(application_id),
                    "idempotency_key_input": body.idempotency_key,
                    "attach_resume_input": body.attach_resume,
                },
            )
        )
        if queued is None:
            raise ApiError(503, "email_queue_unavailable", "The email could not be queued.")
        return {"data": queued, "queued": True, "worker": "persistent"}

    @application.get("/api/v1/connections", tags=["connections"])
    async def list_connections(user: AuthUser = Depends(current_user)) -> dict[str, Any]:
        rows = await store_service.user(user.access_token).fetch_many(
            "connections",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=100,
        )
        by_provider = {row.get("provider"): row for row in rows}
        catalog = provider_catalog(
            runtime_settings.allowed_browser_providers,
            google_configured=runtime_settings.gmail_connection_available,
            browserbase_configured=runtime_settings.managed_browser_available,
        )
        merged = [item | {"connection": by_provider.get(item["id"])} for item in catalog]
        return _items(merged)

    async def google_oauth_client_status(user: AuthUser) -> dict[str, Any]:
        row: dict[str, Any] | None = None
        if runtime_settings.google_byoc_ready:
            row = await store_service.secret().fetch_one(
                "user_google_oauth_clients",
                columns=(
                    "user_id,client_id_ciphertext,client_secret_ciphertext,"
                    "generation,created_at,updated_at"
                ),
                filters={"user_id": str(user.user_id)},
            )
        hint: str | None = None
        requires_reconfiguration = False
        if row is not None:
            try:
                cipher = _cipher(runtime_settings)
                client_id = cipher.decrypt(row["client_id_ciphertext"])
                client_secret = cipher.decrypt(row["client_secret_ciphertext"])
                GoogleOAuthClientUpsert.model_validate(
                    {"client_id": client_id, "client_secret": client_secret}
                )
                hint = _google_client_id_hint(client_id)
            except (ApiError, KeyError, TokenCipherError, TypeError, ValueError):
                requires_reconfiguration = True

        connection = await store_service.user(user.access_token).fetch_one(
            "connections",
            columns="id,status,metadata",
            filters={"user_id": str(user.user_id), "provider": "gmail"},
        )
        connected_source: str | None = None
        if connection is not None and connection.get("status") == "connected":
            metadata = connection.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            source = metadata.get("oauth_client_source", "platform")
            connected_source = source if source in {"platform", "user"} else None

        return {
            "platform_available": runtime_settings.google_configured,
            "byoc_available": runtime_settings.google_byoc_ready,
            "redirect_uri": runtime_settings.google_redirect_uri or None,
            "configured": row is not None,
            "client_id_hint": hint,
            "updated_at": row.get("updated_at") if row is not None else None,
            "connected_source": connected_source,
            "requires_reconfiguration": requires_reconfiguration,
        }

    @application.get(
        "/api/v1/connections/google-oauth-client", tags=["connections"]
    )
    @application.get(
        "/api/v1/connections/gmail/oauth-client",
        tags=["connections"],
        include_in_schema=False,
    )
    async def get_google_oauth_client(
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        return _data(await google_oauth_client_status(user))

    @application.put(
        "/api/v1/connections/google-oauth-client", tags=["connections"]
    )
    @application.put(
        "/api/v1/connections/gmail/oauth-client",
        tags=["connections"],
        include_in_schema=False,
    )
    async def save_user_google_oauth_client(
        body: GoogleOAuthClientUpsert,
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        if not runtime_settings.google_byoc_ready:
            raise ApiError(
                503,
                "gmail_user_oauth_unavailable",
                "User-managed Google OAuth apps are not configured for this deployment.",
            )
        cipher = _cipher(runtime_settings)
        try:
            client_id_ciphertext = cipher.encrypt(body.client_id)
            client_secret_ciphertext = cipher.encrypt(body.client_secret)
        except TokenCipherError as exc:
            raise ApiError(
                503,
                "token_encryption_unavailable",
                "Provider connections are not configured.",
            ) from exc
        generation = _positive_integer(
            await store_service.secret().rpc(
                "save_user_google_oauth_client",
                {
                    "user_id_input": str(user.user_id),
                    "client_id_ciphertext_input": client_id_ciphertext,
                    "client_secret_ciphertext_input": client_secret_ciphertext,
                },
            )
        )
        if generation is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid response.",
            )
        status_data = await google_oauth_client_status(user)
        status_data["configured"] = True
        status_data["client_id_hint"] = _google_client_id_hint(body.client_id)
        status_data["requires_reconfiguration"] = False
        return _data(status_data)

    @application.delete(
        "/api/v1/connections/google-oauth-client", tags=["connections"]
    )
    @application.delete(
        "/api/v1/connections/gmail/oauth-client",
        tags=["connections"],
        include_in_schema=False,
    )
    async def delete_user_google_oauth_client(
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        if not runtime_settings.google_byoc_ready:
            raise ApiError(
                503,
                "gmail_user_oauth_unavailable",
                "User-managed Google OAuth apps are not configured for this deployment.",
            )
        generation = _positive_integer(
            await store_service.secret().rpc(
                "delete_user_google_oauth_client",
                {"user_id_input": str(user.user_id)},
            )
        )
        if generation is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid response.",
            )
        return {"ok": True}

    @application.post("/api/v1/oauth/google/start", tags=["connections"])
    async def start_google_oauth(
        body: GoogleOAuthStartRequest | None = None,
        return_path: str = Query(default="/", max_length=1024),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, str]:
        if not runtime_settings.secret_store_configured or not runtime_settings.google_redirect_uri:
            raise ApiError(503, "gmail_not_configured", "Gmail connection is not configured.")
        credential_source = (body or GoogleOAuthStartRequest()).credential_source
        oauth_credentials = await resolve_google_oauth_credentials(
            credential_source, user.user_id
        )
        await store_service.user(user.access_token).rpc("reserve_google_oauth_start")
        state_value = secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(state_value.encode("ascii")).hexdigest()
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        lifecycle_generation = _positive_integer(
            await store_service.secret().rpc(
                "create_google_oauth_state_v2",
                {
                    "user_id_input": str(user.user_id),
                    "state_hash_input": state_hash,
                    "return_path_input": _safe_return_path(return_path),
                    "pkce_verifier_ciphertext_input": _cipher(runtime_settings).encrypt(
                        verifier
                    ),
                    "expires_at_input": _iso(
                        _now()
                        + timedelta(seconds=runtime_settings.oauth_state_ttl_seconds)
                    ),
                    "credential_source_input": oauth_credentials.source,
                    "credential_generation_input": (
                        oauth_credentials.credential_generation
                    ),
                },
            )
        )
        if lifecycle_generation is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid response.",
            )
        authorization_url = build_google_authorization_url(
            oauth_credentials.client_id,
            runtime_settings.google_redirect_uri,
            state_value,
            code_challenge=challenge,
        )
        return {
            "authorization_url": authorization_url,
            "credential_source": oauth_credentials.source,
        }

    @application.get("/api/v1/oauth/google/callback", tags=["connections"])
    async def google_oauth_callback(
        state: str | None = Query(default=None, max_length=512),
        code: str | None = Query(default=None, max_length=4096),
        error: str | None = Query(default=None, max_length=200),
    ) -> Response:
        if not state:
            return _redirect_with_status(
                runtime_settings, "/", oauth="error", code="oauth_state_invalid"
            )
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        consumed = _first(
            await store_service.secret().rpc(
                "consume_oauth_state",
                {"state_hash_input": state_hash, "provider_input": "google"},
            )
        )
        if consumed is None:
            return _redirect_with_status(
                runtime_settings, "/", oauth="error", code="oauth_state_invalid"
            )
        return_path = _safe_return_path(consumed.get("return_path"))
        if error or not code:
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="oauth_authorization_denied"
            )
        generation = _positive_integer(consumed.get("generation"))
        if generation is None:
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="oauth_state_invalid"
            )
        try:
            user_id = str(UUID(str(consumed["user_id"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="oauth_state_invalid"
            )
        credential_source = consumed.get("credential_source", "platform")
        credential_generation = (
            _positive_integer(consumed.get("credential_generation"))
            if credential_source == "user"
            else None
        )
        if credential_source not in {"platform", "user"} or (
            credential_source == "user" and credential_generation is None
        ) or (
            credential_source == "platform"
            and consumed.get("credential_generation") is not None
        ):
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="oauth_state_invalid"
            )
        token: dict[str, Any] | None = None
        try:
            oauth_credentials = await resolve_google_oauth_credentials(
                str(credential_source),
                user_id,
                expected_generation=credential_generation,
            )
            verifier = _cipher(runtime_settings).decrypt(
                consumed["pkce_verifier_ciphertext"]
            )
            token = await run_in_threadpool(
                exchange_google_code,
                code,
                oauth_credentials.client_id,
                oauth_credentials.client_secret,
                runtime_settings.google_redirect_uri,
                code_verifier=verifier,
            )
        except (ApiError, GoogleProviderError, TokenCipherError, KeyError):
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="oauth_exchange_failed"
            )
        try:
            account = await run_in_threadpool(get_google_userinfo, token["access_token"])
        except (GoogleProviderError, KeyError):
            await _revoke_google_grant_best_effort(token)
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="google_account_lookup_failed"
            )
        if account.get("email_verified") is not True:
            await _revoke_google_grant_best_effort(token)
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="google_email_unverified"
            )

        expires_in = token.get("expires_in")
        expires_at = (
            _iso(_now() + timedelta(seconds=max(0, expires_in)))
            if isinstance(expires_in, int) and not isinstance(expires_in, bool)
            else None
        )
        raw_scopes = token.get("scope")
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else []
        if GMAIL_SEND_SCOPE not in scopes:
            await _revoke_google_grant_best_effort(token)
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="google_scope_missing"
            )
        server = store_service.secret()
        try:
            cipher = _cipher(runtime_settings)
            existing_connection = await server.fetch_one(
                "connections",
                columns="id,metadata",
                filters={"user_id": user_id, "provider": "gmail"},
            )
            if existing_connection is not None and not token.get("refresh_token"):
                prior_metadata = existing_connection.get("metadata")
                prior_metadata = (
                    prior_metadata if isinstance(prior_metadata, Mapping) else {}
                )
                prior_source = prior_metadata.get("oauth_client_source", "platform")
                prior_generation = (
                    _positive_integer(prior_metadata.get("oauth_client_generation"))
                    if prior_source == "user"
                    else None
                )
                if (
                    prior_source != oauth_credentials.source
                    or prior_generation != oauth_credentials.credential_generation
                ):
                    raise ApiError(
                        409,
                        "google_refresh_token_required",
                        "Google did not return offline access. Reconnect and approve access again.",
                    )
            connection = _first(
                await server.rpc(
                    "save_google_connection",
                    {
                        "user_id_input": user_id,
                        "expected_generation_input": generation,
                        "external_account_id_input": account["sub"],
                        "display_name_input": account["email"],
                        "scopes_input": scopes,
                        "expires_at_input": expires_at,
                        "metadata_input": {
                            "email_verified": True,
                            "oauth_client_source": oauth_credentials.source,
                            "oauth_client_generation": (
                                oauth_credentials.credential_generation
                            ),
                        },
                        "access_token_ciphertext_input": cipher.encrypt(token["access_token"]),
                        "refresh_token_ciphertext_input": (
                            cipher.encrypt(token["refresh_token"])
                            if token.get("refresh_token")
                            else None
                        ),
                        "token_type_input": token.get("token_type", "Bearer"),
                    },
                )
            )
        except (ApiError, TokenCipherError, KeyError):
            await _revoke_google_grant_best_effort(token)
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="connection_save_failed"
            )
        if connection is None:
            await _revoke_google_grant_best_effort(token)
            return _redirect_with_status(
                runtime_settings, return_path, oauth="error", code="connection_save_failed"
            )
        return _redirect_with_status(
            runtime_settings, return_path, oauth="connected", provider="gmail"
        )

    @application.delete("/api/v1/connections/gmail", tags=["connections"])
    async def disconnect_gmail(user: AuthUser = Depends(current_user)) -> dict[str, bool]:
        server = store_service.secret()
        lifecycle_generation = _positive_integer(
            await server.rpc(
                "begin_google_disconnect", {"user_id_input": str(user.user_id)}
            )
        )
        if lifecycle_generation is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid response.",
            )
        connection = await server.fetch_one(
            "connections", filters={"user_id": str(user.user_id), "provider": "gmail"}
        )
        revoked = False
        if connection is not None:
            secret_row = await server.fetch_one(
                "connection_secrets",
                filters={
                    "connection_id": connection["id"],
                    "user_id": str(user.user_id),
                },
            )
        else:
            secret_row = None
        if secret_row is not None:
            try:
                cipher = _cipher(runtime_settings)
                token = cipher.decrypt_optional(secret_row.get("refresh_token_ciphertext"))
                token = token or cipher.decrypt_optional(secret_row.get("access_token_ciphertext"))
                if token:
                    revoked = await _revoke_google_grant_best_effort(
                        {"refresh_token": token}
                    )
            except (ApiError, TokenCipherError):
                # Local deletion still disconnects the product; the user can also revoke
                # AutoApply from their Google Account if the provider is unavailable.
                revoked = False
        await server.rpc(
            "finish_google_disconnect",
            {
                "user_id_input": str(user.user_id),
                "expected_generation_input": lifecycle_generation,
            },
        )
        return {"ok": True, "revoked": revoked}

    @application.post(
        "/api/v1/connections/{provider_id}/browser/start", tags=["connections"]
    )
    async def start_browser_connection(
        provider_id: str, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        provider_id = provider_id.strip().lower()
        capability = get_provider(
            provider_id,
            runtime_settings.allowed_browser_providers,
            google_configured=runtime_settings.google_configured,
            browserbase_configured=runtime_settings.managed_browser_available,
        )
        if (
            capability is None
            or provider_id not in MANAGED_BROWSER_LIFECYCLE_PROVIDERS
            or not browser_provider_allowed(
                provider_id, runtime_settings.allowed_browser_providers
            )
            or not runtime_settings.managed_browser_available
        ):
            raise ApiError(
                409,
                "provider_connection_unavailable",
                "This provider is not enabled for managed-browser connection.",
            )
        server = store_service.secret()
        cipher = _cipher(runtime_settings)
        # Move the database lifecycle to `connecting` before decrypting a key or
        # touching Browserbase.  Credential save/delete and application enqueue
        # use the same account lock and reject this lifecycle, closing the
        # resolve-before-start rotation race.
        lifecycle = _browser_lifecycle_snapshot(
            await server.rpc(
                "begin_browser_start",
                {"user_id_input": str(user.user_id), "provider_input": provider_id},
            ),
            include_reuse=True,
        )
        generation = lifecycle["generation"]
        connection_id = lifecycle["connection_id"]
        reuse_context = lifecycle["reuse_context"]
        credentials: BrowserbaseCredentials | None = None
        browser: BrowserbaseClient | None = None
        context_id: str | None = None
        prior_session_id: str | None = None
        replacing_existing_context = False
        created_context = False
        session_id: str | None = None
        session_ciphertext: str | None = None
        context_ciphertext: str | None = None
        remote_cleanup_complete = True
        try:
            credentials = await resolve_browserbase_credentials(
                user.user_id, expected_epoch=lifecycle["credential_epoch"]
            )
            assert_active_browserbase_binding(credentials, lifecycle)
            if lifecycle["context_ciphertext"] or lifecycle["session_ciphertext"]:
                assert_browserbase_binding(credentials, lifecycle)
            browser = BrowserbaseClient(credentials.api_key, credentials.project_id)
            context_id = cipher.decrypt_optional(lifecycle["context_ciphertext"])
            prior_session_id = cipher.decrypt_optional(lifecycle["session_ciphertext"])
            replacing_existing_context = bool(context_id and not reuse_context)
            if prior_session_id:
                remote_cleanup_complete = False
                await run_in_threadpool(browser.release_session, prior_session_id)
                remote_cleanup_complete = True
            if replacing_existing_context and context_id:
                remote_cleanup_complete = False
                await run_in_threadpool(browser.delete_context, context_id)
                context_id = None
                remote_cleanup_complete = True

            if context_id is None:
                context_id = (await run_in_threadpool(browser.create_context))["id"]
                created_context = True
            context_ciphertext = cipher.encrypt(context_id)
            connection = _first(
                await server.rpc(
                    "save_browser_connection_context_bound",
                    {
                        "user_id_input": str(user.user_id),
                        "provider_input": provider_id,
                        "expected_generation_input": generation,
                        "display_name_input": capability["label"],
                        "context_ciphertext_input": context_ciphertext,
                        "credential_source_input": credentials.source,
                        "credential_generation_input": credentials.generation,
                        "credential_epoch_input": credentials.epoch,
                        "project_fingerprint_input": credentials.project_fingerprint,
                    },
                )
            )
            if connection is None:
                raise ApiError(
                    503, "connection_save_failed", "The connection could not be saved."
                )
            try:
                connection_id = str(UUID(str(connection["id"])))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ApiError(
                    503,
                    "data_store_invalid_response",
                    "The data service returned an invalid browser connection.",
                ) from exc
            session = await run_in_threadpool(
                browser.create_session,
                context_id,
                keep_alive=False,
                timeout_seconds=MANAGED_BROWSER_LOGIN_TIMEOUT_SECONDS,
                user_metadata={"provider": provider_id},
            )
            session_id = session["id"]
            session_ciphertext = cipher.encrypt(session_id)
            persisted_session = await server.rpc(
                "save_browser_connection_session",
                {
                    "user_id_input": str(user.user_id),
                    "provider_input": provider_id,
                    "expected_generation_input": generation,
                    "expected_connection_id_input": connection_id,
                    "expected_context_ciphertext_input": context_ciphertext,
                    "session_ciphertext_input": session_ciphertext,
                },
            )
            if persisted_session is not True:
                raise ApiError(
                    503,
                    "data_store_invalid_response",
                    "The data service returned an invalid browser lifecycle response.",
                )
            live_view = await run_in_threadpool(browser.get_session_live_view, session_id)
            confirmed = await server.rpc(
                "confirm_browser_start",
                {
                    "user_id_input": str(user.user_id),
                    "provider_input": provider_id,
                    "expected_generation_input": generation,
                    "expected_connection_id_input": connection_id,
                    "expected_context_ciphertext_input": context_ciphertext,
                    "expected_session_ciphertext_input": session_ciphertext,
                },
            )
            if confirmed is not True:
                raise ApiError(
                    503,
                    "data_store_invalid_response",
                    "The data service returned an invalid browser lifecycle response.",
                )
        except (ApiError, BrowserbaseError, TokenCipherError) as exc:
            if session_id and browser is not None:
                try:
                    await run_in_threadpool(browser.release_session, session_id)
                except BrowserbaseError:
                    remote_cleanup_complete = False
            if created_context and context_id and browser is not None:
                try:
                    await run_in_threadpool(browser.delete_context, context_id)
                except BrowserbaseError:
                    remote_cleanup_complete = False
            if remote_cleanup_complete:
                try:
                    await server.rpc(
                        "abort_browser_start",
                        {
                            "user_id_input": str(user.user_id),
                            "provider_input": provider_id,
                            "expected_generation_input": generation,
                            "expected_connection_id_input": connection_id,
                            "expected_session_ciphertext_input": session_ciphertext,
                            "drop_connection_input": (
                                replacing_existing_context or created_context
                            ),
                        },
                    )
                except ApiError:
                    # A newer start/disconnect owns the row. This request has already
                    # removed only the remote resources it created.
                    pass
            if isinstance(exc, ApiError):
                raise
            if isinstance(exc, BrowserbaseError):
                raise ApiError(503, exc.code, str(exc)) from exc
            raise ApiError(
                503, "token_encryption_unavailable", "Provider connections are not configured."
            ) from exc
        return {
            "data": {
                "provider": provider_id,
                "session_id": session_id,
                "live_view_url": live_view["live_view_url"],
                "expires_at": session.get("expires_at"),
            }
        }

    @application.post(
        "/api/v1/connections/{provider_id}/browser/complete", tags=["connections"]
    )
    async def complete_browser_connection(
        provider_id: str, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        provider_id = provider_id.strip().lower()
        if provider_id not in MANAGED_BROWSER_LIFECYCLE_PROVIDERS:
            raise ApiError(409, "provider_connection_unavailable", "This provider is not enabled.")
        client = store_service.user(user.access_token)
        connection = await client.fetch_one(
            "connections",
            filters={"user_id": str(user.user_id), "provider": provider_id},
        )
        if connection is None or connection.get("mode") != "managed_browser":
            raise ApiError(404, "connection_not_found", "Start the provider login first.")
        server = store_service.secret()
        secret_row = await server.fetch_one(
            "connection_secrets",
            filters={"connection_id": connection["id"], "user_id": str(user.user_id)},
        )
        if (
            not secret_row
            or not secret_row.get("browser_context_id_ciphertext")
            or not secret_row.get("browser_session_id_ciphertext")
            or _positive_integer(secret_row.get("browser_lifecycle_generation")) is None
        ):
            raise ApiError(409, "browser_context_missing", "Start the provider login again.")
        generation = _positive_integer(secret_row["browser_lifecycle_generation"])
        if generation is None:
            raise ApiError(409, "browser_context_missing", "Start the provider login again.")
        session_ciphertext = secret_row["browser_session_id_ciphertext"]
        try:
            cipher = _cipher(runtime_settings)
            context_id = cipher.decrypt(secret_row["browser_context_id_ciphertext"])
            session_id = cipher.decrypt(session_ciphertext)
        except TokenCipherError as exc:
            raise ApiError(409, "browser_context_missing", "Start the provider login again.") from exc
        credentials = await resolve_browserbase_credentials(user.user_id)
        assert_browserbase_binding(credentials, secret_row)
        browser = BrowserbaseClient(credentials.api_key, credentials.project_id)
        try:
            session = await run_in_threadpool(browser.get_session, session_id)
        except BrowserbaseError as exc:
            if exc.status_code == 404:
                await server.rpc(
                    "abort_browser_start",
                    {
                        "user_id_input": str(user.user_id),
                        "provider_input": provider_id,
                        "expected_generation_input": generation,
                        "expected_connection_id_input": connection["id"],
                        "expected_session_ciphertext_input": session_ciphertext,
                        "drop_connection_input": False,
                    },
                )
                raise ApiError(
                    409, "browser_session_expired", "The login session expired. Start it again."
                ) from exc
            raise ApiError(503, exc.code, str(exc)) from exc
        if session.get("context_id") != context_id:
            raise ApiError(
                409,
                "browser_session_mismatch",
                "The login session does not match the saved browser context.",
            )
        if session.get("status") in {"ERROR", "TIMED_OUT"}:
            await server.rpc(
                "abort_browser_start",
                {
                    "user_id_input": str(user.user_id),
                    "provider_input": provider_id,
                    "expected_generation_input": generation,
                    "expected_connection_id_input": connection["id"],
                    "expected_session_ciphertext_input": session_ciphertext,
                    "drop_connection_input": False,
                },
            )
            raise ApiError(
                409, "browser_session_expired", "The login session ended. Start it again."
            )
        try:
            if session.get("status") in {"PENDING", "RUNNING"}:
                await run_in_threadpool(browser.release_session, session_id)
        except BrowserbaseError as exc:
            raise ApiError(503, exc.code, str(exc)) from exc
        updated = _first(
            await server.rpc(
                "finish_browser_start",
                {
                    "user_id_input": str(user.user_id),
                    "provider_input": provider_id,
                    "expected_generation_input": generation,
                    "expected_connection_id_input": connection["id"],
                    "expected_session_ciphertext_input": session_ciphertext,
                },
            )
        )
        if updated is None:
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid browser lifecycle response.",
            )
        return _data(updated)

    @application.delete("/api/v1/connections/{provider_id}", tags=["connections"])
    async def delete_connection(
        provider_id: str, user: AuthUser = Depends(current_user)
    ) -> dict[str, bool]:
        provider_id = provider_id.strip().lower()
        if provider_id == "gmail":
            return await disconnect_gmail(user)
        return await disconnect_managed_browser_connection(provider_id, user)

    @application.post(
        "/api/v1/automation-jobs", status_code=status.HTTP_202_ACCEPTED, tags=["automation"]
    )
    async def create_automation_job(
        body: AutomationJobCreate, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        if len(json.dumps(body.payload, default=str, separators=(",", ":"))) > 32_000:
            raise ApiError(413, "automation_payload_too_large", "The automation request is too large.")
        if body.kind in {
            "discover_public_feeds",
            "discover_linkedin_guest",
            "discover_public_ats",
            "discover_public_contacts",
            "application_scan",
            "application_prefill",
            "application_submit",
        }:
            raise ApiError(
                409,
                "dedicated_workflow_required",
                "Use the discovery or form-review action for this queued work.",
            )
        if not body.provider:
            raise ApiError(422, "provider_required", "Choose a provider for this job.")
        if body.provider:
            capability = get_provider(
                body.provider,
                runtime_settings.allowed_browser_providers,
                google_configured=runtime_settings.google_configured,
                browserbase_configured=runtime_settings.managed_browser_available,
            )
            if capability is None:
                raise ApiError(422, "provider_unknown", "Choose a supported provider.")
            if body.kind == "ats_prepare" and not browser_provider_allowed(
                body.provider, runtime_settings.allowed_browser_providers
            ):
                raise ApiError(
                    409,
                    "provider_automation_unavailable",
                    "This provider supports manual handoff only.",
                )
        row = await enqueue_job(
            user=user,
            kind=body.kind,
            provider=body.provider,
            application_id=body.application_id,
            payload=body.payload,
            idempotency_key=body.idempotency_key,
        )
        return _data(row)

    @application.get("/api/v1/automation-jobs", tags=["automation"])
    async def list_automation_jobs(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
        user: AuthUser = Depends(current_user),
    ) -> dict[str, Any]:
        rows = await store_service.user(user.access_token).fetch_many(
            "automation_jobs",
            filters={"user_id": str(user.user_id)},
            order="created_at.desc",
            limit=limit,
            offset=offset,
        )
        return _items(rows)

    @application.get("/api/v1/automation-jobs/{job_id}", tags=["automation"])
    async def get_automation_job(
        job_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        return _data(
            await _required_row(
                store_service.user(user.access_token), "automation_jobs", user, job_id
            )
        )

    @application.post("/api/v1/automation-jobs/{job_id}/cancel", tags=["automation"])
    async def cancel_automation_job(
        job_id: UUID, user: AuthUser = Depends(current_user)
    ) -> dict[str, Any]:
        client = store_service.user(user.access_token)
        updated = _first(
            await client.rpc("cancel_automation_job", {"job_id": str(job_id)})
        )
        if updated is None:
            raise ApiError(404, "automation_job_not_found", "The automation job was not found.")
        return _data(updated)

    # Vercel serves public/** from its CDN. These explicit routes make direct Uvicorn
    # development behave identically without mounting a catch-all over /api.
    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            PUBLIC_DIR / "index.html", headers={"Cache-Control": "no-store"}
        )

    @application.get("/assets/{asset_path:path}", include_in_schema=False)
    async def next_asset(asset_path: str) -> FileResponse:
        """Serve the checked-in Next export during direct Uvicorn development.

        Vercel serves these files from its public CDN.  The explicit local route
        keeps `./dev.command` equivalent to production without exposing files
        outside the public asset directory.
        """

        candidate = (PUBLIC_ASSET_DIR / asset_path).resolve()
        if PUBLIC_ASSET_DIR not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404, detail="Asset not found")
        return FileResponse(
            candidate,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @application.get("/privacy.html", include_in_schema=False)
    async def privacy_page() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "privacy.html")

    @application.get("/terms.html", include_in_schema=False)
    async def terms_page() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "terms.html")

    return application


app = create_app()
