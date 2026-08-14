from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.saas import hunter
from app.saas.hunter import HunterProviderError


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def test_validation_uses_header_auth_and_returns_only_bounded_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "hunter_super_secret_key"
    captured: dict[str, object] = {}

    def get(url: str, **kwargs: object) -> FakeResponse:
        captured.update(url=url, **kwargs)
        return FakeResponse(
            200,
            {
                "data": {
                    "first_name": "Must not escape",
                    "email": "owner@example.test",
                    "plan_name": "G" * 150,
                    "reset_date": "2026-09-01",
                    "requests": {
                        "credits": {"used": 2.5, "available": 50.0, "remaining": 47.5},
                        "searches": {"used": 7, "available": 50},
                        "verifications": {
                            "used": -2,
                            "available": 10**20,
                            "remaining": float("inf"),
                            "private": "no",
                        },
                        "unknown_bucket": {"used": 1},
                    },
                    "calls": {"used": 99},
                    "api_key": key,
                },
                "meta": {"secret": key},
            },
        )

    monkeypatch.setattr(hunter.requests, "get", get)

    result = hunter.validate_hunter_key(key)

    assert result == {
        "valid": True,
        "status": "ready",
        "quota": {
            "plan_name": "G" * 100,
            "reset_date": "2026-09-01",
            "requests": {
                "credits": {"used": 2.5, "available": 50, "remaining": 47.5},
                "searches": {"used": 7, "available": 50, "remaining": 43},
                "verifications": {
                    "used": 0,
                    "available": 1_000_000_000_000,
                    "remaining": 1_000_000_000_000,
                },
            },
        },
    }
    assert captured["url"] == "https://api.hunter.io/v2/account"
    assert parse_qs(urlsplit(str(captured["url"])).query) == {}
    assert "params" not in captured
    assert captured["headers"] == {"X-API-Key": key, "Accept": "application/json"}
    assert captured["timeout"] == hunter.DEFAULT_TIMEOUT
    assert key not in str(result)


@pytest.mark.parametrize(
    ("status_code", "expected_status"),
    [
        (401, "hunter_invalid_key"),
        (403, "hunter_forbidden"),
        (429, "hunter_quota_exhausted"),
        (503, "hunter_unavailable"),
    ],
)
def test_validation_returns_secret_free_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_status: str,
) -> None:
    key = "hunter_provider_must_not_echo_this"
    monkeypatch.setattr(
        hunter.requests,
        "get",
        lambda *_args, **_kwargs: FakeResponse(status_code, {"errors": [{"details": key}]}),
    )

    result = hunter.validate_hunter_key(key)

    assert result["valid"] is False
    assert result["status"] == expected_status
    assert key not in str(result)


def test_validation_rejects_oversized_key_without_request_or_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "k" * 513

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Hunter must not be called")

    monkeypatch.setattr(hunter.requests, "get", unexpected)
    result = hunter.validate_hunter_key(key)

    assert result == {
        "valid": False,
        "status": "hunter_invalid_key",
        "message": "The Hunter API key is invalid.",
    }
    assert key not in str(result)


