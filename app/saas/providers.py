"""Public provider capability registry.

Managed-browser entries are inert until both Browserbase and the deployment
allowlist enable them. LinkedIn remains discovery/manual-handoff only: an
allowlist value can never turn its guest search into Easy Apply automation.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Literal, TypedDict


ProviderMode = Literal["oauth", "managed_browser", "partner_required", "manual_only"]


class ProviderCapability(TypedDict):
    id: str
    label: str
    mode: ProviderMode
    available: bool
    can_connect: bool
    can_auto_apply: bool
    can_scan: bool
    can_prefill: bool
    requires_review: bool
    connection_required: bool
    reason: str


_REGISTRY: tuple[dict[str, str], ...] = (
    {
        "id": "gmail",
        "label": "Gmail",
        "mode": "oauth",
        "reason": "Connect Gmail with Google OAuth to send reviewed application emails.",
    },
    {
        "id": "linkedin",
        "label": "LinkedIn",
        "mode": "partner_required",
        "reason": "Bounded public job discovery is available; LinkedIn Easy Apply is not connected.",
    },
    {
        "id": "google_forms",
        "label": "Google Forms",
        "mode": "managed_browser",
        "reason": "Scan and prefill application forms in an isolated browser after review.",
    },
    {
        "id": "greenhouse",
        "label": "Greenhouse",
        "mode": "managed_browser",
        "reason": "Scan, review, prefill, and submit supported Greenhouse applications.",
    },
    {
        "id": "lever",
        "label": "Lever",
        "mode": "managed_browser",
        "reason": "Scan, review, prefill, and submit supported Lever applications.",
    },
    {
        "id": "ashby",
        "label": "Ashby",
        "mode": "managed_browser",
        "reason": "Scan, review, prefill, and submit supported Ashby applications.",
    },
    {
        "id": "yc",
        "label": "YC Work at a Startup",
        "mode": "managed_browser",
        "reason": "Use an isolated signed-in browser for reviewed YC applications.",
    },
    {
        "id": "wellfound",
        "label": "Wellfound",
        "mode": "managed_browser",
        "reason": "Use an isolated signed-in browser for reviewed Wellfound applications.",
    },
    {
        "id": "cutshort",
        "label": "Cutshort",
        "mode": "managed_browser",
        "reason": "Use an isolated signed-in browser for reviewed Cutshort applications.",
    },
    {
        "id": "instahyre",
        "label": "Instahyre",
        "mode": "managed_browser",
        "reason": "Use an isolated signed-in browser for reviewed Instahyre applications.",
    },
    {
        "id": "indeed",
        "label": "Indeed",
        "mode": "manual_only",
        "reason": "Save and track jobs here, then complete the application on Indeed.",
    },
    {
        "id": "external_job_board",
        "label": "External job board",
        "mode": "manual_only",
        "reason": "Track any external job and open its application page for a manual handoff.",
    },
)

# These adapters have a bounded, review-gated path from one provider URL to one
# application form.  An allowlist enables a deployment only; it must never turn
# a connection-only adapter into a public automation claim.  YC, Cutshort, and
# Instahyre deliberately remain outside this set until their authenticated,
# multi-step state machines have passed controlled provider canaries.
HOSTED_FORM_AUTOMATION_PROVIDERS = frozenset(
    {"google_forms", "greenhouse", "lever", "ashby", "wellfound"}
)
SAVED_BROWSER_CONTEXT_PROVIDERS = frozenset(
    {"yc", "wellfound", "cutshort", "instahyre"}
)


def _normalise_allowlist(value: str | Iterable[str] | None) -> frozenset[str]:
    if value is None:
        value = os.getenv("ALLOWED_BROWSER_PROVIDERS", "")
    values = value.split(",") if isinstance(value, str) else value
    return frozenset(
        item.strip().lower()
        for item in values
        if isinstance(item, str) and item.strip()
    )


def _google_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")
    )


def _browserbase_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID")
    )


def browser_provider_allowed(
    provider_id: str,
    allowed_browser_providers: str | Iterable[str] | None = None,
) -> bool:
    """Return whether an immutable managed-browser entry is explicitly allowlisted."""

    if not isinstance(provider_id, str):
        return False
    clean_id = provider_id.strip().lower()
    definition = next((item for item in _REGISTRY if item["id"] == clean_id), None)
    return bool(
        definition
        and definition["mode"] == "managed_browser"
        and clean_id in _normalise_allowlist(allowed_browser_providers)
    )


def provider_catalog(
    allowed_browser_providers: str | Iterable[str] | None = None,
    *,
    google_configured: bool | None = None,
    browserbase_configured: bool | None = None,
) -> list[ProviderCapability]:
    """Return fresh, JSON-ready public capability records in stable order."""

    google_ready = _google_configured() if google_configured is None else bool(google_configured)
    browser_ready = _browserbase_configured() if browserbase_configured is None else bool(browserbase_configured)
    allowlist = _normalise_allowlist(allowed_browser_providers)
    result: list[ProviderCapability] = []

    for definition in _REGISTRY:
        provider_id = definition["id"]
        mode = definition["mode"]
        label = definition["label"]

        if mode == "partner_required":
            # Deliberately literal: this is also the documented public contract.
            capability: ProviderCapability = {
                "id": provider_id,
                "label": label,
                "mode": "partner_required",
                "available": False,
                "can_connect": False,
                "can_auto_apply": False,
                "can_scan": provider_id == "linkedin",
                "can_prefill": False,
                "requires_review": True,
                "connection_required": False,
                "reason": definition["reason"],
            }
        elif mode == "oauth":
            capability = {
                "id": provider_id,
                "label": label,
                "mode": "oauth",
                "available": google_ready,
                "can_connect": google_ready,
                "can_auto_apply": False,
                "can_scan": False,
                "can_prefill": False,
                "requires_review": True,
                "connection_required": True,
                "reason": (
                    definition["reason"]
                    if google_ready
                    else "Gmail OAuth is not configured for this deployment."
                ),
            }
        elif mode == "managed_browser":
            explicitly_allowed = provider_id in allowlist
            ready = explicitly_allowed and browser_ready
            automation_ready = ready and provider_id in HOSTED_FORM_AUTOMATION_PROVIDERS
            if not explicitly_allowed:
                reason = "Managed browser application support is disabled for this deployment."
            elif not browser_ready:
                reason = "Managed browser infrastructure is not configured."
            elif provider_id not in HOSTED_FORM_AUTOMATION_PROVIDERS:
                reason = (
                    "Isolated login is available, but hosted application automation is "
                    "paused until this provider's multi-step state machine passes a "
                    "controlled canary."
                )
            else:
                reason = (
                    "Managed browser support is enabled; every captured form revision "
                    "still requires explicit review."
                )
            capability = {
                "id": provider_id,
                "label": label,
                "mode": "managed_browser",
                "available": ready,
                "can_connect": ready and provider_id in SAVED_BROWSER_CONTEXT_PROVIDERS,
                "can_auto_apply": automation_ready,
                "can_scan": automation_ready,
                "can_prefill": automation_ready,
                "requires_review": True,
                "connection_required": provider_id in SAVED_BROWSER_CONTEXT_PROVIDERS,
                "reason": reason,
            }
        else:
            capability = {
                "id": provider_id,
                "label": label,
                "mode": "manual_only",
                "available": True,
                "can_connect": False,
                "can_auto_apply": False,
                "can_scan": False,
                "can_prefill": False,
                "requires_review": True,
                "connection_required": False,
                "reason": definition["reason"],
            }
        result.append(capability)
    return result


def get_provider(
    provider_id: str,
    allowed_browser_providers: str | Iterable[str] | None = None,
    *,
    google_configured: bool | None = None,
    browserbase_configured: bool | None = None,
) -> ProviderCapability | None:
    """Return one capability record, or ``None`` for an unknown provider."""

    if not isinstance(provider_id, str):
        return None
    clean_id = provider_id.strip().lower()
    return next(
        (
            item
            for item in provider_catalog(
                allowed_browser_providers,
                google_configured=google_configured,
                browserbase_configured=browserbase_configured,
            )
            if item["id"] == clean_id
        ),
        None,
    )


__all__ = [
    "HOSTED_FORM_AUTOMATION_PROVIDERS",
    "ProviderCapability",
    "ProviderMode",
    "SAVED_BROWSER_CONTEXT_PROVIDERS",
    "browser_provider_allowed",
    "get_provider",
    "provider_catalog",
]
