from __future__ import annotations

import asyncio
from typing import Any

import pytest

from worker.browser_runtime import BrowserRuntime, ResolvedBrowserTask
from worker.providers import get_adapter
from worker.providers.base import ProviderAdapter, canonical_form_target, checkpoint_present


USER_ID = "00000000-0000-0000-0000-000000000002"
APPLICATION_ID = "00000000-0000-0000-0000-000000000003"
JOB_ID = "00000000-0000-0000-0000-000000000004"


FIELDS = [
    {
        "dom_index": 0,
        "key": "email",
        "label": "Email address",
        "kind": "email",
        "required": True,
        "disabled": False,
        "answered": False,
        "options": [],
        "option_label": "",
        "accept": "",
    }
]


class FixtureControl:
    def __init__(
        self,
        on_click: Any | None = None,
        *,
        visible: bool = True,
        href: str | None = None,
    ) -> None:
        self.on_click = on_click
        self.visible = visible
        self.href = href
        self.clicks = 0
        self.filled: str | None = None

    async def is_visible(self) -> bool:
        return self.visible

    async def is_enabled(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None

    async def fill(self, value: str) -> None:
        self.filled = value

    async def click(self) -> None:
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()


class FixtureLocator:
    def __init__(self, values: list[Any] | None = None, *, body: str = "") -> None:
        self.values = values or []
        self.body = body

    async def count(self) -> int:
        return len(self.values)

    def nth(self, index: int) -> Any:
        return self.values[index]

    async def inner_text(self, **_kwargs: Any) -> str:
        return self.body


class FixtureFormRoot:
    def __init__(self) -> None:
        self.controls = [FixtureControl()]
        self.final_controls = [FixtureControl()]

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self, **_kwargs: Any) -> str:
        return "Apply for this job. Application form. Resume and email are required."

    async def evaluate(self, _script: str) -> list[dict[str, Any]]:
        return FIELDS

    def locator(self, selector: str) -> FixtureLocator:
        if "input:not" in selector:
            return FixtureLocator(self.controls)
        if 'input[type="email"]' in selector:
            return FixtureLocator(self.controls)
        if selector == "textarea":
            return FixtureLocator(self.controls)
        if 'text-is("Send")' in selector or 'text-is("Submit")' in selector:
            return FixtureLocator(self.final_controls)
        return FixtureLocator()


class FixtureRootCollection(FixtureLocator):
    pass


class FixturePage:
    def __init__(
        self,
        adapter: ProviderAdapter,
        start_url: str,
        *,
        after_url: str | None = None,
        form_visible: bool = False,
        entry_count: int = 1,
        root_count: int = 1,
        checkpoint_after_click: bool = False,
        login_after_click: bool = False,
        login_visible: bool = False,
    ) -> None:
        self.adapter = adapter
        self.start_url = start_url
        self.url = start_url
        self.after_url = after_url or start_url
        self.form_visible = form_visible
        self.root_count = root_count
        self.checkpoint_count = 0
        self.checkpoint_after_click = checkpoint_after_click
        self.login_after_click = login_after_click
        self.login_visible = login_visible
        self.main_frame = object()
        self.route_handler: Any | None = None
        self.entry_controls = [
            FixtureControl(self._open_form) for _ in range(entry_count)
        ]

    def _open_form(self) -> None:
        self.url = self.after_url
        if self.login_after_click:
            self.login_visible = True
            self.form_visible = False
        elif self.checkpoint_after_click:
            self.checkpoint_count = 1
            self.form_visible = False
        else:
            self.form_visible = True

    async def route(self, _pattern: str, handler: Any) -> None:
        self.route_handler = handler

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = url
        if url == self.after_url and url != self.start_url:
            self._open_form()

    async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def locator(self, selector: str) -> FixtureLocator:
        if selector == self.adapter.application_entry_selector:
            return FixtureLocator(self.entry_controls)
        if selector in self.adapter.form_selectors:
            roots = (
                [FixtureFormRoot() for _ in range(self.root_count)]
                if self.form_visible
                else []
            )
            return FixtureRootCollection(roots)
        if selector == "body":
            return FixtureLocator(body="Application page")
        if 'type="password"' in selector:
            return FixtureLocator(
                [FixtureControl()] if self.login_visible else []
            )
        if "recaptcha" in selector:
            return FixtureLocator([FixtureControl()] * self.checkpoint_count)
        return FixtureLocator()


