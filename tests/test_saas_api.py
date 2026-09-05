from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from cryptography.fernet import Fernet
import pytest

import app.saas_main as saas_main
from app.saas.auth import AuthUser
from app.saas.config import Settings
from app.saas.crypto import TokenCipher
from app.saas.errors import ApiError
from app.saas.gmail import GMAIL_SEND_SCOPE, GoogleProviderError
from app.saas_main import create_app


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_USER_ID = UUID("22222222-2222-4222-8222-222222222222")


def _browserbase_fingerprint(project_id: str) -> str:
    return hashlib.sha256(
        b"autoapply.browserbase.project.v1\x00" + project_id.encode("utf-8")
    ).hexdigest()


class FakeAuth:
    def __init__(self, last_sign_in_at: datetime | None = None) -> None:
        self.last_sign_in_at = last_sign_in_at or datetime.now(UTC)

    async def __call__(self) -> AuthUser:
        return AuthUser(
            user_id=USER_ID,
            email="owner@example.test",
            access_token="verified-user-jwt",
            last_sign_in_at=self.last_sign_in_at,
            user_metadata={"full_name": "Owner"},
        )


class FakeStoreClient:
    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        mode: str = "user",
        rpc_results: dict[str, Any] | None = None,
        rpc_calls: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        self.tables = tables
        self.mode = mode
        self.last_insert: tuple[str, dict[str, Any]] | None = None
        self.rpc_results = rpc_results if rpc_results is not None else {}
        self.rpc_calls = rpc_calls if rpc_calls is not None else []
        self.deleted_auth_users: list[tuple[UUID, bool]] = []

    @staticmethod
    def _matches(row: dict[str, Any], filters: dict[str, Any] | None) -> bool:
        if not filters:
            return True
        return all(row.get(key) == value for key, value in filters.items())

    async def fetch_many(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        limit: int | None = 100,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        del columns
        rows = [deepcopy(row) for row in self.tables.get(table, []) if self._matches(row, filters)]
        if order == "created_at.desc":
            rows.reverse()
        start = offset or 0
        return rows[start : None if limit is None else start + limit]

    async def fetch_one(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
        required: bool = False,
    ) -> dict[str, Any] | None:
        rows = await self.fetch_many(table, columns=columns, filters=filters, limit=1)
        if rows:
            return rows[0]
        if required:
            from app.saas.errors import ApiError

            raise ApiError(404, "not_found", "The requested resource was not found.")
        return None

    async def insert(
        self, table: str, values: dict[str, Any], *, returning: bool = True
    ) -> list[dict[str, Any]] | None:
        row = deepcopy(values)
        row.setdefault("id", str(uuid4()))
        self.tables.setdefault(table, []).append(row)
        self.last_insert = (table, deepcopy(row))
        return [deepcopy(row)] if returning else None

    async def upsert(
        self,
        table: str,
        values: dict[str, Any],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
        returning: bool = True,
    ) -> list[dict[str, Any]] | None:
        del ignore_duplicates
        keys = (on_conflict or "id").split(",")
        for row in self.tables.setdefault(table, []):
            if all(row.get(key) == values.get(key) for key in keys):
                row.update(deepcopy(values))
                return [deepcopy(row)] if returning else None
        return await self.insert(table, values, returning=returning)

    async def update(
        self,
        table: str,
        values: dict[str, Any],
        *,
        filters: dict[str, Any],
        returning: bool = True,
    ) -> list[dict[str, Any]] | None:
        changed = []
        for row in self.tables.get(table, []):
            if self._matches(row, filters):
                row.update(deepcopy(values))
                changed.append(deepcopy(row))
        return changed if returning else None

    async def delete(
        self, table: str, *, filters: dict[str, Any], returning: bool = True
    ) -> list[dict[str, Any]] | None:
        removed = [row for row in self.tables.get(table, []) if self._matches(row, filters)]
        self.tables[table] = [row for row in self.tables.get(table, []) if not self._matches(row, filters)]
        return deepcopy(removed) if returning else None

    async def rpc(self, function: str, params: dict[str, Any] | None = None) -> Any:
        clean_params = deepcopy(params or {})
        self.rpc_calls.append((function, clean_params))
        if function == "get_browserbase_credential_state" and function not in self.rpc_results:
            result: Any = {"epoch": 0}
        else:
            result = self.rpc_results.get(function, [])
        if callable(result):
            result = result(clean_params)
        return deepcopy(result)

    async def object_exists(self, bucket: str, path: str) -> bool:
        del bucket, path
        return True

    async def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        del bucket, prefix, limit, offset
        return []

    async def delete_objects(self, bucket: str, paths: list[str]) -> list[dict[str, Any]]:
        del bucket
        return [{"name": path} for path in paths]

    async def delete_auth_user(
        self, user_id: UUID, *, should_soft_delete: bool = False
    ) -> None:
        self.deleted_auth_users.append((user_id, should_soft_delete))


class FakeStore:
    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        rpc_results: dict[str, Any] | None = None,
    ) -> None:
        shared_rpc_calls: list[tuple[str, dict[str, Any]]] = []
        self.client = FakeStoreClient(tables or {}, rpc_results=rpc_results, rpc_calls=shared_rpc_calls)
        self.server = FakeStoreClient(
            self.client.tables,
            mode="secret",
            rpc_results=rpc_results,
            rpc_calls=shared_rpc_calls,
        )
        self.rpc_calls = shared_rpc_calls
        self.received_token: str | None = None

    def user(self, token: str) -> FakeStoreClient:
        self.received_token = token
        return self.client

    def secret(self) -> FakeStoreClient:
        return self.server


def configured_settings() -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="publishable-key",
        supabase_secret_key="server-secret",
        site_url="https://app.example.test",
        groq_model="openai/gpt-oss-120b",
    )


def google_settings(encryption_key: str) -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="publishable-key",
        supabase_secret_key="server-secret",
        site_url="https://app.example.test",
        token_encryption_key=encryption_key,
        google_client_id="google-client",
        google_client_secret="google-secret",
        google_redirect_uri="https://app.example.test/api/v1/oauth/google/callback",
    )


def browser_settings(encryption_key: str) -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="publishable-key",
        supabase_secret_key="server-secret",
        site_url="https://app.example.test",
        token_encryption_key=encryption_key,
        browserbase_api_key="browser-secret",
        browserbase_project_id="browser-project",
        allowed_browser_providers=("greenhouse",),
    )


class FakeBrowserbase:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.session_options: list[dict[str, Any]] = []
        self.fail_context_deletes = 0

    def create_context(self) -> dict[str, str]:
        self.events.append(("create_context", "context-new"))
        return {"id": "context-new"}

    def create_session(self, context_id: str, **kwargs: Any) -> dict[str, str]:
        self.events.append(("create_session", context_id))
        self.session_options.append(dict(kwargs))
        return {
            "id": "session-new",
            "context_id": context_id,
            "status": "RUNNING",
            "expires_at": "2026-08-09T12:00:00Z",
        }

    def get_session_live_view(self, session_id: str) -> dict[str, str]:
        self.events.append(("live_view", session_id))
        return {
            "session_id": session_id,
            "live_view_url": "https://browser.example.test/live/session-new",
        }

    def get_session(self, session_id: str) -> dict[str, str]:
        self.events.append(("get_session", session_id))
        return {
            "id": session_id,
            "context_id": "context-new",
            "status": "RUNNING",
        }

    def release_session(self, session_id: str) -> dict[str, Any]:
        self.events.append(("release_session", session_id))
        return {"id": session_id, "released": True}

    def delete_context(self, context_id: str) -> dict[str, Any]:
        self.events.append(("delete_context", context_id))
        if self.fail_context_deletes:
            self.fail_context_deletes -= 1
            from app.saas.browser import BrowserbaseError

            raise BrowserbaseError("browserbase_unavailable", "Browserbase is unavailable.")
        return {"id": context_id, "deleted": True}


def test_public_config_never_exposes_server_secrets() -> None:
    app = create_app(settings=configured_settings(), auth=FakeAuth(), store=FakeStore())
    response = TestClient(app).get("/api/v1/config")
    assert response.status_code == 200
    assert response.json()["supabase_publishable_key"] == "publishable-key"
    serialized = response.text
    assert "server-secret" not in serialized
    assert "token_encryption_key" not in serialized


def test_public_config_exposes_only_turnstile_site_key() -> None:
    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="publishable-key",
        supabase_secret_key="server-secret",
        site_url="https://app.example.test",
        turnstile_site_key="1x00000000000000000000AA",
    )
    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=FakeStore())
    ).get("/api/v1/config")
    assert response.status_code == 200
    assert response.json()["captcha"] == {
        "enabled": True,
        "provider": "turnstile",
        "site_key": "1x00000000000000000000AA",
    }
    assert "captcha_secret" not in response.text


def test_discovery_catalog_is_credential_free_and_excludes_ziprecruiter() -> None:
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=FakeStore())
    ).get("/api/v1/discovery/sources")
    assert response.status_code == 200, response.text
    sources = response.json()["items"]
    assert {item["id"] for item in sources} == {
        "telegram", "rss", "linkedin_guest", "referral_digest", "csv", "xlsx", "public_ats"
    }
    assert all(item["credential_required"] is False for item in sources)
    assert "ziprecruiter" not in response.text.lower()


