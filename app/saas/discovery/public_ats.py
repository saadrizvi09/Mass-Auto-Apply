"""Bounded enumeration of public Greenhouse, Lever, and Ashby job boards.

The input is always an official hosted board URL.  We derive the provider API
endpoint from a strictly validated board identifier, so a tenant-controlled URL
can never turn this adapter into a general-purpose HTTP client.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from html import unescape
from html.parser import HTMLParser
from typing import Any, TypedDict
from urllib.parse import parse_qs, quote, urlsplit

from .common import NormalizedJob, clean_text, humanize_slug, make_job, safe_http_url
from .network import DiscoveryFetchError, fetch_text


MAX_PUBLIC_ATS_BOARDS = 8
MAX_PUBLIC_ATS_RESULTS = 200
MAX_PUBLIC_ATS_RESULTS_PER_BOARD = 100

_BOARD_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_GREENHOUSE_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)
_LEVER_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})
_ASHBY_HOSTS = frozenset({"jobs.ashbyhq.com"})
_API_HOSTS = frozenset(
    {"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com"}
)


class PublicAtsBoard(TypedDict):
    provider: str
    token: str
    region: str
    board_url: str
    api_url: str


def _valid_token(value: object) -> str | None:
    token = str(value or "").strip()
    return token if _BOARD_TOKEN.fullmatch(token) else None


def parse_public_ats_board_url(value: object) -> PublicAtsBoard:
    """Resolve one official hosted URL to a fixed public board API endpoint."""

    clean = safe_http_url(value)
    if not clean:
        raise ValueError("Add a valid public ATS board URL")
    try:
        parsed = urlsplit(clean)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Add a valid public ATS board URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Only official HTTPS ATS board URLs are supported")

    segments = [segment for segment in parsed.path.split("/") if segment]
    provider: str
    token: str | None
    region = "global"

    if host in _GREENHOUSE_HOSTS:
        provider = "greenhouse"
        region = "eu" if ".eu.greenhouse.io" in host else "global"
        query = parse_qs(parsed.query, keep_blank_values=False)
        embedded = len(segments) >= 2 and segments[:2] in (
            ["embed", "job_board"],
            ["embed", "job_app"],
        )
        token = _valid_token((query.get("for") or [None])[0]) if embedded else None
        if token is None:
            token = _valid_token(segments[0] if segments else None)
        if token is None or token.lower() == "embed":
            raise ValueError("The Greenhouse URL does not contain a public board name")
        board_host = (
            "job-boards.eu.greenhouse.io" if region == "eu" else "job-boards.greenhouse.io"
        )
        board_url = f"https://{board_host}/{quote(token, safe='')}"
        api_url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(token, safe='')}/jobs?content=true"
        )
    elif host in _LEVER_HOSTS:
        provider = "lever"
        region = "eu" if host == "jobs.eu.lever.co" else "global"
        token = _valid_token(segments[0] if segments else None)
        if token is None:
            raise ValueError("The Lever URL does not contain a public site name")
        board_host = "jobs.eu.lever.co" if region == "eu" else "jobs.lever.co"
        api_host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        board_url = f"https://{board_host}/{quote(token, safe='')}"
        api_url = (
            f"https://{api_host}/v0/postings/{quote(token, safe='')}"
            f"?mode=json&limit={MAX_PUBLIC_ATS_RESULTS_PER_BOARD}"
        )
    elif host in _ASHBY_HOSTS:
        provider = "ashby"
        token = _valid_token(segments[0] if segments else None)
        if token is None:
            raise ValueError("The Ashby URL does not contain a public board name")
        board_url = f"https://jobs.ashbyhq.com/{quote(token, safe='')}"
        api_url = (
            "https://api.ashbyhq.com/posting-api/job-board/"
            f"{quote(token, safe='')}"
        )
    else:
        raise ValueError("Use a public Greenhouse, Lever, or Ashby hosted URL")

    return {
        "provider": provider,
        "token": token,
        "region": region,
        "board_url": board_url,
        "api_url": api_url,
    }


def canonical_public_ats_board_url(value: object) -> str:
    return parse_public_ats_board_url(value)["board_url"]


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DiscoveryFetchError("The public ATS returned an invalid JSON response") from exc


def _sequence(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise DiscoveryFetchError("The public ATS returned an unexpected response")
    return [item for item in value if isinstance(item, Mapping)]


def _official_job_url(value: object, *, host: str, token: str) -> str | None:
    clean = safe_http_url(value)
    if not clean:
        return None
    parsed = urlsplit(clean)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower().rstrip(".") != host
        or parsed.port not in {None, 443}
        or not segments
        or segments[0].lower() != token.lower()
    ):
        return None
    return clean


def _metadata_value(value: object, *, limit: int = 240) -> str | None:
    clean = clean_text(value, limit=limit)
    return clean or None


class _PlainTextHtmlParser(HTMLParser):
    """Extract visible text from provider HTML without accepting markup."""

    _BLOCKS = frozenset(
        {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3", "h4"}
    )
    _IGNORED = frozenset({"script", "style", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _html_to_text(value: object) -> str | None:
    raw = str(value or "")[:100_000]
    if not raw:
        return None
    # Greenhouse returns hosted-editor markup as HTML entities. Decode the
    # transport representation before extracting text; the result is still
    # persisted and rendered only as plain text.
    raw = unescape(raw)
    parser = _PlainTextHtmlParser()
    try:
        parser.feed(raw)
        parser.close()
    except (TypeError, ValueError):
        return _metadata_value(raw, limit=25_000)
    return _metadata_value("".join(parser.parts), limit=25_000)


def _greenhouse_jobs(board: PublicAtsBoard, payload: Any, limit: int) -> list[NormalizedJob]:
    if not isinstance(payload, Mapping):
        raise DiscoveryFetchError("Greenhouse returned an unexpected response")
    items = _sequence(payload.get("jobs"))
    company = humanize_slug(board["token"]) or "Unknown employer"
    job_host = (
        "job-boards.eu.greenhouse.io"
        if board["region"] == "eu"
        else "job-boards.greenhouse.io"
    )
    jobs: list[NormalizedJob] = []
    for raw in items:
        job_id = _metadata_value(raw.get("id"), limit=120)
        title = _metadata_value(raw.get("title"))
        if not job_id or not title:
            continue
        location_value = raw.get("location")
        location = (
            _metadata_value(location_value.get("name"))
            if isinstance(location_value, Mapping)
            else None
        )
        apply_url = (
            f"https://{job_host}/{quote(board['token'], safe='')}"
            f"/jobs/{quote(job_id, safe='')}"
        )
        jobs.append(
            make_job(
                source="public_ats",
                external_id=f"greenhouse:{board['token']}:{job_id}",
                apply_url=apply_url,
                title=title,
                company=company,
                location=location,
                description=_html_to_text(raw.get("content")),
                metadata={
                    "provider": "greenhouse",
                    "provider_label": "Greenhouse",
                    "board_url": board["board_url"],
                    "published_at": _metadata_value(raw.get("updated_at"), limit=80),
                    "requisition_id": _metadata_value(raw.get("requisition_id"), limit=120),
                },
            )
        )
        if len(jobs) >= limit:
            break
    return jobs


def _lever_jobs(board: PublicAtsBoard, payload: Any, limit: int) -> list[NormalizedJob]:
    items = _sequence(payload)
    company = humanize_slug(board["token"]) or "Unknown employer"
    job_host = "jobs.eu.lever.co" if board["region"] == "eu" else "jobs.lever.co"
    jobs: list[NormalizedJob] = []
    for raw in items:
        job_id = _metadata_value(raw.get("id"), limit=120)
        title = _metadata_value(raw.get("text"))
        if not job_id or not title:
            continue
        categories = raw.get("categories")
        categories = categories if isinstance(categories, Mapping) else {}
        apply_url = _official_job_url(
            raw.get("applyUrl"), host=job_host, token=board["token"]
        ) or f"https://{job_host}/{quote(board['token'], safe='')}/{quote(job_id, safe='')}/apply"
        description = "\n\n".join(
            part
            for part in (
                _metadata_value(raw.get("descriptionPlain"), limit=20_000),
                _metadata_value(raw.get("additionalPlain"), limit=5_000),
            )
            if part
        )
        jobs.append(
            make_job(
                source="public_ats",
                external_id=f"lever:{board['token']}:{job_id}",
                apply_url=apply_url,
                title=title,
                company=company,
                location=_metadata_value(categories.get("location")),
                description=description,
                metadata={
                    "provider": "lever",
                    "provider_label": "Lever",
                    "board_url": board["board_url"],
                    "team": _metadata_value(categories.get("team")),
                    "department": _metadata_value(categories.get("department")),
                    "commitment": _metadata_value(categories.get("commitment")),
                },
            )
        )
        if len(jobs) >= limit:
            break
    return jobs


def _ashby_jobs(board: PublicAtsBoard, payload: Any, limit: int) -> list[NormalizedJob]:
    if not isinstance(payload, Mapping):
        raise DiscoveryFetchError("Ashby returned an unexpected response")
    items = _sequence(payload.get("jobs"))
    company = humanize_slug(board["token"]) or "Unknown employer"
    jobs: list[NormalizedJob] = []
    for raw in items:
        # Ashby's public contract marks direct-link-only postings as unlisted.
        # Board enumeration must never surface those to another user.
        if raw.get("isListed") is not True:
            continue
        title = _metadata_value(raw.get("title"))
        apply_url = _official_job_url(
            raw.get("applyUrl"), host="jobs.ashbyhq.com", token=board["token"]
        ) or _official_job_url(
            raw.get("jobUrl"), host="jobs.ashbyhq.com", token=board["token"]
        )
        if not title or not apply_url:
            continue
        path_parts = [part for part in urlsplit(apply_url).path.split("/") if part]
        posting_id = path_parts[1] if len(path_parts) > 1 else apply_url
        jobs.append(
            make_job(
                source="public_ats",
                external_id=f"ashby:{board['token']}:{posting_id}",
                apply_url=apply_url,
                title=title,
                company=company,
                location=_metadata_value(raw.get("location")),
                description=_metadata_value(raw.get("descriptionPlain"), limit=25_000),
                metadata={
                    "provider": "ashby",
                    "provider_label": "Ashby",
                    "board_url": board["board_url"],
                    "department": _metadata_value(raw.get("department")),
                    "team": _metadata_value(raw.get("team")),
                    "employment_type": _metadata_value(raw.get("employmentType"), limit=80),
                    "workplace_type": _metadata_value(raw.get("workplaceType"), limit=80),
                    "remote": raw.get("isRemote") is True,
                    "published_at": _metadata_value(raw.get("publishedAt"), limit=80),
                },
            )
        )
        if len(jobs) >= limit:
            break
    return jobs


def discover_public_ats_board(
    board_url: object,
    *,
    limit: int = 50,
    fetcher: Callable[..., str] = fetch_text,
) -> list[NormalizedJob]:
    """Fetch one official public board and normalize a bounded set of jobs."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_PUBLIC_ATS_RESULTS_PER_BOARD
    ):
        raise ValueError("Public ATS board limit is invalid")
    board = parse_public_ats_board_url(board_url)
    text = fetcher(
        board["api_url"],
        allowed_hosts=_API_HOSTS,
        timeout_seconds=12.0,
        max_bytes=4_000_000,
        headers={"Accept": "application/json"},
    )
    payload = _load_json(text)
    if board["provider"] == "greenhouse":
        return _greenhouse_jobs(board, payload, limit)
    if board["provider"] == "lever":
        return _lever_jobs(board, payload, limit)
    return _ashby_jobs(board, payload, limit)


__all__ = [
    "MAX_PUBLIC_ATS_BOARDS",
    "MAX_PUBLIC_ATS_RESULTS",
    "MAX_PUBLIC_ATS_RESULTS_PER_BOARD",
    "PublicAtsBoard",
    "canonical_public_ats_board_url",
    "discover_public_ats_board",
    "parse_public_ats_board_url",
]
