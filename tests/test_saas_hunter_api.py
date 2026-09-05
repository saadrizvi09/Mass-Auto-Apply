from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import app.saas.hunter as hunter
import app.saas_main as saas_main
from app.saas_main import create_app
from tests.test_saas_api import (
    OTHER_USER_ID,
    USER_ID,
    FakeAuth,
    FakeStore,
    configured_settings,
)


# Hunter was removed from the hosted API. The provider adapter remains covered
# by tests/test_saas_hunter.py as legacy compatibility code, while this old API
# contract is intentionally retired.
pytestmark = pytest.mark.skip(reason="Hunter API removed from the active hosted app")


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def test_hunter_validation_keeps_header_key_transient_and_returns_bounded_account_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "hunter_browser_held_secret"
    captured: dict[str, object] = {}

    def get(url: str, **kwargs: object) -> FakeResponse:
        captured.update(url=url, **kwargs)
        return FakeResponse(
            200,
            {
                "data": {
                    "first_name": "Private",
                    "last_name": "Account",
                    "email": "private@example.test",
                    "plan_name": "Starter",
                    "reset_date": "2026-09-01",
                    "requests": {
                        "credits": {"used": 4, "available": 50, "remaining": 46},
                        "searches": {"used": 2, "available": 25, "remaining": 23},
                    },
                    "api_key": key,
                },
                "meta": {"provider_echo": key},
            },
        )

    monkeypatch.setattr(hunter.requests, "get", get)
    store = FakeStore()
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post("/api/v1/hunter/validate", headers={"X-Hunter-Api-Key": key})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "valid": True,
        "status": "ready",
        "quota": {
            "plan_name": "Starter",
            "reset_date": "2026-09-01",
            "requests": {
                "credits": {"used": 4, "available": 50, "remaining": 46},
                "searches": {"used": 2, "available": 25, "remaining": 23},
            },
        },
    }
    assert captured["url"] == "https://api.hunter.io/v2/account"
    assert parse_qs(urlsplit(str(captured["url"])).query) == {}
    assert "params" not in captured
    assert captured["headers"] == {"X-API-Key": key, "Accept": "application/json"}
    assert captured["timeout"] == hunter.DEFAULT_TIMEOUT
    assert key not in response.text
    assert key not in str(store.client.tables)
    assert key not in str(store.rpc_calls)


def test_hunter_contact_search_is_tenant_bound_hr_only_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = str(uuid4())
    key = "hunter_search_secret"
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "company": "Acme Labs",
                }
            ]
        }
    )
    captured: dict[str, object] = {}

    def get(url: str, **kwargs: object) -> FakeResponse:
        captured.update(url=url, **kwargs)
        return FakeResponse(
            200,
            {
                "data": {
                    "domain": "acme.example",
                    "organization": "Provider-only field",
                    "emails": [
                        {
                            "value": f"person{index}@acme.example",
                            "first_name": f"Person{index}",
                            "last_name": "Recruiter",
                            "position": "Talent Partner " + ("x" * 400),
                            "confidence": 95 - index,
                            "verification": {"status": "valid", "date": "private"},
                            "department": "hr",
                            "phone_number": "private",
                            "sources": [{"uri": "https://private.example.test"}],
                        }
                        for index in range(8)
                    ],
                },
                "meta": {"results": 1000},
            },
        )

    monkeypatch.setattr(hunter.requests, "get", get)
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/jobs/{job_id}/contacts/hunter?limit=5",
        headers={"X-Hunter-Api-Key": key},
    )

    assert response.status_code == 200, response.text
    assert captured["url"] == "https://api.hunter.io/v2/domain-search"
    assert key not in str(captured["url"])
    assert captured["params"] == {
        "company": "Acme Labs",
        "department": "hr",
        "limit": 5,
    }
    assert "api_key" not in captured["params"]  # type: ignore[operator]
    assert captured["headers"] == {"X-API-Key": key, "Accept": "application/json"}
    data = response.json()["data"]
    assert data["job_id"] == job_id
    assert data["company"] == "Acme Labs"
    assert data["domain"] == "acme.example"
    assert len(data["contacts"]) == 5
    assert all(
        set(contact)
        == {"email", "name", "position", "confidence", "verification_status", "domain"}
        for contact in data["contacts"]
    )
    assert all(len(contact["position"]) <= 240 for contact in data["contacts"])
    assert key not in response.text
    assert key not in str(store.client.tables)
    assert key not in str(store.rpc_calls)


def test_hunter_contact_search_cannot_read_another_tenants_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = str(uuid4())
    key = "hunter_cross_tenant_secret"
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(OTHER_USER_ID),
                    "company": "Other Tenant Company",
                }
            ]
        }
    )
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        provider_calls.append((args, kwargs))
        return {"domain": "should-not-run.example", "contacts": []}

    monkeypatch.setattr(saas_main, "search_hunter_contacts", search)
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/jobs/{job_id}/contacts/hunter?limit=5",
        headers={"X-Hunter-Api-Key": key},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert provider_calls == []
    assert key not in response.text


@pytest.mark.parametrize(
    ("provider_failure", "expected_http", "expected_code"),
    [
        (FakeResponse(401, {"errors": [{"details": "provider secret"}]}), 400, "hunter_invalid_key"),
        (FakeResponse(403, {"errors": [{"details": "provider secret"}]}), 403, "hunter_forbidden"),
        (FakeResponse(429, {"errors": [{"details": "provider secret"}]}), 429, "hunter_quota_exhausted"),
        (FakeResponse(503, {"errors": [{"details": "provider secret"}]}), 503, "hunter_unavailable"),
        (hunter.requests.ConnectionError("provider secret"), 503, "hunter_unavailable"),
    ],
)
def test_hunter_contact_errors_are_mapped_without_provider_or_key_echo(
    monkeypatch: pytest.MonkeyPatch,
    provider_failure: FakeResponse | Exception,
    expected_http: int,
    expected_code: str,
) -> None:
    job_id = str(uuid4())
    key = "hunter_error_path_secret"
    store = FakeStore(
        {
            "jobs": [
                {"id": job_id, "user_id": str(USER_ID), "company": "Acme"}
            ]
        }
    )

    def get(*_args: object, **_kwargs: object) -> FakeResponse:
        if isinstance(provider_failure, Exception):
            raise provider_failure
        return provider_failure

    monkeypatch.setattr(hunter.requests, "get", get)
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/jobs/{job_id}/contacts/hunter?limit=5",
        headers={"X-Hunter-Api-Key": key},
    )

    assert response.status_code == expected_http
    assert response.json()["error"]["code"] == expected_code
    assert len(response.json()["error"]["message"]) <= 300
    assert key not in response.text
    assert "provider secret" not in response.text
    assert response.headers["cache-control"] == "private, no-store"


def test_hunter_api_rejects_missing_key_and_out_of_range_limit_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = str(uuid4())
    store = FakeStore(
        {"jobs": [{"id": job_id, "user_id": str(USER_ID), "company": "Acme"}]}
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Hunter must not be called")

    monkeypatch.setattr(hunter.requests, "get", unexpected)
    app = create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    client = TestClient(app)

    missing = client.post(f"/api/v1/jobs/{job_id}/contacts/hunter?limit=5")
    invalid_limit = client.post(
        f"/api/v1/jobs/{job_id}/contacts/hunter?limit=11",
        headers={"X-Hunter-Api-Key": "hunter_key"},
    )

    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "hunter_missing_key"
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["error"]["code"] == "request_invalid"
