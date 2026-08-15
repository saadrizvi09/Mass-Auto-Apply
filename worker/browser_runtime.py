"""Tenant-bound Browserbase execution for reviewed application jobs.

The web process never receives a CDP URL.  This worker resolves a lease-bound
tenant bundle through a service-only RPC, decrypts only that user's browser
context, and downloads only that user's active résumé.  Provider adapters scan
structure and fill exact approved values; they do not infer answers.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import secrets
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from app.saas.browser import BrowserbaseClient, TrustedBrowserSession
from app.saas.crypto import TokenCipher
from worker.providers import get_adapter
from worker.providers.base import (
    ExecutionPhase,
    FormSchema,
    ProviderAdapter,
    ProviderResult,
    bind_schema_to_target,
    canonical_form_target,
    checkpoint_present,
    fill_approved,
    resume_upload_guard_issue,
    safe_form_url,
    scan_form,
)
from worker.providers.yc import (
    canonical_yc_form_target,
    is_exact_yc_job_url,
    yc_schema_issue,
)


MANAGED_JOB_KINDS: tuple[str, ...] = (
    "application_scan",
    "application_prefill",
    "application_submit",
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESUME_BYTES = 6 * 1024 * 1024
# Keep metered browser tasks short while leaving enough headroom for the
# roughly one-minute Google Forms submission path. Browserbase ends completed
# sessions on disconnect, so this is a stall ceiling rather than a minimum run
# time.
_BROWSER_SESSION_TIMEOUT_SECONDS = 90
_GOOGLE_FORM_RENDER_TIMEOUT_MS = 8_000
_GOOGLE_FORM_CONTROL_SELECTOR = (
    'form input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]),'
    "form textarea,form select,form [role=\"combobox\"],form [role=\"listbox\"],"
    "form [role=\"radio\"],form [role=\"checkbox\"]"
)
_PUBLIC_SESSION_PROVIDERS = frozenset(
    {"company_form", "google_forms", "greenhouse", "lever", "ashby"}
)
_RETAINED_SUBMIT_PROVIDERS = frozenset({"company_form", "google_forms", "yc"})
_SUBMIT_LIVE_VIEW_CODES = frozenset(
    {
        "provider_login_required",
        "provider_redirect_blocked",
        "security_checkpoint",
        "application_entry_ambiguous",
        "application_entry_unavailable",
        "application_entry_unconfirmed",
        "application_form_not_found",
        "application_form_ambiguous",
        "form_approval_required",
        "form_schema_changed",
        "required_answers_missing",
        "file_upload_inspection_failed",
        "required_file_upload_unsupported",
        "provider_file_picker_unsupported",
        "resume_upload_ambiguous",
        "resume_upload_unsupported",
        "final_action_ambiguous",
        "submission_click_unconfirmed",
        "provider_redirect_blocked_after_submit",
        "security_checkpoint_after_submit",
        "provider_validation_failed",
        "submission_unconfirmed",
    }
)


def _is_closed_target_error(error: BaseException) -> bool:
    """Recognize Playwright's page/context/browser teardown error lazily.

    Playwright is an optional worker dependency, so importing its exception
    classes at module import time would make the control-plane-only install
    fail.  The class name and canonical error copy are stable across the sync
    and async Playwright APIs.
    """

    if type(error).__name__ == "TargetClosedError":
        return True
    message = str(error).casefold()
    return any(
        marker in message
        for marker in (
            "target page, context or browser has been closed",
            "target page, context or browser is closed",
        )
    )


class ManagedBrowserError(RuntimeError):
    """Sanitized error whose message can be returned to the owning user."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BrowserbaseWorkerClient(Protocol):
    def create_session_for_worker(
        self,
        context_id: str,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        user_metadata: Mapping[str, str] | None = None,
    ) -> TrustedBrowserSession: ...

    def create_ephemeral_session_for_worker(
        self,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        user_metadata: Mapping[str, str] | None = None,
    ) -> TrustedBrowserSession: ...

    def get_session_live_view(self, session_id: str) -> dict[str, str]: ...

    def release_session(self, session_id: str) -> dict[str, Any]: ...


class TenantRepository(Protocol):
    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any: ...

    async def download_object(self, bucket: str, path: str) -> bytes: ...


class JobView(Protocol):
    id: str
    user_id: str
    kind: str
    provider: str | None
    application_id: str | None


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    id: str
    revision: int
    schema_hash: str
    answers: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ResolvedBrowserTask:
    job_id: str
    user_id: str
    application_id: str
    provider: str
    phase: ExecutionPhase
    target_url: str
    context_id: str | None = field(repr=False)
    context_credential_source: Literal["platform", "user"] | None = field(
        default=None, repr=False
    )
    context_credential_generation: int | None = field(default=None, repr=False)
    context_credential_epoch: int | None = field(default=None, repr=False)
    context_project_fingerprint: str | None = field(default=None, repr=False)
    approval: ApprovalSnapshot | None = field(default=None, repr=False)
    resume_path: str | None = field(default=None, repr=False)
    resume_size_bytes: int | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class BrowserExecution:
    result: ProviderResult
    review_session_id: str | None = None
    live_view_url: str | None = field(default=None, repr=False)

    def details(self) -> dict[str, Any]:
        details = self.result.details()
        if self.result.missing_required:
            # ``missing_required`` is retained for compatibility; ``missing_fields``
            # is the actionable UI contract used by form-review surfaces.
            details["missing_fields"] = list(self.result.missing_required)
        if self.review_session_id is not None:
            details["review_session_id"] = self.review_session_id
        if self.live_view_url is not None:
            details["live_view_url"] = self.live_view_url
        return details


