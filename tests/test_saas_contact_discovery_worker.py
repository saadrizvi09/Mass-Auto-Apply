from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from worker.discovery_runtime import DiscoveryJobHandler
from worker.handlers import AutomationJob, handle_job


class ContactRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any:
        self.calls.append((name, params))
        if name == "get_public_contact_discovery_bundle":
            return {
                "jobs": [
                    {
                        "id": "00000000-0000-0000-0000-000000000010",
                        "apply_url": "https://acme.example/jobs/one",
                        "company": "Acme",
                        "metadata": {},
                    },
                    {
                        "id": "00000000-0000-0000-0000-000000000011",
                        "apply_url": "https://beta.example/jobs/two",
                        "company": "Beta",
                        "metadata": {},
                    },
                ],
                "max_contacts": 30,
                "max_pages": 8,
                "timeout_seconds": 15,
            }
        if name == "store_public_job_contacts":
            return {"count": len(params["contacts_input"])}
        raise AssertionError(name)


def test_contact_worker_uses_bundle_lease_and_persists_deduplicated_contacts() -> None:
    repository = ContactRepository()

    def contact_discovery(job: Mapping[str, Any], **_kwargs: Any) -> list[Mapping[str, Any]]:
        return [
            {
                "job_id": job["id"],
                "company_key": str(job["company"]).casefold(),
                "email": "Recruiting@Example.com",
                "contact_type": "recruiting_inbox",
                "source_url": job["apply_url"],
                "source_date": "2026-09-06",
                "contact_source": "visible email on public company page",
                "email_verification_status": "public_source_verified",
            }
        ]

    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-contacts",
        fallback=handle_job,
        contact_discovery=contact_discovery,
    )
    job = AutomationJob.from_record(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "user_id": "00000000-0000-0000-0000-000000000002",
            "kind": "discover_public_contacts",
            "provider": "public_contacts",
            "attempts": 1,
            "payload": {},
        }
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "succeeded"
    assert outcome.code == "contact_discovery_completed"
    assert outcome.as_result()["jobs_searched"] == 2
    assert outcome.as_result()["contacts_saved"] == 2
    assert repository.calls[0][0] == "get_public_contact_discovery_bundle"
    assert repository.calls[0][1] == {
        "queue_job_id_input": job.id,
        "worker_id_input": "worker-contacts",
    }
    assert repository.calls[1][0] == "store_public_job_contacts"
    assert all(
        item["email"] == "recruiting@example.com"
        for item in repository.calls[1][1]["contacts_input"]
    )


def test_contact_worker_records_source_failure_without_exposing_exception_text() -> None:
    repository = ContactRepository()

    def unavailable(*_args: Any, **_kwargs: Any) -> list[Mapping[str, Any]]:
        raise RuntimeError("secret source response should not be persisted")

    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-contacts",
        fallback=handle_job,
        contact_discovery=unavailable,
    )
    job = AutomationJob.from_record(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "user_id": "00000000-0000-0000-0000-000000000002",
            "kind": "discover_public_contacts",
            "provider": "public_contacts",
            "attempts": 1,
            "payload": {},
        }
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "needs_attention"
    assert outcome.code == "contact_sources_unavailable"
    assert "secret source" not in repr(outcome.as_result())
    assert repository.calls[-1][0] == "store_public_job_contacts"
    assert repository.calls[-1][1]["contacts_input"] == []
