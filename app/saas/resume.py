"""Bounded, in-memory résumé validation and PDF text extraction."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
from typing import Any

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .profile_urls import is_placeholder_profile_url


MAX_PDF_PAGES = 50
MAX_EXTRACTED_CHARACTERS = 250_000
MAX_ANNOTATION_LINKS = 100


def _annotation_urls(page: Any, *, remaining: int) -> list[str]:
    """Read bounded HTTP(S) annotation targets without following those links."""

    if remaining <= 0:
        return []
    try:
        annotations = page.get("/Annots") or []
    except (AttributeError, KeyError, TypeError, ValueError):
        return []
    result: list[str] = []
    try:
        candidates = list(annotations)[:remaining]
    except (TypeError, ValueError):
        return []
    for reference in candidates:
        try:
            annotation = reference.get_object() if hasattr(reference, "get_object") else reference
            action = annotation.get("/A") if hasattr(annotation, "get") else None
            if hasattr(action, "get_object"):
                action = action.get_object()
            uri = action.get("/URI") if hasattr(action, "get") else None
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
            continue
        if not isinstance(uri, str):
            continue
        clean = uri.strip().rstrip(".,;:")
        if len(clean) <= 2_048 and re.match(r"^https?://", clean, re.IGNORECASE):
            result.append(clean)
    return result


@dataclass(slots=True)
class ResumeParseError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def extract_pdf_text(pdf_bytes: bytes, *, max_bytes: int) -> str:
    """Validate a bounded PDF and return normalized text.

    This deliberately does not perform OCR or follow links embedded in the file. Keeping
    parsing bounded is important because this function runs in a request-limited web
    function.
    """
    if not pdf_bytes:
        raise ResumeParseError("resume_empty", "The uploaded résumé is empty.")
    if len(pdf_bytes) > max_bytes:
        raise ResumeParseError(
            "resume_too_large", f"The résumé exceeds the {max_bytes} byte limit."
        )
    if not pdf_bytes.lstrip().startswith(b"%PDF-"):
        raise ResumeParseError(
            "resume_invalid_pdf", "The uploaded file is not a readable PDF."
        )

    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ResumeParseError(
                "resume_encrypted", "Password-protected résumés are not supported."
            )
        if not reader.pages:
            raise ResumeParseError("resume_empty", "The résumé has no pages.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ResumeParseError(
                "resume_too_many_pages",
                f"The résumé must have at most {MAX_PDF_PAGES} pages.",
            )

        chunks: list[str] = []
        annotation_links: list[str] = []
        total = 0
        for page in reader.pages:
            chunk = page.extract_text() or ""
            total += len(chunk)
            if total > MAX_EXTRACTED_CHARACTERS:
                raise ResumeParseError(
                    "resume_text_too_large", "The résumé contains too much text."
                )
            chunks.append(chunk)
            annotation_links.extend(
                _annotation_urls(page, remaining=MAX_ANNOTATION_LINKS - len(annotation_links))
            )

        if annotation_links:
            chunks.append("\n".join(dict.fromkeys(annotation_links)))
    except ResumeParseError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ResumeParseError(
            "resume_invalid_pdf", "The uploaded file is not a readable PDF."
        ) from exc

    text = _normalize_text("\n".join(chunks))
    if len(re.sub(r"\s", "", text)) < 20:
        raise ResumeParseError(
            "resume_no_text",
            "No usable text was found. Upload a text-based PDF instead of an image scan.",
        )
    return text


def profile_suggestions(text: str) -> dict[str, Any]:
    """Return conservative suggestions without silently overwriting a user profile."""
    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.I)
    phones = re.findall(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)", text)
    links = re.findall(r"https?://[^\s<>\])}]+", text, re.I)

    suggestions: dict[str, Any] = {}
    if emails:
        suggestions["email"] = emails[0]
    if phones:
        suggestions["phone"] = re.sub(r"\s+", " ", phones[0]).strip()

    for link in links:
        clean = link.rstrip(".,;:")
        if is_placeholder_profile_url(clean):
            continue
        lower = clean.lower()
        if "linkedin.com/in/" in lower and "linkedin_url" not in suggestions:
            suggestions["linkedin_url"] = clean
        elif "github.com/" in lower and "github_url" not in suggestions:
            suggestions["github_url"] = clean

    return suggestions


def _normalize_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