def _first_row(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return value[0]
    return None


def _browserbase_project_fingerprint(project_id: str) -> str:
    """Mirror the control-plane's domain-separated Browserbase binding."""

    return hashlib.sha256(
        b"autoapply.browserbase.project.v1\x00" + project_id.encode("utf-8")
    ).hexdigest()


def _uuid(value: Any, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ManagedBrowserError(
            "application_bundle_invalid",
            f"The managed application is missing a valid {label}.",
        ) from exc


def _phase(kind: str) -> ExecutionPhase:
    try:
        return {
            "application_scan": "scan",
            "application_prefill": "prefill",
            "application_submit": "submit",
        }[kind]
    except KeyError as exc:
        raise ManagedBrowserError(
            "unsupported_job_kind",
            "This worker does not support the requested managed-browser operation.",
        ) from exc


def _approval_record(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = bundle.get("form_revision")
    return nested if isinstance(nested, Mapping) else bundle


def _parse_approval(bundle: Mapping[str, Any], phase: ExecutionPhase) -> ApprovalSnapshot | None:
    if phase == "scan":
        return None
    record = _approval_record(bundle)
    revision_id = record.get("id") or record.get("form_revision_id") or bundle.get(
        "form_revision_id"
    )
    revision = record.get("revision") or record.get("form_revision") or bundle.get(
        "form_revision"
    )
    schema_hash = (
        record.get("schema_hash")
        or record.get("approved_schema_hash")
        or bundle.get("schema_hash")
    )
    if "answers" in record:
        answers = record.get("answers")
    elif "approved_answers" in record:
        answers = record.get("approved_answers")
    else:
        answers = bundle.get("approved_answers")
    approved = bool(
        record.get("approved_at")
        or record.get("status") == "approved"
        or record.get("approved") is True
    )
    if (
        not approved
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(schema_hash, str)
        or not _HASH.fullmatch(schema_hash)
        or not isinstance(answers, Mapping)
    ):
        raise ManagedBrowserError(
            "form_approval_required",
            "Review and approve the current application form answers before continuing.",
        )
    return ApprovalSnapshot(
        id=_uuid(revision_id, "form revision"),
        revision=revision,
        schema_hash=schema_hash,
        answers=dict(answers),
    )


def _resume_record(bundle: Mapping[str, Any]) -> Mapping[str, Any] | None:
    nested = bundle.get("resume")
    if isinstance(nested, Mapping):
        return nested
    if bundle.get("resume_storage_path") is None:
        return None
    return {
        "storage_path": bundle.get("resume_storage_path"),
        "size_bytes": bundle.get("resume_size_bytes"),
        "mime_type": bundle.get("resume_mime_type", "application/pdf"),
    }


class SupabaseTenantResources:
    """Resolve only the bundle attached to the current queue lease and tenant."""

    def __init__(
        self,
        repository: TenantRepository,
        cipher: TokenCipher,
        *,
        platform_browserbase_api_key: str = "",
        platform_browserbase_project_id: str = "",
        resolve_browserbase_byok: bool = False,
    ) -> None:
        self.repository = repository
        self.cipher = cipher
        self.platform_browserbase_api_key = platform_browserbase_api_key
        self.platform_browserbase_project_id = platform_browserbase_project_id
        self.resolve_browserbase_byok = resolve_browserbase_byok

    async def browserbase_for_job(
        self, job: JobView, worker_id: str
    ) -> BrowserbaseWorkerClient | None:
        """Resolve Browserbase only through the current tenant's active lease.

        A present tenant row always wins and fails closed if it is unverified or
        unreadable.  This prevents a context created in one Browserbase project
        from ever being reused against the platform account after key rotation.
        """

        if not self.resolve_browserbase_byok:
            return None
        row = _first_row(
            await self.repository.rpc(
                "get_application_job_browserbase_credential",
                {"job_id": job.id, "worker_id": worker_id},
            )
        )
        if row is None:
            if self.platform_browserbase_api_key and self.platform_browserbase_project_id:
                return BrowserbaseClient(
                    self.platform_browserbase_api_key,
                    self.platform_browserbase_project_id,
                )
            raise ManagedBrowserError(
                "browserbase_credential_required",
                "Add and validate a Browserbase API key and project ID before running browser work.",
            )
        if (
            row.get("credential_source") != "user"
            or str(row.get("user_id")) != str(job.user_id)
        ):
            raise ManagedBrowserError(
                "browserbase_credential_mismatch",
                "The Browserbase credential no longer matches this queue job.",
            )
        if row.get("verification_status") != "verified":
            raise ManagedBrowserError(
                "browserbase_credential_reconfiguration_required",
                "Validate or replace the saved Browserbase credential before running browser work.",
            )
        generation = row.get("generation")
        epoch = row.get("epoch")
        binding_fingerprint = row.get("binding_fingerprint")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch < 1
            or generation != epoch
            or not isinstance(binding_fingerprint, str)
            or not _HASH.fullmatch(binding_fingerprint)
        ):
            raise ManagedBrowserError(
                "browserbase_credential_reconfiguration_required",
                "Re-save the Browserbase credential before running browser work.",
            )
        try:
            plaintext = self.cipher.decrypt(row["credential_ciphertext"])
            envelope = json.loads(plaintext)
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"version", "provider", "api_key", "project_id"}
                or envelope.get("version") != 1
                or envelope.get("provider") != "browserbase"
            ):
                raise ValueError("invalid Browserbase credential envelope")
            api_key = envelope.get("api_key")
            project_id = envelope.get("project_id")
            if (
                not isinstance(api_key, str)
                or not 8 <= len(api_key) <= 512
                or not api_key.isascii()
                or any(character.isspace() or ord(character) < 33 for character in api_key)
                or not isinstance(project_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,254}", project_id)
            ):
                raise ValueError("invalid Browserbase credential values")
            expected_fingerprint = _browserbase_project_fingerprint(project_id)
            if not secrets.compare_digest(
                binding_fingerprint, expected_fingerprint
            ):
                raise ValueError("Browserbase project binding mismatch")
            return BrowserbaseClient(api_key, project_id)
        except Exception as exc:
            raise ManagedBrowserError(
                "browserbase_credential_reconfiguration_required",
                "Re-save the Browserbase credential before running browser work.",
            ) from exc

    async def resolve(self, job: JobView, worker_id: str) -> ResolvedBrowserTask:
        row = _first_row(
            await self.repository.rpc(
                "get_application_job_bundle",
                {"job_id": job.id, "worker_id": worker_id},
            )
        )
        if row is None:
            raise ManagedBrowserError(
                "application_bundle_unavailable",
                "The managed application is no longer available to this worker.",
            )

        user_id = _uuid(row.get("user_id"), "user")
        application_id = _uuid(row.get("application_id"), "application")
        if user_id != job.user_id or application_id != job.application_id:
            raise ManagedBrowserError(
                "application_bundle_mismatch",
                "The managed application no longer matches this queue job.",
            )
        provider = row.get("provider")
        if not isinstance(provider, str) or provider != job.provider:
            raise ManagedBrowserError(
                "application_bundle_mismatch",
                "The managed provider no longer matches this queue job.",
            )
        target_url = row.get("target_url")
        if not isinstance(target_url, str) or not target_url:
            raise ManagedBrowserError(
                "application_url_missing",
                "The application does not have a provider URL.",
            )
        ciphertext = row.get("browser_context_id_ciphertext")
        context_credential_source: Literal["platform", "user"] | None = None
        context_credential_generation: int | None = None
        context_credential_epoch: int | None = None
        context_project_fingerprint: str | None = None
        if not isinstance(ciphertext, str) or not ciphertext:
            if provider in _PUBLIC_SESSION_PROVIDERS:
                context_id = None
            else:
                raise ManagedBrowserError(
                    "provider_connection_required",
                    "Connect this provider before running a managed application.",
                )
        else:
            binding = _first_row(
                await self.repository.rpc(
                    "get_application_job_browser_context_binding",
                    {"job_id": job.id, "worker_id": worker_id},
                )
            )
            if binding is None or binding.get("browser_context_id_ciphertext") != ciphertext:
                raise ManagedBrowserError(
                    "provider_connection_invalid",
                    "Reconnect this provider before running a managed application.",
                )
            source = binding.get("credential_source")
            generation = binding.get("credential_generation")
            epoch = binding.get("credential_epoch")
            fingerprint = binding.get("project_fingerprint")
            valid_binding = (
                source in {"platform", "user"}
                and isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 0
                and isinstance(fingerprint, str)
                and _HASH.fullmatch(fingerprint) is not None
                and (
                    (source == "platform" and generation is None)
                    or (
                        source == "user"
                        and isinstance(generation, int)
                        and not isinstance(generation, bool)
                        and generation >= 1
                        and generation == epoch
                    )
                )
            )
            if not valid_binding:
                raise ManagedBrowserError(
                    "provider_connection_invalid",
                    "Reconnect this provider before running a managed application.",
                )
            context_credential_source = source
            context_credential_generation = generation
            context_credential_epoch = epoch
            context_project_fingerprint = fingerprint
            try:
                context_id = self.cipher.decrypt(ciphertext)
            except Exception as exc:
                raise ManagedBrowserError(
                    "provider_connection_invalid",
                    "Reconnect this provider before running a managed application.",
                ) from exc

        phase = _phase(job.kind)
        approval = _parse_approval(row, phase)
        resume = _resume_record(row)
        resume_path: str | None = None
        resume_size_bytes: int | None = None
        if resume is not None:
            raw_path = resume.get("storage_path")
            size = resume.get("size_bytes")
            if (
                not isinstance(raw_path, str)
                or not raw_path.startswith(f"{user_id}/")
                or raw_path.count("/") != 1
                or not raw_path.lower().endswith(".pdf")
                or resume.get("mime_type") != "application/pdf"
                or not isinstance(size, int)
                or isinstance(size, bool)
                or not 1 <= size <= _MAX_RESUME_BYTES
            ):
                raise ManagedBrowserError(
                    "resume_invalid",
                    "Select a valid active PDF résumé before continuing.",
                )
            resume_path = raw_path
            resume_size_bytes = size

        return ResolvedBrowserTask(
            job_id=job.id,
            user_id=user_id,
            application_id=application_id,
            provider=provider,
            phase=phase,
            target_url=target_url,
            context_id=context_id,
            context_credential_source=context_credential_source,
            context_credential_generation=context_credential_generation,
            context_credential_epoch=context_credential_epoch,
            context_project_fingerprint=context_project_fingerprint,
            approval=approval,
            resume_path=resume_path,
            resume_size_bytes=resume_size_bytes,
        )

    @asynccontextmanager
    async def materialize_resume(
        self, task: ResolvedBrowserTask
    ) -> AsyncIterator[str | None]:
        if task.resume_path is None:
            yield None
            return
        data = await self.repository.download_object("resumes", task.resume_path)
        if (
            not isinstance(data, bytes)
            or not 1 <= len(data) <= _MAX_RESUME_BYTES
            or len(data) != task.resume_size_bytes
            or not data.startswith(b"%PDF-")
        ):
            raise ManagedBrowserError(
                "resume_invalid",
                "The active résumé is not a valid PDF.",
            )
        with tempfile.TemporaryDirectory(prefix="autoapply-resume-") as directory:
            path = Path(directory) / "resume.pdf"
            path.write_bytes(data)
            yield str(path)

    async def store_scan(
        self,
        task: ResolvedBrowserTask,
        worker_id: str,
        result: ProviderResult,
    ) -> bool:
        if result.schema is None or result.code != "application_form_scanned":
            return False
        saved = await self.repository.rpc(
            "store_application_form_scan",
            {
                "job_id": task.job_id,
                "worker_id": worker_id,
                "provider": task.provider,
                "form_url": result.form_url,
                "schema_hash": result.schema.schema_hash,
                "question_schema": result.schema.public_fields,
                "answers": {},
            },
        )
        return _first_row(saved) is not None

    async def progress(
        self, task: ResolvedBrowserTask, worker_id: str, stage: str
    ) -> bool:
        updated = await self.repository.rpc(
            "update_application_job_progress",
            {
                "job_id": task.job_id,
                "worker_id": worker_id,
                "progress": {"stage": stage},
            },
        )
        return _first_row(updated) is not None

    async def record_submission(
        self,
        task: ResolvedBrowserTask,
        worker_id: str,
        result: ProviderResult,
    ) -> bool:
        """Commit a clear provider confirmation through the current queue lease."""

        if (
            task.phase != "submit"
            or result.status != "succeeded"
            or result.code != "application_submitted"
            or result.submission_state != "confirmed"
        ):
            return False
        recorded = await self.repository.rpc(
            "record_application_form_submission",
            {
                "job_id": task.job_id,
                "worker_id": worker_id,
                "provider_submission_id": None,
                "result": {
                    "code": result.code,
                    "provider": result.provider,
                    "form_url": result.form_url,
                    "schema_hash": result.schema.schema_hash if result.schema else None,
                    "filled_count": result.filled_count,
                    "submission_state": result.submission_state,
                },
            },
        )
        return _first_row(recorded) is not None


