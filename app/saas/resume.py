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
    experience = estimate_years_experience(normalized)
    if experience is not None:
        suggestions["years_experience"] = experience

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


_PROFESSIONAL_HEADINGS = frozenset(
    {
        "experience",
        "professional experience",
        "work experience",
        "employment",
        "employment history",
        "work history",
        "career history",
        "internship",
        "internships",
    }
)
_NON_PROFESSIONAL_HEADINGS = frozenset(
    {
        "education",
        "academic background",
        "academic projects",
        "projects",
        "academic experience",
        "coursework",
        "certifications",
        "certificates",
        "volunteering",
        "volunteer experience",
        "leadership",
        "extracurricular activities",
        "activities",
        "awards",
        "publications",
    }
)
_NON_PROFESSIONAL_CONTEXT = re.compile(
    r"\b(?:education|academic|project|coursework|certificat(?:e|ion)|university|"
    r"college|school|b\.?\s*tech|bachelor|master|degree|graduat(?:e|ion)|"
    r"expected|gpa|cgpa|volunteer|leadership|extracurricular)\b",
    re.I,
)
_ROLE_CONTEXT = re.compile(
    r"\b(?:engineer|developer|designer|architect|analyst|scientist|consultant|"
    r"research(?:er| assistant)?|assistant|associate|manager|lead|intern|"
    r"internship|trainee|fellow|apprentice|employee|freelance|contract(?:or)?|"
    r"company|corp(?:oration)?|technolog(?:y|ies)|solutions|labs?|inc|ltd)\b",
    re.I,
)
_STUDENT_CONTEXT = re.compile(
    r"\b(?:final[- ]year|undergraduate|student|pursuing|expected\s+(?:to\s+)?"
    r"graduat(?:e|ion)|currently\s+studying)\b",
    re.I,
)
_DATE_MONTH_NAMES = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
_DATE_RANGE_PATTERN = re.compile(
    rf"\b(?:(?P<start_month>{_DATE_MONTH_NAMES})\s+)?"
    rf"(?P<start_year>19\d{{2}}|20\d{{2}})\s*(?:-|–|—|to)\s*"
    rf"(?:(?P<end_month>{_DATE_MONTH_NAMES})\s+)?"
    rf"(?P<end_year>19\d{{2}}|20\d{{2}}|(?P<present>present|current))\b",
    re.I,
)
_MONTH_NUMBERS = {
    name[:3].lower(): index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        start=1,
    )
}


def _heading_kind(line: str) -> str | None:
    """Classify common résumé section headings without treating role lines as headings."""

    if not isinstance(line, str) or len(line.strip()) > 100:
        return None
    compact = re.sub(r"[^a-z0-9]+", " ", line.casefold()).strip()
    if compact in _PROFESSIONAL_HEADINGS:
        return "professional"
    if compact in _NON_PROFESSIONAL_HEADINGS:
        return "nonprofessional"
    # PDF extraction often leaves headings such as ``Education: B.Tech`` on one line.
    if re.match(r"^(?:education|academic|projects?|coursework|certifications?)\b", compact):
        return "nonprofessional"
    if re.match(r"^(?:professional|work|employment|career)\s+(?:experience|history)\b", compact):
        return "professional"
    if re.match(r"^internships?\b", compact):
        return "professional"
    return None


def _date_range_months(match: re.Match[str], today: date) -> tuple[int, int] | None:
    start_year = int(match.group("start_year"))
    end_value = match.group("end_year")
    end_year = today.year if end_value.lower() in {"present", "current"} else int(end_value)
    if not 1950 <= start_year <= end_year <= today.year + 1:
        return None
    start_month = _MONTH_NUMBERS.get((match.group("start_month") or "Jan")[:3].lower(), 1)
    if match.group("end_month"):
        end_month = _MONTH_NUMBERS[(match.group("end_month") or "")[:3].lower()]
    elif end_value.lower() in {"present", "current"}:
        end_month = today.month
    else:
        # ``2023 - 2024`` means one elapsed year, not January 2023 through January 2024.
        end_month = 1
    start_index = start_year * 12 + start_month - 1
    end_index = end_year * 12 + end_month - 1
    if not match.group("end_month") and end_value.lower() not in {"present", "current"}:
        end_index -= 1
    return (start_index, end_index) if end_index >= start_index else None


def _professional_date_ranges(text: str) -> list[tuple[int, int]]:
    """Read date ranges only from professional sections or role-like context."""

    lines = normalize_resume_text(text).splitlines()
    today = date.today()
    active_section: str | None = None
    ranges: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        heading = _heading_kind(line)
        if heading is not None:
            active_section = heading
        matches = list(_DATE_RANGE_PATTERN.finditer(line))
        if not matches:
            continue
        context = " ".join(lines[max(0, index - 2) : min(len(lines), index + 3)])
        for match in matches:
            if active_section == "nonprofessional":
                continue
            if active_section != "professional":
                # Unsectioned résumé text must look like an actual role. This keeps
                # ``B.Tech 2022 - 2026`` and academic project dates out of the total.
                if _NON_PROFESSIONAL_CONTEXT.search(context) or not _ROLE_CONTEXT.search(context):
                    continue
            elif not _ROLE_CONTEXT.search(context):
                # A bare ``Experience: 2020 - 2022`` is not enough evidence to count.
                continue
            parsed = _date_range_months(match, today)
            if parsed is not None:
                ranges.append(parsed)
    return ranges


def estimate_years_experience(text: str) -> float | None:
    """Estimate dated professional experience while excluding education and projects.

    Education enrollment dates and academic-project dates are deliberately ignored.
    When a résumé contains both a prose claim and dated roles, the dated professional
    evidence wins and the smaller value is used if the prose claim is more conservative.
    """

    normalized = normalize_resume_text(text)
    ranges = _professional_date_ranges(normalized)
    explicit = [
        float(number)
        for number in re.findall(
            r"(?<!\d)(\d{1,2}(?:\.\d{1,2})?)\s*\+?\s+years?\b",
            normalized,
            re.I,
        )
        if 0 <= float(number) <= 60
    ]
    if ranges:
        ranges.sort()
        merged: list[list[int]] = []
        for start, end in ranges:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        total_months = sum(end - start + 1 for start, end in merged)
        dated_years = round(min(total_months / 12, 60) + 1e-9, 1)
        return round(min(dated_years, max(explicit)), 1) if explicit else dated_years
    if explicit:
        return round(max(explicit), 1)
    if _STUDENT_CONTEXT.search(normalized):
        return 0.0
    return None


def _normalize_text(value: str) -> str:
    value = value.replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()