def test_referral_ingestion_uses_tenant_derived_bulk_rpc() -> None:
    persisted = {
        "id": str(uuid4()),
        "source": "referral_digest",
        "title": "Backend Engineer",
        "company": "Acme",
    }
    store = FakeStore(
        rpc_results={
            "ingest_discovered_jobs": {
                "items": [persisted], "count": 1, "inserted": 1, "updated": 0
            }
        }
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/referrals",
        json={
            "text": (
                "Company: Acme\nRole: Backend Engineer\nLocation: Remote\n"
                "Apply: https://jobs.lever.co/acme/123\nBuild reliable platform services."
            )
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["count"] == 1
    call = next(call for call in store.rpc_calls if call[0] == "ingest_discovered_jobs")
    row = call[1]["jobs_input"][0]
    assert "user_id" not in row
    assert row["metadata"]["provider"] == "lever"
    assert row["metadata"]["discovered"] is True


def test_referral_digest_endpoint_returns_mixed_intake_summary() -> None:
    def persisted(params: dict[str, Any]) -> dict[str, Any]:
        rows = params["jobs_input"]
        return {
            "items": [{"id": str(uuid4()), **row} for row in rows],
            "count": len(rows),
            "inserted": len(rows),
            "updated": 0,
        }

    store = FakeStore(rpc_results={"ingest_discovered_jobs": persisted})
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/referrals",
        json={
            "text": (
                "Referral Alert\n"
                "1) Company - Acme\nRole - Platform Intern\n"
                "How to Apply: https://docs.google.com/forms/d/e/acme/viewform\n\n"
                "2) Company - Beta AI\nRole - ML Intern\n"
                "Email: careers@beta.example\nSubject: ML Intern\n\n"
                "For Free Hiring Updates Join:\n"
                "https://www.whatsapp.com/channel/example"
            )
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["summary"] == {
        "parsed": 2,
        "saved": 2,
        "google_forms": 1,
        "email_apply": 1,
        "ignored_promotional": 1,
    }
    rows = next(
        params["jobs_input"]
        for function, params in store.rpc_calls
        if function == "ingest_discovered_jobs"
    )
    assert rows[0]["apply_url"] == "https://docs.google.com/forms/d/e/acme/viewform"
    assert rows[1]["contact_email"] == "careers@beta.example"
    assert all("whatsapp.com" not in row["description"] for row in rows)


def test_csv_import_is_parsed_in_memory_and_bounded() -> None:
    store = FakeStore(
        rpc_results={
            "ingest_discovered_jobs": {
                "items": [{"id": str(uuid4())}], "count": 1, "inserted": 1, "updated": 0
            }
        }
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/import",
        files={
            "file": (
                "jobs.csv",
                b"title,company,description,apply_url\nEngineer,Acme,Build reliable public services,https://jobs.lever.co/acme/123\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["count"] == 1


def test_linkedin_guest_discovery_queues_only_bounded_public_search() -> None:
    queued_id = str(uuid4())
    store = FakeStore(
        rpc_results={"enqueue_automation_job": [{"id": queued_id, "status": "queued"}]}
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/linkedin",
        json={
            "keywords": "backend engineer",
            "location": "India",
            "remote_only": False,
            "limit": 20,
            "idempotency_key": "linkedin-test-0001",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["data"]["id"] == queued_id
    assert store.rpc_calls[-1] == (
        "enqueue_automation_job",
        {
            "kind_input": "discover_linkedin_guest",
            "provider_input": "linkedin",
            "application_id_input": None,
            "payload_input": {
                "keywords": "backend engineer",
                "location": "India",
                "remote": False,
                "limit": 20,
            },
            "idempotency_key_input": "linkedin-test-0001",
        },
    )


def test_public_ats_board_discovery_queues_only_canonical_official_boards() -> None:
    queued_id = str(uuid4())
    store = FakeStore(
        rpc_results={"enqueue_automation_job": [{"id": queued_id, "status": "queued"}]}
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/ats/boards",
        json={
            "urls": [
                "https://jobs.lever.co/acme/posting-id/apply",
                "https://boards.greenhouse.io/embed/job_board?for=beta_labs",
            ],
            "limit": 120,
            "idempotency_key": "public-ats-test-0001",
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["data"]["id"] == queued_id
    assert store.rpc_calls[-1] == (
        "enqueue_automation_job",
        {
            "kind_input": "discover_public_ats",
            "provider_input": "public_ats",
            "application_id_input": None,
            "payload_input": {
                "board_urls": [
                    "https://jobs.lever.co/acme",
                    "https://job-boards.greenhouse.io/beta_labs",
                ],
                "limit": 120,
            },
            "idempotency_key_input": "public-ats-test-0001",
        },
    )


def test_public_ats_board_discovery_rejects_non_provider_hosts() -> None:
    store = FakeStore()
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/discovery/ats/boards",
        json={
            "urls": ["https://example.com/jobs"],
            "limit": 100,
            "idempotency_key": "public-ats-test-0002",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "public_ats_board_invalid"
    assert store.rpc_calls == []


def test_privileged_discovery_kind_cannot_use_generic_queue() -> None:
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=FakeStore())
    ).post(
        "/api/v1/automation-jobs",
        json={
            "kind": "discover_linkedin_guest",
            "provider": "linkedin",
            "payload": {"keywords": "anything"},
            "idempotency_key": "generic-test-0001",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dedicated_workflow_required"


def test_public_ats_discovery_cannot_bypass_board_validation_via_generic_queue() -> None:
    store = FakeStore()
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/automation-jobs",
        json={
            "kind": "discover_public_ats",
            "provider": "public_ats",
            "payload": {"board_urls": ["https://example.com/private"]},
            "idempotency_key": "generic-ats-test-0001",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dedicated_workflow_required"
    assert store.rpc_calls == []


def test_managed_application_scan_creates_tenant_application_and_queues_scan() -> None:
    job_id = str(uuid4())
    queued_id = str(uuid4())
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "apply_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                    "title": "Engineer",
                    "company": "Acme",
                }
            ],
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                    "storage_path": f"{USER_ID}/resume-1.pdf",
                }
            ],
        },
        rpc_results={"enqueue_automation_job": [{"id": queued_id, "status": "queued"}]},
    )
    response = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    ).post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "scan-test-0001", "form_revision_id": None},
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["application"]["user_id"] == str(USER_ID)
    assert data["application"]["channel"] == "ats"
    assert data["automation_job"]["id"] == queued_id
    assert store.rpc_calls[-1][1]["payload_input"] == {"job_id": job_id}


def test_explicit_company_form_save_creates_service_binding_and_queues_scan() -> None:
    queued_id = str(uuid4())
    store = FakeStore(
        {
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                    "storage_path": f"{USER_ID}/resume.pdf",
                }
            ]
        },
        rpc_results={"enqueue_automation_job": [{"id": queued_id, "status": "queued"}]},
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("company_form",),
    )
    client = TestClient(create_app(settings=settings, auth=FakeAuth(), store=store))
    saved = client.post(
        "/api/v1/jobs",
        json={
            "title": "Platform Engineer",
            "company": "Acme",
            "description": "Build reliable platform systems for Acme customers.",
            "apply_url": "https://Careers.Acme.com/jobs/42?source=manual#apply",
            "metadata": {
                "application_provider": "company_form",
                "company_form_host": "evil.example",
            },
        },
    )
    assert saved.status_code == 201, saved.text
    job = saved.json()["data"]
    assert job["metadata"]["company_form_host"] == "careers.acme.com"
    assert store.client.tables["company_form_targets"] == [
        {
            "id": store.client.tables["company_form_targets"][0]["id"],
            "job_id": job["id"],
            "user_id": str(USER_ID),
            "source_url": "https://Careers.Acme.com/jobs/42?source=manual#apply",
            "target_url": "https://careers.acme.com/jobs/42?source=manual",
            "exact_host": "careers.acme.com",
        }
    ]

    response = client.post(
        f"/api/v1/jobs/{job['id']}/application/scan",
        json={"idempotency_key": "company-scan-test-0001"},
    )

    assert response.status_code == 202, response.text
    assert store.rpc_calls[-1][1]["provider_input"] == "company_form"
    assert store.rpc_calls[-1][1]["payload_input"] == {
        "job_id": job["id"],
        "company_form_host": "careers.acme.com",
        "company_form_target_url": "https://careers.acme.com/jobs/42?source=manual",
    }


def test_discovered_generic_url_without_service_binding_stays_manual() -> None:
    job_id = str(uuid4())
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "apply_url": "https://careers.acme.com/apply",
                    "metadata": {
                        "application_provider": "company_form",
                        "company_form_host": "careers.acme.com",
                    },
                }
            ],
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                }
            ],
        }
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("company_form",),
    )

    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "company-scan-test-0002"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "application_provider_unsupported"
    assert store.rpc_calls == []


@pytest.mark.parametrize(
    ("revision_url", "expected_status"),
    [
        ("https://careers.acme.com/application/42?stage=questions", 202),
        ("https://forms.evil.com/application/42", 409),
    ],
)
def test_company_form_revision_may_move_path_but_never_exact_host(
    revision_url: str, expected_status: int
) -> None:
    job_id = str(uuid4())
    application_id = str(uuid4())
    revision_id = str(uuid4())
    schema_hash = "d" * 64
    source_url = "https://careers.acme.com/jobs/42"
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "apply_url": source_url,
                }
            ],
            "company_form_targets": [
                {
                    "job_id": job_id,
                    "user_id": str(USER_ID),
                    "source_url": source_url,
                    "target_url": source_url,
                    "exact_host": "careers.acme.com",
                }
            ],
            "applications": [
                {
                    "id": application_id,
                    "user_id": str(USER_ID),
                    "job_id": job_id,
                    "channel": "ats",
                }
            ],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "job_id": job_id,
                    "provider": "company_form",
                    "form_url": revision_url,
                    "revision": 1,
                    "schema_hash": schema_hash,
                    "approved_revision": 1,
                    "approved_schema_hash": schema_hash,
                    "status": "approved",
                    "approved_at": "2026-08-13T10:00:00Z",
                    "question_schema": [
                        {"key": "email", "label": "Email", "required": True}
                    ],
                    "answers": {"email": "owner@example.test"},
                }
            ],
        },
        rpc_results={
            "enqueue_automation_job": [{"id": str(uuid4()), "status": "queued"}]
        },
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("company_form",),
    )

    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/application-form-revisions/{revision_id}/submit",
        json={
            "idempotency_key": "company-submit-test-0001",
            "form_revision_id": revision_id,
        },
    )

    assert response.status_code == expected_status, response.text
    if expected_status == 202:
        payload = store.rpc_calls[-1][1]["payload_input"]
        assert payload["company_form_host"] == "careers.acme.com"
        assert payload["company_form_target_url"] == revision_url
    else:
        assert response.json()["error"]["code"] == "company_form_target_changed"
        assert not any(call[0] == "enqueue_automation_job" for call in store.rpc_calls)


def test_ziprecruiter_url_is_not_a_supported_managed_application() -> None:
    job_id = str(uuid4())
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "apply_url": "https://www.ziprecruiter.com/jobs/acme/123",
                }
            ]
        }
    )
    response = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    ).post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "scan-test-zip01"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "application_provider_unsupported"


def test_unbound_yc_job_cannot_queue_application_automation() -> None:
    job_id = str(uuid4())
    settings = browser_settings(Fernet.generate_key().decode())
    settings = replace(
        settings,
        allowed_browser_providers=("yc",),
    )
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "apply_url": "https://www.workatastartup.com/jobs/12345",
                }
            ],
            "connections": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "provider": "yc",
                    "status": "connected",
                }
            ],
        }
    )

    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "scan-test-yc001"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "yc_exact_job_url_required"
    assert store.rpc_calls == []


def test_explicit_yc_job_save_canonicalizes_binds_and_queues_exact_scan() -> None:
    queued_id = str(uuid4())
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("yc",),
    )
    store = FakeStore(
        {
            "connections": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "provider": "yc",
                    "mode": "managed_browser",
                    "status": "connected",
                }
            ],
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                    "parse_status": "parsed",
                }
            ],
        },
        rpc_results={
            "enqueue_automation_job": [{"id": queued_id, "status": "queued"}]
        },
    )
    client = TestClient(create_app(settings=settings, auth=FakeAuth(), store=store))

    saved = client.post(
        "/api/v1/jobs",
        json={
            "title": "Founding Engineer",
            "company": "Acme",
            "description": "Build the first version of a carefully scoped product.",
            "apply_url": (
                "https://ycombinator.com/companies/acme/jobs/12345-founding-engineer"
                "?utm_source=shared#apply"
            ),
            "metadata": {
                "application_provider": "company_form",
                "yc_job_target_url": "https://attacker.example.test/job",
            },
        },
    )
    assert saved.status_code == 201, saved.text
    job = saved.json()["data"]
    expected_url = (
        "https://www.ycombinator.com/companies/acme/jobs/12345-founding-engineer"
    )
    assert job["apply_url"] == expected_url
    assert job["normalized_url"] == expected_url
    assert job["metadata"] == {
        "application_provider": "yc",
        "yc_job_target_url": expected_url,
    }
    assert store.client.tables["yc_application_targets"] == [
        {
            "id": store.client.tables["yc_application_targets"][0]["id"],
            "job_id": job["id"],
            "user_id": str(USER_ID),
            "target_url": expected_url,
        }
    ]
    assert store.client.tables.get("company_form_targets", []) == []

    scan = client.post(
        f"/api/v1/jobs/{job['id']}/application/scan",
        json={"idempotency_key": "yc-exact-scan-001"},
    )
    assert scan.status_code == 202, scan.text
    enqueue = next(call for call in store.rpc_calls if call[0] == "enqueue_automation_job")
    assert enqueue[1] == {
        "kind_input": "application_scan",
        "provider_input": "yc",
        "application_id_input": scan.json()["data"]["application_id"],
        "payload_input": {
            "job_id": job["id"],
            "yc_job_target_url": expected_url,
        },
        "idempotency_key_input": "yc-exact-scan-001",
    }