class BrowserRuntime:
    """Connect Playwright to one short-lived Browserbase session."""

    def __init__(
        self,
        browserbase: BrowserbaseWorkerClient | None,
        *,
        playwright_factory: Any | None = None,
        navigation_timeout_ms: int = 30_000,
    ) -> None:
        self.browserbase = browserbase
        self.playwright_factory = playwright_factory or self._default_playwright_factory
        self.navigation_timeout_ms = navigation_timeout_ms

    @staticmethod
    def _default_playwright_factory() -> Any:
        from playwright.async_api import async_playwright

        return async_playwright()

    @staticmethod
    async def _install_navigation_guard(
        page: Any, adapter: ProviderAdapter
    ) -> dict[str, str | None]:
        blocked: dict[str, str | None] = {"url": None}

        async def guard(route: Any) -> None:
            request = route.request
            try:
                top_level = request.is_navigation_request() and request.frame == page.main_frame
            except Exception:
                top_level = False
            try:
                if top_level and not adapter.allows_url(request.url):
                    blocked["url"] = request.url
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()
            except Exception as exc:
                # A page can close while subresource routes are still resolving.
                # Suppress that one teardown race, but preserve genuine routing
                # failures so the task fails closed instead of silently losing
                # its provider-host guard.
                if _is_closed_target_error(exc):
                    return
                raise

        await page.route("**/*", guard)
        return blocked

    @staticmethod
    async def _wait_for_provider_form_render(
        page: Any, adapter: ProviderAdapter
    ) -> None:
        """Give client-rendered Google Forms a bounded chance to expose controls.

        ``domcontentloaded`` can fire while the Google Forms shell still contains
        no form controls.  Scanning immediately at that point produces a false
        ``application_form_not_found`` terminal result.  Other adapters retain
        their existing timing; a real closed, inaccessible, or unsupported Google
        Form still falls through to the normal structural checks after eight
        seconds.
        """

        if adapter.provider != "google_forms":
            return
        wait_for_selector = getattr(page, "wait_for_selector", None)
        if not callable(wait_for_selector):
            return
        try:
            await wait_for_selector(
                _GOOGLE_FORM_CONTROL_SELECTOR,
                state="visible",
                timeout=_GOOGLE_FORM_RENDER_TIMEOUT_MS,
            )
        except Exception:
            # Readiness is advisory. The provider adapter below remains the
            # authority for login, checkpoint, ambiguity, and form-not-found
            # outcomes, and returns only sanitized terminal messages.
            return

    @staticmethod
    def _attention(
        task: ResolvedBrowserTask,
        code: str,
        message: str,
        *,
        schema: FormSchema | None = None,
        filled_count: int = 0,
        missing_required: tuple[str, ...] = (),
        submission_state: Literal["not_attempted", "confirmed", "uncertain"] = "not_attempted",
        current_url: str | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            status="needs_attention",
            code=code,
            message=message,
            provider=task.provider,
            phase=task.phase,
            form_url=safe_form_url(current_url or task.target_url),
            schema=schema,
            filled_count=filled_count,
            missing_required=missing_required,
            submission_state=submission_state,
        )

    async def _run_page(
        self,
        page: Any,
        adapter: ProviderAdapter,
        task: ResolvedBrowserTask,
        resume_path: str | None,
        before_submit: Any | None = None,
    ) -> ProviderResult:
        if task.provider == "yc" and not is_exact_yc_job_url(task.target_url):
            return self._attention(
                task,
                "provider_url_forbidden",
                "YC automation requires one exact public job-detail URL.",
            )
        if not adapter.allows_url(task.target_url):
            return self._attention(
                task,
                "provider_url_forbidden",
                "The application URL is outside this provider's approved hosts.",
            )
        blocked_navigation = await self._install_navigation_guard(page, adapter)
        try:
            await page.goto(
                task.target_url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
        except Exception:
            blocked_url = blocked_navigation["url"]
            if blocked_url and adapter.is_login_redirect(blocked_url):
                return self._attention(
                    task,
                    "provider_login_required",
                    "Sign in to this provider connection before continuing.",
                )
            if not adapter.allows_url(getattr(page, "url", "")):
                return self._attention(
                    task,
                    "provider_redirect_blocked",
                    "The provider redirected outside its approved hosts.",
                )
            raise

        current_url = getattr(page, "url", "")
        if adapter.is_login_redirect(current_url):
            return self._attention(
                task,
                "provider_login_required",
                "Sign in to this provider connection before continuing.",
                current_url=task.target_url,
            )
        if not adapter.allows_url(current_url):
            return self._attention(
                task,
                "provider_redirect_blocked",
                "The provider redirected outside its approved hosts.",
            )
        if adapter.login_required(current_url):
            return self._attention(
                task,
                "provider_login_required",
                "Sign in to this provider connection before continuing.",
                current_url=current_url,
            )
        if await checkpoint_present(
            page,
            include_passive_widgets=task.phase == "submit",
        ):
            return self._attention(
                task,
                "security_checkpoint",
                "A CAPTCHA, MFA prompt, or security checkpoint requires your attention.",
                current_url=current_url,
            )

        if (
            task.provider == "google_forms"
            and urlsplit(current_url).path.rstrip("/").endswith("/closedform")
        ):
            return self._attention(
                task,
                "application_form_closed",
                "This Google Form is no longer accepting responses.",
                current_url=current_url,
            )

        await self._wait_for_provider_form_render(page, adapter)

        preparation = await adapter.open_application(
            page,
            include_passive_checkpoints=task.phase == "submit",
        )
        blocked_url = blocked_navigation["url"]
        if blocked_url is not None:
            if adapter.is_login_redirect(blocked_url):
                return self._attention(
                    task,
                    "provider_login_required",
                    "Sign in to this provider connection before continuing.",
                    current_url=current_url,
                )
            return self._attention(
                task,
                "provider_redirect_blocked",
                "The provider application entry point left its approved hosts.",
                current_url=current_url,
            )
        current_url = getattr(page, "url", "")
        if not preparation.ready:
            return self._attention(
                task,
                preparation.code,
                preparation.message,
                current_url=current_url,
            )
        form_root = preparation.root
        if form_root is None:  # Defensive guard for an invalid adapter result.
            return self._attention(
                task,
                "application_form_not_found",
                "No unambiguous provider application form was found on this page.",
                current_url=current_url,
            )
        canonical_target = (
            canonical_yc_form_target(current_url)
            if task.provider == "yc"
            else canonical_form_target(task.provider, current_url)
        )
        if not canonical_target:
            return self._attention(
                task,
                "provider_url_forbidden",
                "The provider form identity could not be preserved safely.",
                current_url=current_url,
            )
        schema = bind_schema_to_target(
            await scan_form(form_root, provider=task.provider), canonical_target
        )
        if not schema.fields:
            return self._attention(
                task,
                "application_form_not_found",
                "No supported application fields were found on this page.",
                schema=schema,
                current_url=current_url,
            )
        if task.provider == "yc":
            yc_issue = yc_schema_issue(schema)
            if yc_issue is not None:
                code, message = yc_issue
                return self._attention(
                    task,
                    code,
                    message,
                    schema=schema,
                    current_url=current_url,
                )
        upload_issue = await resume_upload_guard_issue(form_root, schema)
        if upload_issue is not None:
            code, message = upload_issue
            return self._attention(
                task,
                code,
                message,
                schema=schema,
                current_url=current_url,
            )
        if (
            task.provider == "google_forms"
            and task.phase != "scan"
            and task.context_id is None
            and any(field.is_resume_upload for field in schema.fields)
        ):
            return self._attention(
                task,
                "provider_login_required",
                "Connect an isolated Google browser session before filling this form's signed-in résumé upload.",
                schema=schema,
                current_url=current_url,
            )
        if task.phase == "scan":
            return ProviderResult(
                status="succeeded",
                code="application_form_scanned",
                message="The application form is ready for answer review.",
                provider=task.provider,
                phase="scan",
                form_url=canonical_target,
                schema=schema,
            )

        approval = task.approval
        if approval is None:
            return self._attention(
                task,
                "form_approval_required",
                "Review and approve the current application form answers before continuing.",
                schema=schema,
                current_url=current_url,
            )
        if approval.schema_hash != schema.schema_hash:
            return self._attention(
                task,
                "form_schema_changed",
                "The provider changed this form. Review the new questions before continuing.",
                schema=schema,
                current_url=current_url,
            )

        filled_count, missing_required = await fill_approved(
            page,
            schema,
            approval.answers,
            resume_path=resume_path,
            root=form_root,
        )
        current_url = getattr(page, "url", "")
        if not adapter.allows_url(current_url):
            return self._attention(
                task,
                "provider_redirect_blocked",
                "The provider navigated outside its approved hosts.",
                schema=schema,
                filled_count=filled_count,
                missing_required=missing_required,
            )
        if await checkpoint_present(
            page,
            include_passive_widgets=task.phase == "submit",
        ):
            return self._attention(
                task,
                "security_checkpoint",
                "A CAPTCHA, MFA prompt, or security checkpoint requires your attention.",
                schema=schema,
                filled_count=filled_count,
                missing_required=missing_required,
                current_url=current_url,
            )
        if missing_required:
            return self._attention(
                task,
                "required_answers_missing",
                "Review the required or provider-prefilled fields that could not be set from approved answers.",
                schema=schema,
                filled_count=filled_count,
                missing_required=missing_required,
                current_url=current_url,
            )
        if task.phase == "prefill":
            return self._attention(
                task,
                "review_required",
                "The approved answers are filled. Review the live form before submission.",
                schema=schema,
                filled_count=filled_count,
                current_url=current_url,
            )

        if adapter.submission_unsupported_message:
            return self._attention(
                task,
                "provider_submission_unsupported",
                adapter.submission_unsupported_message,
                schema=schema,
                filled_count=filled_count,
                current_url=current_url,
            )
        submit = await adapter.find_submit(page, form_root)
        if submit is None:
            return self._attention(
                task,
                "final_action_ambiguous",
                "The final submit control could not be identified safely.",
                schema=schema,
                filled_count=filled_count,
                current_url=current_url,
            )
        if before_submit is not None:
            lease_result = before_submit()
            if inspect.isawaitable(lease_result):
                lease_result = await lease_result
            if lease_result is False:
                raise ManagedBrowserError(
                    "automation_lease_lost",
                    "The application lease ended before submission; no final action was taken.",
                )
        before_submit_url = safe_form_url(current_url)
        confirmation_preexisted = await adapter.confirmed(page)
        try:
            await submit.click()
        except Exception:
            # Once a final-action click is dispatched, a timeout/transport error is
            # ambiguous: the provider may have received it.  Never bubble this into
            # the worker's retry path and risk a duplicate application.
            return self._attention(
                task,
                "submission_click_unconfirmed",
                "The final action may have been sent, but its outcome could not be observed. Verify it manually and do not resubmit automatically.",
                schema=schema,
                filled_count=filled_count,
                submission_state="uncertain",
                current_url=current_url,
            )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(1_000)
        except Exception:
            pass
        current_url = getattr(page, "url", "")
        if blocked_navigation["url"] is not None:
            return self._attention(
                task,
                "provider_redirect_blocked_after_submit",
                "Submission navigation left the provider's approved hosts; verify it manually.",
                schema=schema,
                filled_count=filled_count,
                submission_state="uncertain",
            )
        if not adapter.allows_url(current_url):
            return self._attention(
                task,
                "provider_redirect_blocked_after_submit",
                "Submission navigation left the provider's approved hosts; verify it manually.",
                schema=schema,
                filled_count=filled_count,
                submission_state="uncertain",
            )
        if await checkpoint_present(page):
            return self._attention(
                task,
                "security_checkpoint_after_submit",
                "A security checkpoint appeared after submission; verify the outcome manually.",
                schema=schema,
                filled_count=filled_count,
                submission_state="uncertain",
                current_url=current_url,
            )
        if await adapter.rejected(page):
            return self._attention(
                task,
                "provider_validation_failed",
                "The provider did not accept the response because one or more visible fields still require a valid answer. Prepare the current form again before submitting.",
                schema=schema,
                filled_count=filled_count,
                submission_state="not_attempted",
                current_url=current_url,
            )
        confirmed = await adapter.confirmed(page)
        confirmation_changed = (
            not confirmation_preexisted
            or safe_form_url(current_url) != before_submit_url
        )
        if not confirmed or not confirmation_changed:
            return self._attention(
                task,
                "submission_unconfirmed",
                "The final action was attempted, but the provider did not show a clear confirmation.",
                schema=schema,
                filled_count=filled_count,
                submission_state="uncertain",
                current_url=current_url,
            )
        return ProviderResult(
            status="succeeded",
            code="application_submitted",
            message="The provider displayed a clear application confirmation.",
            provider=task.provider,
            phase="submit",
            form_url=safe_form_url(current_url),
            schema=schema,
            filled_count=filled_count,
            submission_state="confirmed",
        )

    async def execute(
        self,
        task: ResolvedBrowserTask,
        *,
        resume_path: str | None,
        before_submit: Any | None = None,
        browserbase: BrowserbaseWorkerClient | None = None,
    ) -> BrowserExecution:
        adapter = get_adapter(task.provider, target_url=task.target_url)
        if adapter is None:
            if task.provider == "company_form":
                return BrowserExecution(
                    self._attention(
                        task,
                        "provider_url_forbidden",
                        "Custom company forms must use a public HTTPS DNS host without credentials, IP addresses, or explicit ports.",
                    )
                )
            return BrowserExecution(
                self._attention(
                    task,
                    "provider_handler_unavailable",
                    "No reviewed managed-browser adapter exists for this provider.",
                )
            )
        # Validate before requesting a metered browser session.
        if task.provider == "yc" and not is_exact_yc_job_url(task.target_url):
            return BrowserExecution(
                self._attention(
                    task,
                    "provider_url_forbidden",
                    "YC automation requires one exact public job-detail URL.",
                )
            )
        if not adapter.allows_url(task.target_url):
            return BrowserExecution(
                self._attention(
                    task,
                    "provider_url_forbidden",
                    "The application URL is outside this provider's approved hosts.",
                )
            )

        selected_browserbase = browserbase or self.browserbase
        if selected_browserbase is None:
            raise ManagedBrowserError(
                "browserbase_credential_required",
                "Add and validate a Browserbase API key and project ID before running browser work.",
            )

        keep_alive = task.phase == "prefill" or (
            task.provider in _RETAINED_SUBMIT_PROVIDERS and task.phase == "submit"
        )
        metadata = {
            "user_id": task.user_id,
            "job_id": task.job_id,
            "provider": task.provider,
        }
        if task.context_id is None:
            session = await asyncio.to_thread(
                selected_browserbase.create_ephemeral_session_for_worker,
                keep_alive=keep_alive,
                timeout_seconds=_BROWSER_SESSION_TIMEOUT_SECONDS,
                user_metadata=metadata,
            )
        else:
            session = await asyncio.to_thread(
                selected_browserbase.create_session_for_worker,
                task.context_id,
                keep_alive=keep_alive,
                timeout_seconds=_BROWSER_SESSION_TIMEOUT_SECONDS,
                user_metadata=metadata,
            )
        retain_session = False
        browser: Any | None = None
        page: Any | None = None
        final_action_execution: BrowserExecution | None = None
        try:
            try:
                async with self.playwright_factory() as playwright:
                    browser = await playwright.chromium.connect_over_cdp(
                        session.connect_url,
                        timeout=30_000,
                    )
                    contexts = browser.contexts
                    context = contexts[0] if contexts else await browser.new_context()
                    pages = context.pages
                    page = pages[0] if pages else await context.new_page()
                    result: ProviderResult | None = None
                    try:
                        result = await self._run_page(
                            page,
                            adapter,
                            task,
                            resume_path,
                            before_submit,
                        )
                        execution = BrowserExecution(result)
                        if result.submission_state in {"confirmed", "uncertain"}:
                            final_action_execution = execution

                        live_view_url: str | None = None
                        wants_live_view = (
                            task.phase == "prefill" and result.code == "review_required"
                        ) or (
                            task.provider in _RETAINED_SUBMIT_PROVIDERS
                            and task.phase == "submit"
                            and result.status == "needs_attention"
                            and result.code in _SUBMIT_LIVE_VIEW_CODES
                        )
                        if wants_live_view:
                            try:
                                live = await asyncio.to_thread(
                                    selected_browserbase.get_session_live_view, session.id
                                )
                                live_view_url = live.get("live_view_url")
                            except Exception:
                                # Live View is observability only. In particular,
                                # an outage after a Submit click must never bubble
                                # into the queue retry path and risk a duplicate.
                                live_view_url = None
                            retain_session = bool(live_view_url)
                            if retain_session:
                                execution = BrowserExecution(
                                    result,
                                    review_session_id=session.id,
                                    live_view_url=live_view_url,
                                )
                                if result.submission_state in {"confirmed", "uncertain"}:
                                    final_action_execution = execution
                                return execution
                        return execution
                    finally:
                        # Drain registered route callbacks before Playwright
                        # disconnects. ``ignoreErrors`` applies only to callbacks
                        # already in flight; unrelated unroute failures still fail
                        # the task closed unless a final click may already have run.
                        unroute_all = getattr(page, "unroute_all", None)
                        if callable(unroute_all):
                            try:
                                await unroute_all(behavior="ignoreErrors")
                            except Exception as exc:
                                final_action_may_have_happened = (
                                    result is not None
                                    and result.submission_state in {"confirmed", "uncertain"}
                                )
                                if (
                                    not _is_closed_target_error(exc)
                                    and not final_action_may_have_happened
                                ):
                                    raise
            except Exception:
                if final_action_execution is not None:
                    # Playwright teardown can fail after the provider received the
                    # click. Preserve the terminal outcome instead of asking the
                    # queue to repeat an inherently ambiguous final action.
                    return final_action_execution
                raise
        finally:
            # ``browser.close()`` explicitly terminates a Browserbase session. A
            # retained review/attention Live View instead disconnects through
            # Playwright teardown and lets keepAlive preserve the bounded session.
            if browser is not None and not retain_session:
                try:
                    await browser.close()
                except Exception:
                    pass
            if not retain_session:
                try:
                    await asyncio.to_thread(selected_browserbase.release_session, session.id)
                except Exception:
                    pass


class ManagedBrowserJobHandler:
    """Async worker handler that composes tenant resolution and browser execution."""

    def __init__(
        self,
        resources: SupabaseTenantResources,
        runtime: BrowserRuntime,
        worker_id: str,
        allowed_providers: tuple[str, ...],
        fallback: Any,
    ) -> None:
        self.resources = resources
        self.runtime = runtime
        self.worker_id = worker_id
        self.allowed_providers = frozenset(allowed_providers)
        self.fallback = fallback

    async def __call__(self, job: Any) -> Any:
        if job.kind not in MANAGED_JOB_KINDS:
            result = self.fallback(job)
            return await result if inspect.isawaitable(result) else result

        # Import here to keep worker.handlers independently testable and avoid a
        # module cycle between its legacy handler and this async extension.
        from worker.handlers import HandlerOutcome

        if job.provider not in self.allowed_providers:
            return HandlerOutcome(
                status="needs_attention",
                code="provider_handler_disabled",
                message="Managed-browser handling is disabled for this provider.",
                provider=job.provider,
                connection_required=False,
            )

        try:
            task = await self.resources.resolve(job, self.worker_id)
            credential_resolver = getattr(self.resources, "browserbase_for_job", None)
            selected_browserbase = (
                await credential_resolver(job, self.worker_id)
                if callable(credential_resolver)
                else None
            )
            if task.context_id is not None:
                selected_project_id = getattr(selected_browserbase, "project_id", None)
                if (
                    not isinstance(selected_project_id, str)
                    or not isinstance(task.context_project_fingerprint, str)
                    or not secrets.compare_digest(
                        task.context_project_fingerprint,
                        _browserbase_project_fingerprint(selected_project_id),
                    )
                ):
                    raise ManagedBrowserError(
                        "browserbase_credential_reconfiguration_required",
                        "Reconnect this provider with the active Browserbase project before running browser work.",
                    )
            if not await self.resources.progress(task, self.worker_id, "opening_provider"):
                raise ManagedBrowserError(
                    "automation_lease_lost",
                    "The application lease ended before browser work began.",
                )
            if task.phase == "scan":
                execution = await self.runtime.execute(
                    task,
                    resume_path=None,
                    browserbase=selected_browserbase,
                )
            else:
                async def before_submit() -> bool:
                    return await self.resources.progress(
                        task, self.worker_id, "submitting"
                    )

                async with self.resources.materialize_resume(task) as resume_path:
                    execution = await self.runtime.execute(
                        task,
                        resume_path=resume_path,
                        before_submit=before_submit if task.phase == "submit" else None,
                        browserbase=selected_browserbase,
                    )
            if task.phase == "scan":
                stored = await self.resources.store_scan(
                    task, self.worker_id, execution.result
                )
                if execution.result.code == "application_form_scanned" and not stored:
                    raise ManagedBrowserError(
                        "automation_lease_lost",
                        "The form scan could not be attached to the current application lease.",
                    )
            if (
                task.phase == "submit"
                and execution.result.status == "succeeded"
                and execution.result.code == "application_submitted"
            ):
                try:
                    recorded = await self.resources.record_submission(
                        task, self.worker_id, execution.result
                    )
                except Exception:
                    recorded = False
                if not recorded:
                    execution = BrowserExecution(
                        ProviderResult(
                            status="needs_attention",
                            code="submission_record_unconfirmed",
                            message=(
                                "The provider confirmed submission, but the local application "
                                "record could not be confirmed. Do not resubmit automatically."
                            ),
                            provider=execution.result.provider,
                            phase="submit",
                            form_url=execution.result.form_url,
                            schema=execution.result.schema,
                            filled_count=execution.result.filled_count,
                            submission_state="confirmed",
                        )
                    )
            # Progress is informational.  Once a final-action click may have happened,
            # a telemetry failure must never turn the job into a retryable submission.
            try:
                await self.resources.progress(task, self.worker_id, execution.result.code)
            except Exception:
                pass
        except ManagedBrowserError as exc:
            if exc.retryable:
                raise
            return HandlerOutcome(
                status="needs_attention",
                code=exc.code,
                message=str(exc),
                provider=job.provider,
                connection_required=exc.code.startswith("provider_connection"),
            )

        return HandlerOutcome(
            status=execution.result.status,
            code=execution.result.code,
            message=execution.result.message,
            provider=execution.result.provider,
            connection_required=(
                True
                if execution.result.code in {"provider_login_required", "security_checkpoint"}
                else None
            ),
            details=execution.details(),
        )


def build_managed_job_handler(
    repository: TenantRepository,
    *,
    worker_id: str,
    browserbase_api_key: str,
    browserbase_project_id: str,
    token_encryption_key: str,
    allowed_providers: tuple[str, ...],
    fallback: Any,
) -> ManagedBrowserJobHandler:
    browserbase = (
        BrowserbaseClient(browserbase_api_key, browserbase_project_id)
        if browserbase_api_key and browserbase_project_id
        else None
    )
    resources = SupabaseTenantResources(
        repository,
        TokenCipher(token_encryption_key),
        platform_browserbase_api_key=browserbase_api_key,
        platform_browserbase_project_id=browserbase_project_id,
        resolve_browserbase_byok=True,
    )
    return ManagedBrowserJobHandler(
        resources,
        BrowserRuntime(browserbase),
        worker_id,
        allowed_providers,
        fallback,
    )


__all__ = [
    "BrowserExecution",
    "BrowserRuntime",
    "MANAGED_JOB_KINDS",
    "ManagedBrowserError",
    "ManagedBrowserJobHandler",
    "ResolvedBrowserTask",
    "SupabaseTenantResources",
    "build_managed_job_handler",
]
