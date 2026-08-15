"""Exact-host policy factory for user-supplied public company forms."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .base import ProviderAdapter, ProviderPolicy


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_NON_PUBLIC_SUFFIXES = (
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localhost",
    ".onion",
    ".test",
    ".example",
)
_ADDRESS_ALIAS_SUFFIXES = (
    ".nip.io",
    ".sslip.io",
    ".localtest.me",
    ".lvh.me",
)
_RESERVED_PROVIDER_SUFFIXES = (
    "ycombinator.com",
    "workatastartup.com",
)


def public_company_form_host(value: str) -> str | None:
    """Return a conservative public DNS host or reject the target before browsing."""

    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not host
        or not host.isascii()
        or host.endswith(".")
        or len(host) > 253
        or "." not in host
    ):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # All IP literals are rejected, including globally routed addresses. A
        # custom form must be attached to a stable public DNS identity.
        return None
    if host == "localhost" or host.endswith(_NON_PUBLIC_SUFFIXES):
        return None
    if any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _RESERVED_PROVIDER_SUFFIXES
    ):
        return None
    if host in {suffix.removeprefix(".") for suffix in _ADDRESS_ALIAS_SUFFIXES} or host.endswith(
        _ADDRESS_ALIAS_SUFFIXES
    ):
        return None
    labels = host.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return None
    # Numeric-only final labels cover non-canonical integer/dotted IP spellings.
    if labels[-1].isdigit():
        return None
    return host


def adapter_for_target(value: str) -> ProviderAdapter | None:
    """Bind one generic form adapter to exactly the target's validated host."""

    host = public_company_form_host(value)
    if host is None:
        return None
    return ProviderAdapter(
        provider="company_form",
        policy=ProviderPolicy(
            provider="company_form",
            exact_hosts=frozenset({host}),
            allow_explicit_port=False,
        ),
        submit_selectors=(
            'button:text-is("Submit application"),'
            'button:text-is("Submit Application"),'
            'button:text-is("Send application"),'
            'button:text-is("Send Application"),'
            'button[type="submit"]:text-is("Submit"),'
            'input[type="submit"][value="Submit"]',
        ),
        confirmation_path_patterns=(
            r"/(?:applications?/)?(?:submitted|complete|confirmation|success|thanks?|thank-you)(?:/|$)",
        ),
        confirmation_text=(
            "application submitted",
            "application has been submitted",
            "application received",
            "thank you for applying",
            "we received your application",
        ),
        form_selectors=('[role="dialog"] form', 'form[action]', "form"),
        application_entry_selector=(
            'a:text-is("Apply"),button:text-is("Apply"),'
            'a:text-is("Apply now"),button:text-is("Apply now"),'
            'a:text-is("Apply Now"),button:text-is("Apply Now")'
        ),
    )


__all__ = ["adapter_for_target", "public_company_form_host"]
