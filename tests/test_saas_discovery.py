from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import parse_qs, urlsplit

import openpyxl
import pytest

from app.saas.discovery import (
    canonical_public_ats_board_url,
    detect_provider,
    discover_linkedin_guest,
    discover_provider_urls,
    discover_public_ats_board,
    discover_rss,
    discover_telegram,
    parse_csv_bytes,
    parse_linkedin_guest_html,
    parse_referral_digest,
    parse_public_ats_board_url,
    parse_rss_feed,
    parse_xlsx_bytes,
)
from app.saas.discovery.network import require_allowed_https_url
from app.saas.discovery.network import DiscoveryFetchError
from app.saas.discovery.telegram import parse_telegram_preview
from app.saas.schemas import JobCreate


def _assert_job_contract(jobs: list[dict]) -> None:
    assert jobs
    for job in jobs:
        assert set(job) == {
            "source",
            "external_id",
            "apply_url",
            "title",
            "company",
            "location",
            "description",
            "contact_email",
            "metadata",
        }
        JobCreate.model_validate(job)


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://docs.google.com/forms/d/e/abc/viewform", "google_forms"),
        ("https://forms.gle/abc123", "google_forms"),
        ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://jobs.lever.co/acme/123", "lever"),
        ("https://jobs.ashbyhq.com/acme/123", "ashby"),
        ("https://www.workatastartup.com/companies/acme/jobs/12345-engineer", "yc"),
        ("https://wellfound.com/jobs/123-engineer", "wellfound"),
        ("https://cutshort.io/job/backend-engineer-acme-123", "cutshort"),
        ("https://www.instahyre.com/job-123", "instahyre"),
    ],
)
def test_provider_detection(url: str, provider: str) -> None:
    assert detect_provider(url) == provider


def test_provider_detection_does_not_trust_lookalike_hosts() -> None:
    assert detect_provider("https://jobs.lever.co.evil.example/acme/123") is None
    assert detect_provider("http://jobs.lever.co/acme/123") is None
    assert detect_provider("https://app.greenhouse.io/people/123") is None
    assert detect_provider("javascript:alert(1)") is None
    assert detect_provider("https://evil.example/?next=https://forms.gle/abc") is None


def test_public_ats_urls_become_job_create_items() -> None:
    jobs = discover_provider_urls(
        "Apply at https://jobs.lever.co/nimbus-ai/abc and "
        "https://jobs.ashbyhq.com/forge-labs/def"
    )
    assert [job["metadata"]["provider"] for job in jobs] == ["lever", "ashby"]
    assert jobs[0]["company"] == "Nimbus Ai"
    _assert_job_contract(jobs)


@pytest.mark.parametrize(
    ("url", "provider", "canonical", "api_host"),
    [
        (
            "https://boards.greenhouse.io/embed/job_board?for=acme_labs",
            "greenhouse",
            "https://job-boards.greenhouse.io/acme_labs",
            "boards-api.greenhouse.io",
        ),
        (
            "https://jobs.eu.lever.co/Acme/jobs-id/apply",
            "lever",
            "https://jobs.eu.lever.co/Acme",
            "api.eu.lever.co",
        ),
        (
            "https://jobs.ashbyhq.com/Acme/role-id",
            "ashby",
            "https://jobs.ashbyhq.com/Acme",
            "api.ashbyhq.com",
        ),
    ],
)
def test_public_ats_board_urls_resolve_only_to_fixed_official_apis(
    url: str, provider: str, canonical: str, api_host: str
) -> None:
    board = parse_public_ats_board_url(url)

    assert board["provider"] == provider
    assert board["board_url"] == canonical
    assert urlsplit(board["api_url"]).hostname == api_host
    assert canonical_public_ats_board_url(url) == canonical


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co.evil.example/acme",
        "https://jobs.ashbyhq.com:444/acme",
        "http://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme%2Fother",
        "https://boards.greenhouse.io/embed/job_board",
    ],
)
def test_public_ats_board_parser_rejects_ssrf_and_ambiguous_tokens(url: str) -> None:
    with pytest.raises(ValueError):
        parse_public_ats_board_url(url)


