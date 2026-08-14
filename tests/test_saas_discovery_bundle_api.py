from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.saas_main as saas_main
from app.saas.groq import GroqProviderError
from app.saas_main import create_app
from tests.test_saas_api import (
    OTHER_USER_ID,
    USER_ID,
    FakeAuth,
    FakeStore,
    configured_settings,
)


LINKEDIN_JOB_ID = "33333333-3333-4333-8333-333333333333"
FEED_JOB_ID = "44444444-4444-4444-8444-444444444444"
GROQ_KEY = "gsk_test_browser_only_secret"
RESUME_TEXT = "PRIVATE RESUME: built confidential ledger infrastructure."
PROFILE_CANARY = "private-profile@example.test"


def _resume_tables(
    *,
    include_resume: bool = True,
    parse_status: str = "parsed",
) -> dict[str, list[dict[str, Any]]]:
    resumes = [
        {
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "user_id": str(OTHER_USER_ID),
            "is_active": True,
            "parse_status": "parsed",
            "parsed_text": "OTHER TENANT RESUME",
        }
    ]
    if include_resume:
        resumes.append(
            {
                "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "user_id": str(USER_ID),
                "is_active": True,
                "parse_status": parse_status,
                "parsed_text": RESUME_TEXT if parse_status == "parsed" else None,
            }
        )
    return {
        "resumes": resumes,
        "profiles": [
            {
                "user_id": str(OTHER_USER_ID),
                "skills": ["OtherTenantSkill"],
                "email": "other-tenant@example.test",
                "location": "Other Tenant City",
            },
            {
                "user_id": str(USER_ID),
                "skills": ["Kubernetes"],
                "preferences": {},
                "email": PROFILE_CANARY,
                "location": "Bengaluru",
            },
        ],
        "discovery_preferences": [
            {
                "user_id": str(OTHER_USER_ID),
                "locations": ["Other Tenant City"],
            },
            {"user_id": str(USER_ID), "locations": ["Pune"]},
        ],
    }


def _record_user_fetches(
    store: FakeStore,
) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    original_one = store.client.fetch_one
    original_many = store.client.fetch_many

    async def fetch_one(table: str, **kwargs: Any) -> dict[str, Any] | None:
        calls.append(("one", table, deepcopy(kwargs)))
        return await original_one(table, **kwargs)

    async def fetch_many(table: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(("many", table, deepcopy(kwargs)))
        return await original_many(table, **kwargs)

    store.client.fetch_one = fetch_one  # type: ignore[method-assign]
    store.client.fetch_many = fetch_many  # type: ignore[method-assign]
    return calls


def test_resume_guided_discovery_derives_and_queues_a_bounded_private_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis_calls: list[tuple[str, str, str]] = []

    def analyze(key: str, model: str, resume_text: str) -> dict[str, Any]:
        analysis_calls.append((key, model, resume_text))
        return {
            "target_roles": [
                "Backend Engineer",
                "backend engineer",
                "Platform Engineer",
            ],
            "skills": ["Python", "python", "FastAPI"],
            "email": "model-private@example.test",
        }

    monkeypatch.setattr(saas_main, "analyze_resume_profile", analyze)

    def enqueue(params: dict[str, Any]) -> list[dict[str, Any]]:
        linkedin = params["kind_input"] == "discover_linkedin_guest"
        return [
            {
                "id": LINKEDIN_JOB_ID if linkedin else FEED_JOB_ID,
                "kind": params["kind_input"],
                "provider": params["provider_input"],
                "status": "queued",
                "idempotency_key": params["idempotency_key_input"],
                # The API must not trust the RPC's projection to be browser-safe.
                "user_id": str(USER_ID),
                "payload": deepcopy(params["payload_input"]),
            }
        ]

    store = FakeStore(
        _resume_tables(),
        rpc_results={
            "reserve_groq_request": True,
            "enqueue_automation_job": enqueue,
        },
    )
    fetch_calls = _record_user_fetches(store)
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/resume-guided",
        headers={"X-Groq-Api-Key": GROQ_KEY},
        json={
            "location": "Hyderabad",
            "remote_only": True,
            "linkedin_limit": 25,
            "feed_limit": 120,
            "idempotency_key": "resume-bundle-0001",
        },
    )

    assert response.status_code == 202, response.text
    assert analysis_calls == [
        (GROQ_KEY, configured_settings().groq_model, RESUME_TEXT)
    ]
    data = response.json()["data"]
    assert data["plan"] == {
        "roles": ["Backend Engineer", "Platform Engineer"],
        "keywords": ["Python", "FastAPI", "Kubernetes"],
        "search_terms": [
            "Backend Engineer",
            "Platform Engineer",
            "Python",
            "FastAPI",
            "Kubernetes",
        ],
        "linkedin_query": "Backend Engineer",
        "location": "Hyderabad",
        "remote_only": True,
        "sources": ["linkedin_guest", "telegram", "rss"],
    }
    assert len(data["plan"]["linkedin_query"]) <= 100
    assert len(data["plan"]["search_terms"]) <= 20
    assert all(len(term) <= 100 for term in data["plan"]["search_terms"])
    assert data["automation_jobs"] == [
        {
            "id": LINKEDIN_JOB_ID,
            "kind": "discover_linkedin_guest",
            "provider": "linkedin",
            "status": "queued",
            "idempotency_key": "resume-bundle-0001:linkedin",
        },
        {
            "id": FEED_JOB_ID,
            "kind": "discover_public_feeds",
            "provider": "public_feeds",
            "status": "queued",
            "idempotency_key": "resume-bundle-0001:feeds",
        },
    ]

    assert store.rpc_calls == [
        ("reserve_groq_request", {"operation_input": "generate"}),
        (
            "enqueue_automation_job",
            {
                "kind_input": "discover_linkedin_guest",
                "provider_input": "linkedin",
                "application_id_input": None,
                "payload_input": {
                    "keywords": "Backend Engineer",
                    "location": "Hyderabad",
                    "remote": True,
                    "limit": 25,
                },
                "idempotency_key_input": "resume-bundle-0001:linkedin",
            },
        ),
        (
            "enqueue_automation_job",
            {
                "kind_input": "discover_public_feeds",
                "provider_input": "public_feeds",
                "application_id_input": None,
                "payload_input": {
                    "source_ids": ["telegram", "rss"],
                    "limit": 120,
                    "search_terms": data["plan"]["search_terms"],
                },
                "idempotency_key_input": "resume-bundle-0001:feeds",
            },
        ),
    ]
    owned_reads = [
        (kind, table, call["filters"])
        for kind, table, call in fetch_calls
        if kind == "one"
    ]
    # Authentication performs its own owned profile read before the route runs.
    assert ("one", "profiles", {"user_id": str(USER_ID)}) in owned_reads
    assert owned_reads[-3:] == [
        ("one", "resumes", {"user_id": str(USER_ID), "is_active": True}),
        ("one", "profiles", {"user_id": str(USER_ID)}),
        ("one", "discovery_preferences", {"user_id": str(USER_ID)}),
    ]

    serialized_response = response.text
    for private_value in (
        GROQ_KEY,
        RESUME_TEXT,
        PROFILE_CANARY,
        "model-private@example.test",
        str(USER_ID),
        str(OTHER_USER_ID),
    ):
        assert private_value not in serialized_response
    queue_json = json.dumps(store.rpc_calls[1:], default=str)
    assert GROQ_KEY not in queue_json
    assert RESUME_TEXT not in queue_json
    assert PROFILE_CANARY not in queue_json
    assert "user_id" not in queue_json


