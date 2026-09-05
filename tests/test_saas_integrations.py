from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.saas import groq
from app.saas.browser import BrowserbaseClient, BrowserbaseError
from app.saas.gmail import (
    DEFAULT_GOOGLE_SCOPES,
    GoogleProviderError,
    build_google_authorization_url,
    refresh_google_access_token,
    send_gmail_message,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"" if status_code == 204 else b"{}"

    def json(self) -> object:
        return self._payload


def test_google_authorization_uses_web_flow_and_send_only_scope() -> None:
    url = build_google_authorization_url(
        "client-id",
        "https://app.example.test/api/v1/oauth/google/callback",
        "single-use-state",
    )
    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["state"] == ["single-use-state"]
    assert set(query["scope"][0].split()) == set(DEFAULT_GOOGLE_SCOPES)
    assert not any("gmail.read" in scope for scope in query["scope"][0].split())


def test_groq_validation_never_reflects_key(monkeypatch: pytest.MonkeyPatch) -> None:
    user_key = "gsk_super_secret_value"
    monkeypatch.setattr(
        groq.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(401, {"error": user_key}),
    )
    result = groq.validate_groq_key(user_key, "openai/gpt-oss-120b")
    assert result["valid"] is False
    assert result["status"] == "groq_invalid_key"
    assert user_key not in str(result)


def test_groq_validation_distinguishes_blocked_model_from_invalid_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(403, {"error": "model blocked"}),
    )

    result = groq.validate_groq_key("gsk_valid_but_restricted", "openai/gpt-oss-120b")

    assert result == {
        "valid": False,
        "status": "groq_model_forbidden",
        "message": (
            "This Groq project does not allow the configured model. Enable it in "
            "Groq Model Permissions or use a key from a project that allows it."
        ),
    }


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_message"),
    [
        (
            404,
            "groq_model_unavailable",
            "The selected Groq model is unavailable.",
        ),
        (
            429,
            "groq_rate_limited",
            "Groq is rate limiting this API key. Try again later.",
        ),
    ],
)
def test_groq_validation_preserves_actionable_provider_status(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_status: str,
    expected_message: str,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code, {"error": "redacted"}),
    )

    result = groq.validate_groq_key("gsk_secret", "openai/gpt-oss-120b")

    assert result == {
        "valid": False,
        "status": expected_status,
        "message": expected_message,
    }


def test_groq_validation_reports_network_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise groq.requests.RequestException("provider detail")

    monkeypatch.setattr(groq.requests, "get", fail)

    result = groq.validate_groq_key("gsk_secret", "openai/gpt-oss-120b")

    assert result == {
        "valid": False,
        "status": "unavailable",
        "message": "Groq could not be reached. Try again later.",
    }


def test_groq_validation_lists_models_without_putting_slash_id_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def get(url: str, **kwargs: object) -> FakeResponse:
        captured.update(url=url, **kwargs)
        return FakeResponse(
            200,
            {
                "object": "list",
                "data": [
                    {"id": "llama-3.1-8b-instant", "active": True},
                    {"id": "openai/gpt-oss-120b", "active": True},
                ],
            },
        )

    monkeypatch.setattr(groq.requests, "get", get)

    result = groq.validate_groq_key("gsk_secret", "openai/gpt-oss-120b")

    assert result == {
        "valid": True,
        "status": "ready",
        "model": "openai/gpt-oss-120b",
    }
    assert captured["url"] == "https://api.groq.com/openai/v1/models"
    assert "gpt-oss" not in str(captured["url"])


def test_groq_validation_reports_valid_key_with_blocked_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            {"object": "list", "data": [{"id": "llama-3.1-8b-instant", "active": True}]},
        ),
    )

    result = groq.validate_groq_key("gsk_valid_key", "openai/gpt-oss-120b")

    assert result == {
        "valid": False,
        "status": "groq_model_forbidden",
        "message": (
            "This Groq key is valid, but its project does not allow the configured "
            "model. Enable it in Groq Model Permissions."
        ),
    }