def test_greenhouse_public_board_uses_unauthenticated_list_contract() -> None:
    calls: list[tuple[str, dict]] = []

    def fetcher(url: str, **kwargs: object) -> str:
        calls.append((url, kwargs))
        return json.dumps(
            {
                "jobs": [
                    {
                        "id": 42,
                        "title": "Platform Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": "https://evil.example/redirect",
                        "updated_at": "2026-08-01T10:00:00Z",
                        "content": (
                            "&lt;p&gt;Build safe platform services.&lt;/p&gt;"
                            "&lt;script&gt;ignore()&lt;/script&gt;"
                        ),
                    }
                ]
            }
        )

    jobs = discover_public_ats_board(
        "https://boards.greenhouse.io/acme_labs", fetcher=fetcher
    )

    assert calls[0][0] == "https://boards-api.greenhouse.io/v1/boards/acme_labs/jobs?content=true"
    assert calls[0][1]["headers"] == {"Accept": "application/json"}
    assert jobs[0]["apply_url"] == "https://job-boards.greenhouse.io/acme_labs/jobs/42"
    assert jobs[0]["description"] == "Build safe platform services."
    assert jobs[0]["metadata"]["provider"] == "greenhouse"
    _assert_job_contract(jobs)


def test_lever_public_board_preserves_eu_region_and_plain_description() -> None:
    def fetcher(_url: str, **_kwargs: object) -> str:
        return json.dumps(
            [
                {
                    "id": "posting-1",
                    "text": "Backend Engineer",
                    "categories": {"location": "Berlin", "team": "Platform"},
                    "descriptionPlain": "Build reliable distributed systems for customers.",
                    "applyUrl": "https://jobs.eu.lever.co/acme/posting-1/apply",
                }
            ]
        )

    jobs = discover_public_ats_board(
        "https://jobs.eu.lever.co/acme", fetcher=fetcher
    )

    assert jobs[0]["apply_url"] == "https://jobs.eu.lever.co/acme/posting-1/apply"
    assert jobs[0]["description"] == "Build reliable distributed systems for customers."
    assert jobs[0]["metadata"]["team"] == "Platform"
    _assert_job_contract(jobs)


def test_ashby_public_board_omits_unlisted_posts() -> None:
    def fetcher(_url: str, **_kwargs: object) -> str:
        return json.dumps(
            {
                "apiVersion": "1",
                "jobs": [
                    {
                        "title": "Private role",
                        "isListed": False,
                        "applyUrl": "https://jobs.ashbyhq.com/acme/private/apply",
                    },
                    {
                        "title": "ML Engineer",
                        "location": "Remote",
                        "isListed": True,
                        "isRemote": True,
                        "descriptionPlain": "Build production machine learning systems.",
                        "applyUrl": "https://jobs.ashbyhq.com/acme/public/apply",
                    },
                ],
            }
        )

    jobs = discover_public_ats_board(
        "https://jobs.ashbyhq.com/acme", fetcher=fetcher
    )

    assert [job["title"] for job in jobs] == ["ML Engineer"]
    assert jobs[0]["metadata"]["remote"] is True
    _assert_job_contract(jobs)


def test_referral_digest_parses_form_and_email_jobs_without_network() -> None:
    digest = """
    Referral openings
    1) Company: Acme Labs
    Role: Backend Engineer
    Location: Remote
    How to Apply: https://docs.google.com/forms/d/e/acme/viewform

    2) Company - Beta AI
    Role - ML Intern
    Batch: 2026
    Send your resume to careers@beta.example
    CC: campus@beta.example
    Subject: ML internship referral
    """
    jobs = parse_referral_digest(digest)
    assert len(jobs) == 2
    assert jobs[0]["metadata"]["apply_kind"] == "form"
    assert jobs[0]["metadata"]["provider"] == "google_forms"
    assert jobs[1]["company"] == "Beta AI"
    assert jobs[1]["contact_email"] == "careers@beta.example"
    assert jobs[1]["metadata"]["cc"] == ["campus@beta.example"]
    _assert_job_contract(jobs)


def test_csv_import_supports_flexible_headers_and_deduplicates() -> None:
    payload = (
        "Company Name,Job Title,Contact Email,Apply Link,City,JD,Job ID\n"
        "Acme,Platform Engineer,jobs@acme.example,https://jobs.lever.co/acme/abc,Remote,Build reliable cloud services for customers.,REQ-1\n"
        "Acme,Duplicate,jobs@acme.example,https://jobs.lever.co/acme/other,Remote,This duplicate should be ignored.,REQ-1\n"
    ).encode()
    jobs = parse_csv_bytes(payload)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Platform Engineer"
    assert jobs[0]["metadata"]["provider"] == "lever"
    assert jobs[0]["external_id"] == "REQ-1"
    _assert_job_contract(jobs)


