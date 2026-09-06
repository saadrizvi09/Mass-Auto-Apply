"""Bounded credential-free discovery jobs for the durable worker."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from app.saas.discovery import (
    MAX_PUBLIC_ATS_BOARDS,
    discover_linkedin_guest,
    discover_public_ats_board,
    discover_rss,
    discover_telegram,
    parse_public_ats_board_url,
)
from app.saas.discovery.common import safe_http_url
from app.saas.contact_discovery import discover_public_contacts


DISCOVERY_JOB_KINDS: tuple[str, ...] = (
    "discover_public_feeds",
    "discover_linkedin_guest",
    "discover_public_ats",
    "discover_public_contacts",
)
_MAX_BATCH = 200
_MAX_BATCH_BYTES = 1_500_000
_MAX_CONTACTS_PER_COMPANY = 4
_MAX_CONTACT_CRAWL_CONCURRENCY = 8
_MAX_SEARCH_TERMS = 20
_MAX_SEARCH_TERM_LENGTH = 100
_DEFAULT_TIMEOUT_SECONDS = 60
_MIN_TIMEOUT_SECONDS = 15
_MAX_TIMEOUT_SECONDS = 120
_SEARCHABLE_FIELDS: tuple[str, ...] = (
    "title",
    "company",
    "location",
    "description",
)


class DiscoveryRepository(Protocol):
    async def rpc(self, name: str, params: Mapping[str, Any]) -> Any: ...


class DiscoveryTimeLimitExceeded(TimeoutError):
    """A public-source call exceeded the user-selected worker budget."""


def _bounded_timeout(value: Any) -> int:
    if value is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not _MIN_TIMEOUT_SECONDS <= value <= _MAX_TIMEOUT_SECONDS
    ):
        raise ValueError("discovery timeout is invalid")
    return value


async def _run_with_deadline(
    discover: Callable[..., Any],
    *args: Any,
    deadline: float,
    **kwargs: Any,
) -> Any:
    """Run one synchronous public collector until the shared deadline."""

    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise DiscoveryTimeLimitExceeded
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(discover, *args, **kwargs), timeout=remaining
        )
    except asyncio.TimeoutError as exc:
        raise DiscoveryTimeLimitExceeded from exc


def _bounded_int(value: Any, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError("discovery limit is invalid")
    return value


def _normalize_search_text(value: str) -> str:
    """Make punctuation-insensitive text whose spaces remain word boundaries."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _validated_search_terms(value: Any) -> tuple[str, ...]:
    """Validate an optional, untrusted worker payload and normalize its phrases."""

    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_SEARCH_TERMS:
        raise ValueError("public feed search terms are invalid")

    terms: list[str] = []
    for raw_term in value:
        if (
            not isinstance(raw_term, str)
            or not 1 <= len(raw_term.strip()) <= _MAX_SEARCH_TERM_LENGTH
        ):
            raise ValueError("public feed search terms are invalid")
        term = _normalize_search_text(raw_term)
        if not term:
            raise ValueError("public feed search terms are invalid")
        if term not in terms:
            terms.append(term)
    return tuple(terms)