@pytest.mark.parametrize(
    ("apply_url", "expected_error"),
    [
        ("https://www.ycombinator.com/jobs", "application_provider_unsupported"),
        (
            "https://www.ycombinator.com/companies/acme",
            "application_provider_unsupported",
        ),
        (
            "https://account.ycombinator.com/authenticate",
            "application_provider_unsupported",
        ),
        (
            "https://subdomain.workatastartup.com/application",
            "application_provider_unsupported",
        ),
        ("https://www.workatastartup.com/jobs/12345", "yc_exact_job_url_required"),
    ],
)
def test_non_exact_yc_owned_url_never_becomes_generic_company_automation(
    apply_url: str, expected_error: str
) -> None:
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("company_form", "yc"),
    )
    store = FakeStore(
        {
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                }
            ]
        }
    )
    client = TestClient(create_app(settings=settings, auth=FakeAuth(), store=store))

    saved = client.post(
        "/api/v1/jobs",
        json={
            "title": "YC role requiring manual review",
            "company": "YC company",
            "description": "A saved lead that must never bypass the exact YC job boundary.",
            "apply_url": apply_url,
            "metadata": {
                "application_provider": "company_form",
                "company_form_host": "www.ycombinator.com",
                "company_form_target_url": apply_url,
                "yc_job_target_url": "https://attacker.example.test/job",
            },
        },
    )

    assert saved.status_code == 201, saved.text
    job = saved.json()["data"]
    assert job["metadata"] == {}
    assert store.client.tables.get("company_form_targets", []) == []
    assert store.client.tables.get("yc_application_targets", []) == []

    scan = client.post(
        f"/api/v1/jobs/{job['id']}/application/scan",
        json={"idempotency_key": f"yc-reserved-{job['id']}"},
    )

    assert scan.status_code == 409, scan.text
    assert scan.json()["error"]["code"] == expected_error
    assert not any(call[0] == "enqueue_automation_job" for call in store.rpc_calls)


def test_patching_existing_exact_yc_job_recreates_missing_service_binding() -> None:
    job_id = str(uuid4())
    queued_id = str(uuid4())
    exact_url = "https://www.ycombinator.com/companies/acme/jobs/AbC123-platform-engineer"
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "title": "Platform Engineer",
                    "company": "Acme",
                    "description": "Build reliable platform systems for Acme customers.",
                    "apply_url": exact_url,
                    "normalized_url": exact_url,
                    "metadata": {"source": "legacy_exact_yc_save"},
                }
            ],
            "connections": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "provider": "yc",
                    "mode": "managed_browser",
                    "status": "connected",
                }
            ],
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                    "parse_status": "parsed",
                }
            ],
        },
        rpc_results={
            "enqueue_automation_job": [{"id": queued_id, "status": "queued"}]
        },
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("yc",),
    )
    client = TestClient(create_app(settings=settings, auth=FakeAuth(), store=store))

    before_rebind = client.post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "yc-missing-binding-0001"},
    )
    assert before_rebind.status_code == 409
    assert before_rebind.json()["error"]["code"] == "yc_exact_job_url_required"

    rebound = client.patch(
        f"/api/v1/jobs/{job_id}",
        json={"apply_url": exact_url},
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["data"]["metadata"] == {
        "source": "legacy_exact_yc_save",
        "application_provider": "yc",
        "yc_job_target_url": exact_url,
    }
    assert store.client.tables["yc_application_targets"] == [
        {
            "id": store.client.tables["yc_application_targets"][0]["id"],
            "job_id": job_id,
            "user_id": str(USER_ID),
            "target_url": exact_url,
        }
    ]

    scan = client.post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "yc-rebound-scan-0001"},
    )
    assert scan.status_code == 202, scan.text
    enqueue = next(call for call in store.rpc_calls if call[0] == "enqueue_automation_job")
    assert enqueue[1]["provider_input"] == "yc"
    assert enqueue[1]["payload_input"] == {
        "job_id": job_id,
        "yc_job_target_url": exact_url,
    }


def test_patching_company_form_to_non_exact_yc_url_removes_generic_authority() -> None:
    job_id = str(uuid4())
    source_url = "https://careers.acme.com/jobs/42"
    yc_listing_url = "https://www.ycombinator.com/companies/acme"
    store = FakeStore(
        {
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "title": "Platform Engineer",
                    "company": "Acme",
                    "description": "Build reliable platform systems for Acme customers.",
                    "apply_url": source_url,
                    "metadata": {
                        "application_provider": "company_form",
                        "company_form_host": "careers.acme.com",
                        "company_form_target_url": source_url,
                    },
                }
            ],
            "company_form_targets": [
                {
                    "id": str(uuid4()),
                    "job_id": job_id,
                    "user_id": str(USER_ID),
                    "source_url": source_url,
                    "target_url": source_url,
                    "exact_host": "careers.acme.com",
                }
            ],
        }
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("company_form", "yc"),
    )
    client = TestClient(create_app(settings=settings, auth=FakeAuth(), store=store))

    patched = client.patch(
        f"/api/v1/jobs/{job_id}",
        json={
            "apply_url": yc_listing_url,
            "metadata": {
                "application_provider": "company_form",
                "company_form_host": "www.ycombinator.com",
                "company_form_target_url": yc_listing_url,
            },
        },
    )

    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["metadata"] == {}
    assert store.client.tables["company_form_targets"] == []
    assert store.client.tables.get("yc_application_targets", []) == []

    scan = client.post(
        f"/api/v1/jobs/{job_id}/application/scan",
        json={"idempotency_key": "yc-reserved-patch-0001"},
    )
    assert scan.status_code == 409, scan.text
    assert scan.json()["error"]["code"] == "application_provider_unsupported"
    assert not any(call[0] == "enqueue_automation_job" for call in store.rpc_calls)


def test_yc_preferences_are_matching_only_and_never_enqueue_discovery() -> None:
    store = FakeStore()
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    initial = client.get("/api/v1/providers/yc/preferences")
    assert initial.status_code == 200, initial.text
    assert initial.json()["data"] == {
        "user_id": str(USER_ID),
        "provider": "yc",
        "query": None,
        "remote_only": False,
        "limit": 10,
    }

    updated = client.patch(
        "/api/v1/providers/yc/preferences",
        json={"query": "  machine   learning engineer  ", "remote_only": True, "limit": 7},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"] == {
        "user_id": str(USER_ID),
        "provider": "yc",
        "query": "machine learning engineer",
        "remote_only": True,
        "limit": 7,
    }
    assert not any(
        call[0] == "enqueue_automation_job" for call in store.rpc_calls
    )


def test_form_revision_approval_binds_revision_hash_and_answers() -> None:
    application_id = str(uuid4())
    revision_id = str(uuid4())
    schema_hash = "a" * 64
    approved = {
        "id": revision_id,
        "user_id": str(USER_ID),
        "application_id": application_id,
        "revision": 2,
        "schema_hash": schema_hash,
        "status": "approved",
        "approved_at": "2026-08-11T10:00:00Z",
    }
    store = FakeStore(
        {
            "applications": [{"id": application_id, "user_id": str(USER_ID)}],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "revision": 2,
                    "schema_hash": schema_hash,
                    "status": "draft",
                }
            ],
        },
        rpc_results={"approve_application_form_revision": [approved]},
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/application-form-revisions/{revision_id}/approve",
        json={
            "expected_revision": 2,
            "schema_hash": schema_hash,
            "answers": {"work_authorized": True},
        },
    )
    assert response.status_code == 200, response.text
    assert store.rpc_calls[-1] == (
        "approve_application_form_revision",
        {
            "revision_id_input": revision_id,
            "revision_input": 2,
            "schema_hash_input": schema_hash,
            "answers_input": {"work_authorized": True},
        },
    )


def test_form_answer_suggestions_are_returned_for_review_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application_id = str(uuid4())
    job_id = str(uuid4())
    resume_id = str(uuid4())
    revision_id = str(uuid4())
    store = FakeStore(
        {
            "applications": [
                {"id": application_id, "user_id": str(USER_ID), "job_id": job_id}
            ],
            "jobs": [
                {
                    "id": job_id,
                    "user_id": str(USER_ID),
                    "title": "Engineer",
                    "company": "Acme",
                    "description": "Build reliable public platform services.",
                }
            ],
            "resumes": [
                {
                    "id": resume_id,
                    "user_id": str(USER_ID),
                    "parse_status": "parsed",
                    "parsed_text": "Five years building Python services.",
                }
            ],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "job_id": job_id,
                    "resume_id": resume_id,
                    "revision": 1,
                    "schema_hash": "b" * 64,
                    "question_schema": [
                        {"key": "years", "label": "Years of experience", "type": "number"}
                    ],
                    "status": "scanned",
                }
            ],
            "profiles": [{"user_id": str(USER_ID), "full_name": "Owner"}],
        },
        rpc_results={"reserve_groq_request": True},
    )
    monkeypatch.setattr(
        saas_main,
        "generate_form_answer_suggestions",
        lambda *_args: {"years": 5},
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/application-form-revisions/{revision_id}/suggest",
        headers={"X-Groq-Api-Key": "gsk_test_key_for_route"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["answers"] == {"years": 5}
    assert not any(call[0] == "approve_application_form_revision" for call in store.rpc_calls)


def test_resume_analysis_returns_suggestions_without_overwriting_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = str(uuid4())
    original_profile = {
        "user_id": str(USER_ID),
        "full_name": "User-entered name",
        "skills": [],
    }
    store = FakeStore(
        {
            "resumes": [
                {
                    "id": resume_id,
                    "user_id": str(USER_ID),
                    "parse_status": "parsed",
                    "parsed_text": (
                        "Ada Lovelace ada@example.test "
                        "https://linkedin.com/in/ada Python SQL"
                    ),
                }
            ],
            "profiles": [original_profile],
        },
        rpc_results={"reserve_groq_request": True},
    )
    monkeypatch.setattr(
        saas_main,
        "analyze_resume_profile",
        lambda *_args: {
            "full_name": "Ada Lovelace",
            "graduation_year": 2026,
            "target_roles": ["Backend engineer"],
        },
    )

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/resumes/{resume_id}/analyze",
        headers={"X-Groq-Api-Key": "gsk_test_key_for_resume"},
    )

    assert response.status_code == 200, response.text
    suggestions = response.json()["data"]["suggestions"]
    assert suggestions["full_name"] == "Ada Lovelace"
    assert suggestions["graduation_year"] == 2026
    assert suggestions["linkedin_url"] == "https://linkedin.com/in/ada"
    assert store.client.tables["profiles"] == [original_profile]
    assert ("reserve_groq_request", {"operation_input": "generate"}) in store.rpc_calls


def test_job_listing_adds_transient_resume_fit_and_role_directions() -> None:
    store = FakeStore(
        {
            "profiles": [
                {
                    "user_id": str(USER_ID),
                    "skills": ["Python", "FastAPI", "PostgreSQL"],
                    "preferences": {"target_roles": ["Backend engineer"]},
                }
            ],
            "resumes": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "is_active": True,
                    "parse_status": "parsed",
                    "parsed_text": "Backend engineer using Python FastAPI and PostgreSQL.",
                }
            ],
            "jobs": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "description": "Build Python FastAPI services with PostgreSQL.",
                }
            ],
        }
    )

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).get("/api/v1/jobs")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"][0]["fit"]["evaluated"] is True
    assert payload["items"][0]["fit"]["score"] >= 55
    assert payload["fit_summary"]["recommended_roles"] == ["Backend engineer"]
    assert "fit" not in store.client.tables["jobs"][0]