def test_groq_form_suggestions_exclude_sensitive_and_unknown_question_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"answers":{"years":5,"gender":"x","injected":"bad"}}'
                        }
                    }
                ]
            },
        ),
    )
    result = groq.generate_form_answer_suggestions(
        "gsk_test_key",
        "openai/gpt-oss-120b",
        {"years_experience": 5},
        {"title": "Engineer"},
        "Five years of Python experience.",
        [
            {"key": "years", "label": "Years of experience", "type": "number"},
            {"key": "gender", "label": "Gender", "type": "select"},
        ],
    )
    assert result == {"years": 5}


def test_form_suggestions_map_passout_dropdown_and_public_resume_without_groq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_request(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Structured profile facts must not require a model request")

    monkeypatch.setattr(groq.requests, "post", unexpected_request)
    result = groq.generate_form_answer_suggestions(
        "gsk_test_key",
        "openai/gpt-oss-120b",
        {
            "full_name": "Saad Rizvi",
            "email": "candidate@example.com",
            "phone": "+91 9999999999",
            "college": "Jamia Millia Islamia University",
            "graduation_year": 2026,
            "resume_url": "https://drive.google.com/file/d/resume-id/view?usp=sharing",
        },
        {"title": "Product Intern"},
        "Candidate graduates in 2026.",
        [
            {"key": "field_1", "label": "Full Name", "type": "text", "required": True},
            {"key": "field_2", "label": "Email Address", "type": "email", "required": True},
            {"key": "field_3", "label": "Phone Number", "type": "tel", "required": True},
            {"key": "field_4", "label": "College / University", "type": "text", "required": True},
            {
                "key": "field_5",
                "label": "Graduation Year",
                "type": "listbox",
                "required": True,
                "options": ["2025", "2026", "2027", "2028"],
            },
            {
                "key": "field_6",
                "label": "Resume Link",
                "type": "url",
                "required": True,
                "options": [],
            },
        ],
    )
    assert result == {
        "field_1": "Saad Rizvi",
        "field_2": "candidate@example.com",
        "field_3": "+91 9999999999",
        "field_4": "Jamia Millia Islamia University",
        "field_5": "2026",
        "field_6": "https://drive.google.com/file/d/resume-id/view?usp=sharing",
    }


def test_profile_form_answers_is_available_without_a_groq_request() -> None:
    assert groq.profile_form_answers(
        {
            "graduation_year": 2026,
            "resume_url": "https://drive.google.com/file/d/resume-id/view?usp=sharing",
        },
        [
            {
                "key": "year",
                "label": "Graduation Year",
                "type": "listbox",
                "options": ["2025", "2026", "2027"],
            },
            {"key": "cv_url", "label": "Resume Link", "type": "url"},
        ],
    ) == {
        "year": "2026",
        "cv_url": "https://drive.google.com/file/d/resume-id/view?usp=sharing",
    }


def test_form_suggestions_do_not_guess_missing_dropdown_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"answers":{}}'}}]},
        ),
    )
    result = groq.generate_form_answer_suggestions(
        "gsk_test_key",
        "openai/gpt-oss-120b",
        {"graduation_year": 2026},
        {"title": "Intern"},
        "Graduating in 2026.",
        [
            {
                "key": "grad",
                "label": "Graduation Year",
                "type": "listbox",
                "required": True,
                "options": ["2027", "2028"],
            }
        ],
    )
    assert result == {}


