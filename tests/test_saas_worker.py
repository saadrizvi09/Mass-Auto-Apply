from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import pytest

from worker.handlers import SUPPORTED_JOB_KINDS, AutomationJob, handle_job
from worker.main import BoundedBackoff, SupabaseQueueStore, Worker, WorkerConfig


def _record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "user_id": "00000000-0000-0000-0000-000000000002",
        "kind": "manual_handoff",
        "provider": "external_job_board",
        "attempts": 1,
        "payload": {},
        "application_id": None,
    }
    record.update(overrides)
    return record


def test_payload_is_not_represented_or_copied_to_handler_result() -> None:
    secret = "do-not-log-this-private-answer"
    job = AutomationJob.from_record(_record(payload={"answer": secret}))

    outcome = handle_job(job)

    assert outcome.status == "needs_attention"
    assert secret not in repr(job)
    assert secret not in repr(outcome.as_result())
    assert outcome.as_result()["outcome"] == "needs_attention"


@pytest.mark.parametrize(
    "kind",
    ["manual_handoff", "ats_prepare", "connection_check", "future_submit"],
)
def test_linkedin_never_enters_an_automation_handler(kind: str) -> None:
    outcome = handle_job(AutomationJob.from_record(_record(kind=kind, provider="linkedin")))

    assert outcome.status == "needs_attention"
    assert outcome.code == "linkedin_partner_required"
    assert outcome.provider == "linkedin"


@pytest.mark.parametrize(
    "payload",
    [
        {"captcha_detected": True},
        {"mfa_required": True},
        {"security_checkpoint": True},
        {"checkpoint": "verification"},
    ],
)
def test_security_checkpoints_always_require_user_attention(
    payload: Mapping[str, Any],
) -> None:
    outcome = handle_job(AutomationJob.from_record(_record(payload=payload)))

    assert outcome.status == "needs_attention"
    assert outcome.code == "security_checkpoint"


def test_manual_provider_connection_check_succeeds_without_claiming_a_connection() -> None:
    outcome = handle_job(
        AutomationJob.from_record(
            _record(kind="connection_check", provider="external_job_board")
        )
    )

    assert outcome.status == "succeeded"
    assert outcome.code == "connection_not_required"
    assert outcome.connection_required is False


def test_managed_browser_check_only_reports_deployment_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOWED_BROWSER_PROVIDERS", "greenhouse")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "configured-for-test")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "configured-for-test")

    outcome = handle_job(
        AutomationJob.from_record(_record(kind="connection_check", provider="greenhouse"))
    )

    assert outcome.status == "succeeded"
    assert outcome.code == "deployment_capability_ready"
    assert outcome.connection_required is True
    assert "submitted" not in outcome.message.lower()


def test_ats_prepare_does_not_claim_submission() -> None:
    outcome = handle_job(
        AutomationJob.from_record(_record(kind="ats_prepare", provider="greenhouse"))
    )

    assert outcome.status == "needs_attention"
    assert outcome.code == "provider_handler_not_enabled"
    assert "submitted" not in outcome.message.lower()


def test_invalid_claimed_payload_is_rejected_without_exposing_it() -> None:
    with pytest.raises(ValueError, match="invalid payload"):
        AutomationJob.from_record(_record(payload="not-a-json-object"))


