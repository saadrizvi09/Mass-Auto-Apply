from __future__ import annotations

from email import policy
from email.parser import BytesParser

import pytest

from app.saas.gmail import GoogleProviderError, build_gmail_mime
from app.saas.providers import browser_provider_allowed, get_provider, provider_catalog


def test_linkedin_cannot_be_enabled_by_allowlist() -> None:
    linkedin = get_provider(
        "linkedin",
        "linkedin,greenhouse",
        google_configured=True,
        browserbase_configured=True,
    )
    assert linkedin == {
        "id": "linkedin",
        "label": "LinkedIn",
        "mode": "partner_required",
        "available": False,
        "can_connect": False,
        "can_auto_apply": False,
        "can_scan": True,
        "can_prefill": False,
        "requires_review": True,
        "connection_required": False,
        "reason": "Bounded public job discovery is available; LinkedIn Easy Apply is not connected.",
    }
    assert browser_provider_allowed("linkedin", "linkedin") is False


def test_only_configured_and_allowlisted_managed_browser_is_available() -> None:
    catalog = provider_catalog(
        "greenhouse",
        google_configured=False,
        browserbase_configured=True,
    )
    by_id = {item["id"]: item for item in catalog}
    assert by_id["gmail"]["available"] is False
    assert by_id["greenhouse"]["available"] is True
    assert by_id["greenhouse"]["can_connect"] is False
    assert by_id["greenhouse"]["can_scan"] is True
    assert by_id["greenhouse"]["can_prefill"] is True
    assert by_id["greenhouse"]["can_auto_apply"] is True
    assert by_id["greenhouse"]["requires_review"] is True
    assert by_id["lever"]["available"] is False
    assert by_id["yc"]["mode"] == "managed_browser"
    assert by_id["yc"]["available"] is False
    assert "ziprecruiter" not in by_id


def test_connection_only_providers_never_claim_form_automation() -> None:
    catalog = provider_catalog(
        "yc,cutshort,instahyre,wellfound,google_forms",
        google_configured=False,
        browserbase_configured=True,
    )
    by_id = {item["id"]: item for item in catalog}

    for provider_id in ("yc", "cutshort", "instahyre"):
        capability = by_id[provider_id]
        assert capability["available"] is True
        assert capability["can_connect"] is True
        assert capability["can_scan"] is False
        assert capability["can_prefill"] is False
        assert capability["can_auto_apply"] is False
        assert "multi-step state machine" in capability["reason"]

    assert by_id["wellfound"]["can_connect"] is True
    assert by_id["wellfound"]["can_scan"] is True
    assert by_id["google_forms"]["can_connect"] is True
    assert by_id["google_forms"]["connection_required"] is False
    assert by_id["google_forms"]["can_scan"] is True
    assert by_id["google_forms"]["can_prefill"] is True
    assert by_id["google_forms"]["can_auto_apply"] is True
    assert "without a connection" in by_id["google_forms"]["reason"]
    assert "signed-in file upload" in by_id["google_forms"]["reason"]
    assert "explicit review" in by_id["google_forms"]["reason"]


def test_google_forms_optional_login_is_not_exposed_until_browserbase_is_ready() -> None:
    google_forms = get_provider(
        "google_forms",
        "google_forms",
        google_configured=False,
        browserbase_configured=False,
    )

    assert google_forms is not None
    assert google_forms["available"] is False
    assert google_forms["can_connect"] is False
    assert google_forms["connection_required"] is False


def test_explicit_company_form_is_review_gated_without_a_saved_login() -> None:
    company_form = get_provider(
        "company_form",
        "company_form",
        google_configured=False,
        browserbase_configured=True,
    )

    assert company_form is not None
    assert company_form["available"] is True
    assert company_form["can_connect"] is False
    assert company_form["connection_required"] is False
    assert company_form["can_scan"] is True
    assert company_form["can_prefill"] is True
    assert company_form["can_auto_apply"] is True
    assert company_form["requires_review"] is True


def test_gmail_mime_has_pdf_attachment_and_safe_headers() -> None:
    raw = build_gmail_mime(
        "recruiter@example.test",
        "Application — Backend Engineer",
        "Hello,\n\nPlease find my résumé attached.",
        sender="candidate@example.test",
        pdf_bytes=b"%PDF-1.4\nmock",
        pdf_filename="Ada Resume.pdf",
    )
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message["To"] == "recruiter@example.test"
    assert message["Subject"] == "Application — Backend Engineer"
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Ada Resume.pdf"
    assert attachments[0].get_content_type() == "application/pdf"


def test_gmail_mime_rejects_header_injection() -> None:
    with pytest.raises(GoogleProviderError) as error:
        build_gmail_mime(
            "victim@example.test\nBcc: attacker@example.test",
            "Application",
            "Body",
        )
    assert error.value.code in {"gmail_invalid_message", "gmail_invalid_recipient"}
