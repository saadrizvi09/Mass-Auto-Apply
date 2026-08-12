from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.saas import resume as resume_module
from app.saas.resume import ResumeParseError, extract_pdf_text, profile_suggestions


def test_pdf_parser_rejects_empty_oversized_and_disguised_files() -> None:
    with pytest.raises(ResumeParseError, match="empty") as empty:
        extract_pdf_text(b"", max_bytes=100)
    assert empty.value.code == "resume_empty"

    with pytest.raises(ResumeParseError) as too_large:
        extract_pdf_text(b"%PDF-" + b"x" * 100, max_bytes=10)
    assert too_large.value.code == "resume_too_large"

    with pytest.raises(ResumeParseError) as invalid:
        extract_pdf_text(b"not really a pdf", max_bytes=100)
    assert invalid.value.code == "resume_invalid_pdf"


def test_pdf_parser_normalizes_text_and_bounds_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        SimpleNamespace(
            extract_text=lambda: "Ada   Lovelace\n\n\nBackend Engineer and mathematician"
        )
    ]
    monkeypatch.setattr(
        resume_module,
        "PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_encrypted=False,
            pages=pages,
        ),
    )
    assert extract_pdf_text(b"%PDF-fake", max_bytes=100) == (
        "Ada Lovelace\n\nBackend Engineer and mathematician"
    )

    monkeypatch.setattr(
        resume_module,
        "PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(
            is_encrypted=False,
            pages=pages * (resume_module.MAX_PDF_PAGES + 1),
        ),
    )
    with pytest.raises(ResumeParseError) as error:
        extract_pdf_text(b"%PDF-fake", max_bytes=100)
    assert error.value.code == "resume_too_many_pages"


def test_profile_suggestions_are_conservative() -> None:
    result = profile_suggestions(
        "Ada Lovelace ada@example.test +91 98765 43210 "
        "https://linkedin.com/in/ada https://github.com/ada"
    )
    assert result == {
        "email": "ada@example.test",
        "phone": "+91 98765 43210",
        "linkedin_url": "https://linkedin.com/in/ada",
        "github_url": "https://github.com/ada",
    }


def test_profile_suggestions_skip_placeholder_links_and_use_real_annotation_target() -> None:
    result = profile_suggestions(
        "https://linkedin.com/in/CHANGE-ME "
        "https://www.linkedin.com/in/saad-rizvi-447451256"
    )
    assert result["linkedin_url"] == (
        "https://www.linkedin.com/in/saad-rizvi-447451256"
    )
    assert "linkedin_url" not in profile_suggestions(
        "https://linkedin.com/in/CHANGE-ME"
    )


def test_pdf_annotation_profile_links_are_included_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Annotation:
        def get_object(self) -> dict[str, dict[str, str]]:
            return {"/A": {"/URI": "https://linkedin.com/in/ada"}}

    class Page:
        def extract_text(self) -> str:
            return "Ada Lovelace\nBackend Engineer\nada@example.test"

        def get(self, key: str) -> list[Annotation] | None:
            return [Annotation()] if key == "/Annots" else None

    monkeypatch.setattr(
        resume_module,
        "PdfReader",
        lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[Page()]),
    )

    text = extract_pdf_text(b"%PDF-fake", max_bytes=100)
    assert "https://linkedin.com/in/ada" in text
    assert profile_suggestions(text)["linkedin_url"] == "https://linkedin.com/in/ada"