def test_submit_queues_only_the_approved_owned_revision() -> None:
    application_id = str(uuid4())
    revision_id = str(uuid4())
    store = FakeStore(
        {
            "applications": [
                {"id": application_id, "user_id": str(USER_ID), "channel": "ats"}
            ],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "provider": "greenhouse",
                    "revision": 1,
                    "schema_hash": "a" * 64,
                    "approved_revision": 1,
                    "approved_schema_hash": "a" * 64,
                    "status": "approved",
                    "approved_at": "2026-08-11T10:00:00Z",
                    "question_schema": [
                        {
                            "key": "email",
                            "label": "Email address",
                            "type": "email",
                            "required": True,
                        }
                    ],
                    "answers": {"Email address": "owner@example.test"},
                }
            ],
            "connections": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "provider": "greenhouse",
                    "status": "active",
                }
            ],
        },
        rpc_results={
            "enqueue_automation_job": [{"id": str(uuid4()), "status": "queued"}]
        },
    )
    response = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    ).post(
        f"/api/v1/application-form-revisions/{revision_id}/submit",
        json={
            "idempotency_key": "submit-test-0001",
            "form_revision_id": revision_id,
        },
    )
    assert response.status_code == 202, response.text
    assert store.rpc_calls[-1][1] == {
        "kind_input": "application_submit",
        "provider_input": "greenhouse",
        "application_id_input": application_id,
        "payload_input": {
            "form_revision_id": revision_id,
            "required_answer_preflight": {
                "required_count": 1,
                "answered_count": 1,
                "missing_count": 0,
                "missing_keys": [],
                "missing_labels": [],
                "resume_upload_keys": [],
                "complete": True,
            },
        },
        "idempotency_key_input": "submit-test-0001",
    }


@pytest.mark.parametrize(
    ("outcome", "resolved_status", "retry_created"),
    [
        ("submitted", "submitted", False),
        ("not_submitted", "prefilled", True),
        # An idempotent replay can return a retry revision that has since been
        # submitted. The original resolution is still not_submitted because
        # the returned immutable revision has a different id.
        ("not_submitted", "submitted", True),
    ],
)
def test_uncertain_form_submission_resolution_uses_tenant_rpc(
    outcome: str, resolved_status: str, retry_created: bool
) -> None:
    revision_id = str(uuid4())
    application_id = str(uuid4())
    schema_hash = "9" * 64
    resolved_id = revision_id if outcome == "submitted" else str(uuid4())
    store = FakeStore(
        {
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "revision": 4,
                    "schema_hash": schema_hash,
                    "status": "approved" if outcome == "submitted" else "needs_attention",
                    "submission_result": (
                        None
                        if outcome == "submitted"
                        else {"submission_state": "uncertain"}
                    ),
                }
            ]
        },
        rpc_results={
            "resolve_application_form_submission": [
                {
                    "id": resolved_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "revision": 4 if outcome == "submitted" else 5,
                    "schema_hash": schema_hash,
                    "status": resolved_status,
                }
            ]
        },
    )

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/application-form-revisions/{revision_id}/resolve-submission",
        json={
            "outcome": outcome,
            "expected_revision": 4,
            "schema_hash": schema_hash,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == resolved_status
    assert response.json()["resolution"] == {
        "outcome": outcome,
        "retry_created": retry_created,
        "rescan_required": retry_created,
    }
    assert store.rpc_calls[-1] == (
        "resolve_application_form_submission",
        {
            "revision_id_input": revision_id,
            "expected_revision_input": 4,
            "expected_schema_hash_input": schema_hash,
            "outcome_input": outcome,
        },
    )
    assert store.received_token == "verified-user-jwt"


def test_form_submission_resolution_rejects_unowned_or_nonattention_revision() -> None:
    unowned_revision_id = str(uuid4())
    store = FakeStore(
        {
            "application_form_revisions": [
                {
                    "id": unowned_revision_id,
                    "user_id": str(OTHER_USER_ID),
                    "revision": 1,
                    "schema_hash": "a" * 64,
                    "status": "needs_attention",
                }
            ]
        }
    )
    client = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    )

    unowned = client.post(
        f"/api/v1/application-form-revisions/{unowned_revision_id}/resolve-submission",
        json={
            "outcome": "submitted",
            "expected_revision": 1,
            "schema_hash": "a" * 64,
        },
    )
    assert unowned.status_code == 404, unowned.text
    assert store.rpc_calls == []

    owned_revision_id = str(uuid4())
    store.client.tables["application_form_revisions"].append(
        {
            "id": owned_revision_id,
            "user_id": str(USER_ID),
            "revision": 2,
            "schema_hash": "b" * 64,
            "status": "scanned",
        }
    )
    settled = client.post(
        f"/api/v1/application-form-revisions/{owned_revision_id}/resolve-submission",
        json={
            "outcome": "not_submitted",
            "expected_revision": 2,
            "schema_hash": "b" * 64,
        },
    )
    assert settled.status_code == 409, settled.text
    assert settled.json()["error"]["code"] == "form_submission_not_uncertain"
    assert store.rpc_calls == []


def test_google_forms_allows_exact_approved_prefill_and_submit() -> None:
    application_id = str(uuid4())
    revision_id = str(uuid4())
    store = FakeStore(
        {
            "applications": [
                {"id": application_id, "user_id": str(USER_ID), "channel": "ats"}
            ],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "provider": "google_forms",
                    "revision": 1,
                    "schema_hash": "b" * 64,
                    "approved_revision": 1,
                    "approved_schema_hash": "b" * 64,
                    "status": "approved",
                    "approved_at": "2026-08-13T10:00:00Z",
                    "question_schema": [
                        {
                            "key": "graduation_year",
                            "label": "Graduation Year",
                            "type": "select",
                            "required": True,
                        },
                        {
                            "key": "resume_file",
                            "label": "Upload Resume",
                            "type": "file",
                            "accepts_resume": True,
                            "required": True,
                        },
                    ],
                    "answers": {"Graduation Year": "2026"},
                }
            ],
        },
        rpc_results={
            "enqueue_automation_job": [{"id": str(uuid4()), "status": "queued"}]
        },
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("google_forms",),
    )
    client = TestClient(create_app(settings=settings, auth=FakeAuth(), store=store))

    prefill = client.post(
        f"/api/v1/application-form-revisions/{revision_id}/prefill",
        json={
            "idempotency_key": "google-prefill-test-0001",
            "form_revision_id": revision_id,
        },
    )
    assert prefill.status_code == 202, prefill.text
    assert store.rpc_calls[-1][1]["kind_input"] == "application_prefill"

    submit = client.post(
        f"/api/v1/application-form-revisions/{revision_id}/submit",
        json={
            "idempotency_key": "google-submit-test-0001",
            "form_revision_id": revision_id,
        },
    )
    assert submit.status_code == 202, submit.text
    enqueue_calls = [call for call in store.rpc_calls if call[0] == "enqueue_automation_job"]
    assert len(enqueue_calls) == 2
    assert enqueue_calls[-1][1]["kind_input"] == "application_submit"
    assert enqueue_calls[-1][1]["payload_input"] == {
        "form_revision_id": revision_id,
        "required_answer_preflight": {
            "required_count": 2,
            "answered_count": 2,
            "missing_count": 0,
            "missing_keys": [],
            "missing_labels": [],
            "resume_upload_keys": ["resume_file"],
            "complete": True,
        },
    }


def test_google_forms_submit_blocks_missing_public_resume_link_before_queueing() -> None:
    application_id = str(uuid4())
    revision_id = str(uuid4())
    schema_hash = "c" * 64
    store = FakeStore(
        {
            "applications": [
                {"id": application_id, "user_id": str(USER_ID), "channel": "ats"}
            ],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "provider": "google_forms",
                    "revision": 2,
                    "schema_hash": schema_hash,
                    "approved_revision": 2,
                    "approved_schema_hash": schema_hash,
                    "status": "approved",
                    "approved_at": "2026-08-13T10:00:00Z",
                    "question_schema": [
                        {
                            "key": "graduation_year",
                            "label": "Graduation Year",
                            "type": "select",
                            "required": True,
                        },
                        {
                            "key": "resume_link",
                            "label": "Resume Link",
                            "type": "url",
                            "required": True,
                        },
                    ],
                    "answers": {
                        "graduation_year": "2026",
                        "resume_link": "",
                    },
                }
            ],
        },
        rpc_results={
            "enqueue_automation_job": [{"id": str(uuid4()), "status": "queued"}]
        },
    )
    settings = replace(
        browser_settings(Fernet.generate_key().decode()),
        allowed_browser_providers=("google_forms",),
    )

    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    ).post(
        f"/api/v1/application-form-revisions/{revision_id}/submit",
        json={
            "idempotency_key": "google-submit-missing-resume-link",
            "form_revision_id": revision_id,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "form_required_answers_missing"
    assert "Resume Link" in response.json()["error"]["message"]
    assert "storage_path" not in response.text
    assert not any(call[0] == "enqueue_automation_job" for call in store.rpc_calls)


def test_form_revision_listing_hydrates_saved_profile_facts_only_before_approval() -> None:
    application_id = str(uuid4())
    draft_id = str(uuid4())
    sealed_id = str(uuid4())
    profile_url = "https://drive.google.com/file/d/resume-id/view?usp=sharing"
    schema = [
        {"key": "resume_link", "label": "Resume Link", "type": "url", "required": True},
        {
            "key": "graduation_year",
            "label": "Graduation Year",
            "type": "listbox",
            "options": ["2025", "2026", "2027"],
            "required": True,
        },
    ]
    store = FakeStore(
        {
            "profiles": [
                {
                    "user_id": str(USER_ID),
                    "resume_url": profile_url,
                    "graduation_year": 2026,
                }
            ],
            "applications": [
                {"id": application_id, "user_id": str(USER_ID), "channel": "ats"}
            ],
            "application_form_revisions": [
                {
                    "id": sealed_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "status": "approved",
                    "approved_at": "2026-08-13T10:00:00Z",
                    "question_schema": schema,
                    "answers": {"resume_link": ""},
                    "created_at": "2026-08-13T10:00:00Z",
                },
                {
                    "id": draft_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "status": "scanned",
                    "approved_at": None,
                    "question_schema": schema,
                    "answers": {},
                    "created_at": "2026-08-13T11:00:00Z",
                },
            ],
        }
    )

    response = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    ).get(f"/api/v1/applications/{application_id}/form-revisions")

    assert response.status_code == 200, response.text
    by_id = {row["id"]: row for row in response.json()["items"]}
    assert by_id[draft_id]["profile_answers"] == {
        "resume_link": profile_url,
        "graduation_year": "2026",
    }
    # A later profile edit must never rewrite what an approved worker snapshot
    # displays or submits.
    assert by_id[sealed_id]["profile_answers"] == {}


def test_saving_profile_refreshes_prepared_answers_as_a_new_review_revision() -> None:
    application_id = str(uuid4())
    revision_id = str(uuid4())
    profile_url = "https://drive.google.com/file/d/new-resume/view?usp=sharing"
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "resume_url": None}],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "status": "approved",
                    "revision": 3,
                    "schema_hash": "e" * 64,
                    "question_schema": [
                        {
                            "key": "resume_link",
                            "label": "Resume Link",
                            "type": "url",
                            "required": True,
                        }
                    ],
                    "answers": {"resume_link": ""},
                    "created_at": "2026-08-13T10:00:00Z",
                }
            ],
        }
    )

    response = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    ).patch("/api/v1/profile", json={"resume_url": profile_url})

    assert response.status_code == 200, response.text
    sync_calls = [
        params
        for name, params in store.rpc_calls
        if name == "refresh_application_form_profile_answers_for_user"
    ]
    assert sync_calls == [
        {
            "user_id_input": str(USER_ID),
            "revision_id_input": revision_id,
            "expected_revision_input": 3,
            "expected_schema_hash_input": "e" * 64,
            "answers_input": {"resume_link": profile_url},
        }
    ]


