"""Pure provider detection for public application URLs."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

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


def _host_is(host: str, *candidates: str) -> bool:
    return host in candidates


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
]
