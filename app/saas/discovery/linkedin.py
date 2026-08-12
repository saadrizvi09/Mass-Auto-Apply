"""Conservative discovery through LinkedIn's unauthenticated guest-job endpoint.

This module never receives cookies, credentials, or a LinkedIn profile.  One call
is capped at two public pages / fifty cards and performs no automatic retries.
"""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from .common import NormalizedJob, clean_text, make_job, safe_http_url
from .network import DiscoveryFetchError, fetch_text, require_allowed_https_url


GUEST_ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_PAGE_SIZE = 25
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_CAPTURE_CLASSES = {
    "base-search-card__title": "title",
    "base-search-card__subtitle": "company",
    "job-search-card__location": "location",
    "job-search-card__salary-info": "salary",
    "job-search-card__listdate": "posted_at",
}


def _classes(attrs: dict[str, str]) -> set[str]:
    return set(attrs.get("class", "").split())


class _LinkedInCardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.depth = 0
        self.captures: list[tuple[str, int]] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        classes = _classes(values)
        if self.current is None:
            if tag != "li":
                return
            self.current = {"entity_urn": values.get("data-entity-urn", "")}
            self.depth = 1
        elif tag not in _VOID_TAGS:
            self.depth += 1

        if self.current is None:
            return
        if values.get("data-entity-urn") and not self.current.get("entity_urn"):
            # LinkedIn currently places the posting URN on a nested card <div>
            # rather than the outer list item.  Keep parsing bounded to that card.
            self.current["entity_urn"] = values["data-entity-urn"]
        if tag == "a" and values.get("href") and (
            "base-card__full-link" in classes or "/jobs/view/" in values["href"]
        ):
            self.current.setdefault("url", values["href"])
        if tag == "time" and values.get("datetime"):
            self.current["posted_at"] = values["datetime"]
        for class_name, field in _CAPTURE_CLASSES.items():
            if class_name in classes:
                if field == "posted_at" and values.get("datetime"):
                    continue
                self.captures.append((field, self.depth))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current is not None and tag not in _VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        for field, capture_depth in self.captures:
            if self.depth >= capture_depth:
                self.current[field] = self.current.get(field, "") + data

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        self.captures = [
            (field, capture_depth)
            for field, capture_depth in self.captures
            if capture_depth != self.depth
        ]
        if self.depth == 1 and tag == "li":
            self.cards.append(self.current)
            self.current = None
            self.depth = 0
            self.captures.clear()
            return
        self.depth = max(0, self.depth - 1)


def _canonical_job_url(value: object) -> str | None:
    url = safe_http_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    regional_host = bool(re.fullmatch(r"[a-z]{2,3}\.linkedin\.com", host))
    if (
        host not in {"linkedin.com", "www.linkedin.com"}
        and not regional_host
    ) or "/jobs/view/" not in parsed.path:
        return None
    return urlunsplit(("https", "www.linkedin.com", parsed.path.rstrip("/"), "", ""))


def _job_id(card: dict[str, str], url: str) -> str:
    urn_match = re.search(r"jobPosting:(\d+)", card.get("entity_urn", ""))
    if urn_match:
        return urn_match.group(1)
    path = urlsplit(url).path
    path_match = re.search(r"(?:-|/)(\d+)(?:/)?$", path)
    return path_match.group(1) if path_match else url


def parse_linkedin_guest_html(html: str, *, remote_filter: bool = False) -> list[NormalizedJob]:
    """Parse guest card HTML without fetching job detail pages."""

    if len(html.encode("utf-8", "replace")) > 1_000_000:
        raise ValueError("LinkedIn guest response is too large")
    parser = _LinkedInCardParser()
    parser.feed(html)
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    for card in parser.cards:
        url = _canonical_job_url(card.get("url"))
        title = clean_text(card.get("title"), limit=240)
        if not url or not title:
            continue
        external_id = _job_id(card, url)
        if external_id in seen:
            continue
        seen.add(external_id)
        company = clean_text(card.get("company"), limit=240) or "Unknown employer"
        location = clean_text(card.get("location"), limit=240) or None
        salary = clean_text(card.get("salary"), limit=240) or None
        posted_at = clean_text(card.get("posted_at"), limit=100) or None
        is_remote = bool(remote_filter or (location and "remote" in location.lower()))
        jobs.append(
            make_job(
                source="linkedin_guest",
                external_id=external_id,
                apply_url=url,
                title=title,
                company=company,
                location=location,
                description=(
                    f"Public LinkedIn listing for {title} at {company}. "
                    "Open the listing to review the complete job description before applying."
                ),
                metadata={
                    "provider": "linkedin",
                    "guest_discovery": True,
                    "remote": is_remote,
                    "salary": salary,
                    "posted_at": posted_at,
                },
            )
        )
    return jobs


def discover_linkedin_guest(
    keywords: str,
    *,
    location: str = "India",
    remote: bool = True,
    limit: int = 25,
    max_pages: int = 2,
    fetcher: Callable[[str], str] | None = None,
) -> list[NormalizedJob]:
    """Fetch no more than two guest pages and return at most fifty unique jobs."""

    clean_keywords = clean_text(keywords, limit=101)
    clean_location = clean_text(location, limit=121) or "India"
    if not clean_keywords or len(clean_keywords) > 100:
        raise ValueError("LinkedIn keywords must contain 1 to 100 characters")
    if len(clean_location) > 120:
        raise ValueError("LinkedIn location is too long")
    bounded_limit = max(1, min(int(limit), 50))
    bounded_pages = max(1, min(int(max_pages), 2))
    pages_needed = min(bounded_pages, (bounded_limit + _PAGE_SIZE - 1) // _PAGE_SIZE)
    fetch = fetcher or (
        lambda url: fetch_text(
            url,
            allowed_hosts={"www.linkedin.com"},
            timeout_seconds=12,
            max_bytes=1_000_000,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
                )
            },
        )
    )
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    for page in range(pages_needed):
        parameters = {
            "keywords": clean_keywords,
            "location": clean_location,
            "start": str(page * _PAGE_SIZE),
        }
        if remote:
            parameters["f_WT"] = "2"
        url = f"{GUEST_ENDPOINT}?{urlencode(parameters)}"
        require_allowed_https_url(url, {"www.linkedin.com"})
        try:
            html = fetch(url)
        except DiscoveryFetchError:
            if page == 0:
                raise
            break
        if not html.strip():
            break
        page_jobs = parse_linkedin_guest_html(html, remote_filter=remote)
        if not page_jobs:
            break
        for job in page_jobs:
            identifier = job["external_id"] or ""
            if identifier not in seen:
                seen.add(identifier)
                jobs.append(job)
                if len(jobs) >= bounded_limit:
                    return jobs
    return jobs[:bounded_limit]


__all__ = [
    "GUEST_ENDPOINT",
    "discover_linkedin_guest",
    "parse_linkedin_guest_html",
]
