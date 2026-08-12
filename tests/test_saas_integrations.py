from __future__ import annotations

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