def test_xlsx_import_is_in_memory_and_uses_first_nonempty_header() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append([])
    sheet.append(["Employer", "Position", "Application URL", "Location"])
    sheet.append(["Cobalt", "Frontend Engineer", "https://jobs.ashbyhq.com/cobalt/42", "Pune"])
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()

    jobs = parse_xlsx_bytes(buffer.getvalue())
    assert jobs[0]["company"] == "Cobalt"
    assert jobs[0]["title"] == "Frontend Engineer"
    assert jobs[0]["metadata"]["import_row"] == 3
    _assert_job_contract(jobs)


def test_xlsx_import_rejects_sparse_full_width_worksheet_before_materializing() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "Job Title"
    sheet["XFD2050"] = "sparse marker"
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()

    with pytest.raises(ValueError, match="too many columns"):
        parse_xlsx_bytes(buffer.getvalue(), max_rows=200)


def test_telegram_preview_drops_stale_and_non_actionable_messages() -> None:
    html = """
    <div class="tgme_widget_message" data-post="freshjobs/100">
      <time datetime="2026-08-10T12:00:00+00:00"></time>
      <div class="tgme_widget_message_text">
        Company: Nimbus AI<br>Role: AI Engineer<br>Location: Remote<br>
        <a href="https://jobs.lever.co/nimbus/100">Apply here</a>
      </div>
    </div>
    <div class="tgme_widget_message" data-post="freshjobs/99">
      <time datetime="2026-07-01T12:00:00+00:00"></time>
      <div class="tgme_widget_message_text">Company: Old Co<br>Role: Developer<br>old@example.com</div>
    </div>
    <div class="tgme_widget_message" data-post="freshjobs/98">
      <time datetime="2026-08-10T12:00:00+00:00"></time>
      <div class="tgme_widget_message_text">General hiring news with no application target</div>
    </div>
    """
    jobs = parse_telegram_preview(
        html,
        "freshjobs",
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
        max_age_hours=72,
    )
    assert len(jobs) == 1
    assert jobs[0]["external_id"] == "freshjobs/100"
    assert jobs[0]["apply_url"] == "https://jobs.lever.co/nimbus/100"
    _assert_job_contract(jobs)


def test_telegram_fetch_is_channel_allowlisted_and_bounded() -> None:
    calls: list[str] = []
    fixture = """
      <div class="tgme_widget_message" data-post="safejobs/1">
        <div class="tgme_widget_message_text">Company: Acme<br>Role: Engineer<br>jobs@acme.example</div>
      </div>
    """

    jobs = discover_telegram(
        ["safejobs"], allowed_channels=["safejobs"], fetcher=lambda url: calls.append(url) or fixture
    )
    assert calls == ["https://t.me/s/safejobs"]
    assert len(jobs) == 1
    with pytest.raises(ValueError, match="not allowlisted"):
        discover_telegram(["privatejobs"], allowed_channels=["safejobs"], fetcher=lambda _: fixture)


def test_telegram_fetch_isolates_one_dead_allowlisted_channel() -> None:
    fixture = """
      <div class="tgme_widget_message" data-post="safejobs/1">
        <div class="tgme_widget_message_text">Company: Acme<br>Role: Engineer<br>jobs@acme.example</div>
      </div>
    """

    def fetch(url: str) -> str:
        if url.endswith("/deadjobs"):
            raise DiscoveryFetchError("redirect refused")
        return fixture

    jobs = discover_telegram(
        ["deadjobs", "safejobs"],
        allowed_channels=["deadjobs", "safejobs"],
        fetcher=fetch,
    )
    assert len(jobs) == 1
    with pytest.raises(DiscoveryFetchError):
        discover_telegram(
            ["deadjobs"], allowed_channels=["deadjobs"], fetcher=fetch
        )


