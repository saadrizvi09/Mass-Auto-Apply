from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet

import worker.email_runtime as email_runtime
from app.saas.config import Settings
from app.saas.gmail import GoogleProviderError
from worker.email_runtime import EmailJobHandler, RetryableEmailError
from worker.handlers import AutomationJob, HandlerOutcome, handle_job


USER_ID = "00000000-0000-0000-0000-000000000002"
APPLICATION_ID = "00000000-0000-0000-0000-000000000003"
CONNECTION_ID = "00000000-0000-0000-0000-000000000004"


class FakeEmailRepository:
    def __init__(self) -> None:
        self.rpc_calls: list[tuple[str, Mapping[str, Any]]] = []
        self.updates: list[tuple[str, Mapping[str, Any]]] = []
        self.application = {
            "id": APPLICATION_ID,
            "user_id": USER_ID,
            "status": "queued",
            "send_idempotency_key": "email-request-123",
            "recipient": "recruiter@example.test",
            "subject": "Application",
            "body": "Hello from the reviewed draft.",
        }
        self.connection = {
            "id": CONNECTION_ID,
            "user_id": USER_ID,
            "provider": "gmail",
            "status": "connected",
            "display_name": "owner@example.test",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "metadata": {},
        }
        self.secret: dict[str, Any] = {}

    async def fetch_one(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Mapping[str, Any] | None = None,
        required: bool = False,
    ) -> dict[str, Any] | None:
        if table == "applications":
            return self.application
        if table == "connections":
            return self.connection
        if table == "connection_secrets":
            return self.secret
        return None

    async def update(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        filters: Mapping[str, Any],
        returning: bool = True,
    ) -> Any:
        self.updates.append((table, values))
        return []

    async def download_object(self, bucket: str, path: str) -> bytes:
        raise AssertionError("The no-attachment test must not read a résumé")

    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any:
        self.rpc_calls.append((name, params))
        return [{"outcome": params.get("outcome", "sent")}]


def _settings() -> Settings:
    return Settings(token_encryption_key=Fernet.generate_key().decode())


def _job() -> AutomationJob:
    return AutomationJob.from_record(
        {
            "id": "00000000-0000-0000-0000-000000000005",
            "user_id": USER_ID,
            "kind": "send_email",
            "provider": "gmail",
            "attempts": 1,
            "application_id": APPLICATION_ID,
            "payload": {"attach_resume": False},
        }
    )


def test_email_worker_sends_only_from_a_claimed_job_and_finalizes_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeEmailRepository()
    settings = _settings()
    cipher = email_runtime.TokenCipher.from_settings(settings)
    repository.secret = {
        "access_token_ciphertext": cipher.encrypt("access-token"),
        "refresh_token_ciphertext": None,
    }
    sent: list[tuple[str, str, str, str]] = []

    def send(token: str, recipient: str, subject: str, body: str, **kwargs: Any) -> dict[str, str]:
        sent.append((token, recipient, subject, body))
        return {"id": "gmail-message-id", "thread_id": "gmail-thread-id"}

    monkeypatch.setattr(email_runtime, "send_gmail_message", send)
    outcome = asyncio.run(
        EmailJobHandler(repository, settings=settings, fallback=handle_job)(_job())
    )

    assert isinstance(outcome, HandlerOutcome)
    assert outcome.status == "succeeded"
    assert sent == [
        ("access-token", "recruiter@example.test", "Application", "Hello from the reviewed draft.")
    ]
    assert repository.rpc_calls[0][0] == "finalize_application_send"
    assert repository.rpc_calls[0][1]["outcome"] == "sent"
    assert repository.rpc_calls[0][1]["provider_message_id"] == "gmail-message-id"


def test_gmail_rate_limit_is_retried_by_the_generic_worker_without_finalizing_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeEmailRepository()
    settings = _settings()
    cipher = email_runtime.TokenCipher.from_settings(settings)
    repository.secret = {"access_token_ciphertext": cipher.encrypt("access-token")}

    def rate_limited(*_args: Any, **_kwargs: Any) -> None:
        raise GoogleProviderError("gmail_rate_limited", "try later")

    monkeypatch.setattr(email_runtime, "send_gmail_message", rate_limited)
    handler = EmailJobHandler(repository, settings=settings, fallback=handle_job)

    with pytest.raises(RetryableEmailError, match="gmail_rate_limited"):
        asyncio.run(handler(_job()))
    assert repository.rpc_calls == []