# Poller tests are below the handler tests so the safety behavior remains useful even
# when the Supabase repository is replaced by a fake.
class FakeQueueStore:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.records = list(records or [])
        self.claim_calls: list[tuple[str, int, tuple[str, ...]]] = []
        self.heartbeat_calls: list[tuple[str, str, int]] = []
        self.complete_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_calls: list[tuple[str, str, str, str, int]] = []
        self.closed = False

    async def claim_automation_job(
        self, worker_id: str, lease_seconds: int, kinds: tuple[str, ...]
    ) -> dict[str, Any] | None:
        self.claim_calls.append((worker_id, lease_seconds, kinds))
        return self.records.pop(0) if self.records else None

    async def heartbeat_automation_job(
        self, job_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        self.heartbeat_calls.append((job_id, worker_id, lease_seconds))
        return True

    async def complete_automation_job(
        self, job_id: str, worker_id: str, result: dict[str, Any]
    ) -> bool:
        self.complete_calls.append((job_id, worker_id, result))
        return True

    async def fail_automation_job(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int,
    ) -> bool:
        self.fail_calls.append(
            (job_id, worker_id, error_code, error_message, retry_after_seconds)
        )
        return True

    async def close(self) -> None:
        self.closed = True


def _config() -> WorkerConfig:
    return WorkerConfig(
        worker_id="worker-test-1",
        poll_seconds=0.1,
        lease_seconds=60,
        retry_base_seconds=7,
        max_retry_seconds=60,
    )


def test_worker_claims_heartbeats_and_completes_with_redacted_result() -> None:
    store = FakeQueueStore([_record(payload={"private": "not-returned"})])
    worker = Worker(store, _config())

    assert asyncio.run(worker.run_once()) is True
    assert store.claim_calls == [
        ("worker-test-1", 60, SUPPORTED_JOB_KINDS)
    ]
    assert store.heartbeat_calls == [
        ("00000000-0000-0000-0000-000000000001", "worker-test-1", 60)
    ]
    assert len(store.complete_calls) == 1
    result = store.complete_calls[0][2]
    assert result["outcome"] == "needs_attention"
    assert "private" not in repr(result)
    assert not store.fail_calls


def test_lost_lease_prevents_handler_and_terminal_update() -> None:
    store = FakeQueueStore([_record()])
    async def lose_lease(*_args: Any) -> bool:
        return False

    store.heartbeat_automation_job = lose_lease  # type: ignore[method-assign]
    calls = 0

    def count_handler(job: AutomationJob):
        nonlocal calls
        calls += 1
        return handle_job(job)

    worker = Worker(store, _config(), handler=count_handler)

    assert asyncio.run(worker.run_once()) is True
    assert calls == 0
    assert not store.complete_calls
    assert not store.fail_calls


def test_async_handler_renews_lease_until_it_finishes() -> None:
    store = FakeQueueStore([_record()])
    config = WorkerConfig(
        worker_id="worker-test-1",
        poll_seconds=0.1,
        lease_seconds=60,
        heartbeat_seconds=0.01,
    )

    async def slow_handler(job: AutomationJob):
        await asyncio.sleep(0.035)
        return handle_job(job)

    worker = Worker(store, config, handler=slow_handler)

    assert asyncio.run(worker.run_once()) is True
    assert len(store.heartbeat_calls) >= 3
    assert len(store.complete_calls) == 1


def test_lost_lease_cancels_async_handler_without_retry_or_terminal_write() -> None:
    store = FakeQueueStore([_record()])
    heartbeat_count = 0
    cancelled = False

    async def expiring_heartbeat(*_args: Any) -> bool:
        nonlocal heartbeat_count
        heartbeat_count += 1
        return heartbeat_count == 1

    async def slow_handler(_job: AutomationJob):
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        finally:
            cancelled = True

    store.heartbeat_automation_job = expiring_heartbeat  # type: ignore[method-assign]
    config = WorkerConfig(
        worker_id="worker-test-1",
        poll_seconds=0.1,
        lease_seconds=60,
        heartbeat_seconds=0.01,
    )
    worker = Worker(store, config, handler=slow_handler)

    assert asyncio.run(worker.run_once()) is True
    assert cancelled is True
    assert not store.complete_calls
    assert not store.fail_calls


def test_handler_exception_is_retried_without_logging_or_persisting_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-private-provider-value"
    store = FakeQueueStore([_record(payload={"token": secret}, attempts=2)])

    def failing_handler(_job: AutomationJob):
        raise RuntimeError(f"provider response included {secret}")

    worker = Worker(store, _config(), handler=failing_handler)
    with caplog.at_level(logging.INFO, logger="autoapply.worker"):
        assert asyncio.run(worker.run_once()) is True

    assert secret not in caplog.text
    assert not store.complete_calls
    assert len(store.fail_calls) == 1
    _, _, code, message, retry_after = store.fail_calls[0]
    assert code == "worker_handler_error"
    assert secret not in message
    assert retry_after == 14


def test_worker_closes_store_when_already_stopped() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    store = FakeQueueStore()

    asyncio.run(Worker(store, _config(), stop_event=stop_event).run_forever())

    assert store.closed is True
    assert not store.claim_calls


def test_queue_backoff_is_exponential_jittered_and_bounded() -> None:
    backoff = BoundedBackoff(base_seconds=2.0, maximum_seconds=10.0)

    assert backoff.delay(1, random_value=0.5) == 2.0
    assert backoff.delay(2, random_value=0.5) == 4.0
    assert backoff.delay(20, random_value=1.0) == 10.0


def test_worker_config_rejects_missing_or_unsafe_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKER_ID", raising=False)
    with pytest.raises(ValueError, match="WORKER_ID"):
        WorkerConfig.from_env()

    monkeypatch.setenv("WORKER_ID", "bad id containing spaces")
    with pytest.raises(ValueError, match="WORKER_ID"):
        WorkerConfig.from_env()


class FakeRpcRepository:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.closed = False

    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any:
        self.calls.append((name, params))
        return self.responses[name]

    async def close(self) -> None:
        self.closed = True


def test_supabase_queue_store_uses_stable_rpc_contracts() -> None:
    record = _record(status="running")
    repository = FakeRpcRepository(
        {
            "claim_automation_job": [record],
            "heartbeat_automation_job": [{**record, "status": "running"}],
            "complete_automation_job": [{**record, "status": "needs_attention"}],
            "fail_automation_job": [{**record, "status": "queued"}],
        }
    )
    store = SupabaseQueueStore(repository)

    async def exercise_store() -> None:
        claimed = await store.claim_automation_job("worker-1", 120, ("manual_handoff",))
        assert claimed == record
        assert await store.heartbeat_automation_job(record["id"], "worker-1", 120) is True
        assert await store.complete_automation_job(
            record["id"], "worker-1", {"outcome": "needs_attention"}
        ) is True
        assert await store.fail_automation_job(
            record["id"], "worker-1", "temporary", "Try again later.", 30
        ) is True
        await store.close()

    asyncio.run(exercise_store())

    assert repository.calls == [
        (
            "claim_automation_job",
            {
                "worker_id": "worker-1",
                "lease_seconds": 120,
                "kinds": ["manual_handoff"],
            },
        ),
        (
            "heartbeat_automation_job",
            {
                "job_id": record["id"],
                "worker_id": "worker-1",
                "lease_seconds": 120,
            },
        ),
        (
            "complete_automation_job",
            {
                "job_id": record["id"],
                "worker_id": "worker-1",
                "result": {"outcome": "needs_attention"},
                "terminal_status": "needs_attention",
            },
        ),
        (
            "fail_automation_job",
            {
                "job_id": record["id"],
                "worker_id": "worker-1",
                "error_code": "temporary",
                "error_message": "Try again later.",
                "retry_after_seconds": 30,
            },
        ),
    ]
    assert repository.closed is True
