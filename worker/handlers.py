"""Safety-first handlers for the initial durable worker.

These handlers do not submit an application or attempt to defeat an account
checkpoint.  They either verify a deployment-level capability or tell the web
application which human action is still required.  Provider-specific submission
handlers belong in separately reviewed modules once the provider permits that use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping
from uuid import UUID

from app.saas.providers import get_provider


OutcomeStatus = Literal["succeeded", "needs_attention"]
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")


def _uuid_text(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"claimed job is missing a valid {field_name}")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError(f"claimed job is missing a valid {field_name}") from exc


@dataclass(frozen=True, slots=True)
class AutomationJob:
    """The bounded subset of a claimed database row needed by a handler.

    ``payload`` is excluded from the generated representation so an accidental
    exception or log statement cannot print resume content, answers, or URLs.
    """

    id: str
    user_id: str
    kind: str
    provider: str | None
    attempts: int
    payload: Mapping[str, Any] = field(repr=False)
    application_id: str | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "AutomationJob":
        job_id = record.get("id")
        user_id = record.get("user_id")
        kind = record.get("kind")
        payload = record.get("payload", {})
        provider = record.get("provider")
        application_id = record.get("application_id")

        safe_job_id = _uuid_text(job_id, "id")
        safe_user_id = _uuid_text(user_id, "user id")
        safe_application_id = _uuid_text(application_id, "application id", optional=True)
        assert safe_job_id is not None and safe_user_id is not None
        if not isinstance(kind, str) or not _SAFE_NAME.fullmatch(kind.strip().lower()):
            raise ValueError("claimed job is missing a kind")
        if not isinstance(payload, Mapping):
            raise ValueError("claimed job has an invalid payload")
        if provider is not None and (
            not isinstance(provider, str)
            or not _SAFE_NAME.fullmatch(provider.strip().lower())
        ):
            raise ValueError("claimed job has an invalid provider")

        try:
            attempts = max(1, int(record.get("attempts", 1)))
        except (TypeError, ValueError) as exc:
            raise ValueError("claimed job has an invalid attempt count") from exc

        return cls(
            id=safe_job_id,
            user_id=safe_user_id,
            kind=kind.strip().lower(),
            provider=provider.strip().lower() if provider and provider.strip() else None,
            attempts=attempts,
            payload=payload,
            application_id=safe_application_id,
        )


@dataclass(frozen=True, slots=True)
class HandlerOutcome:
    """A small, redacted result safe to persist through the completion RPC."""

    status: OutcomeStatus
    code: str
    message: str
    provider: str | None = None
    connection_required: bool | None = None
    details: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def as_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            # The completion RPC maps this value to the matching terminal status.
            "outcome": self.status,
            "code": self.code,
            "message": self.message,
        }
        if self.provider is not None:
            result["provider"] = self.provider
        if self.connection_required is not None:
            result["connection_required"] = self.connection_required
        # Managed-browser details are constructed by trusted provider adapters and
        # contain form structure/counts only, never approved answer values or CDP URLs.
        for key, value in self.details.items():
            if key not in result:
                result[key] = value
        return result


def _known_provider(provider: str | None) -> str | None:
    if not provider:
        return None
    return provider if get_provider(provider) is not None else None


def _checkpoint_present(payload: Mapping[str, Any]) -> bool:
    """Recognize only control flags; never copy payload values into output/logs."""

    for key in (
        "captcha_detected",
        "mfa_required",
        "security_checkpoint",
        "checkpoint_required",
    ):
        if payload.get(key) is True:
            return True

    state = payload.get("checkpoint")
    return isinstance(state, str) and state.strip().lower() in {
        "captcha",
        "mfa",
        "security",
        "security_checkpoint",
        "verification",
    }


def _needs_attention(
    code: str,
    message: str,
    provider: str | None,
    *,
    connection_required: bool | None = None,
) -> HandlerOutcome:
    return HandlerOutcome(
        status="needs_attention",
        code=code,
        message=message,
        provider=_known_provider(provider),
        connection_required=connection_required,
    )


def _manual_handoff(job: AutomationJob) -> HandlerOutcome:
    return _needs_attention(
        "manual_handoff_required",
        "Open the provider from the workspace, review the application, and submit it manually.",
        job.provider,
        connection_required=False,
    )


def _ats_prepare(job: AutomationJob) -> HandlerOutcome:
    # This launch handler establishes the queue boundary only.  It intentionally
    # does not describe an application as prepared or submitted.
    return _needs_attention(
        "provider_handler_not_enabled",
        "A reviewed provider-specific handler is not enabled; continue with manual review.",
        job.provider,
    )


def _connection_check(job: AutomationJob) -> HandlerOutcome:
    capability = get_provider(job.provider or "")
    if capability is None:
        return _needs_attention(
            "unknown_provider",
            "Choose a supported provider before checking a connection.",
            None,
        )

    if capability["mode"] == "manual_only":
        return HandlerOutcome(
            status="succeeded",
            code="connection_not_required",
            message=(
                "This provider uses a manual handoff and does not require a saved connection."
            ),
            provider=capability["id"],
            connection_required=False,
        )

    if capability["mode"] == "managed_browser" and capability["available"]:
        return HandlerOutcome(
            status="succeeded",
            code="deployment_capability_ready",
            message=(
                "Managed-browser infrastructure is configured; "
                "user login and review are still required."
            ),
            provider=capability["id"],
            connection_required=True,
        )

    if capability["mode"] == "oauth":
        return _needs_attention(
            "oauth_check_required",
            "Verify this user's OAuth connection from the authenticated web application.",
            capability["id"],
            connection_required=True,
        )

    return _needs_attention(
        "connection_unavailable",
        capability["reason"],
        capability["id"],
        connection_required=capability["can_connect"],
    )


def handle_job(job: AutomationJob) -> HandlerOutcome:
    """Execute one bounded launch handler without external submission side effects."""

    if job.provider == "linkedin":
        return _needs_attention(
            "linkedin_partner_required",
            "LinkedIn application automation is unavailable; open LinkedIn and apply manually.",
            "linkedin",
            connection_required=False,
        )

    if _checkpoint_present(job.payload):
        return _needs_attention(
            "security_checkpoint",
            "A CAPTCHA, MFA, or security checkpoint requires the user to continue manually.",
            job.provider,
            connection_required=True,
        )

    handler = {
        "manual_handoff": _manual_handoff,
        "ats_prepare": _ats_prepare,
        "connection_check": _connection_check,
    }.get(job.kind)
    if handler is None:
        return _needs_attention(
            "unsupported_job_kind",
            "This worker does not have a reviewed handler for the requested operation.",
            job.provider,
        )
    return handler(job)


SUPPORTED_JOB_KINDS: tuple[str, ...] = (
    "manual_handoff",
    "ats_prepare",
    "connection_check",
    "application_scan",
    "application_prefill",
    "application_submit",
    "discover_public_feeds",
    "discover_linkedin_guest",
    "discover_public_ats",
)


__all__ = [
    "AutomationJob",
    "HandlerOutcome",
    "OutcomeStatus",
    "SUPPORTED_JOB_KINDS",
    "handle_job",
]
