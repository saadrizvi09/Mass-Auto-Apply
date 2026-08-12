"""Shared normalization primitives for discovery sources."""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, TypedDict
from urllib.parse import urlsplit, urlunsplit


class NormalizedJob(TypedDict):
    source: str
    external_id: str | None
    apply_url: str | None
    title: str
    company: str
    location: str | None
    description: str
    contact_email: str | None
    metadata: dict[str, Any]


_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\[{}]+", re.IGNORECASE)
_LABEL_RE_TEMPLATE = r"(?im)^\s*(?:\d{{1,3}}[).]\s*)?(?:{labels})\s*[-:\u2013\u2014]\s*(.+?)\s*$"


def clean_text(value: object, *, limit: int | None = None) -> str:
    """Normalize untrusted scalar text without flattening meaningful newlines."""

    if value is None:
        return ""
    text = html.unescape(str(value)).replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[\t\f\v ]+", " ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit].rstrip() if limit is not None else text


def safe_http_url(value: object) -> str | None:
    """Return a bounded public HTTP(S) URL, or ``None`` for malformed input."""

    raw = html.unescape(str(value or "")).strip().rstrip(".,;:!?)]\u201d\"'")
    if not raw or len(raw) > 2_048:
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
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
    if any(ord(character) < 32 for character in raw):
        return None
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def extract_urls(text: object, *, limit: int = 20) -> list[str]:
    """Extract unique, syntactically safe URLs while preserving input order."""

    result: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(html.unescape(str(text or ""))):
        url = safe_http_url(match.group(0))
        if url and url not in seen:
            seen.add(url)
            result.append(url)
            if len(result) >= limit:
                break
    return result


def extract_emails(text: object, *, limit: int = 10) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for match in _EMAIL_RE.finditer(str(text or "")):
        email = match.group(0).lower()[:320]
        if email not in seen:
            seen.add(email)
            result.append(email)
            if len(result) >= limit:
                break
    return result


def labeled_field(text: str, labels: tuple[str, ...]) -> str:
    escaped = "|".join(re.escape(label) for label in labels)
    match = re.search(_LABEL_RE_TEMPLATE.format(labels=escaped), text)
    return clean_text(match.group(1), limit=500) if match else ""


def humanize_slug(value: str) -> str:
    words = re.sub(r"[-_]+", " ", value).strip(" /.")
    return " ".join(word.capitalize() for word in words.split())


def infer_company_title(text: str, apply_url: str | None = None) -> tuple[str, str]:
    """Best-effort company/title extraction for unstructured public listings."""

    company = labeled_field(text, ("company", "company name", "organisation", "organization", "employer"))
    title = labeled_field(text, ("role", "job title", "title", "position", "opening"))

    compact = clean_text(text, limit=2_000)
    if not company or not title:
        hiring = re.search(
            r"(?im)^\s*(?:\d+[).]\s*)?(.{2,120}?)\s+(?:is\s+)?hiring(?:\s+for)?\s+(.{2,160}?)\s*$",
            compact,
        )
        if hiring:
            company = company or clean_text(hiring.group(1), limit=240)
            title = title or clean_text(hiring.group(2), limit=240)
    if not title or not company:
        at_match = re.search(r"(?im)^\s*(.{2,160}?)\s+at\s+(.{2,120}?)\s*$", compact)
        if at_match:
            title = title or clean_text(at_match.group(1), limit=240)
            company = company or clean_text(at_match.group(2), limit=240)

    if apply_url and not company:
        parsed = urlsplit(apply_url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if parsed.hostname in {
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "jobs.lever.co",
            "jobs.eu.lever.co",
            "jobs.ashbyhq.com",
        } and segments:
            company = humanize_slug(segments[0])

    if not title:
        lines = [
            re.sub(r"^\s*\d+[).]\s*", "", line).strip()
            for line in compact.splitlines()
            if line.strip()
        ]
        title = next((line for line in lines if len(line) <= 240), "")
    return company or "Unknown employer", title or "Open position"


def stable_external_id(*parts: object) -> str:
    material = "\x1f".join(clean_text(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()


def make_job(
    *,
    source: object,
    external_id: object = None,
    apply_url: object = None,
    title: object,
    company: object,
    location: object = None,
    description: object = None,
    contact_email: object = None,
    metadata: dict[str, Any] | None = None,
) -> NormalizedJob:
    """Build a bounded dictionary accepted by the public ``JobCreate`` model."""

    clean_source = clean_text(source, limit=60) or "discovery"
    clean_url = safe_http_url(apply_url)
    clean_title = clean_text(title, limit=240) or "Open position"
    clean_company = clean_text(company, limit=240) or "Unknown employer"
    clean_location = clean_text(location, limit=240) or None
    clean_description = clean_text(description, limit=25_000)
    if len(clean_description) < 20:
        place = f" in {clean_location}" if clean_location else ""
        clean_description = f"{clean_title} opportunity at {clean_company}{place}."
    email = (extract_emails(contact_email, limit=1) or [None])[0]
    clean_external_id = clean_text(external_id, limit=255) or stable_external_id(
        clean_source, clean_url, clean_title, clean_company
    )
    return {
        "source": clean_source,
        "external_id": clean_external_id,
        "apply_url": clean_url,
        "title": clean_title,
        "company": clean_company,
        "location": clean_location,
        "description": clean_description,
        "contact_email": email,
        "metadata": dict(metadata or {}),
    }


__all__ = [
    "NormalizedJob",
    "clean_text",
    "extract_emails",
    "extract_urls",
    "humanize_slug",
    "infer_company_title",
    "labeled_field",
    "make_job",
    "safe_http_url",
    "stable_external_id",
]
