"""Public/user-supplied contact candidates for the hosted outreach workflow.

This module deliberately does not probe mailboxes, send verification messages, or
guess a person's address from a company domain. It only extracts addresses already
present in the tenant-owned job record and marks them as unverified public leads for
human review.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .discovery.common import extract_emails, safe_http_url


_MAX_CANDIDATES = 10
_PUBLIC_SOURCES = frozenset(
    {"telegram", "rss", "linkedin", "public_ats", "referral_digest", "csv", "xlsx"}
)


def _candidate(
    email: str,
    *,
    source: str,
    name: str | None = None,
    position: str | None = None,
    linkedin_url: str | None = None,
    source_url: str | None = None,
    verification_status: str = "public_source_unverified",
) -> dict[str, Any]:
    domain = email.rsplit("@", 1)[-1]
    candidate: dict[str, Any] = {
        "email": email,
        "name": name,
        "position": position or "Public listing contact",
        "domain": domain,
        "source": source,
        "verification_status": verification_status,
        "confidence": None,
    }
    if linkedin_url:
        candidate["linkedin_url"] = linkedin_url
    if source_url:
        candidate["source_url"] = source_url
    return candidate


def public_contact_candidates(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return only syntactically valid addresses already present in an owned job.

    A discovered source is useful provenance, but it is not proof that an address
    still exists or that its owner wants outreach. The caller must preserve that
    distinction in the UI and keep the normal draft/review/send gates in place.
    """

    job_source = str(job.get("source") or "job_record").strip().lower()
    source_label = (
        f"public {job_source} listing"
        if job_source in _PUBLIC_SOURCES
        else "user-supplied job record"
    )
    metadata = job.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    contact_name = metadata.get("contact_name")
    contact_name = contact_name.strip()[:160] if isinstance(contact_name, str) and contact_name.strip() else None
    contact_title = metadata.get("contact_title")
    contact_title = contact_title.strip()[:160] if isinstance(contact_title, str) and contact_title.strip() else None
    linkedin_url = metadata.get("linkedin_url")
    linkedin_url = linkedin_url.strip()[:2_048] if isinstance(linkedin_url, str) and linkedin_url.strip() else None
    imported_source = metadata.get("contact_source")
    imported_source = imported_source.strip()[:240] if isinstance(imported_source, str) and imported_source.strip() else None
    imported_source_url = safe_http_url(metadata.get("contact_source_url"))
    role_source_url = safe_http_url(metadata.get("source_url"))
    imported_status = metadata.get("email_verification_status")
    imported_status = imported_status.strip().lower() if isinstance(imported_status, str) else ""
    verification_status = (
        "public_source_verified"
        if imported_status in {"public_source_verified", "source_verified", "publicly_listed"}
        else "public_source_unverified"
    )
    values: list[
        tuple[Any, str, str | None, str | None, str | None, str | None, str]
    ] = [
        (
            job.get("contact_email"),
            imported_source or "saved contact field",
            contact_name,
            contact_title,
            linkedin_url,
            imported_source_url,
            verification_status,
        ),
        (
            job.get("description"),
            source_label,
            None,
            None,
            None,
            role_source_url,
            "public_source_unverified",
        ),
    ]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value, origin, name, position, profile_url, evidence_url, status in values:
        for email in extract_emails(value, limit=_MAX_CANDIDATES):
            if email in seen:
                continue
            seen.add(email)
            candidates.append(
                _candidate(
                    email,
                    source=origin,
                    name=name,
                    position=position,
                    linkedin_url=profile_url,
                    source_url=evidence_url,
                    verification_status=status,
                )
            )
            if len(candidates) >= _MAX_CANDIDATES:
                return candidates
    return candidates


__all__ = ["public_contact_candidates"]
