from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from worker.discovery_runtime import DiscoveryJobHandler
from worker.handlers import AutomationJob, handle_job


def _record(kind: str, provider: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "user_id": "00000000-0000-0000-0000-000000000002",
        "kind": kind,
        "provider": provider,
        "attempts": 1,
        "payload": dict(payload),
        "application_id": None,
    }


def _job(secret: str = "private listing body") -> dict[str, Any]:
    return {
        "source": "rss",
        "external_id": "source-one",
        "apply_url": "https://jobs.ashbyhq.com/acme/role",
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Remote",
        "description": secret,
        "contact_email": None,
        "metadata": {"provider": "ashby"},
    }


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any:
        self.calls.append((name, params))
        return {"count": 1, "inserted": 1, "updated": 0, "items": [{"private": True}]}


def test_public_feed_worker_keeps_tenant_binding_in_service_rpc_and_redacts_jobs() -> None:
    secret = "do-not-copy-this-job-description-to-the-result"
    repository = FakeRepository()

    def telegram_failure() -> list[dict[str, Any]]:
        raise RuntimeError("temporary source failure")

    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        telegram_discovery=telegram_failure,
        rss_discovery=lambda: [_job(secret)],
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_feeds",
            "public_feeds",
            {"source_ids": ["telegram", "rss"], "limit": 20, "user_id": "attacker"},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "succeeded"
    result = outcome.as_result()
    assert result["saved_count"] == 1
    assert result["source_errors"] == [
        {"source": "telegram", "code": "source_unavailable"}
    ]
    assert secret not in repr(result)
    assert repository.calls[0][0] == "ingest_discovered_jobs"
    params = repository.calls[0][1]
    assert params["job_id"] == job.id
    assert params["worker_id"] == "worker-1"
    assert "user_id" not in params
    assert params["jobs"][0]["normalized_url"] == params["jobs"][0]["apply_url"]


def test_combined_feed_run_fairly_includes_telegram_and_rss() -> None:
    repository = FakeRepository()

    def jobs(source: str) -> list[dict[str, Any]]:
        return [
            {
                **_job(),
                "source": source,
                "external_id": f"{source}-{index}",
                "apply_url": f"https://jobs.ashbyhq.com/acme/{source}-{index}",
            }
            for index in range(20)
        ]

    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        telegram_discovery=lambda: jobs("telegram"),
        rss_discovery=lambda: jobs("rss"),
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_feeds",
            "public_feeds",
            {"source_ids": ["telegram", "rss"], "limit": 10},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "succeeded"
    serialized = repository.calls[0][1]["jobs"]
    assert [item["source"] for item in serialized] == ["telegram", "rss"] * 5


def test_public_feed_search_terms_filter_each_source_before_fair_merge() -> None:
    repository = FakeRepository()

    def source_jobs(source: str) -> list[dict[str, Any]]:
        return [
            {
                **_job(),
                "source": source,
                "external_id": f"{source}-javascript",
                "apply_url": f"https://example.com/{source}/javascript",
                "title": "Javascript Developer",
                "company": "Unrelated Co",
            },
            {
                **_job(),
                "source": source,
                "external_id": f"{source}-platform",
                "apply_url": f"https://example.com/{source}/platform",
                "title": "Senior Platform-Engineer",
                "company": "Relevant Co",
            },
            {
                **_job(),
                "source": source,
                "external_id": f"{source}-java",
                "apply_url": f"https://example.com/{source}/java",
                "title": "Developer",
                "company": "Relevant Co",
                "description": "Build services with Java and Spring.",
            },
        ]

    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        telegram_discovery=lambda: source_jobs("telegram"),
        rss_discovery=lambda: source_jobs("rss"),
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_feeds",
            "public_feeds",
            {
                "source_ids": ["telegram", "rss"],
                "limit": 4,
                "search_terms": ["PLATFORM ENGINEER", "java"],
            },
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "succeeded"
    rows = repository.calls[0][1]["jobs"]
    assert [(row["source"], row["external_id"]) for row in rows] == [
        ("telegram", "telegram-platform"),
        ("rss", "rss-platform"),
        ("telegram", "telegram-java"),
        ("rss", "rss-java"),
    ]


def test_public_feed_empty_search_terms_preserve_unfiltered_manual_run() -> None:
    repository = FakeRepository()
    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        telegram_discovery=lambda: [{**_job(), "source": "telegram"}],
        rss_discovery=lambda: [],
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_feeds",
            "public_feeds",
            {"source_ids": ["telegram"], "limit": 10, "search_terms": []},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "succeeded"
    assert len(repository.calls[0][1]["jobs"]) == 1