def test_resume_guided_discovery_requires_a_browser_supplied_groq_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        saas_main,
        "analyze_resume_profile",
        lambda *_args: pytest.fail("Groq must not be called without a key"),
    )
    store = FakeStore(_resume_tables())

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/resume-guided",
        json={"idempotency_key": "resume-bundle-0002"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "groq_key_required"
    assert store.rpc_calls == []


@pytest.mark.parametrize(
    ("include_resume", "parse_status"),
    [(False, "parsed"), (True, "uploaded")],
)
def test_resume_guided_discovery_requires_an_active_parsed_resume(
    monkeypatch: pytest.MonkeyPatch,
    include_resume: bool,
    parse_status: str,
) -> None:
    monkeypatch.setattr(
        saas_main,
        "analyze_resume_profile",
        lambda *_args: pytest.fail("Groq must not be called for an unavailable résumé"),
    )
    store = FakeStore(
        _resume_tables(include_resume=include_resume, parse_status=parse_status)
    )

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/resume-guided",
        headers={"X-Groq-Api-Key": GROQ_KEY},
        json={"idempotency_key": "resume-bundle-0003"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "resume_not_parsed"
    assert store.rpc_calls == []
    assert GROQ_KEY not in response.text


def test_resume_guided_discovery_maps_provider_failure_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: Any) -> dict[str, Any]:
        raise GroqProviderError("groq_unavailable", "Provider detail without secrets.")

    monkeypatch.setattr(saas_main, "analyze_resume_profile", unavailable)
    store = FakeStore(
        _resume_tables(), rpc_results={"reserve_groq_request": True}
    )

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/resume-guided",
        headers={"X-Groq-Api-Key": GROQ_KEY},
        json={"idempotency_key": "resume-bundle-0004"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "groq_unavailable"
    assert store.rpc_calls == [
        ("reserve_groq_request", {"operation_input": "generate"})
    ]
    assert GROQ_KEY not in response.text
    assert RESUME_TEXT not in response.text
    assert str(USER_ID) not in response.text


def test_google_form_queue_is_tenant_scoped_deduplicated_and_links_latest_ats_application() -> None:
    direct_job_id = "55555555-5555-4555-8555-555555555555"
    parent_job_id = "66666666-6666-4666-8666-666666666666"
    other_job_id = "77777777-7777-4777-8777-777777777777"
    direct_url = "https://docs.google.com/forms/d/e/direct-form/viewform"
    metadata_url = "https://forms.gle/metadataForm1"
    other_url = "https://forms.gle/otherTenantForm"
    latest_application_id = "88888888-8888-4888-8888-888888888888"

    store = FakeStore(
        {
            "jobs": [
                {
                    "id": direct_job_id,
                    "user_id": str(USER_ID),
                    "source": "referral_digest",
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "location": "Remote",
                    "apply_url": direct_url,
                    "metadata": {"provider": "google_forms"},
                    "created_at": "2026-08-12T08:00:00Z",
                },
                {
                    "id": parent_job_id,
                    "user_id": str(USER_ID),
                    "source": "telegram",
                    "title": "Platform Engineer",
                    "company": "Beta",
                    "location": "Pune",
                    "apply_url": "https://example.test/job-description",
                    "metadata": {
                        "discovered_urls": [direct_url, metadata_url],
                        "nested": {"form_url": metadata_url},
                    },
                    "created_at": "2026-08-13T08:00:00Z",
                },
                {
                    "id": other_job_id,
                    "user_id": str(OTHER_USER_ID),
                    "source": "telegram",
                    "title": "Other Tenant Secret Role",
                    "company": "Other Tenant Secret Company",
                    "location": "Secret City",
                    "apply_url": other_url,
                    "metadata": {"form_url": "https://forms.gle/anotherOtherForm"},
                    "created_at": "2026-08-14T08:00:00Z",
                },
            ],
            "applications": [
                {
                    "id": "99999999-9999-4999-8999-999999999999",
                    "user_id": str(USER_ID),
                    "job_id": direct_job_id,
                    "channel": "ats",
                    "status": "manual",
                    "created_at": "2026-08-12T09:00:00Z",
                },
                {
                    "id": latest_application_id,
                    "user_id": str(USER_ID),
                    "job_id": direct_job_id,
                    "channel": "ats",
                    "status": "drafted",
                    "created_at": "2026-08-13T09:00:00Z",
                },
                {
                    "id": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa",
                    "user_id": str(OTHER_USER_ID),
                    "job_id": other_job_id,
                    "channel": "ats",
                    "status": "applied",
                    "created_at": "2026-08-14T09:00:00Z",
                },
                {
                    "id": "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb",
                    "user_id": str(USER_ID),
                    "job_id": direct_job_id,
                    "channel": "email",
                    "status": "approved",
                    "created_at": "2026-08-15T09:00:00Z",
                },
            ],
        }
    )
    fetch_calls = _record_user_fetches(store)
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    response = client.get("/api/v1/discovery/google-forms?limit=100&offset=0")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"items", "count", "total", "has_more"}
    assert payload["count"] == 2
    assert payload["total"] == 2
    assert payload["has_more"] is False
    assert len(payload["items"]) == 2
    assert all(
        set(item)
        == {
            "id",
            "job_id",
            "parent_job_id",
            "title",
            "company",
            "location",
            "source",
            "created_at",
            "apply_url",
            "saved",
            "application",
        }
        for item in payload["items"]
    )

    by_url = {item["apply_url"]: item for item in payload["items"]}
    assert set(by_url) == {direct_url, metadata_url}
    direct = by_url[direct_url]
    assert direct["id"] == f"job:{direct_job_id}"
    assert direct["job_id"] == direct_job_id
    assert direct["parent_job_id"] == direct_job_id
    assert direct["saved"] is True
    assert {
        key: direct["application"][key]
        for key in ("id", "status", "channel", "created_at")
    } == {
        "id": latest_application_id,
        "status": "drafted",
        "channel": "ats",
        "created_at": "2026-08-13T09:00:00Z",
    }

    metadata_only = by_url[metadata_url]
    assert metadata_only["id"].startswith("form:")
    assert metadata_only["job_id"] is None
    assert metadata_only["parent_job_id"] == parent_job_id
    assert metadata_only["saved"] is False
    assert metadata_only["application"] is None

    # The same normalized direct URL in metadata is not a second queue item.
    assert sum(item["apply_url"] == direct_url for item in payload["items"]) == 1
    assert str(OTHER_USER_ID) not in response.text
    assert other_url not in response.text
    assert "Other Tenant Secret" not in response.text
    route_list_reads = [
        (table, call["filters"])
        for kind, table, call in fetch_calls
        if kind == "many" and table in {"jobs", "applications"}
    ]
    assert route_list_reads == [
        ("jobs", {"user_id": str(USER_ID)}),
        ("applications", {"user_id": str(USER_ID)}),
    ]

    repeated = client.get("/api/v1/discovery/google-forms?limit=100&offset=0")
    assert repeated.status_code == 200
    assert {
        item["apply_url"]: item["id"] for item in repeated.json()["items"]
    } == {item["apply_url"]: item["id"] for item in payload["items"]}

    page = client.get("/api/v1/discovery/google-forms?limit=1&offset=1")
    assert page.status_code == 200
    assert page.json()["count"] == 1
    assert page.json()["total"] == 2
    assert page.json()["has_more"] is False
