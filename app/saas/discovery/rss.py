"""Bounded RSS/Atom discovery for an explicit deployment allowlist."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .common import (
    NormalizedJob,
    clean_text,
    extract_emails,
    extract_urls,
    infer_company_title,
    make_job,
    safe_http_url,
    stable_external_id,
)
from .network import fetch_text, require_allowed_https_url
from .providers import detect_provider


DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    "https://freshershunt.in/feed/",
    "https://www.fresheroffcampus.com/feed/",
    "https://jobsnet.in/feed/",
    "https://offcampusjobs4u.com/feed/",
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li"}:
            self.parts.append("\n")


def _strip_markup(value: object) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    return clean_text("".join(parser.parts), limit=25_000)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in element:
        if _local(child.tag) in wanted:
            return "".join(child.itertext()).strip()
    return ""


def _entry_link(element: ET.Element) -> str | None:
    for child in element:
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        relation = child.attrib.get("rel", "alternate")
        candidate = href if href and relation in {"", "alternate"} else child.text
        clean = safe_http_url(candidate)
        if clean:
            return clean
    return None


def _published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def parse_rss_feed(
    xml: str,
    feed_url: str,
    *,
    now: datetime | None = None,
    max_age_hours: float = 72.0,
    limit: int = 50,
) -> list[NormalizedJob]:
    """Parse RSS 2.0 or Atom XML already fetched from an allowlisted source."""

    if len(xml.encode("utf-8", "replace")) > 1_000_000:
        raise ValueError("RSS response is too large")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("RSS source returned malformed XML") from exc
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entries = [node for node in root.iter() if _local(node.tag) in {"item", "entry"}]
    jobs: list[NormalizedJob] = []
    for entry in entries:
        published = _published(_child_text(entry, "pubdate", "published", "updated", "date"))
        age_hours: float | None = None
        if published:
            age_hours = max(0.0, (reference - published).total_seconds() / 3_600)
            if age_hours > max_age_hours:
                continue
        title_text = clean_text(_child_text(entry, "title"), limit=240)
        raw_description = _child_text(entry, "description", "summary", "content")
        description = _strip_markup(raw_description)
        link = _entry_link(entry)
        urls = extract_urls(raw_description)
        apply_url = next((url for url in urls if detect_provider(url)), None) or link
        provider = detect_provider(apply_url) if apply_url else None
        context = f"{title_text}\n{description}"
        company, inferred_title = infer_company_title(context, apply_url)
        job_title = inferred_title or title_text
        emails = extract_emails(context)
        guid = clean_text(_child_text(entry, "guid", "id"), limit=255)
        external_id = guid or link or stable_external_id(feed_url, title_text, published)
        if not description:
            description = (
                f"{job_title} at {company}, discovered through the public RSS feed. "
                "Open the listing to review the complete job description."
            )
        jobs.append(
            make_job(
                source="rss",
                external_id=external_id,
                apply_url=apply_url,
                title=job_title,
                company=company,
                description=description,
                contact_email=emails[0] if emails else None,
                metadata={
                    "provider": provider,
                    "feed_url": feed_url,
                    "listing_url": link,
                    "published_at": published.isoformat() if published else None,
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                    "discovered_urls": urls[:10],
                },
            )
        )
        if len(jobs) >= max(1, min(int(limit), 100)):
            break
    return jobs


def discover_rss(
    feed_urls: Iterable[str] | None = None,
    *,
    allowed_feeds: Iterable[str] = DEFAULT_RSS_FEEDS,
    max_age_hours: float = 72.0,
    per_feed_limit: int = 50,
    fetcher: Callable[[str], str] | None = None,
) -> list[NormalizedJob]:
    """Fetch at most eight exact allowlisted RSS endpoints, once each."""

    allowed_ordered = [str(url).strip() for url in allowed_feeds]
    allowed = set(allowed_ordered)
    requested = list(feed_urls) if feed_urls is not None else allowed_ordered
    if len(requested) > 8:
        raise ValueError("At most 8 RSS feeds may be fetched per request")
    allowed_hosts = {
        (urlsplit(url).hostname or "").lower()
        for url in allowed
        if urlsplit(url).hostname
    }
    fetch = fetcher or (
        lambda url: fetch_text(url, allowed_hosts=allowed_hosts, max_bytes=1_000_000)
    )
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    successful_sources = 0
    last_error: Exception | None = None
    for raw_url in requested:
        url = str(raw_url).strip()
        if url not in allowed:
            raise ValueError("RSS feed is not allowlisted")
        require_allowed_https_url(url, allowed_hosts)
        try:
            feed_jobs = parse_rss_feed(
                fetch(url),
                url,
                max_age_hours=max_age_hours,
                limit=per_feed_limit,
            )
            successful_sources += 1
        except Exception as exc:  # isolate a stale/broken feed within the fixed catalog
            last_error = exc
            continue
        for job in feed_jobs:
            identifier = job["external_id"] or ""
            if identifier not in seen:
                seen.add(identifier)
                jobs.append(job)
    if requested and successful_sources == 0:
        from .network import DiscoveryFetchError

        raise DiscoveryFetchError("No allowlisted RSS feed was available") from last_error
    return jobs


__all__ = ["DEFAULT_RSS_FEEDS", "discover_rss", "parse_rss_feed"]
