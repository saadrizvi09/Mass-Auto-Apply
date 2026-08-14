"""Stateless parsing for pasted referral and hiring digests."""

from __future__ import annotations

import re

from .common import (
    NormalizedJob,
    clean_text,
    extract_emails,
    extract_urls,
    infer_company_title,
    labeled_field,
    make_job,
    stable_external_id,
)
from .providers import detect_provider


_NUMBERED_BLOCK = re.compile(r"(?m)^\s*(?=\d{1,3}[).]\s+)")
_SUBJECT_RE = re.compile(
    r"(?im)^\s*(?:email\s+)?subject\s*[-:]\s*[\"'\u201c]?(.+?)[\"'\u201d]?\s*$"
)
_CC_RE = re.compile(r"(?im)^\s*cc\s*[-:]\s*(.+?)\s*$")
_PROMOTIONAL_LINE = re.compile(
    r"(?:whatsapp\.com/channel|t\.me/|topmate\.io|subscribe\s+(?:to|for)|"
    r"join\s+(?:our\s+)?(?:channel|group)|free\s+hiring\s+updates|premium\s+referral)",
    re.IGNORECASE,
)
_PROMOTIONAL_TAIL_START = re.compile(
    r"(?im)^\s*(?:for\s+free\s+hiring\s+updates|if\s+you\s+are\s+a\s+serious\s+job\s+seeker|"
    r"[\W_]*only\s+for\s+serious\s+job\s+seekers|join\s+our\s+premium\s+referral|"
    r"we\s+have\s+started\s+our\s+paid\s+group)\b.*$"
)


def _blocks(text: str) -> list[str]:
    parts = [part.strip() for part in _NUMBERED_BLOCK.split(text) if part.strip()]
    if len(parts) > 1:
        # A short non-actionable introduction before "1)" is not a job.
        return parts
    return [text.strip()] if text.strip() else []


def referral_digest_summary(text: object, jobs: list[NormalizedJob]) -> dict[str, int]:
    """Return stable intake counts without exposing or storing the pasted digest."""

    source = text if isinstance(text, str) else ""
    promotional_urls = {
        url
        for url in extract_urls(source, limit=1_000)
        if _PROMOTIONAL_LINE.search(url)
    }
    google_forms = sum(
        job.get("metadata", {}).get("apply_kind") == "form" for job in jobs
    )
    email_apply = sum(
        job.get("metadata", {}).get("apply_kind") == "email" for job in jobs
    )
    return {
        "parsed": len(jobs),
        "google_forms": google_forms,
        "email_apply": email_apply,
        "ignored_promotional": len(promotional_urls),
    }


def _non_promotional_urls(block: str) -> list[str]:
    return [
        url
        for url in extract_urls(block)
        if not _PROMOTIONAL_LINE.search(url)
    ]


def _actionable_part(block: str) -> str:
    """Drop a paid-group/footer section appended after an actionable job."""

    marker = _PROMOTIONAL_TAIL_START.search(block)
    return block[: marker.start()].rstrip() if marker else block


def _job_from_block(block: str) -> NormalizedJob | None:
    actionable = _actionable_part(block)
    urls = _non_promotional_urls(actionable)
    emails = extract_emails(actionable)
    if not urls and not emails:
        return None

    provider_url = next((url for url in urls if detect_provider(url)), None)
    apply_url = provider_url or (urls[0] if urls else None)
    provider = detect_provider(apply_url) if apply_url else None
    company, title = infer_company_title(actionable, apply_url)
    company = labeled_field(actionable, ("company", "company name", "organisation", "organization")) or company
    title = labeled_field(actionable, ("role", "job title", "position", "opening")) or title
    location = labeled_field(actionable, ("location", "city", "work location")) or None
    batch = labeled_field(actionable, ("batch", "graduation year", "year of passing"))
    stipend = labeled_field(actionable, ("stipend", "salary", "ctc", "compensation"))
    subject_match = _SUBJECT_RE.search(actionable)
    cc_match = _CC_RE.search(actionable)
    cc_emails = extract_emails(cc_match.group(1)) if cc_match else []
    contact = next((email for email in emails if email not in cc_emails), emails[0] if emails else None)
    apply_kind = "form" if provider == "google_forms" else "url" if apply_url else "email"
    if provider and provider != "google_forms":
        apply_kind = "ats"

    description = "\n".join(
        line for line in clean_text(actionable, limit=25_000).splitlines()
        if not _PROMOTIONAL_LINE.search(line)
    ).strip()
    external_id = stable_external_id(
        "referral_digest", apply_url, contact, title.casefold(), company.casefold()
    )
    return make_job(
        source="referral_digest",
        external_id=external_id,
        apply_url=apply_url,
        title=title,
        company=company,
        location=location,
        description=description,
        contact_email=contact,
        metadata={
            "apply_kind": apply_kind,
            "provider": provider,
            "batch": batch,
            "compensation": stipend,
            "email_subject": clean_text(subject_match.group(1), limit=500) if subject_match else "",
            "cc": cc_emails[:5],
            "discovered_urls": urls[:10],
        },
    )


def parse_referral_digest(text: object, *, limit: int = 100) -> list[NormalizedJob]:
    """Parse pasted user text locally; no Groq call or shared state is involved."""

    if not isinstance(text, str) or not text.strip():
        return []
    if len(text.encode("utf-8", "replace")) > 1_000_000:
        raise ValueError("Referral digest is too large")
    limit = max(1, min(int(limit), 250))
    jobs: list[NormalizedJob] = []
    seen_targets: set[tuple[str | None, str | None, str, str]] = set()
    for block in _blocks(text):
        job = _job_from_block(block)
        if not job:
            continue
        target = (
            job["apply_url"],
            job["contact_email"],
            job["title"].lower(),
            job["company"].lower(),
        )
        if target in seen_targets:
            continue
        seen_targets.add(target)
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


__all__ = ["parse_referral_digest", "referral_digest_summary"]
