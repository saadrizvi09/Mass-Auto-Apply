from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.saas.schemas import (
    ApplicationFormApprovalRequest,
    AutomationJobCreate,
    DraftApplicationRequest,
    GoogleOAuthClientUpsert,
    GoogleOAuthStartRequest,
    JobCreate,
    ProfileUpdate,
    PublicAtsDiscoveryRequest,
    ResumeGuidedDiscoveryRequest,
    ResumeRegister,
    SendApplicationBatchRequest,
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


def test_profile_urls_reject_template_placeholders() -> None:
    with pytest.raises(ValidationError, match="actual profile URL"):
        ProfileUpdate(linkedin_url="https://linkedin.com/in/CHANGE-ME")
    assert ProfileUpdate(
        linkedin_url="https://www.linkedin.com/in/saad-rizvi-447451256"
    ).linkedin_url == "https://www.linkedin.com/in/saad-rizvi-447451256"


def test_public_resume_url_requires_real_https_url() -> None:
    url = "https://drive.google.com/file/d/resume-id/view?usp=sharing"
    assert ProfileUpdate(resume_url=url).resume_url == url
    with pytest.raises(ValidationError, match="must use https"):
        ProfileUpdate(resume_url="http://example.test/resume.pdf")
    with pytest.raises(ValidationError, match="actual public resume URL"):
        ProfileUpdate(resume_url="https://example.test/CHANGE-ME")


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
    assert UserSettingsUpdate(daily_send_cap=150).daily_send_cap == 150
    with pytest.raises(ValidationError):
        UserSettingsUpdate(daily_send_cap=151)
    with pytest.raises(ValidationError):
        SendApplicationRequest(idempotency_key="short")


def test_email_batch_selection_is_unique_and_capped_at_thirty() -> None:
    ids = [f"11111111-1111-4111-8111-{index:012d}" for index in range(30)]
    request = SendApplicationBatchRequest(
        application_ids=ids,
        idempotency_key="batch-request-123",
    )
    assert len(request.application_ids) == 30
    with pytest.raises(ValidationError):
        SendApplicationBatchRequest(
            application_ids=ids + ["22222222-2222-4222-8222-222222222222"],
            idempotency_key="batch-request-123",
        )
    with pytest.raises(ValidationError):
        SendApplicationBatchRequest(
            application_ids=[ids[0], ids[0]],
            idempotency_key="batch-request-123",
        )


def test_draft_request_accepts_only_an_email_recipient() -> None:
    assert DraftApplicationRequest(recipient="recruiting@example.com").recipient == "recruiting@example.com"
    with pytest.raises(ValidationError):
        DraftApplicationRequest(recipient="not-an-email")
    with pytest.raises(ValidationError):
        DraftApplicationRequest(recipient="recruiting@example.com", unexpected=True)


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


def test_resume_guided_discovery_request_is_strict_and_bounded() -> None:
    request = ResumeGuidedDiscoveryRequest(idempotency_key="resume-search-001")

    assert request.location is None
    assert request.remote_only is False
    assert request.linkedin_limit == 20
    assert request.feed_limit == 60

    with pytest.raises(ValidationError):
        ResumeGuidedDiscoveryRequest(
            idempotency_key="resume-search-002",
            user_id="00000000-0000-0000-0000-000000000001",
        )
    with pytest.raises(ValidationError):
        ResumeGuidedDiscoveryRequest(
            idempotency_key="resume-search-003",
            resume_id="00000000-0000-0000-0000-000000000001",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("location", "x" * 121),
        ("linkedin_limit", 26),
        ("feed_limit", 201),
        ("idempotency_key", "x" * 181),
    ],
)
def test_resume_guided_discovery_request_rejects_oversized_values(
    field: str, value: object
) -> None:
    payload: dict[str, object] = {"idempotency_key": "resume-search-004"}
    payload[field] = value
    with pytest.raises(ValidationError):
        ResumeGuidedDiscoveryRequest(**payload)


def test_url_normalization_is_stable_without_fetching() -> None:
    assert (
        normalized_http_url("HTTPS://EXAMPLE.COM//jobs/123/?utm_source=x")
        == "https://example.com/jobs/123?utm_source=x"
    )
    assert normalized_http_url("file:///etc/passwd") is None
