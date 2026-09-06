from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.saas.public_contacts import public_contact_candidates
from app.saas_main import create_app
from tests.test_saas_api import FakeAuth, FakeStore, USER_ID, configured_settings


def test_public_contact_candidates_only_extract_owned_record_text_and_mark_it_unverified() -> None:
    candidates = public_contact_candidates(
        {
            "source": "rss",
            "contact_email": "careers@acme.example",
            "description": "Hiring contact: recruiter@acme.example. Ignore user@example.",
        }
    )

    assert [candidate["email"] for candidate in candidates] == [
        "careers@acme.example",
        "recruiter@acme.example",
    ]
    assert all(
        candidate["verification_status"] == "public_source_unverified"
        for candidate in candidates
    )
    assert all(candidate["confidence"] is None for candidate in candidates)


def test_public_contact_endpoint_is_tenant_bound_and_has_no_verification_side_effect() -> None:
    job_id = str(uuid4())
    secret_text = "private description marker"
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "source": "referral_digest",
                    "company": "Acme",
                    "contact_email": "careers@acme.example",
                    "description": f"Apply with careers@acme.example. {secret_text}",
                }
            ]
        }
    )
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    response = client.post(f"/api/v1/jobs/{job_id}/contacts/public")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["job_id"] == job_id
    assert [contact["email"] for contact in data["contacts"]] == [
        "careers@acme.example"
    ]
    assert data["contacts"][0]["source"] == "saved contact field"
    assert data["contacts"][0]["verification_status"] == "public_source_unverified"
    assert data["verification"]["status"] == "syntax_only"
    assert "No email was sent" in data["verification"]["message"]
    assert secret_text not in response.text
    assert store.rpc_calls == []


def test_imported_public_source_status_is_preserved_for_review() -> None:
    candidates = public_contact_candidates(
        {
            "contact_email": "recruiter@acme.example",
            "metadata": {
                "contact_source": "company team page",
                "contact_source_url": "https://acme.example/team",
                "email_verification_status": "public_source_verified",
            },
        }
    )

    assert candidates[0]["verification_status"] == "public_source_verified"
    assert candidates[0]["source_url"] == "https://acme.example/team"


def test_public_contact_endpoint_cannot_read_another_tenants_job() -> None:
    job_id = str(uuid4())
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": "22222222-2222-4222-8222-222222222222",
                    "company": "Other tenant",
                    "contact_email": "private@other.example",
                }
            ]
        }
    )
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    response = client.post(f"/api/v1/jobs/{job_id}/contacts/public")

    assert response.status_code == 404
    assert "private@other.example" not in response.text


def test_public_contact_discovery_is_queued_as_one_bounded_batch() -> None:
    first_job_id = str(uuid4())
    second_job_id = str(uuid4())
    queue_id = str(uuid4())
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": first_job_id,
                    "user_id": str(USER_ID),
                    "company": "Acme",
                    "apply_url": "https://acme.test/jobs/one",
                },
                {
                    "id": second_job_id,
                    "user_id": str(USER_ID),
                    "company": "Beta",
                    "apply_url": "https://beta.test/jobs/two",
                },
            ],
            "job_contacts": [
                {
                    "job_id": first_job_id,
                    "user_id": str(USER_ID),
                    "email": "recruiting@acme.test",
                    "person_name": "Alex Recruiter",
                    "email_verification_status": "public_source_verified",
                }
            ],
        },
        rpc_results={
            "enqueue_public_contact_discovery": {
                "id": queue_id,
                "kind": "discover_public_contacts",
                "provider": "public_contacts",
                "status": "queued",
            }
        },
    )
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    response = client.post(
        "/api/v1/contacts/discover",
        json={
            "job_ids": [first_job_id, second_job_id],
            "max_contacts_per_job": 20,
            "max_pages_per_job": 6,
            "timeout_seconds": 30,
            "idempotency_key": "contact-batch-123",
        },
    )

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["automation_job"]["id"] == queue_id
    assert data["contacts"][first_job_id][0]["email"] == "recruiting@acme.test"
    calls = [
        (name, params)
        for name, params in store.rpc_calls
        if name == "enqueue_public_contact_discovery"
    ]
    assert len(calls) == 1
    assert calls[0][1]["job_ids_input"] == [first_job_id, second_job_id]
    assert calls[0][1]["max_contacts_input"] == 20


def test_public_contact_get_includes_persisted_evidence_and_legacy_fallback() -> None:
    job_id = str(uuid4())
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "company": "Acme",
                    "contact_email": "old@acme.test",
                }
            ],
            "job_contacts": [
                {
                    "job_id": job_id,
                    "user_id": str(USER_ID),
                    "email": "new@acme.test",
                    "person_name": "Taylor Hiring",
                    "person_title": "Recruiter",
                    "email_verification_status": "public_source_verified",
                    "source_url": "https://acme.test/team",
                }
            ],
        }
    )
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    response = client.get(f"/api/v1/jobs/{job_id}/contacts/public")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [item["email"] for item in data["contacts"]] == [
        "new@acme.test",
        "old@acme.test",
    ]
    assert data["contacts"][0]["name"] == "Taylor Hiring"
    assert data["verification"]["status"] == "source_evidence"
