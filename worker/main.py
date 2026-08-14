"""Lease durable automation jobs from Supabase and run bounded handlers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import re
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from worker.handlers import (
    SUPPORTED_JOB_KINDS,
    AutomationJob,
    HandlerOutcome,
    handle_job,
)


LOGGER = logging.getLogger("autoapply.worker")
_WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WorkerConfigurationError(ValueError):
    """Raised for a missing or unsafe worker-only environment setting."""


class WorkerLeaseLost(RuntimeError):
    """The queue lease ended while an async handler was still running."""


class QueueStore(Protocol):
    """Queue operations named after the stable database RPC contracts."""

    async def claim_automation_job(
        self, worker_id: str, lease_seconds: int, kinds: tuple[str, ...]
    ) -> Mapping[str, Any] | None: ...

    async def heartbeat_automation_job(
        self, job_id: str, worker_id: str, lease_seconds: int
    ) -> bool: ...

    async def complete_automation_job(
        self, job_id: str, worker_id: str, result: dict[str, Any]
    ) -> bool: ...

    async def fail_automation_job(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int,
    ) -> bool: ...

    async def close(self) -> None: ...


class RpcRepository(Protocol):
    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any: ...


def _first_row(value: Any) -> Mapping[str, Any] | None:
    """Normalize the SETOF row shape returned by Supabase PostgREST."""

    if isinstance(value, Mapping):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        first = value[0] if value else None
        return first if isinstance(first, Mapping) else None
    return None


class SupabaseQueueStore:
    """Typed queue facade over the server-secret Supabase repository."""

    def __init__(self, repository: RpcRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> RpcRepository:
        """Expose the same server client to trusted worker-only extensions."""

        return self._repository

    async def claim_automation_job(
        self, worker_id: str, lease_seconds: int, kinds: tuple[str, ...]
    ) -> Mapping[str, Any] | None:
        return _first_row(
            await self._repository.rpc(
                "claim_automation_job",
                {
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                    "kinds": list(kinds),
                },
            )
        )

    async def heartbeat_automation_job(
        self, job_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        row = _first_row(
            await self._repository.rpc(
                "heartbeat_automation_job",
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                },
            )
        )
        return row is not None and row.get("status") == "running"

    async def complete_automation_job(
        self, job_id: str, worker_id: str, result: dict[str, Any]
    ) -> bool:
        terminal_status = result.get("outcome")
        if terminal_status not in {"succeeded", "needs_attention"}:
            raise ValueError("invalid terminal worker outcome")
        submission_state = result.get("submission_state")
        if (
            terminal_status == "needs_attention"
            and result.get("phase") == "submit"
            and submission_state in {"uncertain", "confirmed"}
        ):
            # A provider click may already have happened. Persist the immutable
            # form-revision fence and close the queue lease in one transaction;
            # a pruned terminal queue row must never make this revision reusable.
            row = _first_row(
                await self._repository.rpc(
                    "complete_application_form_submit_attention",
                    {
                        "job_id": job_id,
                        "worker_id": worker_id,
                        "result": result,
                    },
                )
            )
            return row is not None and row.get("status") == "needs_attention"
        row = _first_row(
            await self._repository.rpc(
                "complete_automation_job",
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "result": result,
                    "terminal_status": terminal_status,
                },
            )
        )
        return row is not None and row.get("status") == terminal_status

    async def fail_automation_job(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
        retry_after_seconds: int,
    ) -> bool:
        row = _first_row(
            await self._repository.rpc(
                "fail_automation_job",
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "error_code": error_code,
                    "error_message": error_message,
                    "retry_after_seconds": retry_after_seconds,
                },
            )
        )
        return row is not None

    async def close(self) -> None:
        close = getattr(self._repository, "aclose", None) or getattr(
            self._repository, "close", None
        )
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise WorkerConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise WorkerConfigurationError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkerConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise WorkerConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    worker_id: str
    poll_seconds: float = 2.0
    max_idle_poll_seconds: float = 15.0
    lease_seconds: int = 120
    retry_base_seconds: int = 15
    max_retry_seconds: int = 300
    max_queue_backoff_seconds: float = 30.0
    heartbeat_seconds: float = 20.0
    kinds: tuple[str, ...] = SUPPORTED_JOB_KINDS

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        worker_id = os.getenv("WORKER_ID", "").strip()
        if not worker_id:
            raise WorkerConfigurationError("WORKER_ID is required")
        if not _WORKER_ID_PATTERN.fullmatch(worker_id):
            raise WorkerConfigurationError(
                "WORKER_ID must use only letters, numbers, dots, colons, underscores, or hyphens"
            )
        poll_seconds = _bounded_float("WORKER_POLL_SECONDS", 2.0, 0.1, 60.0)
        max_idle_poll_seconds = _bounded_float(
            "WORKER_MAX_IDLE_POLL_SECONDS",
            max(15.0, poll_seconds),
            poll_seconds,
            300.0,
        )
        return cls(
            worker_id=worker_id,
            poll_seconds=poll_seconds,
            max_idle_poll_seconds=max_idle_poll_seconds,
            lease_seconds=_bounded_int("WORKER_LEASE_SECONDS", 120, 30, 3600),
            heartbeat_seconds=_bounded_float(
                "WORKER_HEARTBEAT_SECONDS", 20.0, 1.0, 300.0
            ),
        )


@dataclass(frozen=True, slots=True)
class BoundedBackoff:
    base_seconds: float
    maximum_seconds: float

    def delay(self, failure_count: int, random_value: float | None = None) -> float:
        exponent = min(max(failure_count - 1, 0), 20)
        without_jitter = min(self.maximum_seconds, self.base_seconds * (2**exponent))
        sample = random.random() if random_value is None else min(max(random_value, 0.0), 1.0)
        jittered = without_jitter * (0.8 + sample * 0.4)
        return min(self.maximum_seconds, max(0.0, jittered))


class Worker:
    def __init__(
        self,
        store: QueueStore,
        config: WorkerConfig,
        *,
        stop_event: asyncio.Event | None = None,
        handler: Callable[
            [AutomationJob], HandlerOutcome | Awaitable[HandlerOutcome]
        ] = handle_job,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.store = store
        self.config = config
        self.stop_event = stop_event or asyncio.Event()
        self.handler = handler
        self.logger = logger

    def _retry_delay(self, attempts: int) -> int:
        exponent = min(max(attempts - 1, 0), 20)
        return min(
            self.config.max_retry_seconds,
            self.config.retry_base_seconds * (2**exponent),
        )

    async def _record_handler_failure(self, job: AutomationJob) -> None:
        try:
            await self.store.fail_automation_job(
                job.id,
                self.config.worker_id,
                "worker_handler_error",
                "The worker could not complete this job safely.",
                self._retry_delay(job.attempts),
            )
        except Exception:
            # The lease will expire and become claimable.  Do not print exception
            # text because provider libraries can put request bodies in it.
            self.logger.warning("Could not persist a redacted worker failure.")

    async def _await_handler(
        self,
        job: AutomationJob,
        awaitable: Awaitable[HandlerOutcome],
    ) -> HandlerOutcome:
        """Renew the lease while a remote browser/network handler is in flight."""

        task = asyncio.ensure_future(awaitable)
        interval = min(
            max(self.config.heartbeat_seconds, 0.001),
            max(self.config.lease_seconds / 3, 0.001),
        )
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return task.result()
                try:
                    lease_active = await self.store.heartbeat_automation_job(
                        job.id,
                        self.config.worker_id,
                        self.config.lease_seconds,
                    )
                except Exception:
                    # Managed submission performs a separate lease-bound database
                    # check immediately before its final action.  A transient
                    # heartbeat transport error must not itself create ambiguity.
                    self.logger.warning("Could not renew an in-flight job lease.")
                    continue
                if not lease_active:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise WorkerLeaseLost
        finally:
            if not task.done():
                task.cancel()

    async def run_once(self) -> bool:
        """Claim and process at most one job; return whether a row was claimed."""

        record = await self.store.claim_automation_job(
            self.config.worker_id,
            self.config.lease_seconds,
            self.config.kinds,
        )
        if record is None:
            return False

        try:
            job = AutomationJob.from_record(record)
        except (TypeError, ValueError):
            # A database constraint normally makes this unreachable.  Leaving the
            # malformed row leased is safer than logging its potentially private body.
            self.logger.error(
                "A claimed job failed structural validation; its lease will expire."
            )
            return True

        self.logger.info("Claimed job %s with kind %s.", job.id, job.kind)

        try:
            lease_active = await self.store.heartbeat_automation_job(
                job.id,
                self.config.worker_id,
                self.config.lease_seconds,
            )
        except Exception:
            self.logger.warning("Could not confirm the job lease; no handler was run.")
            return True
        if not lease_active:
            self.logger.info("Job %s is cancelled or no longer owned; no handler was run.", job.id)
            return True

        try:
            outcome_or_awaitable = self.handler(job)
            outcome = (
                await self._await_handler(job, outcome_or_awaitable)
                if inspect.isawaitable(outcome_or_awaitable)
                else outcome_or_awaitable
            )
            if not isinstance(outcome, HandlerOutcome):
                raise TypeError("worker handler returned an invalid outcome")
        except WorkerLeaseLost:
            self.logger.info(
                "Job %s lost its lease while running; no terminal update was attempted.",
                job.id,
            )
            return True
        except Exception:
            self.logger.warning("A job handler failed; recording a redacted retry outcome.")
            await self._record_handler_failure(job)
            return True

        result = outcome.as_result()
        try:
            completed = await self.store.complete_automation_job(
                job.id,
                self.config.worker_id,
                result,
            )
        except Exception:
            # Completion is ambiguous after a transport error.  Do not convert it to
            # a failure or blindly repeat an external action; let lease reconciliation
            # decide.  Launch handlers themselves have no submission side effects.
            self.logger.warning(
                "Job completion could not be confirmed; awaiting lease reconciliation."
            )
            return True

        if completed:
            self.logger.info("Job %s reached %s.", job.id, outcome.status)
        else:
            self.logger.info("Job %s lost its lease before completion was recorded.", job.id)
        return True

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run_forever(self) -> None:
        queue_backoff = BoundedBackoff(
            base_seconds=max(self.config.poll_seconds, 0.1),
            maximum_seconds=self.config.max_queue_backoff_seconds,
        )
        idle_backoff = BoundedBackoff(
            base_seconds=max(self.config.poll_seconds, 0.1),
            maximum_seconds=max(
                self.config.poll_seconds,
                self.config.max_idle_poll_seconds,
            ),
        )
        consecutive_errors = 0
        consecutive_idle_polls = 0
        self.logger.info("Worker started for %d safe job kinds.", len(self.config.kinds))
        try:
            while not self.stop_event.is_set():
                try:
                    processed = await self.run_once()
                except Exception:
                    consecutive_errors += 1
                    consecutive_idle_polls = 0
                    delay = queue_backoff.delay(consecutive_errors)
                    self.logger.warning("Queue request failed; retrying in %.1f seconds.", delay)
                    await self._wait_or_stop(delay)
                    continue

                consecutive_errors = 0
                if processed:
                    consecutive_idle_polls = 0
                    continue
                consecutive_idle_polls += 1
                await self._wait_or_stop(idle_backoff.delay(consecutive_idle_polls))
        finally:
            try:
                await self.store.close()
            except Exception:
                self.logger.warning("Worker store did not close cleanly.")
            self.logger.info("Worker stopped.")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            signal.signal(signum, lambda _signum, _frame: stop_event.set())


def _configure_logging() -> None:
    level_name = os.getenv("WORKER_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx reports every successful Supabase poll at INFO. Those entries turn a
    # healthy idle worker into thousands of noisy lines per day. Queue failures are
    # still surfaced by the worker logger at WARNING with redacted messages.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_queue_store() -> SupabaseQueueStore:
    """Build the server-secret repository without importing the web application."""

    from app.saas.config import get_settings
    from app.saas.store import SupabaseStore

    settings = get_settings()
    repository = SupabaseStore(settings).secret()
    return SupabaseQueueStore(repository)


def build_job_handler(
    store: QueueStore, config: WorkerConfig
) -> Callable[[AutomationJob], HandlerOutcome | Awaitable[HandlerOutcome]]:
    """Enable managed-browser jobs only when every worker secret is configured."""

    if not isinstance(store, SupabaseQueueStore):
        return handle_job
    from app.saas.config import get_settings

    settings = get_settings()
    handler: Callable[
        [AutomationJob], HandlerOutcome | Awaitable[HandlerOutcome]
    ] = handle_job
    if (
        settings.browserbase_configured
        and settings.token_encryption_configured
        and settings.allowed_browser_providers
    ):
        from worker.browser_runtime import build_managed_job_handler

        handler = build_managed_job_handler(
            store.repository,  # type: ignore[arg-type]
            worker_id=config.worker_id,
            browserbase_api_key=settings.browserbase_api_key,
            browserbase_project_id=settings.browserbase_project_id,
            token_encryption_key=settings.token_encryption_key,
            allowed_providers=settings.allowed_browser_providers,
            fallback=handler,
        )
    from worker.discovery_runtime import DiscoveryJobHandler

    return DiscoveryJobHandler(
        store.repository,  # type: ignore[arg-type]
        worker_id=config.worker_id,
        fallback=handler,
    )


async def _run(
    config: WorkerConfig,
    store: QueueStore,
    *,
    handler: Callable[
        [AutomationJob], HandlerOutcome | Awaitable[HandlerOutcome]
    ] | None = None,
) -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    await Worker(
        store,
        config,
        stop_event=stop_event,
        handler=handler or build_job_handler(store, config),
    ).run_forever()


def main() -> int:
    _configure_logging()
    try:
        config = WorkerConfig.from_env()
        store = build_queue_store()
    except Exception:
        # Settings errors contain variable names, but a future backend may include
        # values.  Keep the process log intentionally generic.
        LOGGER.error("Worker configuration is incomplete or invalid.")
        return 2

    asyncio.run(_run(config, store))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BoundedBackoff",
    "QueueStore",
    "SupabaseQueueStore",
    "Worker",
    "WorkerConfig",
    "WorkerConfigurationError",
    "WorkerLeaseLost",
    "build_job_handler",
    "build_queue_store",
    "main",
]
