from __future__ import annotations

from typing import Any

from app.saas.contact_discovery import discover_public_contacts


def test_public_contact_crawler_stays_on_site_and_keeps_visible_mailto_evidence() -> None:
    pages = {
        "https://acme.test/robots.txt": "User-agent: *\nDisallow: /private",
        "https://acme.test/jobs/role": (
            '<a href="/careers">Careers</a>'
            '<a href="https://outside.test/team">Outside</a>'
        ),
        "https://acme.test/careers": (
            "<h1>Careers</h1><p>Jane Smith, Recruiting</p>"
            '<a href="mailto:jane@acme.test">Jane Smith</a>'
            '<a href="/private/team">Private</a>'
        ),
        "https://acme.test/team": (
            '<a href="mailto:engineering@acme.test">Engineering team</a>'
        ),
    }
    requested: list[str] = []

    def fetcher(url: str, **_kwargs: Any) -> str:
        requested.append(url)
        if url not in pages:
            raise RuntimeError("not in test fixture")
        return pages[url]

    contacts = discover_public_contacts(
        {
            "id": "job-1",
            "company": "Acme",
            "apply_url": "https://acme.test/jobs/role",
            "metadata": {},
        },
        max_pages=8,
        max_contacts=10,
        timeout_seconds=15,
        fetcher=fetcher,
    )

    assert [item["email"] for item in contacts] == [
        "jane@acme.test",
        "engineering@acme.test",
    ]
    assert contacts[0]["email_verification_status"] == "public_source_verified"
    assert contacts[0]["person_name"] == "Jane Smith"
    assert contacts[0]["person_title"] == "Recruiting"
    assert "https://outside.example/team" not in requested
    assert "https://acme.example/private/team" not in requested


def test_public_contact_crawler_does_not_guess_from_a_company_domain() -> None:
    requested: list[str] = []

    def fetcher(url: str, **_kwargs: Any) -> str:
        requested.append(url)
        return "<h1>Careers</h1><p>Apply through the form.</p>"

    contacts = discover_public_contacts(
        {
            "id": "job-2",
            "company": "Acme",
            "metadata": {"company_domain": "acme.test"},
        },
        timeout_seconds=15,
        fetcher=fetcher,
    )

    assert contacts == []
    assert any(url == "https://acme.test/" for url in requested)


def test_public_contact_crawler_rejects_linkedin_and_telegram_targets() -> None:
    called = False

    def fetcher(_url: str, **_kwargs: Any) -> str:
        nonlocal called
        called = True
        return "<a href=\"mailto:person@company.test\">person</a>"

    contacts = discover_public_contacts(
        {
            "id": "job-3",
            "company": "Acme",
            "apply_url": "https://www.linkedin.com/jobs/view/123",
            "metadata": {"company_domain": "t.me/acme"},
        },
        timeout_seconds=15,
        fetcher=fetcher,
    )

    assert contacts == []
    assert called is False
