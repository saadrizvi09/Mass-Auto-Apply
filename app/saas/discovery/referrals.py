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
    r"(?:whatsapp\.com/channel|t\.me/|topmate\.io|subscribe\s+(?:to|for)|join\s+our\s+channel)",
    re.IGNORECASE,
)


def _blocks(text: str) -> list[str]:
    parts = [part.strip() for part in _NUMBERED_BLOCK.split(text) if part.strip()]
    if len(parts) > 1:
        # A short non-actionable introduction before "1)" is not a job.
        return parts
    return [text.strip()] if text.strip() else []


def _non_promotional_urls(block: str) -> list[str]:
    return [
        url
        for url in extract_urls(block)
        if not _PROMOTIONAL_LINE.search(url)
    ]


def _job_from_block(block: str, ordinal: int) -> NormalizedJob | None:
    urls = _non_promotional_urls(block)
    emails = extract_emails(block)
    if not urls and not emails:
        return None

    provider_url = next((url for url in urls if detect_provider(url)), None)
    apply_url = provider_url or (urls[0] if urls else None)
    provider = detect_provider(apply_url) if apply_url else None
    company, title = infer_company_title(block, apply_url)
    company = labeled_field(block, ("company", "company name", "organisation", "organization")) or company
    title = labeled_field(block, ("role", "job title", "position", "opening")) or title
    location = labeled_field(block, ("location", "city", "work location")) or None
    batch = labeled_field(block, ("batch", "graduation year", "year of passing"))
    stipend = labeled_field(block, ("stipend", "salary", "ctc", "compensation"))
    subject_match = _SUBJECT_RE.search(block)
    cc_match = _CC_RE.search(block)
    cc_emails = extract_emails(cc_match.group(1)) if cc_match else []
    contact = next((email for email in emails if email not in cc_emails), emails[0] if emails else None)
    apply_kind = "form" if provider == "google_forms" else "url" if apply_url else "email"
    if provider and provider != "google_forms":
        apply_kind = "ats"

    description = "\n".join(
        line for line in clean_text(block, limit=25_000).splitlines()
        if not _PROMOTIONAL_LINE.search(line)
    ).strip()
    external_id = stable_external_id("referral_digest", apply_url, contact, ordinal, description)
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
    for ordinal, block in enumerate(_blocks(text), start=1):
        job = _job_from_block(block, ordinal)
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


__all__ = ["parse_referral_digest"]
