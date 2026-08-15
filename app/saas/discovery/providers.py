"""Pure provider detection for public application URLs."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .common import (
    NormalizedJob,
    extract_urls,
    humanize_slug,
    make_job,
    safe_http_url,
)


PROVIDER_LABELS: dict[str, str] = {
    "google_forms": "Google Forms",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "yc": "YC Work at a Startup",
    "wellfound": "Wellfound",
    "cutshort": "Cutshort",
    "instahyre": "Instahyre",
}

_PUBLIC_DNS_LABEL = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE
)
_NON_PUBLIC_HOST_SUFFIXES = (
    ".internal",
    ".intranet",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".private",
    ".test",
    ".example",
)
_ADDRESS_ALIAS_SUFFIXES = (".nip.io", ".sslip.io", ".localtest.me", ".lvh.me")
_RESERVED_COMPANY_FORM_SUFFIXES = (
    "ycombinator.com",
    "workatastartup.com",
)


def _host_is(host: str, *candidates: str) -> bool:
    return host in candidates


def _reserved_company_form_host(host: str) -> bool:
    """Keep YC-owned hosts inside the stricter exact-job provider boundary."""

    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in _RESERVED_COMPANY_FORM_SUFFIXES
    )


def detect_provider(url: object) -> str | None:
    """Identify a supported application host without making a network request."""

    clean = safe_http_url(url)
    if not clean:
        return None
    parsed = urlsplit(clean)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    if parsed.scheme != "https":
        return None

    if host == "forms.gle" or (host == "docs.google.com" and path.startswith("/forms")):
        return "google_forms"
    if _host_is(
        host,
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    ) and ("/job" in path or host.startswith(("boards.", "job-boards."))):
        return "greenhouse"
    if _host_is(host, "jobs.lever.co", "jobs.eu.lever.co"):
        return "lever"
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if _host_is(host, "workatastartup.com", "www.workatastartup.com") and (
        path.startswith("/jobs") or "/jobs/" in path
    ):
        return "yc"
    if _host_is(host, "wellfound.com", "www.wellfound.com") and "/jobs" in path:
        return "wellfound"
    if _host_is(host, "cutshort.io", "www.cutshort.io") and path.startswith(("/job", "/jobs")):
        return "cutshort"
    if _host_is(host, "instahyre.com", "www.instahyre.com") and path.startswith(("/job", "/jobs")):
        return "instahyre"
    return None


def public_company_form_target(url: object) -> dict[str, str] | None:
    """Validate one explicitly saved generic company form and bind its exact host.

    This stays separate from :func:`detect_provider`, so arbitrary URLs found by
    Telegram, RSS, imports, or pasted text never become browser-automation
    targets. Literal IPs, local/special-use names, credentials, and every explicit
    port are rejected. Rejecting explicit ``:443`` too keeps the worker target
    canonical and is stricter than merely rejecting non-443 ports.
    """

    if not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw or len(raw) > 2_048 or any(ord(character) < 32 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
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
        return None
    if host == "localhost" or host.endswith(_NON_PUBLIC_HOST_SUFFIXES):
        return None
    if _reserved_company_form_host(host):
        return None
    if host in {suffix.removeprefix(".") for suffix in _ADDRESS_ALIAS_SUFFIXES} or host.endswith(
        _ADDRESS_ALIAS_SUFFIXES
    ):
        return None
    labels = host.split(".")
    if labels[-1].isdigit() or any(not _PUBLIC_DNS_LABEL.fullmatch(label) for label in labels):
        return None
    target_url = urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    return {"host": host, "target_url": target_url}


def extract_provider_urls(text: object, *, limit: int = 20) -> list[dict[str, str]]:
    """Return unique supported application URLs found in arbitrary pasted text."""

    found: list[dict[str, str]] = []
    for url in extract_urls(text, limit=max(limit * 3, 20)):
        provider = detect_provider(url)
        if provider:
            found.append({"provider": provider, "url": url})
            if len(found) >= limit:
                break
    return found


def _identity_from_url(provider: str, url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    query = parse_qs(parsed.query)
    company = "Unknown employer"
    title = f"{PROVIDER_LABELS[provider]} application"

    if provider in {"greenhouse", "lever", "ashby"} and segments:
        company = humanize_slug(segments[0]) or company
    if provider == "yc" and len(segments) > 1:
        title = humanize_slug(segments[-1]) or title
    elif provider in {"wellfound", "cutshort", "instahyre"} and segments:
        candidate = segments[-1]
        if candidate.lower() not in {"job", "jobs"}:
            title = humanize_slug(candidate) or title
    elif provider == "greenhouse":
        title = humanize_slug((query.get("for") or [""])[0]) or title
    return company, title


def discover_provider_urls(text: object, *, limit: int = 20) -> list[NormalizedJob]:
    """Turn public ATS URLs in pasted text into reviewable normalized jobs."""

    jobs: list[NormalizedJob] = []
    for item in extract_provider_urls(text, limit=limit):
        provider, url = item["provider"], item["url"]
        company, title = _identity_from_url(provider, url)
        jobs.append(
            make_job(
                source="public_ats",
                external_id=f"{provider}:{url}",
                apply_url=url,
                title=title,
                company=company,
                description=(
                    f"Public {PROVIDER_LABELS[provider]} application discovered from pasted text. "
                    "Review the complete job description before applying."
                ),
                metadata={"provider": provider, "provider_label": PROVIDER_LABELS[provider]},
            )
        )
    return jobs


__all__ = [
    "PROVIDER_LABELS",
    "detect_provider",
    "discover_provider_urls",
    "extract_provider_urls",
    "public_company_form_target",
]