def test_saving_unchanged_profile_fact_does_not_churn_form_revisions() -> None:
    application_id = str(uuid4())
    revision_id = str(uuid4())
    profile_url = "https://drive.google.com/file/d/resume/view?usp=sharing"
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "resume_url": profile_url}],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "status": "prefilled",
                    "revision": 1,
                    "schema_hash": "f" * 64,
                    "question_schema": [
                        {"key": "resume_link", "label": "Resume Link", "type": "url"}
                    ],
                    "answers": {"resume_link": profile_url},
                    "created_at": "2026-08-13T10:00:00Z",
                }
            ],
        }
    )

    response = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    ).patch("/api/v1/profile", json={"resume_url": profile_url})

    assert response.status_code == 200, response.text
    assert not any(
        name == "refresh_application_form_profile_answers_for_user"
        for name, _params in store.rpc_calls
    )


def test_form_suggest_returns_deterministic_profile_answers_without_groq_or_quota() -> None:
    application_id = str(uuid4())
    job_id = str(uuid4())
    resume_id = str(uuid4())
    revision_id = str(uuid4())
    resume_url = "https://drive.google.com/file/d/resume/view?usp=sharing"
    store = FakeStore(
        {
            "applications": [
                {"id": application_id, "user_id": str(USER_ID), "job_id": job_id}
            ],
            "jobs": [{"id": job_id, "user_id": str(USER_ID), "title": "Engineer"}],
            "resumes": [
                {
                    "id": resume_id,
                    "user_id": str(USER_ID),
                    "parse_status": "parsed",
                    "parsed_text": "Candidate graduates in 2026.",
                }
            ],
            "profiles": [
                {
                    "user_id": str(USER_ID),
                    "resume_url": resume_url,
                    "graduation_year": 2026,
                }
            ],
            "application_form_revisions": [
                {
                    "id": revision_id,
                    "user_id": str(USER_ID),
                    "application_id": application_id,
                    "job_id": job_id,
                    "resume_id": resume_id,
                    "revision": 1,
                    "schema_hash": "a" * 64,
                    "status": "scanned",
                    "question_schema": [
                        {
                            "key": "year",
                            "label": "Graduation Year",
                            "type": "listbox",
                            "options": ["2025", "2026", "2027"],
                        },
                        {"key": "resume", "label": "Resume Link", "type": "url"},
                    ],
                }
            ],
        }
    )

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).post(f"/api/v1/application-form-revisions/{revision_id}/suggest")

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "revision_id": revision_id,
        "revision": 1,
        "schema_hash": "a" * 64,
        "answers": {"year": "2026", "resume": resume_url},
        "source": "profile",
    }
    assert not any(name == "reserve_groq_request" for name, _ in store.rpc_calls)


def test_application_listing_can_filter_one_valid_channel_before_pagination() -> None:
    applications = [
        {
            "id": str(uuid4()),
            "user_id": str(USER_ID),
            "channel": "ats",
            "status": "draft_pending",
        },
        {
            "id": str(uuid4()),
            "user_id": str(USER_ID),
            "channel": "email",
            "status": "drafted",
        },
        {
            "id": str(uuid4()),
            "user_id": str(USER_ID),
            "channel": "manual",
            "status": "draft_pending",
        },
    ]
    client = TestClient(
        create_app(
            settings=configured_settings(),
            auth=FakeAuth(),
            store=FakeStore({"applications": applications}),
        )
    )

    email = client.get("/api/v1/applications?channel=email&limit=1")
    assert email.status_code == 200, email.text
    assert [row["channel"] for row in email.json()["items"]] == ["email"]

    invalid = client.get("/api/v1/applications?channel=social")
    assert invalid.status_code == 422, invalid.text


def test_openapi_contract_is_buildable() -> None:
    response = TestClient(create_app()).get("/api/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["version"] == "2.0.0"


def test_private_route_requires_bearer_session() -> None:
    app = create_app(settings=Settings(), store=FakeStore())
    response = TestClient(app).get("/api/v1/profile")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers.get("x-request-id")


def test_deleting_account_is_locked_out_of_normal_workspace_routes() -> None:
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "deleting"}]}
    )
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).get("/api/v1/jobs")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "account_deletion_in_progress"


