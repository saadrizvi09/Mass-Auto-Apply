"""Persistent Gmail delivery for reviewed outreach messages.

This module runs only in the trusted worker process. Queue payloads contain no
tokens or message bodies; all sensitive values are loaded from Supabase after a
lease is claimed and are decrypted only for the one Gmail request.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from app.saas.config import Settings
from app.saas.crypto import TokenCipher, TokenCipherError
from app.saas.gmail import (
    GoogleProviderError,
    refresh_google_access_token,
    send_gmail_message,
)
from worker.handlers import AutomationJob, HandlerOutcome


EMAIL_JOB_KIND = "send_email"


class RetryableEmailError(RuntimeError):
    """A provider response that is known not to have sent the message."""


class EmailRepository(Protocol):
    async def fetch_one(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Mapping[str, Any] | None = None,
        required: bool = False,
    ) -> dict[str, Any] | None: ...

    async def update(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        filters: Mapping[str, Any],
        returning: bool = True,
    ) -> Any: ...

    async def download_object(self, bucket: str, path: str) -> bytes: ...

    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any: ...


def _first_row(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return value[0]
    return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _failure(code: str, message: str) -> HandlerOutcome:
    return HandlerOutcome(
        status="needs_attention",
        code=code,
        message=message,
        provider="gmail",
        connection_required=code in {"gmail_not_connected", "gmail_reauthorization_required"},
    )


class EmailJobHandler:
    """Claimed-row handler that sends one already-approved application."""

    def __init__(
        self,
        repository: EmailRepository,
        *,
        settings: Settings,
        fallback: Callable[[AutomationJob], HandlerOutcome | Any],
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.fallback = fallback

    async def _oauth_client(self, user_id: str, connection: Mapping[str, Any]) -> tuple[str, str]:
        metadata = connection.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        source = metadata.get("oauth_client_source", "platform")
        if source == "platform":
            if not self.settings.google_configured:
                raise ValueError("gmail_platform_oauth_unavailable")
            return self.settings.google_client_id, self.settings.google_client_secret
        if source != "user" or not self.settings.google_byoc_ready:
            raise ValueError("gmail_oauth_client_stale")
        row = await self.repository.fetch_one(
            "user_google_oauth_clients",
            columns="client_id_ciphertext,client_secret_ciphertext,generation",
            filters={"user_id": user_id},
        )
        if row is None:
            raise ValueError("gmail_oauth_client_missing")
        generation = metadata.get("oauth_client_generation")
        if not isinstance(generation, int) or generation < 1 or row.get("generation") != generation:
            raise ValueError("gmail_oauth_client_stale")
        cipher = TokenCipher.from_settings(self.settings)
        return (
            cipher.decrypt(row["client_id_ciphertext"]),
            cipher.decrypt(row["client_secret_ciphertext"]),
        )

    async def _access_token(self, job: AutomationJob, connection: Mapping[str, Any], secret: Mapping[str, Any]) -> str:
        cipher = TokenCipher.from_settings(self.settings)
        access_token = cipher.decrypt_optional(secret.get("access_token_ciphertext"))
        refresh_token = cipher.decrypt_optional(secret.get("refresh_token_ciphertext"))
        expires_at = _timestamp(connection.get("expires_at"))
        if access_token and (expires_at is None or expires_at > datetime.now(UTC) + timedelta(seconds=90)):
            return access_token
        if not refresh_token:
            raise ValueError("gmail_reauthorization_required")
        client_id, client_secret = await self._oauth_client(job.user_id, connection)
        refreshed = await asyncio.to_thread(
            refresh_google_access_token, refresh_token, client_id, client_secret
        )
        new_access = refreshed["access_token"]
        new_refresh = refreshed.get("refresh_token", refresh_token)
        await self.repository.update(
            "connection_secrets",
            {
                "access_token_ciphertext": cipher.encrypt(new_access),
                "refresh_token_ciphertext": cipher.encrypt(new_refresh),
                "token_type": refreshed.get("token_type", "Bearer"),
            },
            filters={"connection_id": connection["id"], "user_id": job.user_id},
            returning=False,
        )
        expires_in = refreshed.get("expires_in")
        if isinstance(expires_in, int) and not isinstance(expires_in, bool):
            await self.repository.update(
                "connections",
                {
                    "expires_at": (datetime.now(UTC) + timedelta(seconds=max(0, expires_in))).isoformat(),
                    "last_verified_at": datetime.now(UTC).isoformat(),
                },
                filters={"id": connection["id"], "user_id": job.user_id},
                returning=False,
            )
        return new_access

    async def _finalize(
        self,
        application_id: str,
        idempotency_key: str,
        outcome: str,
        *,
        message_id: str | None = None,
        thread_id: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        try:
            row = _first_row(
                await self.repository.rpc(
                    "finalize_application_send",
                    {
                        "application_id": application_id,
                        "idempotency_key": idempotency_key,
                        "outcome": outcome,
                        "provider_message_id": message_id,
                        "provider_thread_id": thread_id,
                        "error_code": error_code,
                    },
                )
            )
        except Exception:
            return False
        return row is not None

    async def __call__(self, job: AutomationJob) -> HandlerOutcome:
        if job.kind != EMAIL_JOB_KIND:
            result = self.fallback(job)
            return await result if inspect.isawaitable(result) else result

        application_id = job.application_id
        if not application_id:
            return _failure("application_required", "The queued email is missing its application.")
        application = await self.repository.fetch_one(
            "applications",
            filters={"id": application_id, "user_id": job.user_id},
        )
        if application is None:
            return _failure("application_not_found", "The queued application no longer exists.")
        if application.get("status") == "sent":
            return HandlerOutcome(
                status="succeeded", code="email_already_sent", message="This email was already sent.", provider="gmail"
            )
        if application.get("status") != "queued":
            return _failure("application_not_queued", "The application is no longer queued for delivery.")

        idempotency_key = application.get("send_idempotency_key") or job.payload.get("idempotency_key") or job.id
        if not isinstance(idempotency_key, str):
            return _failure("send_idempotency_missing", "The queued email has no send identifier.")
        connection = await self.repository.fetch_one(
            "connections",
            filters={"user_id": job.user_id, "provider": "gmail", "status": "connected"},
        )
        if connection is None:
            await self._finalize(application_id, idempotency_key, "failed", error_code="gmail_not_connected")
            return _failure("gmail_not_connected", "Connect Gmail before queued messages can be sent.")
        secret = await self.repository.fetch_one(
            "connection_secrets",
            filters={"connection_id": connection["id"], "user_id": job.user_id},
        )
        if secret is None:
            await self._finalize(application_id, idempotency_key, "failed", error_code="gmail_reauthorization_required")
            return _failure("gmail_reauthorization_required", "Reconnect Gmail before queued messages can be sent.")

        try:
            access_token = await self._access_token(job, connection, secret)
            pdf_bytes: bytes | None = None
            pdf_filename = "resume.pdf"
            if job.payload.get("attach_resume") is not False:
                resume = await self.repository.fetch_one(
                    "resumes", filters={"user_id": job.user_id, "is_active": True}
                )
                if resume is None:
                    raise ValueError("resume_required")
                pdf_bytes = await self.repository.download_object("resumes", resume["storage_path"])
                if len(pdf_bytes) > self.settings.max_resume_bytes or not pdf_bytes.lstrip().startswith(b"%PDF-"):
                    raise ValueError("resume_invalid_pdf")
                pdf_filename = resume.get("original_name") or "resume.pdf"
        except (TokenCipherError, KeyError, ValueError) as exc:
            code = str(exc) if str(exc) in {
                "gmail_reauthorization_required", "gmail_oauth_client_stale", "gmail_oauth_client_missing",
                "gmail_platform_oauth_unavailable", "resume_required", "resume_invalid_pdf",
            } else "send_preparation_failed"
            await self._finalize(application_id, idempotency_key, "failed", error_code=code)
            return _failure(code, "The queued email could not be prepared. Review the message and connection settings.")

        try:
            sent = await asyncio.to_thread(
                send_gmail_message,
                access_token,
                application["recipient"],
                application["subject"],
                application["body"],
                sender=connection.get("display_name"),
                pdf_bytes=pdf_bytes,
                pdf_filename=pdf_filename,
            )
        except GoogleProviderError as exc:
            # Gmail's 429 response is explicit evidence that this request was
            # rejected before dispatch. Let the generic worker requeue it with
            # bounded backoff; transport errors remain needs-attention because
            # Gmail may have accepted them.
            if exc.code == "gmail_rate_limited":
                raise RetryableEmailError("gmail_rate_limited") from exc
            outcome = "needs_attention" if exc.code == "gmail_send_ambiguous" else "failed"
            finalized = await self._finalize(application_id, idempotency_key, outcome, error_code=exc.code)
            if not finalized:
                return _failure("send_confirmation_missing", "Gmail responded, but the send result needs reconciliation.")
            return _failure(exc.code, str(exc))
        except Exception:
            finalized = await self._finalize(application_id, idempotency_key, "needs_attention", error_code="gmail_send_unconfirmed")
            if not finalized:
                return _failure("send_confirmation_missing", "Gmail may have received the message; reconcile it before retrying.")
            return _failure("gmail_send_ambiguous", "Gmail did not confirm the send; reconcile it before retrying.")

        finalized = await self._finalize(
            application_id,
            idempotency_key,
            "sent",
            message_id=sent.get("id"),
            thread_id=sent.get("thread_id"),
        )
        if not finalized:
            return _failure("send_confirmation_missing", "Gmail accepted the message; reconcile the application before retrying.")
        return HandlerOutcome(
            status="succeeded",
            code="email_sent",
            message="The approved email was sent through the persistent Gmail worker.",
            provider="gmail",
        )


def build_email_job_handler(
    repository: EmailRepository,
    *,
    settings: Settings,
    fallback: Callable[[AutomationJob], HandlerOutcome | Any],
) -> EmailJobHandler:
    return EmailJobHandler(repository, settings=settings, fallback=fallback)


__all__ = ["EMAIL_JOB_KIND", "EmailJobHandler", "build_email_job_handler"]