PROVIDER_FIXTURES = [
    (
        "google_forms",
        "https://docs.google.com/forms/d/e/example/viewform",
        "https://docs.google.com/forms/d/e/example/viewform",
        True,
    ),
    (
        "greenhouse",
        "https://job-boards.greenhouse.io/acme/jobs/1",
        "https://job-boards.greenhouse.io/acme/jobs/1#app",
        False,
    ),
    (
        "lever",
        "https://jobs.lever.co/acme/role-id",
        "https://jobs.lever.co/acme/role-id/apply",
        False,
    ),
    (
        "ashby",
        "https://jobs.ashbyhq.com/acme/role-id",
        "https://jobs.ashbyhq.com/acme/role-id/application",
        False,
    ),
    (
        "yc",
        "https://www.workatastartup.com/jobs/1",
        "https://www.workatastartup.com/jobs/1",
        False,
    ),
    (
        "wellfound",
        "https://wellfound.com/jobs/1",
        "https://wellfound.com/jobs/1?autoOpenApplication=true",
        False,
    ),
    (
        "cutshort",
        "https://cutshort.io/job/one",
        "https://cutshort.io/job/one",
        False,
    ),
    (
        "instahyre",
        "https://www.instahyre.com/candidate/opportunities/one",
        "https://www.instahyre.com/candidate/opportunities/one",
        False,
    ),
]


@pytest.mark.parametrize(
    ("provider", "start_url", "after_url", "already_open"), PROVIDER_FIXTURES
)
def test_provider_prepare_hook_opens_at_most_one_application_entry(
    provider: str,
    start_url: str,
    after_url: str,
    already_open: bool,
) -> None:
    adapter = get_adapter(provider)
    assert adapter is not None
    unsupported = {"cutshort", "instahyre"}
    if provider in {"google_forms", "wellfound", *unsupported}:
        assert adapter.application_entry_selector is None
    else:
        assert adapter.application_entry_selector
    if provider == "wellfound":
        assert adapter.application_entry_query == (("autoOpenApplication", "true"),)
    page = FixturePage(
        adapter,
        start_url,
        after_url=after_url,
        form_visible=already_open,
        entry_count=(
            0 if already_open or provider == "wellfound" or provider in unsupported else 1
        ),
    )

    prepared = asyncio.run(adapter.open_application(page))

    if provider in unsupported:
        assert prepared.code == "provider_transition_unsupported"
        assert prepared.transitioned is False
        assert not page.entry_controls
        return
    assert prepared.ready
    assert prepared.transitioned is (not already_open)
    expected_clicks = 0 if already_open or provider == "wellfound" else 1
    assert sum(control.clicks for control in page.entry_controls) == expected_clicks
    assert page.url == after_url


def test_prepare_hook_rejects_ambiguous_apply_controls_without_clicking() -> None:
    adapter = get_adapter("lever")
    assert adapter is not None
    page = FixturePage(
        adapter,
        "https://jobs.lever.co/acme/role-id",
        after_url="https://jobs.lever.co/acme/role-id/apply",
        entry_count=2,
    )

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.code == "application_entry_ambiguous"
    assert all(control.clicks == 0 for control in page.entry_controls)


def test_custom_company_form_allows_one_same_host_query_addressed_transition() -> None:
    start_url = "https://careers.acme.com/jobs/one"
    form_url = (
        "https://careers.acme.com/forms/application?opening=one&token=a%2Bb#review"
    )
    adapter = get_adapter("company_form", target_url=start_url)
    assert adapter is not None
    page = FixturePage(adapter, start_url, after_url=form_url)

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.ready
    assert prepared.transitioned is True
    assert page.entry_controls[0].clicks == 1
    assert canonical_form_target("company_form", page.url) == (
        "https://careers.acme.com/forms/application?opening=one&token=a%2Bb"
    )


@pytest.mark.parametrize(
    ("provider", "start_url", "target"),
    [
        (
            "lever",
            "https://jobs.lever.co/acme/role-id",
            "https://jobs.lever.co/acme/role-id/apply",
        ),
        (
            "ashby",
            "https://jobs.ashbyhq.com/acme/role-id",
            "https://jobs.ashbyhq.com/acme/role-id/application",
        ),
    ],
)
def test_duplicate_links_to_same_allowlisted_form_are_one_safe_transition(
    provider: str,
    start_url: str,
    target: str,
) -> None:
    adapter = get_adapter(provider)
    assert adapter is not None
    page = FixturePage(adapter, start_url, after_url=target, entry_count=0)
    page.entry_controls = [
        FixtureControl(page._open_form, href=target),
        FixtureControl(page._open_form, href=target),
    ]

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.ready
    assert prepared.transitioned is True
    assert page.url == target
    assert all(control.clicks == 0 for control in page.entry_controls)