def test_rss_and_atom_freshness_and_provider_preference() -> None:
    xml = """<?xml version="1.0"?>
    <rss><channel>
      <item>
        <guid>fresh-1</guid><title>Acme is hiring Backend Engineer</title>
        <link>https://news.example/acme</link>
        <pubDate>Mon, 10 Aug 2026 12:00:00 +0000</pubDate>
        <description><![CDATA[Apply at https://job-boards.greenhouse.io/acme/jobs/42]]></description>
      </item>
      <item>
        <guid>old-1</guid><title>Old role</title><link>https://news.example/old</link>
        <pubDate>Wed, 01 Jul 2026 12:00:00 +0000</pubDate>
      </item>
    </channel></rss>"""
    jobs = parse_rss_feed(
        xml,
        "https://news.example/feed/",
        now=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme"
    assert jobs[0]["apply_url"] == "https://job-boards.greenhouse.io/acme/jobs/42"
    _assert_job_contract(jobs)


def test_rss_fetch_requires_exact_feed_allowlist() -> None:
    xml = "<rss><channel></channel></rss>"
    feed = "https://jobs.example/feed/"
    calls: list[str] = []
    assert discover_rss([feed], allowed_feeds=[feed], fetcher=lambda url: calls.append(url) or xml) == []
    assert calls == [feed]
    with pytest.raises(ValueError, match="not allowlisted"):
        discover_rss(["https://jobs.example/other"], allowed_feeds=[feed], fetcher=lambda _: xml)


def test_rss_fetch_isolates_one_dead_allowlisted_feed() -> None:
    good = "https://jobs.example/feed/"
    dead = "https://dead.example/feed/"
    xml = "<rss><channel></channel></rss>"

    def fetch(url: str) -> str:
        if url == dead:
            raise DiscoveryFetchError("unavailable")
        return xml

    assert discover_rss([dead, good], allowed_feeds=[dead, good], fetcher=fetch) == []
    with pytest.raises(DiscoveryFetchError):
        discover_rss([dead], allowed_feeds=[dead], fetcher=fetch)


def test_linkedin_guest_cards_are_normalized_and_tracking_is_removed() -> None:
    html = """
    <li data-entity-urn="urn:li:jobPosting:123456">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/backend-engineer-123456?trk=guest">open</a>
      <h3 class="base-search-card__title"> Backend Engineer </h3>
      <h4 class="base-search-card__subtitle"><a>Acme Labs</a></h4>
      <span class="job-search-card__location">Remote</span>
      <time class="job-search-card__listdate" datetime="2026-08-10">1 day ago</time>
    </li>
    """
    jobs = parse_linkedin_guest_html(html)
    assert jobs[0]["external_id"] == "123456"
    assert jobs[0]["apply_url"] == "https://www.linkedin.com/jobs/view/backend-engineer-123456"
    assert jobs[0]["metadata"]["posted_at"] == "2026-08-10"
    _assert_job_contract(jobs)


def test_linkedin_current_nested_urn_and_regional_host_are_canonicalized() -> None:
    html = """
    <li>
      <div class="base-card" data-entity-urn="urn:li:jobPosting:987654">
        <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/platform-engineer-987654?trk=guest">open</a>
        <h3 class="base-search-card__title">Platform Engineer</h3>
        <h4 class="base-search-card__subtitle">Nimbus</h4>
        <span class="job-search-card__location">Bengaluru</span>
      </div>
    </li>
    """
    jobs = parse_linkedin_guest_html(html)
    assert jobs[0]["external_id"] == "987654"
    assert jobs[0]["apply_url"] == "https://www.linkedin.com/jobs/view/platform-engineer-987654"


def test_linkedin_first_page_network_failure_is_not_reported_as_empty_success() -> None:
    def unavailable(_url: str) -> str:
        raise DiscoveryFetchError("rate limited")

    with pytest.raises(DiscoveryFetchError):
        discover_linkedin_guest("backend", fetcher=unavailable)


def test_linkedin_guest_search_never_exceeds_two_pages_and_deduplicates() -> None:
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        start = int(parse_qs(urlsplit(url).query)["start"][0])
        cards = []
        for number in range(start, start + 25):
            cards.append(
                f'<li data-entity-urn="urn:li:jobPosting:{number}">'
                f'<a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/role-{number}">open</a>'
                f'<h3 class="base-search-card__title">Engineer {number}</h3>'
                '<h4 class="base-search-card__subtitle">Acme</h4>'
                '<span class="job-search-card__location">India</span></li>'
            )
        return "".join(cards)

    jobs = discover_linkedin_guest("backend engineer", limit=50, max_pages=99, fetcher=fetch)
    assert len(calls) == 2
    assert [parse_qs(urlsplit(url).query)["start"][0] for url in calls] == ["0", "25"]
    assert all(parse_qs(urlsplit(url).query)["f_WT"] == ["2"] for url in calls)
    assert len(jobs) == 50
    _assert_job_contract(jobs)


def test_http_boundary_rejects_ports_credentials_and_non_https() -> None:
    assert require_allowed_https_url("https://t.me/s/safejobs", {"t.me"})
    with pytest.raises(ValueError):
        require_allowed_https_url("http://t.me/s/safejobs", {"t.me"})
    with pytest.raises(ValueError):
        require_allowed_https_url("https://user:pass@t.me/s/safejobs", {"t.me"})
    with pytest.raises(ValueError):
        require_allowed_https_url("https://t.me:8443/s/safejobs", {"t.me"})
