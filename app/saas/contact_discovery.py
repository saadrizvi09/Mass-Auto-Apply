"""Bounded public-source contact discovery for a saved job.

This adapter is intentionally narrower than a general web crawler.  It starts
from a job's public URL or an explicitly supplied company URL, stays on the
same public host, follows only hiring/team/contact-style links, and extracts
addresses that are visibly published on the fetched page.  It never logs in,
uses a search-engine account, guesses mailbox patterns, probes SMTP, or crawls
LinkedIn/Telegram member data.
"""

from __future__ import annotations

import ipaddress
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .discovery.common import clean_text, extract_emails, safe_http_url
from .discovery.network import DiscoveryFetchError, fetch_text


DEFAULT_MAX_CONTACT_PAGES = 8
MAX_CONTACT_PAGES = 12
DEFAULT_MAX_CONTACTS = 30
MAX_CONTACTS = 50
DEFAULT_CONTACT_TIMEOUT_SECONDS = 60
MIN_CONTACT_TIMEOUT_SECONDS = 15
MAX_CONTACT_TIMEOUT_SECONDS = 120
MAX_CONTACT_PAGE_BYTES = 1_000_000

_COMMON_PATHS = (
    "/careers",
    "/jobs",
    "/about",
    "/team",
    "/contact",
    "/recruiting",
    "/join-us",
)
_RELEVANT_PATH = re.compile(
    r"(?:career|job|hiring|recruit|talent|team|people|contact|join|about|work-with-us)",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z'’.-]{1,40}(?:\s+[A-Z][A-Za-z'’.-]{1,40}){1,3})\b"
)
_TITLE_RE = re.compile(
    r"\b(?:recruiter|recruiting|talent acquisition|hiring manager|people ops|"
    r"people operations|founder|co-founder|cto|engineering manager|"
    r"head of (?:engineering|people|talent|hr)|human resources|hr)\b",
    re.IGNORECASE,
)
_IGNORED_LOCALS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "mailer-daemon",
        "postmaster",
        "example",
        "support",
        "sales",
        "press",
        "privacy",
        "legal",
        "security",
        "abuse",
        "billing",
        "webmaster",
        "admin",
    }
)
_IGNORED_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
_FREE_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
    }
)
_PROVIDER_HOSTS = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "jobs.ashbyhq.com",
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)
_BLOCKED_CONTACT_HOSTS = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "telegram.org",
        "www.telegram.org",
        "t.me",
    }
)


class _PublicPageParser(HTMLParser):
    """Keep visible page text, links, and mailto anchors without HTML output."""

    _BLOCKS = frozenset(
        {"br", "div", "li", "p", "section", "tr", "ul", "ol", "h1", "h2", "h3", "h4"}
    )
    _IGNORED = frozenset({"script", "style", "template", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str, bool]] = []
        self._ignored_depth = 0
        self._anchor: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in self._IGNORED:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self._BLOCKS:
            self.parts.append("\n")
        if tag == "a":
            self.parts.append("\n")
            rel = set(values.get("rel", "").lower().split())
            self._anchor = {
                "href": values.get("href", ""),
                "parts": [],
                "nofollow": bool(rel & {"nofollow", "noindex"}),
            }

    def handle_endtag(self, tag: str) -> None:
        if tag in self._IGNORED:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag in self._BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self._anchor is not None:
            self.links.append(
                (
                    str(self._anchor.get("href") or ""),
                    clean_text("".join(self._anchor.get("parts") or []), limit=240),
                    bool(self._anchor.get("nofollow")),
                )
            )
            self._anchor = None
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.parts.append(data)
        if self._anchor is not None:
            self._anchor.setdefault("parts", []).append(data)


def _public_target(value: object) -> str | None:
    raw = str(value or "").strip()
    if raw and "://" not in raw:
        raw = f"https://{raw}"
    clean = safe_http_url(raw)
    if not clean:
        return None
    parsed = urlsplit(clean)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not host.isascii()
    ):
        return None
    if host in _BLOCKED_CONTACT_HOSTS or host.endswith((".linkedin.com", ".telegram.org")):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None or host == "localhost" or "." not in host:
        return None
    if host.endswith((".local", ".internal", ".intranet", ".localhost", ".example")):
        return None
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


def _site_hosts(host: str) -> frozenset[str]:
    clean = host.lower().rstrip(".")
    if clean.startswith("www."):
        return frozenset({clean, clean[4:]})
    return frozenset({clean, f"www.{clean}"})


def _canonical_page_url(value: object, allowed_hosts: frozenset[str]) -> str | None:
    clean = _public_target(value)
    if not clean:
        return None
    parsed = urlsplit(clean)
    if (parsed.hostname or "").lower().rstrip(".") not in allowed_hosts:
        return None
    path = parsed.path or "/"
    return urlunsplit(("https", (parsed.hostname or "").lower(), path, "", ""))


