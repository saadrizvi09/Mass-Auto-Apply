"""Bounded, in-memory résumé validation and PDF text extraction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    normalized = normalize_resume_text(text)
    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", normalized, re.I)
    phones = _phone_candidates(normalized)
    links = re.findall(r"https?://[^\s<>\])}]+", normalized, re.I)

    suggestions: dict[str, Any] = {}
    if emails:
        suggestions["email"] = emails[0]
    if phones:
        suggestions["phone"] = phones[0]

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


def _phone_candidates(text: str) -> list[str]:
    """Extract phone-like values after PDF text extraction has split digits.

    Some PDF fonts expose a phone number as ``8 2 8 7 6``.  The old parser kept
    those spaces, which made the value look like a sequence of unrelated numbers
    to downstream models.  Only values with a plausible international digit count
    are returned, and punctuation is retained only when it is meaningful.
    """

    candidates: list[str] = []
    for match in re.finditer(r"(?<!\w)(?:\+\s*)?\d(?:[\d\s().-]{5,}\d)(?!\w)", text):
        raw = match.group(0).strip()
        digits = re.sub(r"\D", "", raw)
        if not 7 <= len(digits) <= 15:
            continue
        # A year range is numeric and punctuation-only too, but is not a phone.
        # Rejecting this common résumé shape prevents e.g. ``2020 - 2022`` from
        # becoming an eight-digit contact suggestion.
        if re.fullmatch(r"\d{4}\s*[-–—]\s*\d{4}", raw):
            continue
        if raw.lstrip().startswith("+"):
            # Preserve one readable separator after an explicit country code while
            # collapsing digit-by-digit PDF gaps in the subscriber number.
            country = re.match(r"^\+\s*(\d{1,3})\s+", raw)
            if country:
                subscriber = re.sub(r"\D", "", raw[country.end() :])
                normalized = f"+{country.group(1)} {subscriber}"
            else:
                normalized = f"+{digits}"
        else:
            normalized = digits
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates


def normalize_resume_text(value: str) -> str:
    """Normalize extracted résumé text without discarding line or word boundaries."""

    if not isinstance(value, str):
        return ""
    return _normalize_text(value)


def estimate_years_experience(text: str) -> float | None:
    """Estimate total professional experience from explicit years and date ranges.

    This is a conservative planning signal for the external research prompt, not a
    claim that an employer will accept the applicant.  Explicit ``X years`` text is
    preferred; otherwise non-overlapping year ranges are unioned so concurrent roles
    are not double-counted.
    """

    normalized = normalize_resume_text(text)
    explicit = [
        float(number)
        for number in re.findall(r"(?<!\d)(\d{1,2}(?:\.\d{1,2})?)\s*\+?\s+years?\b", normalized, re.I)
        if 0 <= float(number) <= 60
    ]
    if explicit:
        return round(max(explicit), 1)

    today = date.today()
    month_names = (
        r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
        r"Nov(?:ember)?|Dec(?:ember)?"
    )
    month_numbers = {
        name[:3].lower(): index
        for index, name in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
            start=1,
        )
    }
    ranges: list[tuple[int, int]] = []
    range_pattern = re.compile(
        rf"\b(?:(?P<start_month>{month_names})\s+)?(?P<start_year>19\d{{2}}|20\d{{2}})"
        rf"\s*(?:-|–|—|to)\s*"
        rf"(?:(?P<end_month>{month_names})\s+)?(?P<end_year>19\d{{2}}|20\d{{2}}|(?P<present>present|current))\b",
        re.I,
    )
    for match in range_pattern.finditer(normalized):
        start_year = int(match.group("start_year"))
        end_value = match.group("end_year")
        end_year = today.year if end_value.lower() in {"present", "current"} else int(end_value)
        if not 1950 <= start_year <= end_year <= today.year + 1:
            continue
        start_month = month_numbers.get((match.group("start_month") or "Jan")[:3].lower(), 1)
        if match.group("end_month"):
            end_month = month_numbers[(match.group("end_month") or "")[:3].lower()]
        elif end_value.lower() in {"present", "current"}:
            end_month = today.month
        else:
            # Year-only ranges are interpreted as elapsed calendar years; this
            # avoids overstating ``2020 - 2022`` as three full years.
            end_month = 1
        start_index = start_year * 12 + start_month - 1
        end_index = end_year * 12 + end_month - 1
        if not match.group("end_month") and end_value.lower() not in {"present", "current"}:
            end_index -= 1
        if end_index >= start_index:
            ranges.append((start_index, end_index))
    if not ranges:
        return None
    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    total_months = sum(end - start + 1 for start, end in merged)
    # Add a tiny epsilon so a half-year (e.g. 27 months = 2.25 years) rounds
    # in the human-expected direction instead of Python's binary tie behavior.
    return round(min(total_months / 12, 60) + 1e-9, 1)


def _normalize_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