def test_account_deletion_requires_a_recent_sign_in() -> None:
    store = FakeStore(rpc_results={"begin_account_deletion": True})
    response = TestClient(
        create_app(
            settings=configured_settings(),
            auth=FakeAuth(datetime.now(UTC) - timedelta(minutes=11)),
            store=store,
        )
    ).request("DELETE", "/api/v1/account", json={"confirmation": "DELETE"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "recent_authentication_required"
    assert store.rpc_calls == []
    assert store.server.deleted_auth_users == []


def test_recently_authenticated_account_can_be_deleted() -> None:
    store = FakeStore(rpc_results={"begin_account_deletion": True})
    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).request("DELETE", "/api/v1/account", json={"confirmation": "DELETE"})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert store.rpc_calls[0] == (
        "begin_account_deletion",
        {"confirmation_input": "DELETE"},
    )
    assert store.server.deleted_auth_users == [(USER_ID, False)]


def test_account_deletion_waits_for_running_automation_before_auth_deletion() -> None:
    store = FakeStore(rpc_results={"begin_account_deletion": False})

    response = TestClient(
        create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    ).request("DELETE", "/api/v1/account", json={"confirmation": "DELETE"})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "account_automation_jobs_running"
    assert store.rpc_calls == [
        ("begin_account_deletion", {"confirmation_input": "DELETE"})
    ]
    assert store.server.deleted_auth_users == []


def test_confirmed_account_deletion_records_unconfirmed_browserbase_cleanup() -> None:
    connection_id = str(uuid4())
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "connections": [
                {
                    "id": connection_id,
                    "user_id": str(USER_ID),
                    "provider": "greenhouse",
                    "mode": "managed_browser",
                    "status": "active",
                }
            ],
            "connection_secrets": [
                {
                    "connection_id": connection_id,
                    "user_id": str(USER_ID),
                    "browser_context_id_ciphertext": cipher.encrypt("context-old"),
                    "browser_session_id_ciphertext": None,
                    "browser_lifecycle_generation": 2,
                    "browser_credential_source": "platform",
                    "browser_credential_generation": None,
                    "browser_credential_epoch": 0,
                    "browser_project_fingerprint": _browserbase_fingerprint(
                        "browser-project"
                    ),
                }
            ],
        },
        rpc_results={
            "begin_account_deletion": True,
            # An unavailable lifecycle/remote cleanup path must be explicitly
            # recorded as abandonment during a fully confirmed account delete.
            "begin_browser_disconnect": None,
            "abandon_browserbase_resources": True,
        },
    )

    response = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    ).request("DELETE", "/api/v1/account", json={"confirmation": "DELETE"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "remote_browser_cleanup_confirmed": False,
        "browserbase_dashboard_url": "https://www.browserbase.com/overview",
    }
    assert any(
        name == "abandon_browserbase_resources" for name, _params in store.rpc_calls
    )
    assert store.server.deleted_auth_users == [(USER_ID, False)]


def test_client_cannot_inject_tenant_id() -> None:
    app = create_app(settings=configured_settings(), auth=FakeAuth(), store=FakeStore())
    response = TestClient(app).post(
        "/api/v1/jobs",
        json={
            "user_id": str(OTHER_USER_ID),
            "title": "Backend Engineer",
            "company": "Example",
            "description": "Build and operate a reliable public API service.",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_invalid"


def test_job_owner_comes_only_from_verified_auth() -> None:
    store = FakeStore()
    app = create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    response = TestClient(app).post(
        "/api/v1/jobs",
        json={
            "title": "Backend Engineer",
            "company": "Example",
            "description": "Build and operate a reliable public API service.",
            "apply_url": "https://example.test/jobs/123",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["user_id"] == str(USER_ID)
    assert store.received_token == "verified-user-jwt"
    assert store.client.last_insert is not None
    assert store.client.last_insert[1]["normalized_url"] == "https://example.test/jobs/123"


def test_editing_approved_content_invalidates_approval() -> None:
    application_id = str(uuid4())
    store = FakeStore(
        {
            "applications": [
                {
                    "id": application_id,
                    "user_id": str(USER_ID),
                    "channel": "email",
                    "status": "approved",
                    "recipient": "recruiter@example.test",
                    "subject": "Original",
                    "body": "Original body",
                    "approved_at": "2026-08-08T00:00:00+00:00",
                }
            ]
        }
    )
    app = create_app(settings=configured_settings(), auth=FakeAuth(), store=store)
    response = TestClient(app).patch(
        f"/api/v1/applications/{application_id}", json={"body": "Edited body"}
    )
    assert response.status_code == 200, response.text
    row = response.json()["data"]
    assert row["status"] == "drafted"
    assert row["approved_at"] is None


def test_linkedin_browser_connection_is_always_unavailable() -> None:
    settings = configured_settings()
    settings = Settings(
        supabase_url=settings.supabase_url,
        supabase_publishable_key=settings.supabase_publishable_key,
        supabase_secret_key=settings.supabase_secret_key,
        site_url=settings.site_url,
        browserbase_api_key="browser-secret",
        browserbase_project_id="project",
        allowed_browser_providers=("linkedin",),
    )
    app = create_app(settings=settings, auth=FakeAuth(), store=FakeStore())
    response = TestClient(app).post("/api/v1/connections/linkedin/browser/start")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_connection_unavailable"


def test_browser_start_persists_context_and_session_for_one_generation(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    connection_id = str(uuid4())
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={
            "begin_browser_start": {
                "generation": 4,
                "connection_id": None,
                "context_ciphertext": None,
                "session_ciphertext": None,
                "reuse_context": False,
                "credential_epoch": 0,
                "active_credential_source": "platform",
                "active_credential_generation": None,
                "active_project_fingerprint": None,
                "context_credential_source": None,
                "context_credential_generation": None,
                "context_credential_epoch": None,
                "context_project_fingerprint": None,
            },
            "save_browser_connection_context_bound": [
                {
                    "id": connection_id,
                    "user_id": str(USER_ID),
                    "provider": "greenhouse",
                    "mode": "managed_browser",
                    "status": "pending",
                }
            ],
            "save_browser_connection_session": True,
            "confirm_browser_start": True,
        },
    )
    browser = FakeBrowserbase()
    monkeypatch.setattr(saas_main, "BrowserbaseClient", lambda *_args, **_kwargs: browser)

    response = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    ).post("/api/v1/connections/greenhouse/browser/start")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["live_view_url"].startswith("https://")
    assert browser.session_options == [
        {
            "keep_alive": False,
            "timeout_seconds": 180,
            "user_metadata": {"provider": "greenhouse"},
        }
    ]
    call_names = [name for name, _params in store.rpc_calls]
    assert "reserve_browser_start" not in call_names
    assert call_names.index("begin_browser_start") < call_names.index(
        "save_browser_connection_context_bound"
    ) < call_names.index("save_browser_connection_session") < call_names.index(
        "confirm_browser_start"
    )
    save_context = next(
        params
        for name, params in store.rpc_calls
        if name == "save_browser_connection_context_bound"
    )
    save_session = next(
        params for name, params in store.rpc_calls if name == "save_browser_connection_session"
    )
    assert save_context["expected_generation_input"] == 4
    assert save_context["credential_source_input"] == "platform"
    assert save_context["credential_generation_input"] is None
    assert save_context["credential_epoch_input"] == 0
    assert save_context["project_fingerprint_input"] == _browserbase_fingerprint(
        "browser-project"
    )
    assert save_session["expected_generation_input"] == 4
    assert save_session["expected_connection_id_input"] == connection_id
    assert save_session["expected_context_ciphertext_input"] == save_context[
        "context_ciphertext_input"
    ]
    assert "context-new" not in save_context["context_ciphertext_input"]
    assert "session-new" not in save_session["session_ciphertext_input"]


def test_browser_start_never_adopts_or_rebinds_unbound_legacy_context(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    connection_id = str(uuid4())
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={
            "begin_browser_start": {
                "generation": 5,
                "connection_id": connection_id,
                "context_ciphertext": cipher.encrypt("legacy-context"),
                "session_ciphertext": cipher.encrypt("legacy-session"),
                # Application validation must fail closed even if an older or
                # stale database function incorrectly offers context reuse.
                "reuse_context": True,
                "credential_epoch": 0,
                "active_credential_source": "platform",
                "active_credential_generation": None,
                "active_project_fingerprint": None,
                "context_credential_source": None,
                "context_credential_generation": None,
                "context_credential_epoch": None,
                "context_project_fingerprint": None,
            },
            "abort_browser_start": True,
        },
    )
    browser = FakeBrowserbase()
    constructor_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def browserbase_factory(*args: Any, **kwargs: Any) -> FakeBrowserbase:
        constructor_calls.append((args, kwargs))
        return browser

    monkeypatch.setattr(saas_main, "BrowserbaseClient", browserbase_factory)

    response = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    ).post("/api/v1/connections/greenhouse/browser/start")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "browserbase_credential_binding_stale"
    assert constructor_calls == []
    assert browser.events == []
    assert not any(
        name == "save_browser_connection_context_bound" for name, _params in store.rpc_calls
    )
    assert not any(
        name in {"save_browser_connection_session", "confirm_browser_start"}
        for name, _params in store.rpc_calls
    )
    abort = next(params for name, params in store.rpc_calls if name == "abort_browser_start")
    assert abort["expected_generation_input"] == 5
    assert abort["expected_connection_id_input"] == connection_id
    assert abort["drop_connection_input"] is False


def test_stale_browser_start_cleans_every_remote_resource_it_created(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    connection_id = str(uuid4())

    def reject_stale_confirmation(_params: dict[str, Any]) -> bool:
        raise ApiError(
            409,
            "browser_connection_operation_stale",
            "The managed-browser connection changed.",
        )

    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={
            "begin_browser_start": {
                "generation": 9,
                "connection_id": None,
                "context_ciphertext": None,
                "session_ciphertext": None,
                "reuse_context": False,
                "credential_epoch": 0,
                "active_credential_source": "platform",
                "active_credential_generation": None,
                "active_project_fingerprint": None,
                "context_credential_source": None,
                "context_credential_generation": None,
                "context_credential_epoch": None,
                "context_project_fingerprint": None,
            },
            "save_browser_connection_context_bound": [
                {
                    "id": connection_id,
                    "user_id": str(USER_ID),
                    "provider": "greenhouse",
                }
            ],
            "save_browser_connection_session": True,
            "confirm_browser_start": reject_stale_confirmation,
            "abort_browser_start": False,
        },
    )
    browser = FakeBrowserbase()
    monkeypatch.setattr(saas_main, "BrowserbaseClient", lambda *_args, **_kwargs: browser)

    response = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    ).post("/api/v1/connections/greenhouse/browser/start")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "browser_connection_operation_stale"
    assert ("live_view", "session-new") in browser.events
    assert ("release_session", "session-new") in browser.events
    assert ("delete_context", "context-new") in browser.events
    abort = next(params for name, params in store.rpc_calls if name == "abort_browser_start")
    assert abort["expected_generation_input"] == 9
    assert abort["expected_connection_id_input"] == connection_id
    assert abort["drop_connection_input"] is True


def test_browser_disconnect_retries_same_generation_and_exact_connection(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    connection_id = str(uuid4())
    snapshot = {
        "generation": 12,
        "connection_id": connection_id,
        "context_ciphertext": cipher.encrypt("context-old"),
        "session_ciphertext": cipher.encrypt("session-old"),
        "credential_epoch": 0,
        "context_credential_source": "platform",
        "context_credential_generation": None,
        "context_credential_epoch": 0,
        "context_project_fingerprint": _browserbase_fingerprint("browser-project"),
    }
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={
            "begin_browser_disconnect": snapshot,
            "finish_browser_disconnect": True,
        },
    )
    browser = FakeBrowserbase()
    browser.fail_context_deletes = 1
    monkeypatch.setattr(saas_main, "BrowserbaseClient", lambda *_args, **_kwargs: browser)
    client = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    )

    first = client.delete("/api/v1/connections/greenhouse")
    assert first.status_code == 503, first.text
    assert first.json()["error"]["code"] == "provider_disconnect_failed"
    assert not any(name == "finish_browser_disconnect" for name, _params in store.rpc_calls)

    second = client.delete("/api/v1/connections/greenhouse")
    assert second.status_code == 200, second.text
    assert second.json() == {"ok": True}
    begins = [params for name, params in store.rpc_calls if name == "begin_browser_disconnect"]
    finishes = [params for name, params in store.rpc_calls if name == "finish_browser_disconnect"]
    assert len(begins) == 2
    assert len(finishes) == 1
    assert finishes[0]["expected_generation_input"] == 12
    assert finishes[0]["expected_connection_id_input"] == connection_id
    assert browser.events.count(("release_session", "session-old")) == 2
    assert browser.events.count(("delete_context", "context-old")) == 2


def test_browser_complete_finishes_only_the_saved_generation(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    connection_id = str(uuid4())
    session_ciphertext = cipher.encrypt("session-new")
    connection = {
        "id": connection_id,
        "user_id": str(USER_ID),
        "provider": "greenhouse",
        "mode": "managed_browser",
        "status": "pending",
    }
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "connections": [connection],
            "connection_secrets": [
                {
                    "connection_id": connection_id,
                    "user_id": str(USER_ID),
                    "browser_context_id_ciphertext": cipher.encrypt("context-new"),
                    "browser_session_id_ciphertext": session_ciphertext,
                    "browser_lifecycle_generation": 15,
                    "browser_credential_source": "platform",
                    "browser_credential_generation": None,
                    "browser_credential_epoch": 0,
                    "browser_project_fingerprint": _browserbase_fingerprint(
                        "browser-project"
                    ),
                }
            ],
        },
        rpc_results={
            "finish_browser_start": [
                {**connection, "status": "needs_attention"}
            ]
        },
    )
    browser = FakeBrowserbase()
    monkeypatch.setattr(saas_main, "BrowserbaseClient", lambda *_args, **_kwargs: browser)

    response = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    ).post("/api/v1/connections/greenhouse/browser/complete")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "needs_attention"
    finish = next(params for name, params in store.rpc_calls if name == "finish_browser_start")
    assert finish["expected_generation_input"] == 15
    assert finish["expected_connection_id_input"] == connection_id
    assert finish["expected_session_ciphertext_input"] == session_ciphertext
    assert ("release_session", "session-new") in browser.events


def test_browser_complete_rejects_unbound_legacy_context_before_remote_access(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    connection_id = str(uuid4())
    connection = {
        "id": connection_id,
        "user_id": str(USER_ID),
        "provider": "greenhouse",
        "mode": "managed_browser",
        "status": "pending",
    }
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "connections": [connection],
            "connection_secrets": [
                {
                    "connection_id": connection_id,
                    "user_id": str(USER_ID),
                    "browser_context_id_ciphertext": cipher.encrypt("legacy-context"),
                    "browser_session_id_ciphertext": cipher.encrypt("legacy-session"),
                    "browser_lifecycle_generation": 16,
                    "browser_credential_source": None,
                    "browser_credential_generation": None,
                    "browser_credential_epoch": None,
                    "browser_project_fingerprint": None,
                }
            ],
        }
    )
    browser = FakeBrowserbase()
    constructor_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def browserbase_factory(*args: Any, **kwargs: Any) -> FakeBrowserbase:
        constructor_calls.append((args, kwargs))
        return browser

    monkeypatch.setattr(saas_main, "BrowserbaseClient", browserbase_factory)

    response = TestClient(
        create_app(settings=browser_settings(key), auth=FakeAuth(), store=store)
    ).post("/api/v1/connections/greenhouse/browser/complete")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "browserbase_credential_binding_stale"
    assert constructor_calls == []
    assert browser.events == []
    assert not any(name == "finish_browser_start" for name, _params in store.rpc_calls)
    assert not any(name == "abort_browser_start" for name, _params in store.rpc_calls)


def test_linkedin_cannot_enter_browser_disconnect_lifecycle() -> None:
    settings = configured_settings()
    app = create_app(settings=settings, auth=FakeAuth(), store=FakeStore())
    response = TestClient(app).delete("/api/v1/connections/linkedin")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_connection_unavailable"


def test_google_callback_revokes_grant_when_account_lookup_fails(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    consumed = {
        "user_id": str(USER_ID),
        "generation": 7,
        "return_path": "/?view=connections",
        "pkce_verifier_ciphertext": TokenCipher(key).encrypt("pkce-verifier"),
    }
    store = FakeStore(rpc_results={"consume_oauth_state": [consumed]})
    token = {
        "access_token": "issued-access-token",
        "refresh_token": "issued-refresh-token",
        "scope": GMAIL_SEND_SCOPE,
    }
    revoked: list[str] = []
    monkeypatch.setattr(saas_main, "exchange_google_code", lambda *_args, **_kwargs: token)

    def fail_userinfo(_access_token: str) -> dict[str, Any]:
        raise GoogleProviderError("google_unavailable", "Google is unavailable.")

    monkeypatch.setattr(saas_main, "get_google_userinfo", fail_userinfo)
    monkeypatch.setattr(
        saas_main,
        "revoke_google_token",
        lambda value: revoked.append(value) or {"revoked": True},
    )

    response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=store)
    ).get(
        "/api/v1/oauth/google/callback?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "code=google_account_lookup_failed" in response.headers["location"]
    assert revoked == ["issued-refresh-token"]


def test_google_callback_revokes_grant_when_atomic_save_returns_no_row(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    consumed = {
        "user_id": str(USER_ID),
        "generation": 11,
        "return_path": "/",
        "pkce_verifier_ciphertext": TokenCipher(key).encrypt("pkce-verifier"),
    }
    store = FakeStore(
        rpc_results={"consume_oauth_state": [consumed], "save_google_connection": []}
    )
    token = {
        "access_token": "issued-access-token",
        "refresh_token": "issued-refresh-token",
        "scope": f"openid email {GMAIL_SEND_SCOPE}",
        "expires_in": 3600,
    }
    revoked: list[str] = []
    monkeypatch.setattr(saas_main, "exchange_google_code", lambda *_args, **_kwargs: token)
    monkeypatch.setattr(
        saas_main,
        "get_google_userinfo",
        lambda _value: {
            "sub": "google-subject",
            "email": "owner@gmail.test",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        saas_main,
        "revoke_google_token",
        lambda value: revoked.append(value) or {"revoked": True},
    )

    response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=store)
    ).get(
        "/api/v1/oauth/google/callback?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "code=connection_save_failed" in response.headers["location"]
    assert revoked == ["issued-refresh-token"]
    save_call = next(params for name, params in store.rpc_calls if name == "save_google_connection")
    assert save_call["expected_generation_input"] == 11


def test_google_oauth_start_uses_atomic_generation_rpc() -> None:
    key = Fernet.generate_key().decode()
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={
            "reserve_google_oauth_start": True,
            "create_google_oauth_state_v2": 3,
        },
    )
    response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=store)
    ).post("/api/v1/oauth/google/start")
    assert response.status_code == 200, response.text
    create_call = next(
        params for name, params in store.rpc_calls if name == "create_google_oauth_state_v2"
    )
    assert create_call["user_id_input"] == str(USER_ID)
    assert create_call["return_path_input"] == "/"
    assert create_call["credential_source_input"] == "platform"
    assert create_call["credential_generation_input"] is None
    assert create_call["state_hash_input"] not in response.text