@pytest.mark.parametrize(
    "search_terms",
    [
        "backend engineer",
        ["term"] * 21,
        ["x" * 101],
        ["   "],
        [42],
    ],
)
def test_public_feed_rejects_invalid_search_terms_before_network(
    search_terms: object,
) -> None:
    network_calls = 0

    def telegram() -> list[dict[str, Any]]:
        nonlocal network_calls
        network_calls += 1
        return []

    repository = FakeRepository()
    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        telegram_discovery=telegram,
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_feeds",
            "public_feeds",
            {"source_ids": ["telegram"], "search_terms": search_terms},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.code == "discovery_payload_invalid"
    assert network_calls == 0
    assert repository.calls == []


def test_linkedin_worker_applies_hard_bounds_before_network_discovery() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def linkedin(keywords: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append((keywords, kwargs))
        return []

    repository = FakeRepository()
    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        linkedin_discovery=linkedin,
    )
    job = AutomationJob.from_record(
        _record(
            "discover_linkedin_guest",
            "linkedin",
            {
                "keywords": "backend engineer",
                "location": "India",
                "remote": True,
                "limit": 25,
            },
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.code == "discovery_completed"
    assert calls == [
        (
            "backend engineer",
            {"location": "India", "remote": True, "limit": 25, "max_pages": 2},
        )
    ]
    assert repository.calls == []


def test_linkedin_source_failure_is_needs_attention_not_empty_success() -> None:
    repository = FakeRepository()

    def unavailable(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("provider detail")

    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        linkedin_discovery=unavailable,
    )
    job = AutomationJob.from_record(
        _record(
            "discover_linkedin_guest",
            "linkedin",
            {"keywords": "backend engineer", "location": "India", "limit": 20},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "needs_attention"
    assert outcome.code == "discovery_source_unavailable"
    assert repository.calls == []


def test_invalid_discovery_payload_never_calls_network_or_persistence() -> None:
    network_calls = 0

    def linkedin(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal network_calls
        network_calls += 1
        return []

    repository = FakeRepository()
    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        linkedin_discovery=linkedin,
    )
    job = AutomationJob.from_record(
        _record(
            "discover_linkedin_guest",
            "linkedin",
            {"keywords": "x" * 101, "location": "India", "limit": 25},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.code == "discovery_payload_invalid"
    assert network_calls == 0
    assert repository.calls == []


def test_public_ats_worker_interleaves_boards_and_uses_lease_bound_ingest() -> None:
    calls: list[tuple[str, int]] = []

    def public_ats(board_url: str, *, limit: int) -> list[dict[str, Any]]:
        calls.append((board_url, limit))
        provider = "lever" if "lever" in board_url else "ashby"
        return [
            {
                **_job(),
                "source": "public_ats",
                "external_id": f"{provider}-{index}",
                "apply_url": f"{board_url}/{provider}-{index}",
                "metadata": {"provider": provider},
            }
            for index in range(5)
        ]

    repository = FakeRepository()
    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        public_ats_discovery=public_ats,
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_ats",
            "public_ats",
            {
                "board_urls": [
                    "https://jobs.lever.co/acme/one",
                    "https://jobs.ashbyhq.com/beta/two",
                ],
                "limit": 6,
                "user_id": "attacker",
            },
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "succeeded"
    assert calls == [
        ("https://jobs.lever.co/acme", 6),
        ("https://jobs.ashbyhq.com/beta", 6),
    ]
    rows = repository.calls[0][1]["jobs"]
    assert [row["metadata"]["provider"] for row in rows] == [
        "lever",
        "ashby",
        "lever",
        "ashby",
        "lever",
        "ashby",
    ]
    assert repository.calls[0][1]["job_id"] == job.id
    assert "user_id" not in repository.calls[0][1]


def test_public_ats_worker_reports_bad_payload_before_network() -> None:
    network_calls = 0

    def public_ats(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal network_calls
        network_calls += 1
        return []

    repository = FakeRepository()
    handler = DiscoveryJobHandler(
        repository,
        worker_id="worker-1",
        fallback=handle_job,
        public_ats_discovery=public_ats,
    )
    job = AutomationJob.from_record(
        _record(
            "discover_public_ats",
            "public_ats",
            {"board_urls": ["https://evil.example/acme"], "limit": 100},
        )
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "needs_attention"
    assert outcome.code == "discovery_payload_invalid"
    assert network_calls == 0
    assert repository.calls == []


def test_worker_image_packages_the_public_ats_adapter() -> None:
    dockerfile = (Path(__file__).parents[1] / "worker" / "Dockerfile").read_text()
    assert "app/saas/discovery/public_ats.py" in dockerfile
