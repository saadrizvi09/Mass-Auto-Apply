from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.saas.schemas import (
    ApplicationFormApprovalRequest,
    AutomationJobCreate,
    GoogleOAuthClientUpsert,
    GoogleOAuthStartRequest,
    JobCreate,
    ProfileUpdate,
    PublicAtsDiscoveryRequest,
    ResumeRegister,
    SendApplicationRequest,
    UserSettingsUpdate,
    normalized_http_url,
)


def test_tenant_identity_cannot_be_supplied_by_client() -> None:
    with pytest.raises(ValidationError):
        ProfileUpdate(full_name="Ada", user_id="00000000-0000-0000-0000-000000000001")


def test_profile_education_facts_are_bounded() -> None:
    profile = ProfileUpdate(
        college="Example Institute",
        degree="B.Tech in Computer Science",
        graduation_year=2026,
    )
    assert profile.graduation_year == 2026
    with pytest.raises(ValidationError):
        ProfileUpdate(graduation_year=1949)
    with pytest.raises(ValidationError):
        ProfileUpdate(graduation_year=2101)


def test_google_oauth_client_is_strict_and_secret_is_redacted_from_repr() -> None:
    credentials = GoogleOAuthClientUpsert(
        client_id="123456789-example.apps.googleusercontent.com",
        client_secret="GOCSPX-example-secret",
    )
    assert credentials.client_id.endswith(".apps.googleusercontent.com")
    assert "GOCSPX-example-secret" not in repr(credentials)
    assert GoogleOAuthStartRequest().credential_source == "platform"
    assert GoogleOAuthStartRequest(credential_source="user").credential_source == "user"

    for invalid_id in (
        "not-a-web-client",
        "client.apps.googleusercontent.com\n",
        "https://client.apps.googleusercontent.com",
    ):
        with pytest.raises(ValidationError):
            GoogleOAuthClientUpsert(
                client_id=invalid_id,
                client_secret="GOCSPX-example-secret",
            )
    with pytest.raises(ValidationError):
        GoogleOAuthClientUpsert(
            client_id="123456789-example.apps.googleusercontent.com",
            client_secret="secret with spaces",
        )
    with pytest.raises(ValidationError):
        GoogleOAuthClientUpsert(
            client_id="123456789-example.apps.googleusercontent.com",
            client_secret="GOCSPX-example-secret",
            user_id="00000000-0000-0000-0000-000000000001",
        )


@pytest.mark.parametrize(
    "path",
    ["/owner/resume.pdf", "owner/../victim/resume.pdf", r"owner\resume.pdf"],
)
def test_resume_storage_path_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ResumeRegister(
            storage_path=path,
            original_name="resume.pdf",
            mime_type="application/pdf",
            size_bytes=100,
        )


def test_resume_metadata_is_bounded_and_pdf_only() -> None:
    with pytest.raises(ValidationError):
        ResumeRegister(
            storage_path="owner/resume.txt",
            original_name="resume.txt",
            mime_type="text/plain",
            size_bytes=100,
        )
    with pytest.raises(ValidationError):
        ResumeRegister(
            storage_path="owner/resume.pdf",
            original_name="resume.pdf",
            size_bytes=6_291_457,
        )


def test_job_requires_real_http_url_and_description() -> None:
    with pytest.raises(ValidationError):
        JobCreate(
            apply_url="javascript:alert(1)",
            title="Engineer",
            company="Example",
            description="A sufficiently detailed job description.",
        )


def test_user_send_cap_and_idempotency_are_bounded() -> None:
    assert UserSettingsUpdate(daily_send_cap=25).daily_send_cap == 25
    with pytest.raises(ValidationError):
        UserSettingsUpdate(daily_send_cap=26)
    with pytest.raises(ValidationError):
        SendApplicationRequest(idempotency_key="short")


def test_worker_kind_is_allowlisted() -> None:
    with pytest.raises(ValidationError):
        AutomationJobCreate(kind="linkedin_auto_apply", idempotency_key="request-123")

    assert (
        AutomationJobCreate(
            kind="application_submit",
            provider="greenhouse",
            idempotency_key="submit-request-123",
        ).kind
        == "application_submit"
    )


def test_form_approval_requires_an_exact_sha256_schema_hash() -> None:
    approved = ApplicationFormApprovalRequest(
        expected_revision=1,
        schema_hash="a" * 64,
        answers={"authorized": True},
    )
    assert approved.expected_revision == 1
    with pytest.raises(ValidationError):
        ApplicationFormApprovalRequest(
            expected_revision=1,
            schema_hash="not-a-hash",
            answers={},
        )


def test_public_ats_intake_rejects_non_http_urls() -> None:
    with pytest.raises(ValidationError):
        PublicAtsDiscoveryRequest(urls=["file:///etc/passwd"])


def test_url_normalization_is_stable_without_fetching() -> None:
    assert (
        normalized_http_url("HTTPS://EXAMPLE.COM//jobs/123/?utm_source=x")
        == "https://example.com/jobs/123?utm_source=x"
    )
    assert normalized_http_url("file:///etc/passwd") is None