def _matches_search_terms(item: Mapping[str, Any], terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    fields = [
        _normalize_search_text(value)
        for field in _SEARCHABLE_FIELDS
        if isinstance((value := item.get(field)), str) and value
    ]
    return any(
        f" {term} " in f" {field_text} "
        for term in terms
        for field_text in fields
    )


def _serialize_job(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize the pure discovery shape for the database validation boundary."""

    apply_url = safe_http_url(raw.get("apply_url"))
    metadata = raw.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    return {
        "source": raw.get("source"),
        "external_id": raw.get("external_id"),
        "normalized_url": apply_url,
        "apply_url": apply_url,
        "title": raw.get("title"),
        "company": raw.get("company"),
        "location": raw.get("location"),
        "description": raw.get("description"),
        "contact_email": raw.get("contact_email"),
        "status": "saved",
        "metadata": {**dict(metadata), "discovered": True},
    }


def _counts(value: Any, fallback: int) -> tuple[int, int, int]:
    if not isinstance(value, Mapping):
        return fallback, fallback, 0
    count = value.get("count")
    inserted = value.get("inserted")
    updated = value.get("updated")
    return (
        count if isinstance(count, int) and count >= 0 else fallback,
        inserted if isinstance(inserted, int) and inserted >= 0 else fallback,
        updated if isinstance(updated, int) and updated >= 0 else 0,
    )


class DiscoveryJobHandler:
    """Run public fetchers and persist through a lease/tenant-bound service RPC."""

    def __init__(
        self,
        repository: DiscoveryRepository,
        *,
        worker_id: str,
        fallback: Any,
        telegram_discovery: Callable[..., list[Mapping[str, Any]]] = discover_telegram,
        rss_discovery: Callable[..., list[Mapping[str, Any]]] = discover_rss,
        linkedin_discovery: Callable[..., list[Mapping[str, Any]]] = discover_linkedin_guest,
        public_ats_discovery: Callable[..., list[Mapping[str, Any]]] = discover_public_ats_board,
        contact_discovery: Callable[..., list[Mapping[str, Any]]] = discover_public_contacts,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id
        self.fallback = fallback
        self.telegram_discovery = telegram_discovery
        self.rss_discovery = rss_discovery
        self.linkedin_discovery = linkedin_discovery
        self.public_ats_discovery = public_ats_discovery
        self.contact_discovery = contact_discovery

    async def _public_feeds(
        self, payload: Mapping[str, Any]
    ) -> tuple[list[Mapping[str, Any]], list[dict[str, str]]]:
        raw_sources = payload.get("source_ids") or ["telegram", "rss"]
        if (
            not isinstance(raw_sources, list)
            or not raw_sources
            or len(raw_sources) > 2
            or any(source not in {"telegram", "rss"} for source in raw_sources)
        ):
            raise ValueError("discovery sources are invalid")
        sources = list(dict.fromkeys(raw_sources))
        limit = _bounded_int(payload.get("limit"), 60, _MAX_BATCH)
        timeout_seconds = _bounded_timeout(payload.get("timeout_seconds"))
        search_terms = _validated_search_terms(payload.get("search_terms"))
        buckets: list[tuple[str, list[Mapping[str, Any]]]] = []
        errors: list[dict[str, str]] = []

        # The individual collectors already enforce their own source allowlists
        # and response limits. Running the selected collectors concurrently keeps
        # the combined run within the one user-visible time budget instead of
        # multiplying the worst-case timeout by the number of sources.
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        async def collect(
            source: str,
        ) -> tuple[str, list[Mapping[str, Any]], dict[str, str] | None]:
            discover = (
                self.telegram_discovery if source == "telegram" else self.rss_discovery
            )
            try:
                found = await _run_with_deadline(discover, deadline=deadline)
                if not isinstance(found, list):
                    raise TypeError("discovery source returned an invalid result")
            except DiscoveryTimeLimitExceeded:
                return source, [], {"source": source, "code": "source_timeout"}
            except Exception:
                return source, [], {"source": source, "code": "source_unavailable"}
            return (
                source,
                [
                    item
                    for item in found
                    if isinstance(item, Mapping)
                    and _matches_search_terms(item, search_terms)
                ],
                None,
            )

        results = await asyncio.gather(*(collect(source) for source in sources))
        for source, found, error in results:
            if error is not None:
                errors.append(error)
            else:
                buckets.append((source, found))

        # A busy Telegram catalog must not starve RSS (or vice versa).  Merge one
        # result per source per round, deduplicating before applying the global cap.
        jobs: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        maximum = max((len(items) for _, items in buckets), default=0)
        for index in range(maximum):
            for source, items in buckets:
                if index >= len(items):
                    continue
                item = items[index]
                identity = (
                    str(item.get("source") or source),
                    str(item.get("external_id") or ""),
                    str(item.get("apply_url") or ""),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                jobs.append(item)
                if len(jobs) >= limit:
                    return jobs, errors
        return jobs, errors

    async def _linkedin(self, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        keywords = payload.get("keywords")
        location = payload.get("location", "India")
        limit = _bounded_int(payload.get("limit"), 20, 25)
        timeout_seconds = _bounded_timeout(payload.get("timeout_seconds"))
        remote = payload.get("remote", False)
        if (
            not isinstance(keywords, str)
            or not 1 <= len(keywords.strip()) <= 100
            or not isinstance(location, str)
            or len(location.strip()) > 120
            or not isinstance(remote, bool)
        ):
            raise ValueError("LinkedIn discovery payload is invalid")
        result = await _run_with_deadline(
            self.linkedin_discovery,
            keywords.strip(),
            location=location.strip() or "India",
            remote=remote,
            limit=limit,
            max_pages=2,
            deadline=asyncio.get_running_loop().time() + timeout_seconds,
        )
        if not isinstance(result, list):
            raise TypeError("LinkedIn discovery returned an invalid result")
        return [item for item in result if isinstance(item, Mapping)][:limit]

    async def _public_ats(
        self, payload: Mapping[str, Any]
    ) -> tuple[list[Mapping[str, Any]], list[dict[str, str]]]:
        raw_urls = payload.get("board_urls")
        if (
            not isinstance(raw_urls, list)
            or not 1 <= len(raw_urls) <= MAX_PUBLIC_ATS_BOARDS
            or any(not isinstance(url, str) for url in raw_urls)
        ):
            raise ValueError("public ATS board payload is invalid")
        limit = _bounded_int(payload.get("limit"), 100, _MAX_BATCH)
        timeout_seconds = _bounded_timeout(payload.get("timeout_seconds"))

        # Re-canonicalize the persisted payload before any request.  This keeps the
        # worker safe even if a queue row was created outside the HTTP API.
        boards: list[tuple[str, str]] = []
        for raw_url in raw_urls:
            parsed = parse_public_ats_board_url(raw_url)
            canonical = parsed["board_url"]
            if canonical not in {url for _, url in boards}:
                boards.append((parsed["provider"], canonical))

        buckets: list[tuple[str, list[Mapping[str, Any]]]] = []
        errors: list[dict[str, str]] = []
        per_board_limit = min(limit, 100)

        deadline = asyncio.get_running_loop().time() + timeout_seconds

        async def collect(
            provider: str, board_url: str
        ) -> tuple[str, str, list[Mapping[str, Any]], dict[str, str] | None]:
            try:
                found = await _run_with_deadline(
                    self.public_ats_discovery,
                    board_url,
                    limit=per_board_limit,
                    deadline=deadline,
                )
                if not isinstance(found, list):
                    raise TypeError("public ATS source returned an invalid result")
            except DiscoveryTimeLimitExceeded:
                return provider, board_url, [], {"source": provider, "code": "source_timeout"}
            except Exception:
                return provider, board_url, [], {"source": provider, "code": "source_unavailable"}
            return (
                provider,
                board_url,
                [item for item in found if isinstance(item, Mapping)],
                None,
            )

        results = await asyncio.gather(*(collect(provider, url) for provider, url in boards))
        for provider, _board_url, found, error in results:
            if error is not None:
                errors.append(error)
            else:
                buckets.append((provider, found))

        # Interleave boards so a large employer cannot consume the whole tenant run.
        jobs: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        maximum = max((len(items) for _, items in buckets), default=0)
        for index in range(maximum):
            for provider, items in buckets:
                if index >= len(items):
                    continue
                item = items[index]
                identity = (
                    str(item.get("source") or provider),
                    str(item.get("external_id") or ""),
                    str(item.get("apply_url") or ""),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                jobs.append(item)
                if len(jobs) >= limit:
                    return jobs, errors
        return jobs, errors

    async def _public_contacts(
        self, queue_job: Any
    ) -> tuple[int, int, list[dict[str, str]]]:
        """Crawl saved jobs concurrently, then persist only page-backed evidence."""

        bundle = await self.repository.rpc(
            "get_public_contact_discovery_bundle",
            {"queue_job_id_input": queue_job.id, "worker_id_input": self.worker_id},
        )
        if not isinstance(bundle, Mapping):
            raise ValueError("contact discovery bundle is invalid")
        raw_jobs = bundle.get("jobs")
        if not isinstance(raw_jobs, list) or not 1 <= len(raw_jobs) <= 30:
            raise ValueError("contact discovery jobs are invalid")
        max_contacts = _bounded_int(bundle.get("max_contacts"), 30, 50)
        max_pages = _bounded_int(bundle.get("max_pages"), 8, 12)
        timeout_seconds = _bounded_timeout(bundle.get("timeout_seconds"))
        targets = [item for item in raw_jobs if isinstance(item, Mapping)]
        if len(targets) != len(raw_jobs):
            raise ValueError("contact discovery jobs are invalid")

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        # Contact crawls are independent, network-bound jobs. Keep the worker
        # responsive with a small bounded fan-out; the shared deadline,
        # per-site page cap, and source-level HTTP limits still apply.
        semaphore = asyncio.Semaphore(_MAX_CONTACT_CRAWL_CONCURRENCY)

        async def collect(
            target: Mapping[str, Any],
        ) -> tuple[list[Mapping[str, Any]], dict[str, str] | None]:
            async with semaphore:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return [], {"code": "source_timeout"}
                try:
                    found = await _run_with_deadline(
                        self.contact_discovery,
                        target,
                        max_pages=max_pages,
                        max_contacts=max_contacts,
                        # The outer wait_for enforces the shared budget. The
                        # collector also uses this value for individual requests.
                        timeout_seconds=max(15, min(timeout_seconds, int(remaining))),
                        deadline=deadline,
                    )
                    if not isinstance(found, list):
                        raise TypeError("contact source returned an invalid result")
                except DiscoveryTimeLimitExceeded:
                    return [], {"code": "source_timeout"}
                except Exception:
                    # Do not persist exception text: requests can contain source
                    # URLs or response fragments that are not safe in job history.
                    return [], {"code": "source_unavailable"}
                return [item for item in found if isinstance(item, Mapping)], None

        results = await asyncio.gather(*(collect(target) for target in targets))
        contacts: list[Mapping[str, Any]] = []
        errors: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        company_counts: dict[str, int] = {}
        for target, (found, error) in zip(targets, results, strict=True):
            if error is not None:
                error_with_job = {"job_id": str(target.get("id") or ""), **error}
                errors.append(error_with_job)
                continue
            for contact in found:
                email = str(contact.get("email") or "").strip().lower()
                company_key = str(contact.get("company_key") or "").strip().lower()
                if (
                    not email
                    or not company_key
                    or (company_key, email) in seen
                    or company_counts.get(company_key, 0) >= _MAX_CONTACTS_PER_COMPANY
                ):
                    continue
                seen.add((company_key, email))
                company_counts[company_key] = company_counts.get(company_key, 0) + 1
                normalized_contact = dict(contact)
                normalized_contact["email"] = email
                normalized_contact["company_key"] = company_key
                contacts.append(normalized_contact)
                if len(contacts) >= 1_500:
                    break
            if len(contacts) >= 1_500:
                break

        persisted = await self.repository.rpc(
            "store_public_job_contacts",
            {
                "queue_job_id_input": queue_job.id,
                "worker_id_input": self.worker_id,
                "contacts_input": contacts,
            },
        )
        saved_count = (
            int(persisted.get("count", 0))
            if isinstance(persisted, Mapping)
            and isinstance(persisted.get("count", 0), int)
            and persisted.get("count", 0) >= 0
            else 0
        )
        return len(targets), saved_count, errors

    async def __call__(self, job: Any) -> Any:
        if job.kind not in DISCOVERY_JOB_KINDS:
            result = self.fallback(job)
            return await result if inspect.isawaitable(result) else result

        from worker.handlers import HandlerOutcome

        if job.kind == "discover_public_contacts":
            try:
                searched, saved_count, source_errors = await self._public_contacts(job)
            except (TypeError, ValueError):
                return HandlerOutcome(
                    status="needs_attention",
                    code="contact_discovery_payload_invalid",
                    message="Review the saved roles and contact search limits, then try again.",
                    provider=job.provider,
                )
            except Exception:
                return HandlerOutcome(
                    status="needs_attention",
                    code="contact_discovery_unavailable",
                    message="The public contact sources could not be reached safely.",
                    provider=job.provider,
                )
            status = "succeeded" if not source_errors else "needs_attention"
            return HandlerOutcome(
                status=status,
                code=(
                    "contact_discovery_completed"
                    if status == "succeeded"
                    else "contact_sources_unavailable"
                ),
                message=(
                    "Public contact search completed. Review each address before drafting."
                    if status == "succeeded"
                    else "Some public contact pages were unavailable; review the saved results."
                ),
                provider=job.provider,
                details={
                    "jobs_searched": searched,
                    "contacts_saved": saved_count,
                    "source_errors": source_errors,
                },
            )

        try:
            if job.kind == "discover_public_feeds":
                jobs, source_errors = await self._public_feeds(job.payload)
            elif job.kind == "discover_linkedin_guest":
                jobs = await self._linkedin(job.payload)
                source_errors = []
            elif job.kind == "discover_public_ats":
                jobs, source_errors = await self._public_ats(job.payload)
        except (TypeError, ValueError):
            return HandlerOutcome(
                status="needs_attention",
                code="discovery_payload_invalid",
                message="Review the bounded discovery search settings and try again.",
                provider=job.provider,
            )
        except DiscoveryTimeLimitExceeded:
            return HandlerOutcome(
                status="needs_attention",
                code="discovery_time_limit",
                message="The public discovery source reached the selected time limit.",
                provider=job.provider,
            )
        except Exception:
            return HandlerOutcome(
                status="needs_attention",
                code="discovery_source_unavailable",
                message="The public discovery source could not be reached safely.",
                provider=job.provider,
            )

        serialized = [_serialize_job(item) for item in jobs[:_MAX_BATCH]]
        if len(json.dumps(serialized, default=str, separators=(",", ":"))) > _MAX_BATCH_BYTES:
            return HandlerOutcome(
                status="needs_attention",
                code="discovery_batch_too_large",
                message="The bounded discovery result was too large to save.",
                provider=job.provider,
            )
        if serialized:
            persisted = await self.repository.rpc(
                "ingest_discovered_jobs",
                {
                    # The RPC derives the tenant from this running lease.  No user
                    # identifier from the job payload crosses this boundary.
                    "job_id": job.id,
                    "worker_id": self.worker_id,
                    "jobs": serialized,
                },
            )
            count, inserted, updated = _counts(persisted, len(serialized))
        else:
            count = inserted = updated = 0

        status = "succeeded" if not source_errors or serialized else "needs_attention"
        code = (
            "discovery_completed"
            if status == "succeeded"
            else "discovery_sources_unavailable"
        )
        return HandlerOutcome(
            status=status,
            code=code,
            message=(
                "Public job discovery finished."
                if status == "succeeded"
                else "No jobs could be loaded from the selected public sources."
            ),
            provider=job.provider,
            details={
                "discovered_count": len(serialized),
                "saved_count": count,
                "inserted_count": inserted,
                "updated_count": updated,
                "source_errors": source_errors,
            },
        )


__all__ = ["DISCOVERY_JOB_KINDS", "DiscoveryJobHandler"]