def test_user_google_oauth_client_is_encrypted_masked_and_tenant_bound() -> None:
    key = Fernet.generate_key().decode()
    client_id = "123456789-tenantweb.apps.googleusercontent.com"
    client_secret = "GOCSPX-tenant-client-secret"
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results={"save_user_google_oauth_client": 8},
    )
    response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=store)
    ).put(
        "/api/v1/connections/google-oauth-client",
        json={"client_id": client_id, "client_secret": client_secret},
    )
    assert response.status_code == 200, response.text
    serialized = response.text
    assert client_id not in serialized
    assert client_secret not in serialized
    assert "client_secret" not in serialized
    assert response.json()["data"]["configured"] is True

    params = next(
        values
        for name, values in store.rpc_calls
        if name == "save_user_google_oauth_client"
    )
    assert params["user_id_input"] == str(USER_ID)
    assert params["client_id_ciphertext_input"] != client_id
    assert params["client_secret_ciphertext_input"] != client_secret
    cipher = TokenCipher(key)
    assert cipher.decrypt(params["client_id_ciphertext_input"]) == client_id
    assert cipher.decrypt(params["client_secret_ciphertext_input"]) == client_secret

    other_store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_google_oauth_clients": [
                {
                    "user_id": str(OTHER_USER_ID),
                    "client_id_ciphertext": cipher.encrypt(client_id),
                    "client_secret_ciphertext": cipher.encrypt(client_secret),
                    "generation": 2,
                }
            ],
        }
    )
    other_response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=other_store)
    ).get("/api/v1/connections/google-oauth-client")
    assert other_response.status_code == 200
    assert other_response.json()["data"]["configured"] is False


def test_google_oauth_start_can_use_exact_saved_user_client() -> None:
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    client_id = "123456789-tenantweb.apps.googleusercontent.com"
    client_secret = "GOCSPX-tenant-client-secret"
    settings = replace(
        google_settings(key), google_client_id="", google_client_secret=""
    )
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_google_oauth_clients": [
                {
                    "user_id": str(USER_ID),
                    "client_id_ciphertext": cipher.encrypt(client_id),
                    "client_secret_ciphertext": cipher.encrypt(client_secret),
                    "generation": 12,
                }
            ],
        },
        rpc_results={
            "reserve_google_oauth_start": True,
            "create_google_oauth_state_v2": 13,
        },
    )
    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/oauth/google/start?return_path=/?view=connections",
        json={"credential_source": "user"},
    )
    assert response.status_code == 200, response.text
    query = parse_qs(urlparse(response.json()["authorization_url"]).query)
    assert query["client_id"] == [client_id]
    assert query["redirect_uri"] == [settings.google_redirect_uri]
    create_call = next(
        values
        for name, values in store.rpc_calls
        if name == "create_google_oauth_state_v2"
    )
    assert create_call["credential_source_input"] == "user"
    assert create_call["credential_generation_input"] == 12
    assert client_secret not in response.text


def test_corrupt_user_google_oauth_client_fails_closed_without_platform_fallback() -> None:
    key = Fernet.generate_key().decode()
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_google_oauth_clients": [
                {
                    "user_id": str(USER_ID),
                    "client_id_ciphertext": "not-fernet-ciphertext",
                    "client_secret_ciphertext": "also-not-fernet",
                    "generation": 4,
                }
            ],
        },
        rpc_results={
            "reserve_google_oauth_start": True,
            "create_google_oauth_state_v2": 5,
        },
    )
    response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=store)
    ).post(
        "/api/v1/oauth/google/start",
        json={"credential_source": "user"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "google_oauth_client_reconfiguration_required"
    assert not any(name == "create_google_oauth_state_v2" for name, _ in store.rpc_calls)


def test_google_callback_and_refresh_use_the_bound_user_client(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    cipher = TokenCipher(key)
    client_id = "123456789-tenantweb.apps.googleusercontent.com"
    client_secret = "GOCSPX-tenant-client-secret"
    settings = replace(
        google_settings(key), google_client_id="", google_client_secret=""
    )
    custom_row = {
        "user_id": str(USER_ID),
        "client_id_ciphertext": cipher.encrypt(client_id),
        "client_secret_ciphertext": cipher.encrypt(client_secret),
        "generation": 21,
    }
    consumed = {
        "user_id": str(USER_ID),
        "generation": 22,
        "credential_source": "user",
        "credential_generation": 21,
        "return_path": "/?view=connections",
        "pkce_verifier_ciphertext": cipher.encrypt("pkce-verifier"),
    }
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_google_oauth_clients": [custom_row],
        },
        rpc_results={
            "consume_oauth_state": [consumed],
            "save_google_connection": [
                {
                    "id": str(uuid4()),
                    "user_id": str(USER_ID),
                    "provider": "gmail",
                    "status": "connected",
                }
            ],
        },
    )
    exchange_args: list[tuple[str, str]] = []

    def exchange(_code: str, selected_id: str, selected_secret: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        exchange_args.append((selected_id, selected_secret))
        return {
            "access_token": "issued-access-token",
            "refresh_token": "issued-refresh-token",
            "scope": f"openid email {GMAIL_SEND_SCOPE}",
            "expires_in": 3600,
        }

    monkeypatch.setattr(saas_main, "exchange_google_code", exchange)
    monkeypatch.setattr(
        saas_main,
        "get_google_userinfo",
        lambda _token: {
            "sub": "google-subject",
            "email": "owner@gmail.test",
            "email_verified": True,
        },
    )
    response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    ).get(
        "/api/v1/oauth/google/callback?state=opaque-state&code=authorization-code",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "oauth=connected" in response.headers["location"]
    assert exchange_args == [(client_id, client_secret)]
    save_call = next(
        values for name, values in store.rpc_calls if name == "save_google_connection"
    )
    assert save_call["metadata_input"]["oauth_client_source"] == "user"
    assert save_call["metadata_input"]["oauth_client_generation"] == 21

    application_id = str(uuid4())
    connection_id = str(uuid4())
    send_store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "applications": [
                {
                    "id": application_id,
                    "user_id": str(USER_ID),
                    "status": "approved",
                    "channel": "email",
                    "recipient": "recruiter@example.test",
                    "subject": "Application",
                    "body": "Hello",
                }
            ],
            "connections": [
                {
                    "id": connection_id,
                    "user_id": str(USER_ID),
                    "provider": "gmail",
                    "status": "connected",
                    "display_name": "owner@gmail.test",
                    "expires_at": "2020-01-01T00:00:00Z",
                    "metadata": {
                        "oauth_client_source": "user",
                        "oauth_client_generation": 21,
                    },
                }
            ],
            "connection_secrets": [
                {
                    "connection_id": connection_id,
                    "user_id": str(USER_ID),
                    "access_token_ciphertext": cipher.encrypt("expired-access"),
                    "refresh_token_ciphertext": cipher.encrypt("refresh-token"),
                }
            ],
            "user_google_oauth_clients": [custom_row],
        },
        rpc_results={
            "enqueue_email_send": [{"id": str(uuid4()), "kind": "send_email", "status": "queued"}],
        },
    )
    send_response = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=send_store)
    ).post(
        f"/api/v1/applications/{application_id}/send",
        json={"idempotency_key": "send-request-123", "attach_resume": False},
    )
    assert send_response.status_code == 202, send_response.text
    assert any(name == "enqueue_email_send" for name, _params in send_store.rpc_calls)


def test_google_disconnect_uses_retryable_lifecycle_before_revocation(
    monkeypatch: Any,
) -> None:
    key = Fernet.generate_key().decode()
    connection_id = str(uuid4())
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "connections": [
                {
                    "id": connection_id,
                    "user_id": str(USER_ID),
                    "provider": "gmail",
                    "status": "connected",
                }
            ],
            "connection_secrets": [
                {
                    "connection_id": connection_id,
                    "user_id": str(USER_ID),
                    "refresh_token_ciphertext": TokenCipher(key).encrypt("refresh-token"),
                }
            ],
        },
        rpc_results={
            "begin_google_disconnect": 5,
            "finish_google_disconnect": True,
        },
    )
    revoked: list[str] = []
    monkeypatch.setattr(
        saas_main,
        "revoke_google_token",
        lambda value: revoked.append(value) or {"revoked": True},
    )
    response = TestClient(
        create_app(settings=google_settings(key), auth=FakeAuth(), store=store)
    ).delete("/api/v1/connections/gmail")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "revoked": True}
    assert revoked == ["refresh-token"]
    call_names = [name for name, _params in store.rpc_calls]
    assert call_names.index("begin_google_disconnect") < call_names.index(
        "finish_google_disconnect"
    )
    finish_call = next(
        params for name, params in store.rpc_calls if name == "finish_google_disconnect"
    )
    assert finish_call["expected_generation_input"] == 5