def test_groq_resume_analysis_is_strictly_allowlisted_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"full_name":"Ada Lovelace","email":"ada@example.test",'
                                '"graduation_year":2026,"years_experience":2.5,'
                                '"linkedin_url":"https://linkedin.com/in/ada",'
                                '"github_url":"javascript:alert(1)",'
                                '"skills":["Python","Python","SQL"],'
                                '"target_roles":["Backend engineer"],'
                                '"protected_trait":"must not escape"}'
                            )
                        }
                    }
                ]
            },
        ),
    )

    result = groq.analyze_resume_profile(
        "gsk_test_key",
        "openai/gpt-oss-120b",
        "Ada explicitly lists Python and SQL experience.",
    )

    assert result == {
        "full_name": "Ada Lovelace",
        "email": "ada@example.test",
        "linkedin_url": "https://linkedin.com/in/ada",
        "years_experience": 2.5,
        "graduation_year": 2026,
        "skills": ["Python", "SQL"],
        "target_roles": ["Backend engineer"],
    }
    assert "protected_trait" not in result
    assert "github_url" not in result


def test_groq_resume_analysis_discards_placeholder_profile_urls() -> None:
    result = groq._clean_resume_analysis(  # noqa: SLF001 - boundary regression test
        {
            "linkedin_url": "https://linkedin.com/in/CHANGE-ME",
            "github_url": "https://github.com/ada",
        }
    )
    assert result == {"github_url": "https://github.com/ada"}


def test_groq_resume_analysis_replaces_llm_experience_with_deterministic_work_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        groq.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            {"choices": [{"message": {"content": '{"years_experience":4.8}'}}]},
        ),
    )

    result = groq.analyze_resume_profile(
        "gsk_test_key",
        "openai/gpt-oss-120b",
        "Final-year B.Tech student\nEducation: 2022 - 2026\n"
        "Experience\nASIC Design Intern\n2023 - 2024\n"
        "Academic Project\n2024 - 2025",
    )

    assert result["years_experience"] == 1.0


def test_refresh_preserves_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.saas import gmail

    monkeypatch.setattr(
        gmail.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(
            200,
            {"access_token": "new-access", "expires_in": 3600, "token_type": "Bearer"},
        ),
    )
    result = refresh_google_access_token("existing-refresh", "client", "secret")
    assert result["refresh_token"] == "existing-refresh"
    assert result["access_token"] == "new-access"


def test_google_errors_do_not_include_provider_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.saas import gmail

    provider_secret = "provider-response-secret"
    monkeypatch.setattr(
        gmail.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse(400, {"error_description": provider_secret}),
    )
    with pytest.raises(GoogleProviderError) as error:
        refresh_google_access_token("refresh", "client", "secret")
    assert provider_secret not in str(error.value)


@pytest.mark.parametrize(
    "response",
    [FakeResponse(503, {"error": "backend failure"}), FakeResponse(200, {})],
)
def test_uncertain_gmail_send_outcomes_are_never_safe_to_retry(
    monkeypatch: pytest.MonkeyPatch, response: FakeResponse
) -> None:
    from app.saas import gmail

    monkeypatch.setattr(gmail.requests, "post", lambda *_args, **_kwargs: response)
    with pytest.raises(GoogleProviderError) as error:
        send_gmail_message(
            "access-token",
            "recruiter@example.test",
            "Application",
            "Message body",
        )
    assert error.value.code == "gmail_send_ambiguous"


def test_gmail_connection_reset_after_dispatch_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.saas import gmail

    def fail(*_args: object, **_kwargs: object) -> None:
        raise gmail.requests.ConnectionError("provider detail")

    monkeypatch.setattr(gmail.requests, "post", fail)
    with pytest.raises(GoogleProviderError) as error:
        send_gmail_message(
            "access-token",
            "recruiter@example.test",
            "Application",
            "Message body",
        )
    assert error.value.code == "gmail_send_ambiguous"


class FakeBrowserHttp:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse(
            200,
            {
                "id": "session-1",
                "status": "RUNNING",
                "connectUrl": "wss://secret.example.test/devtools?token=secret",
            },
        )
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def request(self, *args: object, **kwargs: object) -> FakeResponse:
        self.calls.append((args, kwargs))
        return self.response