def test_prepare_hook_stops_when_apply_opens_a_checkpoint() -> None:
    adapter = get_adapter("ashby")
    assert adapter is not None
    page = FixturePage(
        adapter,
        "https://jobs.ashbyhq.com/acme/role-id",
        after_url="https://jobs.ashbyhq.com/acme/role-id/application",
        checkpoint_after_click=True,
    )

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.code == "security_checkpoint"
    assert prepared.transitioned is True
    assert page.entry_controls[0].clicks == 1


class CheckpointFixturePage:
    def __init__(
        self,
        widget_controls: list[FixtureControl],
        *,
        verification_controls: list[FixtureControl] | None = None,
        body: str = "",
    ) -> None:
        self.widget_controls = widget_controls
        self.verification_controls = verification_controls or []
        self.body = body

    def locator(self, selector: str) -> FixtureLocator:
        if selector == "body":
            return FixtureLocator(body=self.body)
        if "one-time-code" in selector:
            return FixtureLocator(self.verification_controls)
        return FixtureLocator(self.widget_controls)


def test_invisible_background_captcha_widget_is_not_an_active_checkpoint() -> None:
    page = CheckpointFixturePage([FixtureControl(visible=False)])

    assert asyncio.run(checkpoint_present(page)) is False


def test_visible_captcha_or_checkpoint_copy_requires_attention() -> None:
    visible = CheckpointFixturePage([FixtureControl(visible=True)])
    copy = CheckpointFixturePage(
        [FixtureControl(visible=False)],
        body="Please verify that you are human to continue.",
    )

    assert asyncio.run(checkpoint_present(visible)) is True
    assert asyncio.run(checkpoint_present(copy)) is True


@pytest.mark.parametrize(
    ("page", "expected_non_submit", "expected_submit"),
    [
        (CheckpointFixturePage([FixtureControl(visible=True)]), False, True),
        (
            CheckpointFixturePage(
                [],
                verification_controls=[FixtureControl(visible=True)],
            ),
            True,
            True,
        ),
        (
            CheckpointFixturePage(
                [],
                body="Please verify that you are human to continue.",
            ),
            True,
            True,
        ),
    ],
    ids=("passive-widget", "otp-control", "challenge-copy"),
)
def test_checkpoint_policy_is_phase_aware(
    page: CheckpointFixturePage,
    expected_non_submit: bool,
    expected_submit: bool,
) -> None:
    assert (
        asyncio.run(checkpoint_present(page, include_passive_widgets=False))
        is expected_non_submit
    )
    assert (
        asyncio.run(checkpoint_present(page, include_passive_widgets=True))
        is expected_submit
    )


def test_prepare_hook_stops_on_login_or_cross_provider_redirect() -> None:
    yc = get_adapter("yc")
    lever = get_adapter("lever")
    assert yc is not None and lever is not None

    login = FixturePage(
        yc,
        "https://account.ycombinator.com/authenticate",
    )
    outside = FixturePage(
        lever,
        "https://jobs.lever.co/acme/role-id",
        after_url="https://attacker.example/apply",
    )

    assert asyncio.run(yc.open_application(login)).code == "provider_login_required"
    assert asyncio.run(lever.open_application(outside)).code == "provider_redirect_blocked"


def test_login_modal_after_wellfound_transition_is_never_scanned_as_application() -> None:
    adapter = get_adapter("wellfound")
    assert adapter is not None
    page = FixturePage(
        adapter,
        "https://wellfound.com/jobs/1",
        after_url="https://wellfound.com/jobs/1?autoOpenApplication=true",
        entry_count=0,
        login_after_click=True,
    )

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.code == "provider_login_required"
    assert prepared.root is None
    assert prepared.transitioned is True
    assert not page.entry_controls


@pytest.mark.parametrize("provider", ["yc", "wellfound", "cutshort", "instahyre"])
def test_visible_login_prompt_prevents_any_application_transition(provider: str) -> None:
    adapter = get_adapter(provider)
    assert adapter is not None
    page = FixturePage(
        adapter,
        next(fixture[1] for fixture in PROVIDER_FIXTURES if fixture[0] == provider),
        login_visible=True,
    )

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.code == "provider_login_required"
    assert page.entry_controls[0].clicks == 0


def test_custom_company_form_never_scans_or_fills_a_login_prompt() -> None:
    target_url = "https://careers.acme.com/jobs/one/apply"
    adapter = get_adapter("company_form", target_url=target_url)
    assert adapter is not None
    page = FixturePage(adapter, target_url, login_visible=True)

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.code == "provider_login_required"
    assert prepared.root is None
    assert page.entry_controls[0].clicks == 0