def _company_key(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return normalized[:160] or "unknown employer"


def _target_urls(job: Mapping[str, Any]) -> list[str]:
    metadata = job.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    values: list[object] = [
        # The queue bundle keeps these fields on the job record rather than
        # copying them into untrusted payload JSON.
        job.get("company_url"),
        job.get("company_website"),
        job.get("source_url"),
        job.get("apply_url"),
    ]
    for key in (
        "company_url",
        "company_website",
        "website",
        "company_domain",
        "contact_source_url",
        "source_url",
        "listing_url",
        "apply_url",
    ):
        values.append(metadata.get(key))
    domain = metadata.get("company_domain") or metadata.get("domain")
    if isinstance(domain, str) and domain.strip().casefold() not in _FREE_EMAIL_DOMAINS:
        values.append(domain)
    extra_targets = metadata.get("contact_source_urls")
    if isinstance(extra_targets, list):
        values.extend(extra_targets)
    targets: list[str] = []
    seen: set[str] = set()
    for value in values:
        target = _public_target(value)
        if target and target not in seen:
            seen.add(target)
            targets.append(target)
    return targets[:4]


def _parse_robots(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse the small subset needed for a conservative same-site crawl."""

    applies = False
    disallow: list[str] = []
    allow: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip().casefold() for part in line.split(":", 1))
        if key == "user-agent":
            applies = value in {"*", "autoapplycloud-discovery"}
        elif applies and key == "disallow" and value:
            disallow.append(value)
        elif applies and key == "allow" and value:
            allow.append(value)
    return tuple(disallow), tuple(allow)


def _robots_allows(path: str, rules: tuple[tuple[str, ...], tuple[str, ...]]) -> bool:
    disallow, allow = rules
    allowed_prefix = max((item for item in allow if path.startswith(item)), key=len, default="")
    blocked_prefix = max((item for item in disallow if path.startswith(item)), key=len, default="")
    return not blocked_prefix or len(allowed_prefix) >= len(blocked_prefix)


def _nearby_name(context: str) -> str | None:
    for match in _NAME_RE.finditer(context):
        candidate = clean_text(match.group(1), limit=160)
        candidate = re.sub(
            r"^(?:recruiter|recruiting|talent acquisition|hiring manager|people ops|"
            r"people operations|human resources|hr)\s+",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        tokens = re.findall(r"[A-Z][A-Za-z'’.-]{1,40}", candidate)
        ignored = {
            "contact", "email", "team", "careers", "hiring", "human", "resources",
            "recruiter", "recruiting", "talent", "acquisition", "manager",
            "private", "outside",
        }
        # Prefer the first adjacent two-token name. This prevents a nearby
        # navigation label (for example, "Private") from being absorbed into a
        # real name when several links are rendered without punctuation.
        for index in range(max(0, len(tokens) - 1)):
            pair = tokens[index : index + 2]
            if len(pair) == 2 and not ({item.casefold() for item in pair} & ignored):
                return " ".join(pair)
        if len(tokens) == 1 and tokens[0].casefold() not in ignored:
            return tokens[0]
        return None
    return None


def _nearby_title(context: str) -> str | None:
    match = _TITLE_RE.search(context)
    return clean_text(match.group(0), limit=120) if match else None


def _candidate(
    email: str,
    *,
    job: Mapping[str, Any],
    page_url: str,
    page_text: str,
) -> dict[str, Any] | None:
    normalized = email.strip().casefold()
    if "@" not in normalized:
        return None
    local, domain = normalized.rsplit("@", 1)
    if local in _IGNORED_LOCALS or domain in _IGNORED_DOMAINS:
        return None
    index = page_text.casefold().find(normalized)
    context = page_text[max(0, index - 180) : index + len(normalized) + 180] if index >= 0 else ""
    person_name = _nearby_name(context)
    person_title = _nearby_title(context)
    generic = local in {
        "careers",
        "career",
        "jobs",
        "job",
        "hiring",
        "recruiting",
        "recruitment",
        "talent",
        "hr",
        "people",
        "hello",
        "info",
        "contact",
    }
    contact_type = (
        "named_person" if person_name else "recruiting_inbox" if generic else "company_contact"
    )
    return {
        "job_id": str(job.get("id") or ""),
        "company_key": _company_key(job.get("company")),
        "email": normalized,
        "person_name": person_name,
        "person_title": person_title,
        "contact_type": contact_type,
        "source_url": page_url,
        "source_date": time.strftime("%Y-%m-%d", time.gmtime()),
        "contact_source": "visible email on public company or job page",
        "email_verification_status": "public_source_verified",
        "metadata": {"same_site_crawl": True},
    }


def discover_public_contacts(
    job: Mapping[str, Any],
    *,
    max_pages: int = DEFAULT_MAX_CONTACT_PAGES,
    max_contacts: int = DEFAULT_MAX_CONTACTS,
    timeout_seconds: int = DEFAULT_CONTACT_TIMEOUT_SECONDS,
    fetcher: Callable[..., str] = fetch_text,
) -> list[dict[str, Any]]:
    """Discover only explicitly published addresses from bounded public pages."""

    if not 1 <= int(max_pages) <= MAX_CONTACT_PAGES:
        raise ValueError("contact page limit is invalid")
    if not 1 <= int(max_contacts) <= MAX_CONTACTS:
        raise ValueError("contact limit is invalid")
    if not MIN_CONTACT_TIMEOUT_SECONDS <= int(timeout_seconds) <= MAX_CONTACT_TIMEOUT_SECONDS:
        raise ValueError("contact timeout is invalid")

    queue: deque[tuple[str, frozenset[str], bool]] = deque()
    queued: set[str] = set()
    for target in _target_urls(job):
        parsed = urlsplit(target)
        hosts = _site_hosts(parsed.hostname or "")
        canonical = _canonical_page_url(target, hosts)
        if canonical and canonical not in queued:
            queued.add(canonical)
            queue.append((canonical, hosts, (parsed.hostname or "").lower() in _PROVIDER_HOSTS))

    if not queue:
        return []

    deadline = time.monotonic() + int(timeout_seconds)
    pages_seen: set[str] = set()
    robots_cache: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    found: list[dict[str, Any]] = []
    found_keys: set[tuple[str, str]] = set()

    while queue and len(pages_seen) < int(max_pages) and len(found) < int(max_contacts):
        if time.monotonic() >= deadline:
            break
        page_url, allowed_hosts, provider_page = queue.popleft()
        if page_url in pages_seen:
            continue
        parsed_page = urlsplit(page_url)
        host = (parsed_page.hostname or "").lower()
        if host not in robots_cache:
            robots_url = f"https://{host}/robots.txt"
            try:
                robots_text = fetcher(
                    robots_url,
                    allowed_hosts=allowed_hosts,
                    timeout_seconds=min(5, max(1, int(deadline - time.monotonic()))),
                    max_bytes=100_000,
                )
                robots_cache[host] = _parse_robots(robots_text)
            except Exception:
                # A missing or unavailable robots file is not treated as an
                # authorization grant; it simply leaves no parsed block rule.
                robots_cache[host] = ((), ())
        if not _robots_allows(parsed_page.path or "/", robots_cache[host]):
            pages_seen.add(page_url)
            continue
        try:
            html = fetcher(
                page_url,
                allowed_hosts=allowed_hosts,
                timeout_seconds=max(1, int(deadline - time.monotonic())),
                max_bytes=MAX_CONTACT_PAGE_BYTES,
            )
        except (DiscoveryFetchError, TimeoutError, ValueError):
            pages_seen.add(page_url)
            continue
        except Exception:
            pages_seen.add(page_url)
            continue
        pages_seen.add(page_url)

        parser = _PublicPageParser()
        try:
            parser.feed(html[:MAX_CONTACT_PAGE_BYTES])
            parser.close()
        except (TypeError, ValueError):
            continue
        page_text = clean_text("".join(parser.parts), limit=25_000)
        page_emails = extract_emails(page_text, limit=max(1, int(max_contacts)))
        for href, anchor_text, nofollow in parser.links:
            if href.casefold().startswith("mailto:"):
                address = unquote(href[7:].split("?", 1)[0])
                page_emails.extend(extract_emails(address, limit=2))
                if anchor_text:
                    # Mailto links often show a person's name rather than the
                    # address. Keep that nearby context for deterministic naming.
                    page_text = f"{page_text}\n{anchor_text} {address}"
            if nofollow or provider_page:
                continue
            link = _canonical_page_url(urljoin(page_url, href), allowed_hosts)
            if not link or link in queued or not _RELEVANT_PATH.search(urlsplit(link).path):
                continue
            queued.add(link)
            queue.append((link, allowed_hosts, False))
        for email in page_emails:
            candidate = _candidate(email, job=job, page_url=page_url, page_text=page_text)
            if not candidate:
                continue
            key = (candidate["company_key"], candidate["email"])
            if key in found_keys:
                continue
            found_keys.add(key)
            found.append(candidate)
            if len(found) >= int(max_contacts):
                break

        if not provider_page and len(pages_seen) == 1:
            for path in _COMMON_PATHS:
                common = _canonical_page_url(
                    urlunsplit(("https", host, path, "", "")), allowed_hosts
                )
                if common and common not in queued:
                    queued.add(common)
                    queue.append((common, allowed_hosts, False))

    return found[: int(max_contacts)]


__all__ = [
    "DEFAULT_CONTACT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONTACT_PAGES",
    "DEFAULT_MAX_CONTACTS",
    "MAX_CONTACT_PAGES",
    "MAX_CONTACTS",
    "MAX_CONTACT_TIMEOUT_SECONDS",
    "MIN_CONTACT_TIMEOUT_SECONDS",
    "discover_public_contacts",
]