def test_browserbase_byok_is_validated_without_a_session_and_encrypted_at_rest(
    monkeypatch: Any,
) -> None:
    encryption_key = Fernet.generate_key().decode()
    rpc_results: dict[str, Any] = {}
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]},
        rpc_results=rpc_results,
    )

    def save_credential(params: dict[str, Any]) -> list[dict[str, Any]]:
        row = {
            "user_id": params["user_id_input"],
            "provider": params["provider_input"],
            "credential_ciphertext": params["credential_ciphertext_input"],
            "verification_status": params["verification_status_input"],
            "verification_code": params["verification_code_input"],
            "verified_at": params["verified_at_input"],
            "generation": 1,
            "binding_fingerprint": params["binding_fingerprint_input"],
            "updated_at": "2026-08-15T12:00:00Z",
        }
        store.client.tables.setdefault("user_provider_credentials", []).append(row)
        return [row]

    rpc_results["save_user_provider_credential"] = save_credential
    constructed: list[tuple[str, str]] = []

    class ValidatingBrowserbase:
        def __init__(self, api_key: str, project_id: str) -> None:
            constructed.append((api_key, project_id))

        def validate_project(self) -> dict[str, Any]:
            return {"valid": True, "status": "ready", "project_id": "tenant-project"}

    monkeypatch.setattr(saas_main, "BrowserbaseClient", ValidatingBrowserbase)
    client = TestClient(
        create_app(settings=browser_settings(encryption_key), auth=FakeAuth(), store=store)
    )
    api_key = "bb_live_tenant_secret"

    response = client.put(
        "/api/v1/provider-credentials/browserbase",
        json={"api_key": api_key, "project_id": "tenant-project"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["verification_status"] == "verified"
    assert api_key not in response.text
    assert "tenant-project" not in response.text
    assert "tenant-project" not in json.dumps(response.json()["verification"])
    assert "project_id" not in response.json()["verification"]
    assert constructed == [(api_key, "tenant-project")]
    saved = store.client.tables["user_provider_credentials"][0]
    assert api_key not in saved["credential_ciphertext"]
    envelope = json.loads(TokenCipher(encryption_key).decrypt(saved["credential_ciphertext"]))
    assert envelope == {
        "api_key": api_key,
        "project_id": "tenant-project",
        "provider": "browserbase",
        "version": 1,
    }


def test_stored_groq_key_is_used_when_legacy_headers_are_absent(
    monkeypatch: Any,
) -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = TokenCipher(encryption_key)
    groq_key = "gsk_stored_groq_secret"
    rows = [{
        "user_id": str(USER_ID),
        "provider": "groq",
        "credential_ciphertext": cipher.encrypt(
            json.dumps(
                {"api_key": groq_key, "provider": "groq", "version": 1},
                separators=(",", ":"),
                sort_keys=True,
            )
        ),
        "verification_status": "verified",
        "verification_code": None,
        "verified_at": "2026-08-15T12:00:00Z",
        "generation": 1,
        "created_at": "2026-08-15T12:00:00Z",
        "updated_at": "2026-08-15T12:00:00Z",
    }]
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_provider_credentials": rows,
        }
    )
    seen: list[tuple[str, str]] = []

    def validate_groq(api_key: str, _model: str) -> dict[str, Any]:
        seen.append(("groq", api_key))
        return {"valid": True, "status": "ready"}

    monkeypatch.setattr(saas_main, "validate_groq_key", validate_groq)
    client = TestClient(
        create_app(settings=browser_settings(encryption_key), auth=FakeAuth(), store=store)
    )

    groq_response = client.post("/api/v1/groq/validate")
    assert groq_response.status_code == 200, groq_response.text
    assert seen == [("groq", groq_key)]
    assert groq_key not in groq_response.text


def test_saved_groq_key_survives_a_fresh_app_and_client_without_a_header(
    monkeypatch: Any,
) -> None:
    encryption_key = Fernet.generate_key().decode()
    store = FakeStore(
        {"profiles": [{"user_id": str(USER_ID), "account_status": "active"}]}
    )
    api_key = "gsk_persisted_between_app_instances"
    seen: list[str] = []

    def validate(api_key_input: str, _model: str) -> dict[str, Any]:
        seen.append(api_key_input)
        return {"valid": True, "status": "ready"}

    def save_credential(params: dict[str, Any]) -> list[dict[str, Any]]:
        row = {
            "user_id": params["user_id_input"],
            "provider": params["provider_input"],
            "credential_ciphertext": params["credential_ciphertext_input"],
            "verification_status": params["verification_status_input"],
            "verification_code": params["verification_code_input"],
            "verified_at": params["verified_at_input"],
            "generation": 1,
            "binding_fingerprint": None,
            "created_at": "2026-08-15T12:00:00Z",
            "updated_at": "2026-08-15T12:00:00Z",
        }
        store.client.tables["user_provider_credentials"] = [row]
        return [row]

    store.client.rpc_results["save_user_provider_credential"] = save_credential
    store.server.rpc_results["save_user_provider_credential"] = save_credential
    monkeypatch.setattr(saas_main, "validate_groq_key", validate)
    settings = browser_settings(encryption_key)

    first_client = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    )
    saved = first_client.put(
        "/api/v1/provider-credentials/groq", json={"api_key": api_key}
    )
    assert saved.status_code == 200, saved.text
    assert api_key not in saved.text

    # A separate app and TestClient must resolve the encrypted row from the
    # shared store; this guards against accidental process-local persistence.
    second_client = TestClient(
        create_app(settings=settings, auth=FakeAuth(), store=store)
    )
    validated = second_client.post("/api/v1/groq/validate")

    assert validated.status_code == 200, validated.text
    assert validated.json()["valid"] is True
    assert seen == [api_key, api_key]
    assert api_key not in validated.text
    ciphertext = store.client.tables["user_provider_credentials"][0][
        "credential_ciphertext"
    ]
    assert api_key not in ciphertext


@pytest.mark.parametrize(
    ("provider", "validation_code", "expected_status"),
    [
        ("groq", "groq_invalid_key", 422),
        ("groq", "groq_unavailable", 503),
    ],
)
def test_failed_key_replacement_preserves_an_existing_verified_credential(
    monkeypatch: Any,
    provider: str,
    validation_code: str,
    expected_status: int,
) -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = TokenCipher(encryption_key)
    old_key = f"{provider}_existing_verified_secret"
    ciphertext = cipher.encrypt(
        json.dumps(
            {"version": 1, "provider": provider, "api_key": old_key},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_provider_credentials": [
                {
                    "user_id": str(USER_ID),
                    "provider": provider,
                    "credential_ciphertext": ciphertext,
                    "verification_status": "verified",
                    "verification_code": None,
                    "verified_at": "2026-08-15T12:00:00Z",
                    "generation": 2,
                    "binding_fingerprint": None,
                }
            ],
        }
    )
    invalid = {
        "valid": False,
        "status": validation_code,
        "message": "The replacement key could not be verified.",
    }
    if provider == "groq":
        monkeypatch.setattr(saas_main, "validate_groq_key", lambda *_args: invalid)
    response = TestClient(
        create_app(
            settings=browser_settings(encryption_key), auth=FakeAuth(), store=store
        )
    ).put(
        f"/api/v1/provider-credentials/{provider}",
        json={"api_key": f"{provider}_invalid_replacement"},
    )

    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == validation_code
    assert store.client.tables["user_provider_credentials"][0][
        "credential_ciphertext"
    ] == ciphertext
    assert not any(
        name == "save_user_provider_credential" for name, _params in store.rpc_calls
    )


@pytest.mark.parametrize(
    ("validation_code", "expected_status"),
    [("browserbase_not_authorized", 422), ("browserbase_unavailable", 503)],
)
def test_failed_browserbase_replacement_preserves_old_key_and_project(
    monkeypatch: Any, validation_code: str, expected_status: int
) -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = TokenCipher(encryption_key)
    old_project = "tenant-old-project"
    old_ciphertext = cipher.encrypt(
        json.dumps(
            {
                "version": 1,
                "provider": "browserbase",
                "api_key": "bb_live_existing_verified_secret",
                "project_id": old_project,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_provider_credentials": [
                {
                    "user_id": str(USER_ID),
                    "provider": "browserbase",
                    "credential_ciphertext": old_ciphertext,
                    "verification_status": "verified",
                    "verification_code": None,
                    "verified_at": "2026-08-15T12:00:00Z",
                    "generation": 1,
                    "binding_fingerprint": _browserbase_fingerprint(old_project),
                }
            ],
        },
        rpc_results={"get_browserbase_credential_state": {"epoch": 1}},
    )

    class InvalidBrowserbase:
        def __init__(self, _api_key: str, _project_id: str) -> None:
            pass

        def validate_project(self) -> dict[str, Any]:
            return {
                "valid": False,
                "status": validation_code,
                "message": "The replacement key and project do not match.",
            }

    monkeypatch.setattr(saas_main, "BrowserbaseClient", InvalidBrowserbase)

    response = TestClient(
        create_app(
            settings=browser_settings(encryption_key), auth=FakeAuth(), store=store
        )
    ).put(
        "/api/v1/provider-credentials/browserbase",
        json={
            "api_key": "bb_live_invalid_replacement",
            "project_id": "tenant-new-project",
        },
    )

    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == validation_code
    assert store.client.tables["user_provider_credentials"][0][
        "credential_ciphertext"
    ] == old_ciphertext
    assert not any(
        name == "begin_browser_disconnect" for name, _params in store.rpc_calls
    )
    assert not any(
        name == "save_user_provider_credential" for name, _params in store.rpc_calls
    )


def test_browserbase_local_abandon_requires_exact_confirmation_and_is_explicit() -> None:
    store = FakeStore(rpc_results={"abandon_browserbase_resources": True})
    client = TestClient(
        create_app(
            settings=browser_settings(Fernet.generate_key().decode()),
            auth=FakeAuth(),
            store=store,
        )
    )

    rejected = client.post(
        "/api/v1/provider-credentials/browserbase/abandon",
        json={"confirmation": "DELETE"},
    )
    assert rejected.status_code == 422, rejected.text
    assert not any(
        name == "abandon_browserbase_resources" for name, _params in store.rpc_calls
    )

    abandoned = client.post(
        "/api/v1/provider-credentials/browserbase/abandon",
        json={"confirmation": "ABANDON REMOTE BROWSER DATA"},
    )
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json() == {
        "ok": True,
        "remote_cleanup_confirmed": False,
        "browserbase_dashboard_url": "https://www.browserbase.com/overview",
    }
    assert store.rpc_calls[-1] == (
        "abandon_browserbase_resources",
        {
            "user_id_input": str(USER_ID),
            "confirmation_input": "ABANDON REMOTE BROWSER DATA",
        },
    )


def test_provider_credential_status_returns_only_masked_hints() -> None:
    encryption_key = Fernet.generate_key().decode()
    cipher = TokenCipher(encryption_key)
    api_key = "bb_live_status_secret"
    project_id = "tenant-project-status"
    store = FakeStore(
        {
            "profiles": [{"user_id": str(USER_ID), "account_status": "active"}],
            "user_provider_credentials": [
                {
                    "user_id": str(USER_ID),
                    "provider": "browserbase",
                    "credential_ciphertext": cipher.encrypt(
                        json.dumps(
                            {
                                "api_key": api_key,
                                "project_id": project_id,
                                "provider": "browserbase",
                                "version": 1,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    ),
                    "verification_status": "verified",
                    "verification_code": None,
                    "verified_at": "2026-08-15T12:00:00Z",
                    "generation": 1,
                    "binding_fingerprint": _browserbase_fingerprint(project_id),
                    "created_at": "2026-08-15T12:00:00Z",
                    "updated_at": "2026-08-15T12:00:00Z",
                }
            ],
        },
        rpc_results={"get_browserbase_credential_state": {"epoch": 1}},
    )
    client = TestClient(
        create_app(settings=browser_settings(encryption_key), auth=FakeAuth(), store=store)
    )

    response = client.get("/api/v1/provider-credentials")

    assert response.status_code == 200, response.text
    payload = response.json()
    browserbase = next(
        item for item in payload["items"] if item["provider"] == "browserbase"
    )
    assert browserbase["configured"] is True
    assert browserbase["key_hint"].endswith("cret")
    assert browserbase["project_id_hint"] == "tenant-p…atus"
    assert browserbase["key_url"] == "https://www.browserbase.com/settings"
    assert browserbase["signup_url"] == "https://www.browserbase.com/sign-up"
    assert browserbase["project_url"] == "https://www.browserbase.com/overview"
    assert api_key not in response.text
    assert project_id not in response.text
    assert "credential_ciphertext" not in response.text