def test_prepare_hook_rejects_multiple_application_roots() -> None:
    adapter = get_adapter("greenhouse")
    assert adapter is not None
    page = FixturePage(
        adapter,
        "https://job-boards.greenhouse.io/acme/jobs/1",
        form_visible=True,
        entry_count=0,
        root_count=2,
    )

    prepared = asyncio.run(adapter.open_application(page))

    assert prepared.code == "application_form_ambiguous"


def test_browser_runtime_scans_the_form_opened_by_provider_transition() -> None:
    adapter = get_adapter("lever")
    assert adapter is not None
    start_url = "https://jobs.lever.co/acme/role-id"
    page = FixturePage(
        adapter,
        start_url,
        after_url=f"{start_url}/apply",
    )
    task = ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="lever",
        phase="scan",
        target_url=start_url,
        context_id=None,
    )

    result = asyncio.run(
        BrowserRuntime(object())._run_page(page, adapter, task, None)  # type: ignore[arg-type]
    )

    assert result.code == "application_form_scanned"
    assert result.form_url == f"{start_url}/apply"
    assert page.entry_controls[0].clicks == 1


def test_browser_runtime_uses_deterministic_wellfound_application_query() -> None:
    adapter = get_adapter("wellfound")
    assert adapter is not None
    start_url = "https://wellfound.com/jobs/1"
    transition_url = f"{start_url}?autoOpenApplication=true"
    page = FixturePage(
        adapter,
        start_url,
        after_url=transition_url,
        entry_count=0,
    )
    task = ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="wellfound",
        phase="scan",
        target_url=start_url,
        context_id=None,
    )

    result = asyncio.run(
        BrowserRuntime(object())._run_page(page, adapter, task, None)  # type: ignore[arg-type]
    )

    assert result.code == "application_form_scanned"
    assert page.url == transition_url
    # The provider-only dialog query is reconstructed at runtime, not persisted
    # as part of the job/application identity.
    assert result.form_url == start_url


@pytest.mark.parametrize("provider", ["cutshort", "instahyre"])
def test_runtime_reports_unsupported_authenticated_provider_transition(
    provider: str,
) -> None:
    adapter = get_adapter(provider)
    assert adapter is not None
    start_url = next(
        fixture[1] for fixture in PROVIDER_FIXTURES if fixture[0] == provider
    )
    page = FixturePage(adapter, start_url, entry_count=0)
    task = ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider=provider,
        phase="scan",
        target_url=start_url,
        context_id=None,
    )

    result = asyncio.run(
        BrowserRuntime(object())._run_page(page, adapter, task, None)  # type: ignore[arg-type]
    )

    assert result.status == "needs_attention"
    assert result.code == "provider_transition_unsupported"


def test_final_actions_never_include_multi_page_next_or_continue_controls() -> None:
    for provider, *_fixture in PROVIDER_FIXTURES:
        adapter = get_adapter(provider)
        assert adapter is not None
        selector_contract = " ".join(adapter.submit_selectors).casefold()
        assert "next" not in selector_contract
        assert "continue" not in selector_contract
        assert adapter.submit_selectors != (
            'button[type="submit"],input[type="submit"]',
        )


def test_authenticated_provider_entry_never_uses_a_bare_apply_control() -> None:
    wellfound = get_adapter("wellfound")
    assert wellfound is not None
    assert wellfound.application_entry_selector is None
    assert wellfound.application_entry_query == (("autoOpenApplication", "true"),)

    yc = get_adapter("yc")
    assert yc is not None
    assert yc.application_entry_selector == (
        'a:text-is("Apply"),button:text-is("Apply"),'
        'a:text-is("Apply now"),button:text-is("Apply now"),'
        'a:text-is("Apply Now"),button:text-is("Apply Now")'
    )
    assert ":has-text" not in yc.application_entry_selector
    assert yc.application_transition_unsupported_message is None
    assert yc.submission_unsupported_message is None

    for provider in ("cutshort", "instahyre"):
        adapter = get_adapter(provider)
        assert adapter is not None
        assert adapter.application_entry_selector is None
        assert adapter.application_transition_unsupported_message
        assert adapter.submission_unsupported_message
        assert adapter.submit_selectors == ()


def test_live_audited_lever_and_ashby_contracts_are_registered() -> None:
    lever = get_adapter("lever")
    ashby = get_adapter("ashby")
    assert lever is not None and ashby is not None

    assert '#btn-submit[data-qa="btn-submit"]' in " ".join(
        lever.submit_selectors
    )
    assert '#form[role="tabpanel"]' in ashby.form_selectors
    assert ".ashby-application-form-container" in ashby.form_selectors
    assert ".ashby-application-form-submit-button" in " ".join(
        ashby.submit_selectors
    )
