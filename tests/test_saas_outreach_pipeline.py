from __future__ import annotations

from fastapi.testclient import TestClient

from app.saas_main import create_app
from tests.test_saas_api import FakeAuth, FakeStore, USER_ID, configured_settings


def test_active_provider_catalog_has_no_hunter_and_research_prompt_is_resume_bound() -> None:
    store = FakeStore(
        {
            "profiles": [
                {
                    "user_id": str(USER_ID),
                    "full_name": "Ada Lovelace",
                    "location": "London",
                    "skills": ["Python"],
                    "preferences": {"target_roles": ["Backend Engineer"]},
                }
            ],
            "resumes": [
                {
                    "user_id": str(USER_ID),
                    "is_active": True,
                    "parse_status": "parsed",
                    "parsed_text": "Backend Engineer\nJan 2020 - Mar 2022\nPython",
                }
            ],
        }
    )
    client = TestClient(create_app(settings=configured_settings(), auth=FakeAuth(), store=store))

    catalog = client.get("/api/v1/provider-credentials")
    assert catalog.status_code == 200
    assert all(item.get("provider") != "hunter" for item in catalog.json().get("items", []))

    prompt = client.post("/api/v1/outreach/research-prompt", json={})
    assert prompt.status_code == 200, prompt.text
    data = prompt.json()["data"]
    assert data["estimated_years_experience"] == 2.3
    assert "job_url" in data["workbook_columns"]
    assert "Do not guess an email pattern" in data["prompt"]
    assert "mailbox" in data["prompt"]
    assert "100 distinct, usable email leads" in data["prompt"]
    assert "does NOT have to be an HR person" in data["prompt"]
    assert "never include more than 4 rows for the same normalized company" in data["prompt"]
    assert "MANDATORY SEARCH EXPANSION ORDER" in data["prompt"]
    assert "BEGIN_RESUME_REFERENCE" in data["prompt"]
    assert "canonical phone value" in data["prompt"]
    assert "public_source_verified" in data["prompt"]
    assert "public_source_unverified" in data["prompt"]
    assert "faithful, concise paraphrase" in data["prompt"]
    assert "education enrollment range" in data["prompt"]
    assert "contact_source_url" in data["workbook_columns"]
    assert "email_verification_status" in data["workbook_columns"]


def test_single_send_compatibility_route_only_enqueues_the_persistent_worker() -> None:
    application_id = "33333333-3333-4333-8333-333333333333"
    queue_id = "44444444-4444-4444-8444-444444444444"
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={
            "enqueue_email_send": [
                {"id": queue_id, "kind": "send_email", "status": "queued"}
            ]
        },
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/applications/{application_id}/send",
        json={"idempotency_key": "compat-send-123", "attach_resume": True},
    )

    assert response.status_code == 202, response.text
    assert response.json()["queued"] is True
    assert response.json()["worker"] == "persistent"
    assert [name for name, _params in store.rpc_calls] == ["enqueue_email_send"]
