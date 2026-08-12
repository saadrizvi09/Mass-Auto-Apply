"""Bounded discovery from Telegram's public channel preview pages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from html.parser import HTMLParser
import re

from .common import (
    NormalizedJob,
    clean_text,
    extract_emails,
    extract_urls,
    infer_company_title,
    labeled_field,
    make_job,
)
from .network import fetch_text, require_allowed_https_url
from .providers import detect_provider


DEFAULT_TELEGRAM_CHANNELS: tuple[str, ...] = (
    "fresheroffcampus",
    "freshershunt",
    "fresherjobsadda",
    "fresherjobinfo",
    "jobs4fresherdotcom",
    "campusdrivejobs",
)
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_NOISE_HOSTS = ("t.me/", "telegram.me/", "whatsapp.com/", "topmate.io/")
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class _TelegramPreviewParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict[str, object]] = []
        self.current: dict[str, object] | None = None
        self.depth = 0
        self.text_depth: int | None = None

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        classes = set(values.get("class", "").split())
        if self.current is None:
            if tag == "div" and "tgme_widget_message" in classes:
                self.current = {
                    "id": values.get("data-post", ""),
                    "datetime": "",
                    "parts": [],
                    "links": [],
                }
                self.depth = 1
            return

        if tag not in _VOID_TAGS:
            self.depth += 1
        if tag == "div" and "tgme_widget_message_text" in classes:
            self.text_depth = self.depth
        if tag == "time" and values.get("datetime"):
            self.current["datetime"] = values["datetime"]
        if self.text_depth is not None:
            if tag == "br":
                parts = self.current["parts"]
                assert isinstance(parts, list)
                parts.append("\n")
            if tag == "a" and values.get("href"):
                links = self.current["links"]
                assert isinstance(links, list)
                links.append(values["href"])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.current is not None and tag not in _VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.text_depth is not None and self.depth >= self.text_depth:
            parts = self.current["parts"]
            assert isinstance(parts, list)
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if self.text_depth is not None and self.depth == self.text_depth and tag == "div":
            self.text_depth = None
        if self.depth == 1 and tag == "div":
            self.messages.append(self.current)
            self.current = None
            self.depth = 0
            self.text_depth = None
            return
        self.depth = max(0, self.depth - 1)


def _timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _action_urls(text: str, embedded: Iterable[object]) -> list[str]:
    combined = [*extract_urls(text), *extract_urls("\n".join(str(url) for url in embedded))]
    result: list[str] = []
    for url in combined:
        lower = url.lower()
        if any(noise in lower for noise in _NOISE_HOSTS):
            continue
        if url not in result:
            result.append(url)
    return result[:10]


def parse_telegram_preview(
    html: str,
    channel: str,
    *,
    now: datetime | None = None,
    max_age_hours: float = 72.0,
    limit: int = 25,
) -> list[NormalizedJob]:
    """Parse one already-fetched `t.me/s/...` page into actionable job records."""

    if not _CHANNEL_RE.fullmatch(channel):
        raise ValueError("Telegram channel name is invalid")
    if len(html.encode("utf-8", "replace")) > 1_000_000:
        raise ValueError("Telegram preview is too large")
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    parser = _TelegramPreviewParser()
    parser.feed(html)
    jobs: list[NormalizedJob] = []
    for message in parser.messages:
        published = _timestamp(message.get("datetime"))
        age_hours: float | None = None
        if published:
            age_hours = max(0.0, (reference - published).total_seconds() / 3_600)
            if age_hours > max_age_hours:
                continue
        parts = message.get("parts") or []
        assert isinstance(parts, list)
        text = clean_text("".join(str(part) for part in parts), limit=25_000)
        links = message.get("links") or []
        assert isinstance(links, list)
        urls = _action_urls(text, links)
        emails = extract_emails(text)
        if not text or (not urls and not emails):
            continue
        apply_url = next((url for url in urls if detect_provider(url)), urls[0] if urls else None)
        provider = detect_provider(apply_url) if apply_url else None
        company, title = infer_company_title(text, apply_url)
        company = labeled_field(text, ("company", "company name", "organization")) or company
        title = labeled_field(text, ("role", "job title", "position")) or title
        location = labeled_field(text, ("location", "city", "work location")) or None
        post_id = clean_text(message.get("id"), limit=255) or f"{channel}:unknown"
        message_url = f"https://t.me/{post_id}" if "/" in post_id else f"https://t.me/{channel}"
        jobs.append(
            make_job(
                source="telegram",
                external_id=post_id,
                apply_url=apply_url,
                title=title,
                company=company,
                location=location,
                description=text,
                contact_email=emails[0] if emails else None,
                metadata={
                    "provider": provider,
                    "channel": channel,
                    "message_url": message_url,
                    "published_at": published.isoformat() if published else None,
                    "age_hours": round(age_hours, 2) if age_hours is not None else None,
                    "discovered_urls": urls,
                },
            )
        )
    bounded = max(1, min(int(limit), 50))
    return jobs[-bounded:]


def discover_telegram(
    channels: Iterable[str] | None = None,
    *,
    allowed_channels: Iterable[str] = DEFAULT_TELEGRAM_CHANNELS,
    max_age_hours: float = 72.0,
    per_channel_limit: int = 25,
    fetcher: Callable[[str], str] | None = None,
) -> list[NormalizedJob]:
    """Fetch at most ten explicitly allowlisted public Telegram channels once each."""

    allowed_ordered = [
        str(channel).strip().lstrip("@").lower()
        for channel in allowed_channels
        if _CHANNEL_RE.fullmatch(str(channel).strip().lstrip("@"))
    ]
    allowed = set(allowed_ordered)
    requested = list(channels) if channels is not None else allowed_ordered
    if len(requested) > 10:
        raise ValueError("At most 10 Telegram channels may be fetched per request")
    fetch = fetcher or (
        lambda url: fetch_text(url, allowed_hosts={"t.me"}, max_bytes=1_000_000)
    )
    jobs: list[NormalizedJob] = []
    seen: set[str] = set()
    successful_sources = 0
    last_error: Exception | None = None
    for raw_channel in requested:
        channel = str(raw_channel).strip().lstrip("@").lower()
        if not _CHANNEL_RE.fullmatch(channel) or channel not in allowed:
            raise ValueError(f"Telegram channel is not allowlisted: {raw_channel}")
        url = f"https://t.me/s/{channel}"
        require_allowed_https_url(url, {"t.me"})
        try:
            channel_jobs = parse_telegram_preview(
                fetch(url),
                channel,
                max_age_hours=max_age_hours,
                limit=per_channel_limit,
            )
            successful_sources += 1
        except Exception as exc:  # one dead public preview must not discard good channels
            last_error = exc
            continue
        for job in channel_jobs:
            if job["external_id"] not in seen:
                seen.add(job["external_id"] or "")
                jobs.append(job)
    if requested and successful_sources == 0:
        from .network import DiscoveryFetchError

        raise DiscoveryFetchError("No allowlisted Telegram preview was available") from last_error
    return jobs


__all__ = [
    "DEFAULT_TELEGRAM_CHANNELS",
    "discover_telegram",
    "parse_telegram_preview",
]