def test_domain_search_is_hr_only_bounded_and_strictly_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "hunter_transient_header_key"
    captured: dict[str, object] = {}
    raw_emails = [
        {
            "value": "ADA@EXAMPLE.COM",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "position": "Recruiting " + ("lead " * 100),
            "confidence": 97.5,
            "verification": {"date": "private", "status": "valid"},
            "department": "hr",
            "phone_number": "+1-secret",
            "linkedin": "https://example.test/private",
            "sources": [{"uri": "https://example.test/private"}],
        },
        {
            "value": "grace@example.com",
            "first_name": "Grace",
            "last_name": "Hopper",
            "position": "People Ops",
            "confidence": 500,
            "verification": {"status": "provider-new-status"},
            "unexpected": "must not escape",
        },
        {"value": "not-an-email", "first_name": "Skip"},
        {
            "value": "third@example.com",
            "first_name": "Third",
            "position": "Recruiter",
            "confidence": 80,
            "verification": {"status": "accept_all"},
        },
    ]

    def get(url: str, **kwargs: object) -> FakeResponse:
        captured.update(url=url, **kwargs)
        return FakeResponse(
            200,
            {
                "data": {
                    "domain": "Example.COM",
                    "organization": "Private provider field",
                    "emails": raw_emails,
                },
                "meta": {"results": 500},
            },
        )

    monkeypatch.setattr(hunter.requests, "get", get)

    result = hunter.search_hunter_contacts(
        key,
        domain="Example.COM",
        company="ignored because domain wins",
        limit=2,
    )

    assert captured["url"] == "https://api.hunter.io/v2/domain-search"
    assert key not in str(captured["url"])
    assert captured["params"] == {"domain": "example.com", "department": "hr", "limit": 2}
    assert "api_key" not in captured["params"]  # type: ignore[operator]
    assert captured["headers"] == {"X-API-Key": key, "Accept": "application/json"}
    assert result["domain"] == "example.com"
    assert len(result["contacts"]) == 2
    assert result["contacts"][0] == {
        "email": "ada@example.com",
        "name": "Ada Lovelace",
        "position": ("Recruiting " + ("lead " * 100)).strip()[:240],
        "confidence": 97.5,
        "verification_status": "valid",
        "domain": "example.com",
    }
    assert result["contacts"][1] == {
        "email": "grace@example.com",
        "name": "Grace Hopper",
        "position": "People Ops",
        "confidence": 100,
        "verification_status": "unknown",
        "domain": "example.com",
    }
    assert all(
        set(contact)
        == {"email", "name", "position", "confidence", "verification_status", "domain"}
        for contact in result["contacts"]
    )
    assert key not in str(result)


def test_company_search_uses_resolved_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def get(_url: str, **kwargs: object) -> FakeResponse:
        captured.update(**kwargs)
        return FakeResponse(
            200,
            {
                "data": {
                    "domain": "acme.example",
                    "emails": [
                        {
                            "value": "recruiter@acme.example",
                            "name": "Recruiting Team",
                            "position": "Recruiter",
                            "confidence": 71,
                            "verification": {"status": "accept_all"},
                        }
                    ],
                }
            },
        )

    monkeypatch.setattr(hunter.requests, "get", get)
    result = hunter.search_hunter_contacts("hunter_key", company=" Acme Inc. ", limit=1)

    assert captured["params"] == {"company": "Acme Inc.", "department": "hr", "limit": 1}
    assert result == {
        "domain": "acme.example",
        "contacts": [
            {
                "email": "recruiter@acme.example",
                "name": "Recruiting Team",
                "position": "Recruiter",
                "confidence": 71,
                "verification_status": "accept_all",
                "domain": "acme.example",
            }
        ],
    }


@pytest.mark.parametrize("limit", [0, 11, True, 1.5])
def test_search_rejects_limits_outside_one_to_ten(
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
) -> None:
    monkeypatch.setattr(
        hunter.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected request")),
    )

    with pytest.raises(HunterProviderError) as error:
        hunter.search_hunter_contacts("hunter_key", domain="example.com", limit=limit)  # type: ignore[arg-type]

    assert error.value.code == "hunter_invalid_request"


@pytest.mark.parametrize(
    ("provider_error", "expected_code"),
    [
        (hunter.requests.Timeout("contains hunter_secret"), "hunter_timeout"),
        (hunter.requests.ConnectionError("contains hunter_secret"), "hunter_unavailable"),
    ],
)
def test_search_redacts_network_errors(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception,
    expected_code: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise provider_error

    monkeypatch.setattr(hunter.requests, "get", fail)
    with pytest.raises(HunterProviderError) as error:
        hunter.search_hunter_contacts("hunter_secret", domain="example.com")

    assert error.value.code == expected_code
    assert "hunter_secret" not in str(error.value)
    assert error.value.__cause__ is None