def test_browserbase_project_validation_is_read_only_and_allowlisted() -> None:
    http = FakeBrowserHttp(
        FakeResponse(
            200,
            {
                "id": "project-1",
                "name": "Tenant project",
                "concurrency": 3,
                "defaultTimeout": 120,
                "privateAccountField": "not returned",
            },
        )
    )
    client = BrowserbaseClient("api-secret", "project-1", http=http)

    result = client.validate_project()

    assert result == {
        "valid": True,
        "status": "ready",
        "project_name": "Tenant project",
        "concurrency": 3,
        "default_timeout": 120,
    }
    assert "project_id" not in result
    assert "project-1" not in json.dumps(result)
    assert http.calls[0][0][:2] == (
        "GET",
        "https://api.browserbase.com/v1/projects/project-1",
    )
    assert http.calls[0][1]["headers"]["X-BB-API-Key"] == "api-secret"
    assert "/sessions" not in str(http.calls)


def test_browser_adapter_allowlists_returned_session_fields() -> None:
    client = BrowserbaseClient("api-secret", "project-1", http=FakeBrowserHttp())
    result = client.create_session("context-1")
    assert result == {
        "id": "session-1",
        "context_id": "context-1",
        "status": "RUNNING",
    }
    assert "connectUrl" not in result


def test_browser_session_status_is_strictly_allowlisted() -> None:
    http = FakeBrowserHttp(
        FakeResponse(
            200,
            {
                "id": "session-1",
                "projectId": "project-1",
                "status": "COMPLETED",
                "contextId": "context-1",
                "expiresAt": "2026-08-09T12:00:00Z",
                "endedAt": "2026-08-09T11:59:00Z",
                "connectUrl": "wss://secret.example.test/devtools?token=secret",
                "seleniumRemoteUrl": "https://secret.example.test/selenium",
                "signingKey": "provider-signing-secret",
                "userMetadata": {"private": "value"},
                "proxyBytes": 123,
            },
        )
    )
    client = BrowserbaseClient("api-secret", "project-1", http=http)

    result = client.get_session("session-1")

    assert result == {
        "id": "session-1",
        "status": "COMPLETED",
        "context_id": "context-1",
        "expires_at": "2026-08-09T12:00:00Z",
        "ended_at": "2026-08-09T11:59:00Z",
    }
    assert set(result) == {"id", "status", "context_id", "expires_at", "ended_at"}
    assert http.calls[0][0][:2] == (
        "GET",
        "https://api.browserbase.com/v1/sessions/session-1",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", "different-session"),
        ("projectId", "different-project"),
        ("status", "SECRET"),
        ("status", {"provider": "malformed"}),
    ),
)
def test_browser_session_status_rejects_unbound_or_unknown_response(
    field: str, value: object
) -> None:
    payload = {
        "id": "session-1",
        "projectId": "project-1",
        "status": "RUNNING",
        field: value,
    }
    client = BrowserbaseClient(
        "api-secret", "project-1", http=FakeBrowserHttp(FakeResponse(200, payload))
    )

    with pytest.raises(BrowserbaseError) as error:
        client.get_session("session-1")

    assert error.value.code == "browserbase_invalid_response"


def test_browser_cleanup_reports_remote_resources_that_are_already_absent() -> None:
    http = FakeBrowserHttp(FakeResponse(404, {"provider": "detail"}))
    client = BrowserbaseClient("api-secret", "project-1", http=http)

    released = client.release_session("session-1")
    deleted = client.delete_context("context-1")

    assert released == {"id": "session-1", "released": True, "already_absent": True}
    assert deleted == {"id": "context-1", "deleted": True, "already_absent": True}


def test_browser_cleanup_does_not_hide_non_404_provider_errors() -> None:
    client = BrowserbaseClient(
        "api-secret", "project-1", http=FakeBrowserHttp(FakeResponse(403, {}))
    )

    with pytest.raises(BrowserbaseError) as error:
        client.delete_context("context-1")

    assert error.value.code == "browserbase_not_authorized"
    assert error.value.status_code == 403


def test_browser_adapter_rejects_empty_credentials() -> None:
    with pytest.raises(BrowserbaseError):
        BrowserbaseClient("", "project")
