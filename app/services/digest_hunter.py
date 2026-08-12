"""Digest Hunter: auto-discover 'Referral Alert'-style job digests from public
Telegram job channels — no login, no API key, no account risk.

How: public channels render their recent posts as plain HTML at
https://t.me/s/<channel>  (web preview). We fetch that, extract each message,
keep only postings that (a) match Saad's roles (AI/ML/SDE/dev), (b) fit the 2026
batch / fresher scope, and (c) contain an actionable apply target (Google Form,
portal link, or apply-email). New finds are deduped against digest_seen.json and
can be piped straight into forms.ingest_digest() — after which the existing
dashboard cards / Google-Forms auto-fill / email pipeline take over.

CLI: py -3.11 digest_tool.py hunt [--ingest]
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from ..config import ROOT
from ..logging_setup import log_event

# Public channels that post these digests (t.me/s/<name> must render for guests).
CHANNELS = [
    "fresheroffcampus",
    "freshershunt",
    "fresherjobsadda",
    "freshopenings",
    "fresherjobinfo",
    "jobs4fresherdotcom",
    "campusdrivejobs",
]

SEEN_PATH = ROOT / "digest_seen.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# --- fit filters -------------------------------------------------------------------
ROLE_OK = re.compile(
    r"\b(ai|ml|machine learning|data scien|data engineer|gen ?ai|llm|sde|software|"
    r"developer|full ?stack|backend|front ?end|python|java(script)?|react|node)\b", re.I)
ROLE_BAD = re.compile(
    r"\b(sales|marketing|bde|business development|hr\b|recruit|design(er)?|video|"
    r"content|counsel|product management|finance|accounts?|support executive|bpo|"
    r"customer (care|support)|operations executive)\b", re.I)
# batch scope: 2026 explicitly, or a fresher/0-x-years posting with no batch line
BATCH_LINE = re.compile(r"batch\s*[-:]\s*(.+)", re.I)
FRESHER_HINT = re.compile(r"\b(fresher|off.?campus|0\s*-\s*[12]\s*(yr|year))\b", re.I)
# actionable apply targets
APPLY_LINK = re.compile(
    r"https?://(?:docs\.google\.com/forms|forms\.gle|forms\.office\.com|"
    r"job-boards\.greenhouse\.io|boards\.greenhouse\.io|jobs\.lever\.co|"
    r"jobs\.ashbyhq\.com|[\w.-]+\.myworkdayjobs\.com|[\w.-]+/careers?/)\S*", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
NOISE_DOMAINS = re.compile(r"(whatsapp\.com|topmate\.io|t\.me/|telegram\.me)", re.I)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def _messages(channel: str, max_age_hours: float = 72.0) -> list[dict]:
    """Return [{'id': 'channel/123', 'text': ..., 'age_h': float}] from the t.me/s/
    preview page. Posts carry an exact <time datetime=...> stamp - anything older
    than max_age_hours is dropped so stale reposted drives never enter the pipe."""
    import datetime as _dt

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        raise RuntimeError("beautifulsoup4 is required (already a project dep)")
    html = _fetch(f"https://t.me/s/{channel}")
    soup = BeautifulSoup(html, "html.parser")
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for wrap in soup.select("div.tgme_widget_message"):
        post = wrap.get("data-post") or ""
        body = wrap.select_one("div.tgme_widget_message_text")
        if not body:
            continue
        age_h = None
        t = wrap.select_one("time[datetime]")
        if t and t.get("datetime"):
            try:
                stamp = _dt.datetime.fromisoformat(t["datetime"])
                age_h = (now - stamp).total_seconds() / 3600.0
            except ValueError:
                age_h = None
        if age_h is not None and age_h > max_age_hours:
            continue  # stale post - the whole point of the freshness gate
        # <br> -> newline so the digest keeps its line structure for parse_digest
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = body.get_text("\n").strip()
        if text:
            out.append({"id": post or f"{channel}/?", "text": text,
                        "age_h": round(age_h, 1) if age_h is not None else None})
    return out


# Markers that PROVE an apply page is dead (conservative: only drop on a hit;
# JS-shell pages that render nothing stay - absence of proof is not proof of life).
CLOSED_MARKERS = re.compile(
    r"(no longer accepting|application window closed|deadline for this job has passed|"
    r"job has expired|posting has expired|position has been filled|"
    r"no longer available|not accepting responses|this form is no longer accepting|"
    r"can't view this job|cannot view this job|not available at this time|"
    r"page you are looking for doesn't exist|job you are looking for)", re.I)


def _workday_alive(url: str) -> bool | None:
    """Workday job pages are JS shells (plain fetch can't see the error text), but each
    has a JSON API whose HTTP status is truthful:
      https://<host>/<tenantSite>/job/<...>  ->  https://<host>/wday/cxs/<tenant>/<tenantSite>/job/<...>
    200 => alive, 404/410 => dead. None => couldn't determine (odd URL shape)."""
    m = re.match(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+)$", url)
    if not m:
        return None
    host_tenant, wd, site, rest = m.groups()
    api = f"https://{host_tenant}.{wd}.myworkdayjobs.com/wday/cxs/{host_tenant}/{site}/job/{rest}"
    req = urllib.request.Request(api, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return False
            body = r.read().decode("utf-8", "replace")
            # a live req returns jobPostingInfo; removed ones return 200 + empty/error JSON
            return '"jobPostingInfo"' in body
    except urllib.error.HTTPError as e:
        return False if e.code in (404, 410) else None
    except Exception:
        return None


def _link_is_closed(url: str) -> bool:
    """True only if the apply target is PROVABLY dead: Workday JSON API 404s, or the
    page text carries an explicit closed/removed marker."""
    import urllib.error  # noqa: F401 - used in _workday_alive via module

    if "myworkdayjobs.com" in url:
        alive = _workday_alive(url)
        if alive is not None:
            return not alive
    try:
        html = _fetch(url)
    except Exception:
        return False  # unreachable != closed; let the operator judge
    return bool(CLOSED_MARKERS.search(html))


def _batch_ok(text: str, my_batch: str) -> bool:
    m = BATCH_LINE.search(text)
    if m:
        return my_batch in m.group(1)
    return bool(FRESHER_HINT.search(text))


ANY_LINK = re.compile(r"https?://\S+", re.I)


def _action_links(text: str) -> list[str]:
    """Every actionable link in the post (excluding channel/promo links)."""
    return [u.rstrip(").,;'\"”")
            for u in ANY_LINK.findall(text) if not NOISE_DOMAINS.search(u)]


def _fit(text: str, my_batch: str) -> bool:
    if not ROLE_OK.search(text):
        return False
    if ROLE_BAD.search(text) and not ROLE_OK.search(text[:120]):
        # a bad-role hit only disqualifies when the headline isn't clearly technical
        return False
    if not _batch_ok(text, my_batch):
        return False
    # must have something we can act on that isn't just channel promo
    if _action_links(text):
        return True
    emails = [e for e in EMAIL.findall(text) if not NOISE_DOMAINS.search(e)]
    return bool(emails)


def _resolve_apply(url: str) -> list[str]:
    """Follow an aggregator 'Apply Now' page and pull the REAL apply target(s)
    (Google Form / MS Form / ATS / careers portal). Best-effort; [] on failure."""
    if APPLY_LINK.search(url):
        return [url]  # already a direct form/ATS link
    try:
        html = _fetch(url)
    except Exception:
        return []
    junk = re.compile(r"(/feed/?$|/comments/|fresheroffcampus|freshershunt|fresherjob|"
                      r"campusdrive|jobs4fresher|freshopenings|jobsnet\.in|"
                      r"dailypharmajobs)", re.I)
    direct = [m.group(0).rstrip(").,;'\"")
              for m in APPLY_LINK.finditer(html) if not junk.search(m.group(0))]
    if direct:
        return list(dict.fromkeys(direct))[:3]
    # fall back: outbound 'apply/career/register' links on the page
    out = []
    for m in re.finditer(r'href="(https?://[^"]+)"', html):
        u = m.group(1)
        if NOISE_DOMAINS.search(u) or junk.search(u):
            continue
        if re.search(r"(apply|career|register|recruit|hiring|job)", u, re.I):
            out.append(u)
    return list(dict.fromkeys(out))[:3]


# WordPress job aggregators expose RSS with pubDate - machine-verifiable freshness
# (their HTML pages endlessly republish old drives; the feed does not lie).
FEEDS = [
    "https://freshershunt.in/feed/",
    "https://www.fresheroffcampus.com/feed/",
    "https://jobsnet.in/feed/",
    "https://offcampusjobs4u.com/feed/",
]


def _feed_items(feed_url: str, max_age_hours: float = 72.0) -> list[dict]:
    """[{'id': link, 'text': 'title\\nlink', 'age_h': ...}] for fresh feed items."""
    import datetime as _dt
    from email.utils import parsedate_to_datetime

    try:
        xml = _fetch(feed_url)
    except Exception:
        return []
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        blk = m.group(1)
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", blk, re.S)
        link = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", blk, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", blk, re.S)
        if not (title and link):
            continue
        age_h = None
        if pub:
            try:
                stamp = parsedate_to_datetime(pub.group(1).strip())
                age_h = (now - stamp).total_seconds() / 3600.0
            except Exception:
                age_h = None
        if age_h is not None and age_h > max_age_hours:
            continue
        t, l = title.group(1).strip(), link.group(1).strip()
        out.append({"id": l, "text": f"{t}\n{l}",
                    "age_h": round(age_h, 1) if age_h is not None else None})
    return out


def _load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    SEEN_PATH.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def hunt(channels: list[str] | None = None, my_batch: str = "2026",
         ingest: bool = False, max_per_channel: int = 20,
         max_age_hours: float = 72.0, check_liveness: bool = True) -> dict:
    """Sweep Telegram channels + RSS feeds for FRESH digests (posts older than
    max_age_hours are dropped at source), resolve real apply links, and reject any
    whose apply page explicitly says it's closed. Optionally ingest survivors."""
    summary = {"channels": 0, "messages": 0, "matched": 0, "new": 0, "closed": 0,
               "ingested": 0, "errors": [], "finds": []}
    seen = _load_seen()

    candidates: list[dict] = []
    for ch in (channels or CHANNELS):
        try:
            msgs = _messages(ch, max_age_hours=max_age_hours)
        except Exception as e:  # noqa: BLE001 - one dead channel must not kill the sweep
            summary["errors"].append(f"{ch}: {str(e)[:60]}")
            continue
        summary["channels"] += 1
        summary["messages"] += len(msgs)
        for m in msgs[-max_per_channel:]:
            m["src"] = f"https://t.me/{m['id']}"
            candidates.append(m)
    for feed in FEEDS:
        items = _feed_items(feed, max_age_hours=max_age_hours)
        summary["channels"] += 1
        summary["messages"] += len(items)
        for m in items:
            m["src"] = m["id"]
            candidates.append(m)

    for m in candidates:
        if not _fit(m["text"], my_batch):
            continue
        summary["matched"] += 1
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        # resolve aggregator 'Apply Now' pages to the REAL apply link(s)
        resolved: list[str] = []
        for link in _action_links(m["text"])[:2]:
            resolved += _resolve_apply(link)
        resolved = list(dict.fromkeys(resolved))[:3]
        # LIVENESS GATE: drop links whose page explicitly says closed/expired
        if check_liveness and resolved:
            alive = [u for u in resolved if not _link_is_closed(u)]
            if not alive:
                summary["closed"] += 1
                continue  # every apply target is provably dead - skip entirely
            resolved = alive
        summary["new"] += 1
        text = m["text"]
        if resolved:
            text += "\n\nHow to Apply:\n" + "\n".join(resolved)
        find = {"source": m["src"], "text": text, "apply_links": resolved,
                "age_h": m.get("age_h")}
        summary["finds"].append(find)
        if ingest:
            from . import forms
            try:
                res = forms.ingest_digest(text)
                summary["ingested"] += res.get("added", 0)
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"ingest {m['id']}: {str(e)[:60]}")
    _save_seen(seen)
    summary["message"] = (
        f"Swept {summary['channels']} source(s), {summary['messages']} fresh posts "
        f"(<{int(max_age_hours)}h) -> {summary['matched']} match, {summary['new']} new+alive, "
        f"{summary['closed']} rejected as closed"
        + (f", {summary['ingested']} ingested." if ingest else "."))
    log_event("digest_hunter", "hunt", "ok", summary["message"])
    return summary
