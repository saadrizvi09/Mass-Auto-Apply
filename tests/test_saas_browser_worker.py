from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import worker.browser_runtime as browser_runtime_module

from app.saas.browser import BrowserbaseClient, BrowserbaseError, TrustedBrowserSession
from app.saas.crypto import TokenCipher
from worker.browser_runtime import (
    ApprovalSnapshot,
    BrowserRuntime,
    ManagedBrowserError,
    ManagedBrowserJobHandler,
    ResolvedBrowserTask,
    SupabaseTenantResources,
)
from worker.providers import ADAPTERS, get_adapter
from worker.providers.base import (
    bind_schema_to_target,
    canonical_form_target,
    safe_form_url,
    scan_form,
)
from worker.providers.company_form import public_company_form_host
from worker.providers.base import ProviderResult
from worker.providers.yc import canonical_yc_form_target, is_exact_yc_job_url
from worker.handlers import handle_job


USER_ID = "00000000-0000-0000-0000-000000000002"
APPLICATION_ID = "00000000-0000-0000-0000-000000000003"
JOB_ID = "00000000-0000-0000-0000-000000000004"
REVISION_ID = "00000000-0000-0000-0000-000000000005"


def _browserbase_fingerprint(project_id: str) -> str:
    return hashlib.sha256(
        b"autoapply.browserbase.project.v1\x00" + project_id.encode("utf-8")
    ).hexdigest()


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self.content = b"json"
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttp:
    def __init__(self, connect_url: str) -> None:
        self.connect_url = connect_url
        self.calls: list[dict[str, Any]] = []

    def request(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
        self.calls.append(dict(_kwargs))
        return FakeResponse(
            {
                "id": "session-1",
                "status": "RUNNING",
                "connectUrl": self.connect_url,
            }
        )


def test_worker_only_browserbase_session_hides_and_validates_cdp_credential() -> None:
    secret_url = "wss://connect.browserbase.com?sessionId=one&token=secret"
    client = BrowserbaseClient("secret", "project", http=FakeHttp(secret_url))

    public = client.create_session("context-1")
    trusted = client.create_session_for_worker("context-1")

    assert "connect" not in public
    assert trusted.connect_url == secret_url
    assert secret_url not in repr(trusted)

    untrusted = BrowserbaseClient(
        "secret",
        "project",
        http=FakeHttp("wss://attacker.example/cdp?token=secret"),
    )
    with pytest.raises(BrowserbaseError) as error:
        untrusted.create_session_for_worker("context-1")
    assert error.value.code == "browserbase_invalid_response"


def test_ephemeral_worker_session_does_not_attach_a_persistent_context() -> None:
    http = FakeHttp("wss://connect.browserbase.com?sessionId=one&token=secret")
    client = BrowserbaseClient("secret", "project", http=http)

    session = client.create_ephemeral_session_for_worker(timeout_seconds=300)

    assert session.context_id is None
    assert "browserSettings" not in http.calls[0]["json"]


@pytest.mark.parametrize(
    ("provider", "allowed_url"),
    [
        ("google_forms", "https://docs.google.com/forms/d/e/abc/viewform"),
        ("google_forms", "https://forms.gle/abc123"),
        ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1"),
        ("greenhouse", "https://job-boards.eu.greenhouse.io/acme/jobs/1"),
        ("lever", "https://jobs.lever.co/acme/role/apply"),
        ("ashby", "https://jobs.ashbyhq.com/acme/role/application"),
        (
            "yc",
            "https://www.ycombinator.com/companies/circleback/jobs/dUH7zjN-software-engineer",
        ),
        ("wellfound", "https://wellfound.com/jobs/1"),
        ("cutshort", "https://cutshort.io/job/one"),
        ("instahyre", "https://www.instahyre.com/candidate/opportunities/"),
    ],
)
def test_every_managed_provider_has_a_strict_https_host_policy(
    provider: str, allowed_url: str
) -> None:
    adapter = get_adapter(provider)
    assert adapter is not None and adapter.allows_url(allowed_url)
    assert not adapter.allows_url(allowed_url.replace("https://", "http://"))
    assert not adapter.allows_url("https://attacker.example/redirect")
    assert not adapter.allows_url("https://docs.google.com.attacker.example/forms/one")
    assert not adapter.allows_url("https://user:password@docs.google.com/forms/one")


def test_google_forms_policy_scopes_short_links_without_opening_all_google_docs() -> None:
    adapter = get_adapter("google_forms")
    assert adapter is not None
    assert adapter.allows_url("https://forms.gle/abc123")
    assert adapter.allows_url("https://docs.google.com/forms/d/e/abc/viewform")
    assert not adapter.allows_url("https://docs.google.com/document/d/private")


@pytest.mark.parametrize(
    "target_url",
    [
        "http://careers.acme.com/apply",
        "https://localhost/apply",
        "https://127.0.0.1/apply",
        "https://10.0.0.1/apply",
        "https://[::1]/apply",
        "https://user:password@careers.acme.com/apply",
        "https://careers.acme.com:443/apply",
        "https://intranet/apply",
        "https://jobs.acme.local/apply",
        "https://127.0.0.1.nip.io/apply",
    ],
)
def test_custom_company_form_rejects_non_public_or_credentialed_targets(
    target_url: str,
) -> None:
    assert public_company_form_host(target_url) is None
    assert get_adapter("company_form", target_url=target_url) is None


@pytest.mark.parametrize(
    "target_url",
    [
        "https://www.ycombinator.com/jobs",
        "https://ycombinator.com/companies/acme",
        "https://account.ycombinator.com/authenticate",
        "https://www.workatastartup.com/jobs/12345",
        "https://subdomain.workatastartup.com/application",
    ],
)
def test_custom_company_form_never_claims_yc_owned_hosts(target_url: str) -> None:
    assert public_company_form_host(target_url) is None
    assert get_adapter("company_form", target_url=target_url) is None


def test_custom_company_form_is_bound_to_the_exact_validated_https_host() -> None:
    target_url = "https://careers.acme.com/jobs/one/apply?source=public"
    adapter = get_adapter("company_form", target_url=target_url)

    assert adapter is not None
    assert public_company_form_host(target_url) == "careers.acme.com"
    assert adapter.allows_url(target_url)
    assert adapter.allows_url("https://careers.acme.com/application/confirmation")
    assert not adapter.allows_url("https://apply.careers.acme.com/jobs/one")
    assert not adapter.allows_url("https://acme.com/jobs/one")
    assert not adapter.allows_url("https://careers.acme.com:443/jobs/one")
    assert not adapter.allows_url("http://careers.acme.com/jobs/one")
    assert "next" not in " ".join(adapter.submit_selectors).casefold()
    assert "continue" not in " ".join(adapter.submit_selectors).casefold()


def test_custom_company_form_identity_preserves_bounded_query_and_drops_fragment() -> None:
    first = canonical_form_target(
        "company_form",
        "https://careers.acme.com/application?opening=one&token=a%2Bb#private",
    )
    second = canonical_form_target(
        "company_form",
        "https://careers.acme.com/application?opening=two&token=a%2Bb#other",
    )

    assert first == (
        "https://careers.acme.com/application?opening=one&token=a%2Bb"
    )
    assert second.endswith("?opening=two&token=a%2Bb")
    page = FakePage(first, _fields())  # type: ignore[name-defined]
    first_schema = bind_schema_to_target(asyncio.run(scan_form(page)), first)
    second_schema = bind_schema_to_target(asyncio.run(scan_form(page)), second)
    assert first_schema.schema_hash != second_schema.schema_hash


def test_custom_company_form_identity_rejects_oversized_query() -> None:
    target_url = "https://careers.acme.com/application?token=" + ("x" * 2_049)

    assert canonical_form_target("company_form", target_url) == ""


def test_yc_policy_is_limited_to_exact_job_and_confirmation_paths() -> None:
    adapter = get_adapter("yc")
    current_job = (
        "https://www.ycombinator.com/companies/circleback/jobs/"
        "dUH7zjN-software-engineer"
    )

    assert adapter is not None
    assert is_exact_yc_job_url(current_job)
    assert adapter.allows_url(current_job)
    assert not is_exact_yc_job_url("https://www.workatastartup.com/jobs/12345")
    assert not is_exact_yc_job_url(
        "https://www.workatastartup.com/companies/acme/jobs/12345-engineer"
    )
    assert not adapter.allows_url("https://www.workatastartup.com/jobs/12345")
    assert not adapter.allows_url(
        "https://www.workatastartup.com/companies/acme/jobs/12345-engineer"
    )
    assert adapter.allows_url(
        "https://www.workatastartup.com/applications/submitted"
    )
    assert not adapter.allows_url("https://www.workatastartup.com/jobs")
    assert not adapter.allows_url("https://www.workatastartup.com/companies/acme")
    assert not adapter.allows_url("https://account.ycombinator.com/authenticate")
    assert not adapter.allows_url("https://www.ycombinator.com/jobs")
    assert not adapter.allows_url(
        "https://www.ycombinator.com/companies/circleback/jobs/dUH7zjN-software-engineer/related"
    )
    assert not adapter.allows_url(
        "https://www.ycombinator.com/companies/circleback/jobs/dUH7zjN-software-engineer?role=all"
    )


def test_yc_account_handoff_is_allowed_only_for_one_numeric_signup_job() -> None:
    adapter = get_adapter("yc")
    assert adapter is not None
    valid = (
        "https://account.ycombinator.com/authenticate?"
        "continue=https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D91924"
        "&defaults%5BsignUpActive%5D=true&defaults%5Bwaas_company%5D=29408"
    )

    assert adapter.allows_url(valid)
    assert adapter.allows_url(
        "https://www.workatastartup.com/application?signup_job_id=91924"
    )
    assert not is_exact_yc_job_url(valid)
    assert not adapter.allows_url(
        valid.replace("www.workatastartup.com", "attacker.example")
    )
    assert not adapter.allows_url(valid + "&continue=https%3A%2F%2Fattacker.example")
    assert not adapter.allows_url(
        "https://account.ycombinator.com/authenticate?continue="
        "https%3A%2F%2Fwww.workatastartup.com%2Fcompanies"
    )


def test_yc_form_identity_preserves_signup_job_id_and_changes_schema_hash() -> None:
    first = canonical_yc_form_target(
        "https://www.workatastartup.com/application?signup_job_id=91924"
    )
    second = canonical_yc_form_target(
        "https://www.workatastartup.com/application?signup_job_id=91925"
    )
    page = FakePage(first, _yc_fields(), submit_count=1)  # type: ignore[name-defined]
    raw = asyncio.run(scan_form(page))

    assert first.endswith("?signup_job_id=91924")
    assert second.endswith("?signup_job_id=91925")
    assert bind_schema_to_target(raw, first).schema_hash != bind_schema_to_target(
        raw, second
    ).schema_hash


def test_worker_registry_contains_no_ziprecruiter_adapter() -> None:
    assert set(ADAPTERS) == {
        "google_forms",
        "greenhouse",
        "lever",
        "ashby",
        "yc",
        "wellfound",
        "cutshort",
        "instahyre",
    }


def test_persisted_form_url_strips_credentials_query_and_fragment() -> None:
    assert safe_form_url(
        "https://user:password@jobs.lever.co/acme/role?token=secret#private"
    ) == "https://jobs.lever.co/acme/role"


def test_greenhouse_target_preserves_identity_query_but_removes_tracking() -> None:
    target = canonical_form_target(
        "greenhouse",
        "https://boards.greenhouse.io/embed/job_app?gh_src=tracking&token=6778798&for=Acme#private",
    )
    assert target == "https://boards.greenhouse.io/embed/job_app?for=Acme&token=6778798"


def test_schema_approval_hash_is_bound_to_canonical_form_identity() -> None:
    schema = asyncio.run(scan_form(FakePage(  # type: ignore[name-defined]
        "https://boards.greenhouse.io/embed/job_app?token=one", _fields()  # type: ignore[name-defined]
    )))
    first = bind_schema_to_target(
        schema, "https://boards.greenhouse.io/embed/job_app?token=one"
    )
    second = bind_schema_to_target(
        schema, "https://boards.greenhouse.io/embed/job_app?token=two"
    )
    assert first.schema_hash != second.schema_hash


class FakeControl:
    def __init__(
        self,
        *,
        text: str = "",
        attributes: dict[str, str] | None = None,
        options: list["FakeControl"] | None = None,
    ) -> None:
        self.filled: str | None = None
        self.files: str | None = None
        self.checked: bool | None = None
        self.selected: str | None = None
        self.clicks = 0
        self.on_click: Any = None
        self.click_error: Exception | None = None
        self.text = text
        self.attributes = dict(attributes or {})
        self.options = list(options or [])

    async def fill(self, value: str) -> None:
        self.filled = value

    async def set_input_files(self, value: str) -> None:
        self.files = value

    async def evaluate(self, _script: str) -> dict[str, Any]:
        if not self.files:
            return {"count": 0}
        return {
            "count": 1,
            "name": "resume.pdf",
            "type": "application/pdf",
            "size": 1_024,
        }

    async def set_checked(self, value: bool) -> None:
        self.checked = value

    async def check(self) -> None:
        self.checked = True

    async def select_option(self, *, label: str) -> None:
        self.selected = label

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def inner_text(self, **_kwargs: Any) -> str:
        return self.text

    async def get_attribute(self, name: str) -> str | None:
        return self.attributes.get(name)

    async def input_value(self) -> str:
        return self.filled or ""

    def locator(self, selector: str) -> "FakeLocator":
        if '[role="option"]' in selector:
            if 'aria-selected="true"' in selector:
                return FakeLocator(
                    [option for option in self.options if option.attributes.get("aria-selected") == "true"]
                )
            return FakeLocator(self.options)
        return FakeLocator()

    async def click(self) -> None:
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()
        if self.click_error is not None:
            raise self.click_error


class FakeLocator:
    def __init__(
        self,
        controls: list[FakeControl] | None = None,
        *,
        body: str = "",
    ) -> None:
        self.controls = controls or []
        self.body = body

    async def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> FakeControl:
        return self.controls[index]

    async def inner_text(self, **_kwargs: Any) -> str:
        return self.body


class FakeFormRoot:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def is_visible(self) -> bool:
        return True

    async def inner_text(self, **_kwargs: Any) -> str:
        return self.page.body

    async def evaluate(self, _script: str) -> list[dict[str, Any]]:
        return self.page.fields

    def locator(self, selector: str) -> FakeLocator:
        if '[role="listitem"]' in selector:
            return FakeLocator(self.page.upload_prompt_containers)
        if "input:not" in selector:
            return FakeLocator(self.page.controls)
        if 'input[type="file"]' in selector:
            return FakeLocator(
                [
                    control
                    for control, field in zip(self.page.controls, self.page.fields)
                    if field.get("kind") == "file"
                ]
            )
        if 'input[type="email"]' in selector:
            return FakeLocator(
                [
                    control
                    for control, field in zip(self.page.controls, self.page.fields)
                    if field.get("kind") == "email"
                ]
            )
        if selector == "textarea":
            return FakeLocator(
                [
                    control
                    for control, field in zip(self.page.controls, self.page.fields)
                    if field.get("kind") == "textarea"
                ]
            )
        if "submit" in selector.lower():
            return FakeLocator(self.page.submit_controls)
        return FakeLocator()


class FakeRootCollection:
    def __init__(self, roots: list[FakeFormRoot]) -> None:
        self.roots = roots

    async def count(self) -> int:
        return len(self.roots)

    def nth(self, index: int) -> FakeFormRoot:
        return self.roots[index]


class FakePage:
    def __init__(
        self,
        url: str,
        fields: list[dict[str, Any]],
        *,
        body: str = "Application form",
        checkpoint_count: int = 0,
        submit_count: int = 0,
        confirmed_url: str | None = None,
        confirmed_body: str | None = None,
        redirect_url: str | None = None,
        controls: list[FakeControl] | None = None,
        options: list[FakeControl] | None = None,
        upload_prompt_containers: list[FakeControl] | None = None,
    ) -> None:
        self.url = url
        self.target_url = url
        self.fields = fields
        self.body = body
        self.checkpoint_count = checkpoint_count
        self.controls = controls or [FakeControl() for _ in fields]
        self.options = list(options or [])
        self.upload_prompt_containers = list(upload_prompt_containers or [])
        self.submit_controls = [FakeControl() for _ in range(submit_count)]
        self.redirect_url = redirect_url
        self.main_frame = object()
        self.route_handler: Any = None
        self.unroute_behaviors: list[str] = []
        for control in self.submit_controls:
            control.on_click = lambda: self._confirm(confirmed_url, confirmed_body)

    def _confirm(self, url: str | None, body: str | None) -> None:
        if url is not None:
            self.url = url
        if body is not None:
            self.body = body

    async def route(self, _pattern: str, handler: Any) -> None:
        self.route_handler = handler

    async def unroute_all(self, *, behavior: str) -> None:
        self.unroute_behaviors.append(behavior)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = self.redirect_url or url

    async def evaluate(self, _script: str) -> list[dict[str, Any]]:
        return self.fields

    def locator(self, selector: str) -> FakeLocator:
        if "form" in selector.lower() or '[role="dialog"]' in selector.lower():
            return FakeRootCollection([FakeFormRoot(self)])  # type: ignore[return-value]
        if selector == "body":
            return FakeLocator(body=self.body)
        if "recaptcha" in selector:
            return FakeLocator([FakeControl()] * self.checkpoint_count)
        if "input:not" in selector:
            return FakeLocator(self.controls)
        if '[role="option"]' in selector:
            return FakeLocator(self.options)
        if "submit" in selector.lower():
            return FakeLocator(self.submit_controls)
        return FakeLocator()

    async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class CurrentYcJobPage(FakePage):
    """Current YC job page whose exact Apply link reaches one signed-in form."""

    target = (
        "https://www.ycombinator.com/companies/circleback/jobs/"
        "dUH7zjN-software-engineer"
    )
    application = "https://www.workatastartup.com/application?signup_job_id=91924"
    handoff = (
        "https://account.ycombinator.com/authenticate?"
        "continue=https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D91924"
        "&defaults%5BsignUpActive%5D=true&defaults%5Bwaas_company%5D=29408"
    )

    def __init__(
        self,
        fields: list[dict[str, Any]] | None = None,
        *,
        checkpoint_count: int = 0,
        submit_count: int = 1,
        confirmed_body: str | None = None,
        confirmed_url: str | None = None,
    ) -> None:
        super().__init__(
            self.target,
            fields or _yc_fields(),
            checkpoint_count=checkpoint_count,
            submit_count=submit_count,
            confirmed_body=confirmed_body,
            confirmed_url=confirmed_url,
        )
        self.entry_control = FakeControl(attributes={"href": self.handoff})
        self.visited: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        if "account.ycombinator.com/authenticate" in selector:
            return FakeLocator(
                [self.entry_control] if self.url == self.target else []
            )
        if (
            "form" in selector.lower() or '[role="dialog"]' in selector.lower()
        ) and self.url == self.target:
            return FakeRootCollection([])  # type: ignore[return-value]
        return super().locator(selector)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.visited.append(url)
        self.url = self.application if url == self.handoff else url


class DelayedGoogleFormPage(FakePage):
    """Model Google's empty HTML shell becoming interactive after page load."""

    def __init__(self, url: str, rendered_fields: list[dict[str, Any]]) -> None:
        super().__init__(url, [])
        self.rendered_fields = rendered_fields
        self.form_waits: list[dict[str, Any]] = []

    async def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        self.form_waits.append({"selector": selector, **kwargs})
        self.fields = self.rendered_fields
        self.controls = [FakeControl() for _ in self.rendered_fields]


def _runtime_schema(page: FakePage, provider: str = "greenhouse") -> Any:
    return bind_schema_to_target(
        asyncio.run(scan_form(page)),
        canonical_form_target(provider, page.url),
    )


def test_google_forms_waits_for_client_rendered_controls_before_scanning() -> None:
    page = DelayedGoogleFormPage(
        _google_task("scan", None).target_url,
        _fields(),
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            _google_task("scan", None),
            None,
        )
    )

    assert result.code == "application_form_scanned"
    assert result.schema is not None
    assert result.schema.public_fields[0]["label"] == "Email address"
    assert len(page.form_waits) == 1
    assert page.form_waits[0]["state"] == "visible"
    assert page.form_waits[0]["timeout"] == 8_000


def test_google_forms_reports_closed_form_instead_of_missing_form() -> None:
    task = _google_task("scan", None)
    page = DelayedGoogleFormPage(task.target_url, _fields())
    page.redirect_url = task.target_url.replace("/viewform", "/closedform")

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.status == "needs_attention"
    assert result.code == "application_form_closed"
    assert result.message == "This Google Form is no longer accepting responses."
    assert result.submission_state == "not_attempted"
    assert page.form_waits == []


def _fields() -> list[dict[str, Any]]:
    return [
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
        },
        {
            "dom_index": 1,
            "key": "private_note",
            "label": "Optional note",
            "kind": "textarea",
            "required": False,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "",
        },
    ]


def _yc_fields() -> list[dict[str, Any]]:
    return [
        {
            "dom_index": 0,
            "key": "application_message",
            "label": "Message to the founders",
            "kind": "textarea",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "",
        }
    ]


def _task(
    phase: str,
    schema_hash: str | None,
    *,
    answers: dict[str, Any] | None = None,
) -> ResolvedBrowserTask:
    approval = None
    if phase != "scan" and schema_hash:
        approval = ApprovalSnapshot(
            id=REVISION_ID,
            revision=1,
            schema_hash=schema_hash,
            answers=answers or {},
        )
    return ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="greenhouse",
        phase=phase,  # type: ignore[arg-type]
        target_url="https://job-boards.greenhouse.io/acme/jobs/1",
        context_id="context-1",
        context_credential_source="platform",
        context_credential_epoch=0,
        context_project_fingerprint=_browserbase_fingerprint("platform-project"),
        approval=approval,
    )


def _google_task(
    phase: str,
    schema_hash: str | None,
    *,
    answers: dict[str, Any] | None = None,
    context_id: str | None = None,
) -> ResolvedBrowserTask:
    approval = None
    if phase != "scan" and schema_hash:
        approval = ApprovalSnapshot(
            id=REVISION_ID,
            revision=1,
            schema_hash=schema_hash,
            answers=answers or {},
        )
    return ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="google_forms",
        phase=phase,  # type: ignore[arg-type]
        target_url="https://docs.google.com/forms/d/e/example/viewform",
        context_id=context_id,
        approval=approval,
    )


def _provider_task(
    provider: str,
    target_url: str,
    phase: str,
    schema_hash: str | None,
    *,
    answers: dict[str, Any] | None = None,
    context_id: str | None = None,
) -> ResolvedBrowserTask:
    approval = None
    if phase != "scan" and schema_hash:
        approval = ApprovalSnapshot(
            id=REVISION_ID,
            revision=1,
            schema_hash=schema_hash,
            answers=answers or {},
        )
    return ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider=provider,
        phase=phase,  # type: ignore[arg-type]
        target_url=target_url,
        context_id=context_id,
        approval=approval,
    )


class UnusedBrowserbase:
    pass


def test_prefill_uses_only_exact_approved_answers_and_never_submits() -> None:
    page = FakePage(_task("scan", None).target_url, _fields(), submit_count=1)
    schema = _runtime_schema(page)
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]
    task = _task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )

    result = asyncio.run(runtime._run_page(page, get_adapter("greenhouse"), task, None))  # type: ignore[arg-type]

    assert result.code == "review_required"
    assert result.status == "needs_attention"
    assert page.controls[0].filled == "candidate@example.com"
    assert page.controls[1].filled is None
    assert page.submit_controls[0].clicks == 0


def _google_listbox_fields() -> list[dict[str, Any]]:
    return _fields()[:1] + [
        {
            "dom_index": 1,
            "key": "Graduation Year",
            "label": "Graduation Year",
            "kind": "listbox",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": ["2025", "2026"],
            "option_label": "",
            "accept": "",
        }
    ]


def test_google_listbox_scan_preserves_group_label_key_and_options() -> None:
    class GoogleLikeRoot:
        async def evaluate(self, script: str) -> list[dict[str, Any]]:
            assert '[role="listbox"]' in script
            assert "'combobox', 'listbox'" in script
            return _google_listbox_fields()[1:]

    schema = asyncio.run(scan_form(GoogleLikeRoot()))

    assert schema.public_fields == [
        {
            "key": "Graduation Year",
            "label": "Graduation Year",
            "kind": "listbox",
            "type": "listbox",
            "required": True,
            "disabled": False,
            "prefilled": False,
            "options": ["2025", "2026"],
            "accepts_resume": False,
        }
    ]


def test_file_scan_uses_the_google_question_group_label_for_resume_detection() -> None:
    class GoogleFileRoot:
        async def evaluate(self, script: str) -> list[dict[str, Any]]:
            assert "'listbox', 'file'" in script
            return [
                {
                    "dom_index": 0,
                    "key": "file_upload",
                    "label": "Upload your Resume / CV",
                    "kind": "file",
                    "required": True,
                    "disabled": False,
                    "answered": False,
                    "options": [],
                    "option_label": "",
                    "accept": "application/pdf",
                }
            ]

    schema = asyncio.run(scan_form(GoogleFileRoot()))

    assert schema.public_fields[0]["label"] == "Upload your Resume / CV"
    assert schema.public_fields[0]["accepts_resume"] is True


def test_google_listbox_selects_exact_approved_year_and_verifies_state() -> None:
    option_2025 = FakeControl(text="2025", attributes={"data-value": "2025"})
    option_2026 = FakeControl(text="2026", attributes={"data-value": "2026"})
    year = FakeControl(options=[option_2025, option_2026])

    def choose_2026() -> None:
        option_2026.attributes["aria-selected"] = "true"
        year.text = "2026"

    option_2026.on_click = choose_2026
    page = FakePage(
        _google_task("scan", None).target_url,
        _google_listbox_fields(),
        controls=[FakeControl(), year],
        options=[option_2025, option_2026],
    )
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={
            "Email address": "candidate@example.com",
            "Graduation Year": " 2026 ",
        },
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "review_required"
    assert result.filled_count == 2
    assert option_2025.clicks == 0
    assert option_2026.clicks == 1
    assert option_2026.attributes["aria-selected"] == "true"


def test_google_listbox_mismatch_fails_closed_before_submit() -> None:
    option_2026 = FakeControl(text="2026", attributes={"data-value": "2026"})
    year = FakeControl(options=[option_2026])
    page = FakePage(
        _google_task("scan", None).target_url,
        _google_listbox_fields(),
        controls=[FakeControl(), year],
        options=[option_2026],
        submit_count=1,
    )
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "submit",
        schema.schema_hash,
        answers={
            "Email address": "candidate@example.com",
            "Graduation Year": "2027",
        },
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "required_answers_missing"
    assert result.missing_required == ("Graduation Year",)
    assert option_2026.clicks == 0
    assert page.submit_controls[0].clicks == 0


def test_resume_is_uploaded_only_to_explicit_resume_or_cv_field() -> None:
    fields = _fields() + [
        {
            "dom_index": 2,
            "key": "resume_file",
            "label": "Resume / CV",
            "kind": "file",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": ".pdf",
        },
        {
            "dom_index": 3,
            "key": "work_sample",
            "label": "Optional work sample",
            "kind": "file",
            "required": False,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": ".pdf",
        },
    ]
    page = FakePage(_task("scan", None).target_url, fields)
    schema = _runtime_schema(page)
    assert schema.public_fields[2]["accepts_resume"] is True
    assert schema.public_fields[2]["accepted_file_types"] == ".pdf"
    assert schema.public_fields[3]["accepts_resume"] is False
    task = _task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    result = asyncio.run(
        runtime._run_page(
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            task,
            "/tmp/tenant-resume.pdf",
        )
    )

    assert result.code == "review_required"
    assert page.controls[2].files == "/tmp/tenant-resume.pdf"
    assert page.controls[3].files is None


def test_google_resume_upload_requires_optional_saved_browser_context() -> None:
    fields = _fields() + [
        {
            "dom_index": 2,
            "key": "resume_file",
            "label": "Upload Resume / CV",
            "kind": "file",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "application/pdf",
        }
    ]
    page = FakePage(_google_task("scan", None).target_url, fields)
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            "/tmp/tenant-resume.pdf",
        )
    )

    assert result.code == "provider_login_required"
    assert result.submission_state == "not_attempted"
    assert page.controls[0].filled is None
    assert page.controls[2].files is None


def test_google_resume_upload_reuses_optional_saved_browser_context() -> None:
    fields = _fields() + [
        {
            "dom_index": 2,
            "key": "resume_file",
            "label": "Upload Resume / CV",
            "kind": "file",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": ".pdf",
        }
    ]
    page = FakePage(_google_task("scan", None).target_url, fields)
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
        context_id="google-context",
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(
        runtime.execute(task, resume_path="/tmp/tenant-resume.pdf")
    )

    assert execution.result.code == "review_required"
    assert page.controls[2].files == "/tmp/tenant-resume.pdf"
    assert browserbase.created == [("google-context", True)]
    assert browserbase.ephemeral == []
    assert browserbase.released == []
    assert browser.closed is False


def test_multiple_resume_upload_controls_fail_closed_before_any_attachment() -> None:
    fields = _fields() + [
        {
            "dom_index": index,
            "key": key,
            "label": label,
            "kind": "file",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "application/pdf",
        }
        for index, (key, label) in enumerate(
            (("resume", "Upload resume"), ("cv", "Attach CV")), start=2
        )
    ]
    page = FakePage(_task("scan", None).target_url, fields)

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task("scan", None),
            None,
        )
    )

    assert result.code == "resume_upload_ambiguous"
    assert page.controls[2].files is None
    assert page.controls[3].files is None


def test_resume_upload_that_excludes_pdf_is_not_approved_for_automation() -> None:
    fields = _fields() + [
        {
            "dom_index": 2,
            "key": "resume_file",
            "label": "Upload Resume",
            "kind": "file",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "application/msword,.docx",
        }
    ]
    page = FakePage(_task("scan", None).target_url, fields)
    schema = _runtime_schema(page)
    assert schema.public_fields[2]["accepts_resume"] is False

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task("scan", None),
            None,
        )
    )

    assert result.code == "resume_upload_unsupported"
    assert page.controls[2].files is None


def test_required_unrelated_file_control_never_receives_the_private_resume() -> None:
    fields = _fields() + [
        {
            "dom_index": 2,
            "key": "identity_document",
            "label": "Upload identity document",
            "kind": "file",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "application/pdf",
        }
    ]
    page = FakePage(_task("scan", None).target_url, fields)

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task("scan", None),
            None,
        )
    )

    assert result.code == "required_file_upload_unsupported"
    assert page.controls[2].files is None


def test_provider_owned_resume_picker_is_never_clicked_or_guessed() -> None:
    picker = FakeControl(text="Upload Resume Add file")
    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        upload_prompt_containers=[picker],
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            _google_task("scan", None),
            None,
        )
    )

    assert result.code == "provider_file_picker_unsupported"
    assert picker.clicks == 0


class GooglePickerFileInput(FakeControl):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.on_set_files: Any = None

    async def set_input_files(self, value: str) -> None:
        await super().set_input_files(value)
        if self.on_set_files is not None:
            self.on_set_files(value)

    async def evaluate(self, _script: str) -> dict[str, Any]:
        if not self.files:
            return {"count": 0}
        path = Path(self.files)
        return {
            "count": 1,
            "name": path.name,
            "type": "application/pdf",
            "size": path.stat().st_size,
        }


class GooglePickerFrame:
    def __init__(self, file_input: GooglePickerFileInput) -> None:
        self.file_input = file_input
        self.action = FakeControl(text="Upload")
        self.body = ""

    def locator(self, selector: str) -> FakeLocator:
        if 'input[type="file"]' in selector:
            return FakeLocator([self.file_input])
        if '[role="button"]' in selector:
            return FakeLocator([self.action])
        if selector == "body":
            return FakeLocator(body=self.body)
        return FakeLocator()


class GooglePickerFrameNode(FakeControl):
    def __init__(self, picker: GooglePickerFrame) -> None:
        super().__init__(
            attributes={"src": "https://docs.google.com/picker?protocol=gadgets"}
        )
        self.picker = picker
        self.visible = False

    @property
    def content_frame(self) -> GooglePickerFrame:
        return self.picker

    async def is_visible(self) -> bool:
        return self.visible


class GooglePickerQuestion(FakeControl):
    def __init__(self) -> None:
        super().__init__(text="Resume / CV *\nAdd file")
        self.trigger = FakeControl(text="Add file")

    def locator(self, selector: str) -> FakeLocator:
        if 'input[type="file"]' in selector:
            return FakeLocator()
        if '[role="button"]' in selector:
            return FakeLocator([self.trigger])
        if selector == "body":
            return FakeLocator(body=self.text)
        return FakeLocator()


class GooglePickerPage(FakePage):
    def __init__(self) -> None:
        self.question = GooglePickerQuestion()
        self.file_input = GooglePickerFileInput(
            attributes={"accept": "application/pdf"}
        )
        self.picker = GooglePickerFrame(self.file_input)
        self.frame_node = GooglePickerFrameNode(self.picker)
        super().__init__(
            _google_task("scan", None).target_url,
            _fields(),
            upload_prompt_containers=[self.question],
            submit_count=1,
        )

        def open_picker() -> None:
            self.frame_node.visible = True

        def show_selected_file(value: str) -> None:
            self.picker.body = Path(value).name

        def finish_upload() -> None:
            path = Path(self.file_input.files or "resume.pdf")
            self.picker.body = path.name
            self.question.text += f"\n{path.name}"
            self.frame_node.visible = False

        self.question.trigger.on_click = open_picker
        self.file_input.on_set_files = show_selected_file
        self.picker.action.on_click = finish_upload

    def locator(self, selector: str) -> Any:
        if "iframe.picker-frame" in selector:
            return FakeLocator([self.frame_node])
        return super().locator(selector)


def _google_picker_schema(page: GooglePickerPage) -> Any:
    return bind_schema_to_target(
        asyncio.run(
            scan_form(FakeFormRoot(page), provider="google_forms")
        ),
        canonical_form_target("google_forms", page.url),
    )


def test_google_picker_scan_models_one_resume_question_without_opening_it() -> None:
    page = GooglePickerPage()

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            _google_task("scan", None, context_id="tenant-google-context"),
            None,
        )
    )

    assert result.code == "application_form_scanned"
    assert result.schema is not None
    assert result.schema.public_fields[-1]["upload_mode"] == "google_picker"
    assert result.schema.public_fields[-1]["accepts_resume"] is True
    assert page.question.trigger.clicks == 0
    assert page.submit_controls[0].clicks == 0


def test_google_picker_upload_uses_one_saved_context_and_verifies_exact_pdf(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "candidate-resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nprivate test resume")
    page = GooglePickerPage()
    schema = _google_picker_schema(page)
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
        context_id="tenant-google-context",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            str(resume),
        )
    )

    assert result.code == "review_required"
    assert result.filled_count == 2
    assert schema.public_fields[-1]["upload_mode"] == "google_picker"
    assert page.file_input.files == str(resume)
    assert page.question.trigger.clicks == 1
    assert page.picker.action.clicks == 1
    assert page.submit_controls[0].clicks == 0


def test_google_picker_resume_never_opens_without_saved_context(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nprivate test resume")
    page = GooglePickerPage()
    schema = _google_picker_schema(page)
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
        context_id=None,
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            str(resume),
        )
    )

    assert result.code == "provider_login_required"
    assert result.submission_state == "not_attempted"
    assert page.question.trigger.clicks == 0
    assert page.submit_controls[0].clicks == 0


def test_google_picker_rejects_untrusted_iframe_before_attaching_or_submitting(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nprivate test resume")
    page = GooglePickerPage()
    page.frame_node.attributes["src"] = "https://attacker.example/picker"
    schema = _google_picker_schema(page)
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
        context_id="tenant-google-context",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            str(resume),
        )
    )

    assert result.code == "required_answers_missing"
    assert result.submission_state == "not_attempted"
    assert page.file_input.files is None
    assert page.submit_controls[0].clicks == 0


def test_google_picker_requires_provider_visible_filename_before_upload_action(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nprivate test resume")
    page = GooglePickerPage()
    page.file_input.on_set_files = None
    schema = _google_picker_schema(page)
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
        context_id="tenant-google-context",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            str(resume),
        )
    )

    assert result.code == "required_answers_missing"
    assert result.submission_state == "not_attempted"
    assert page.file_input.files == str(resume)
    assert page.picker.action.clicks == 0
    assert page.submit_controls[0].clicks == 0


def test_google_picker_rejects_pdf_when_real_picker_excludes_it(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nprivate test resume")
    page = GooglePickerPage()
    page.file_input.attributes["accept"] = "image/png,image/jpeg"
    schema = _google_picker_schema(page)
    task = _google_task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
        context_id="tenant-google-context",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            task,
            str(resume),
        )
    )

    assert result.code == "required_answers_missing"
    assert result.submission_state == "not_attempted"
    assert page.file_input.files is None
    assert page.picker.action.clicks == 0
    assert page.submit_controls[0].clicks == 0


def test_google_picker_multiple_resume_questions_fail_before_opening_any_picker() -> None:
    page = GooglePickerPage()
    second = GooglePickerQuestion()
    second.text = "Attach another Resume *\nAdd file"
    page.upload_prompt_containers.append(second)

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            _google_task("scan", None, context_id="tenant-google-context"),
            None,
        )
    )

    assert result.code == "resume_upload_ambiguous"
    assert result.submission_state == "not_attempted"
    assert page.question.trigger.clicks == 0
    assert second.trigger.clicks == 0
    assert page.submit_controls[0].clicks == 0


def test_uninspectable_upload_widget_fails_closed_before_fill_or_submit() -> None:
    class BrokenUploadWidget(FakeControl):
        def locator(self, _selector: str) -> FakeLocator:
            raise RuntimeError("detached upload widget")

    widget = BrokenUploadWidget(text="Upload Resume Add file")
    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        body="Upload Resume Add file",
        submit_count=1,
        upload_prompt_containers=[widget],
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("google_forms"),  # type: ignore[arg-type]
            _google_task("scan", None),
            None,
        )
    )

    assert result.code == "file_upload_inspection_failed"
    assert page.controls[0].filled is None
    assert page.submit_controls[0].clicks == 0


def test_unverified_resume_attachment_blocks_even_an_optional_resume_field() -> None:
    class UnverifiedUploadControl(FakeControl):
        async def evaluate(self, _script: str) -> dict[str, Any]:
            return {"count": 0}

    fields = _fields() + [
        {
            "dom_index": 2,
            "key": "resume_file",
            "label": "Optional Resume",
            "kind": "file",
            "required": False,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": ".pdf",
        }
    ]
    controls = [FakeControl(), FakeControl(), UnverifiedUploadControl()]
    page = FakePage(
        _task("scan", None).target_url,
        fields,
        controls=controls,
        submit_count=1,
    )
    schema = _runtime_schema(page)
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            task,
            "/tmp/tenant-resume.pdf",
        )
    )

    assert result.code == "required_answers_missing"
    assert result.missing_required == ("Optional Resume",)
    assert page.submit_controls[0].clicks == 0


def test_grouped_checkbox_answer_must_match_at_least_one_captured_option() -> None:
    fields = _fields()[:1] + [
        {
            "dom_index": index,
            "key": "skills",
            "label": "Choose skills",
            "kind": "checkbox",
            "required": True,
            "disabled": False,
            "answered": False,
            "options": [option],
            "option_label": option,
            "accept": "",
        }
        for index, option in enumerate(("Python", "SQL"), start=1)
    ]
    page = FakePage(_task("scan", None).target_url, fields)
    schema = _runtime_schema(page)
    assert schema.public_fields[1:] == [
        {
            "key": "skills",
            "label": "Choose skills",
            "kind": "checkbox",
            "type": "multiselect",
            "required": True,
            "disabled": False,
            "prefilled": False,
            "options": ["Python", "SQL"],
            "accepts_resume": False,
        }
    ]
    task = _task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com", "skills": ["Rust"]},
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "required_answers_missing"
    assert result.missing_required == ("Choose skills",)


def test_provider_prefilled_value_must_be_overwritten_by_an_exact_approved_answer() -> None:
    fields = _fields()
    fields[1]["answered"] = True
    page = FakePage(_task("scan", None).target_url, fields, submit_count=1)
    schema = _runtime_schema(page)
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "required_answers_missing"
    assert result.missing_required == ("Optional note",)
    assert page.submit_controls[0].clicks == 0

    approved_page = FakePage(_task("scan", None).target_url, fields)
    approved_schema = _runtime_schema(approved_page)
    approved_task = _task(
        "prefill",
        approved_schema.schema_hash,
        answers={
            "Email address": "candidate@example.com",
            "Optional note": "Reviewed replacement",
        },
    )
    approved_result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            approved_page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            approved_task,
            None,
        )
    )
    assert approved_result.code == "review_required"
    assert approved_page.controls[1].filled == "Reviewed replacement"


def test_changed_schema_blocks_all_filling_and_submission() -> None:
    page = FakePage(_task("scan", None).target_url, _fields(), submit_count=1)
    task = _task(
        "submit",
        "a" * 64,
        answers={"Email address": "candidate@example.com"},
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    result = asyncio.run(runtime._run_page(page, get_adapter("greenhouse"), task, None))  # type: ignore[arg-type]

    assert result.code == "form_schema_changed"
    assert page.controls[0].filled is None
    assert page.submit_controls[0].clicks == 0


def test_listing_search_form_is_never_treated_as_an_application_or_submitted() -> None:
    fields = [
        {
            "dom_index": 0,
            "key": "query",
            "label": "Search jobs",
            "kind": "text",
            "required": False,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "",
        }
    ]
    page = FakePage(
        _task("scan", None).target_url,
        fields,
        body="Search jobs by keyword and location",
        submit_count=1,
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    result = asyncio.run(
        runtime._run_page(
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task("scan", None),
            None,
        )
    )

    assert result.code == "application_form_not_found"
    assert page.submit_controls[0].clicks == 0


def test_checkpoint_never_enters_fill_or_submit() -> None:
    page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        body="Complete the CAPTCHA to continue",
        checkpoint_count=1,
        submit_count=1,
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    result = asyncio.run(
        runtime._run_page(page, get_adapter("greenhouse"), _task("scan", None), None)  # type: ignore[arg-type]
    )

    assert result.code == "security_checkpoint"
    assert all(control.filled is None for control in page.controls)
    assert page.submit_controls[0].clicks == 0


def test_passive_captcha_allows_scan_but_blocks_submit() -> None:
    scan_page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        checkpoint_count=1,
        submit_count=1,
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    scan_result = asyncio.run(
        runtime._run_page(
            scan_page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task("scan", None),
            None,
        )
    )
    assert scan_result.code == "application_form_scanned"

    prefill_page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        checkpoint_count=1,
        submit_count=1,
    )
    prefill_schema = _runtime_schema(prefill_page)
    prefill_result = asyncio.run(
        runtime._run_page(
            prefill_page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task(
                "prefill",
                prefill_schema.schema_hash,
                answers={"Email address": "candidate@example.com"},
            ),
            None,
        )
    )
    assert prefill_result.code == "review_required"
    assert prefill_page.controls[0].filled == "candidate@example.com"
    assert prefill_page.submit_controls[0].clicks == 0

    submit_page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        checkpoint_count=1,
        submit_count=1,
    )
    schema = _runtime_schema(submit_page)
    submit_result = asyncio.run(
        runtime._run_page(
            submit_page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            _task(
                "submit",
                schema.schema_hash,
                answers={"Email address": "candidate@example.com"},
            ),
            None,
        )
    )
    assert submit_result.code == "security_checkpoint"
    assert all(control.filled is None for control in submit_page.controls)
    assert submit_page.submit_controls[0].clicks == 0


def test_google_forms_login_redirect_is_needs_attention_not_cross_host_navigation() -> None:
    target = "https://docs.google.com/forms/d/e/abc/viewform"
    page = FakePage(
        target,
        _fields(),
        redirect_url="https://accounts.google.com/ServiceLogin?continue=secret",
    )
    task = ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="google_forms",
        phase="scan",
        target_url=target,
        context_id=None,
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    result = asyncio.run(
        runtime._run_page(page, get_adapter("google_forms"), task, None)  # type: ignore[arg-type]
    )

    assert result.code == "provider_login_required"
    assert "accounts.google.com" not in result.form_url


@pytest.mark.parametrize(
    ("confirmed_url", "confirmed_body", "expected_code", "expected_state"),
    [
        (
            "https://job-boards.greenhouse.io/acme/jobs/1/confirmation",
            "Thank you for applying",
            "application_submitted",
            "confirmed",
        ),
        (
            "https://job-boards.greenhouse.io/acme/jobs/1",
            "Application form",
            "submission_unconfirmed",
            "uncertain",
        ),
    ],
)
def test_submit_result_requires_clear_provider_confirmation(
    confirmed_url: str,
    confirmed_body: str,
    expected_code: str,
    expected_state: str,
) -> None:
    page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        submit_count=1,
        confirmed_url=confirmed_url,
        confirmed_body=confirmed_body,
    )
    schema = _runtime_schema(page)
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    result = asyncio.run(runtime._run_page(page, get_adapter("greenhouse"), task, None))  # type: ignore[arg-type]

    assert result.code == expected_code
    assert result.submission_state == expected_state
    assert page.submit_controls[0].clicks == 1


def test_submit_refuses_final_click_when_lease_check_fails() -> None:
    page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        submit_count=1,
    )
    schema = _runtime_schema(page)
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    async def lost_lease() -> bool:
        return False

    with pytest.raises(ManagedBrowserError) as error:
        asyncio.run(
            runtime._run_page(
                page,
                get_adapter("greenhouse"),  # type: ignore[arg-type]
                task,
                None,
                lost_lease,
            )
        )

    assert error.value.code == "automation_lease_lost"
    assert page.submit_controls[0].clicks == 0


def test_submit_click_error_is_ambiguous_and_never_raises_into_retry_path() -> None:
    page = FakePage(
        _task("scan", None).target_url,
        _fields(),
        submit_count=1,
    )
    page.submit_controls[0].click_error = TimeoutError("provider response hidden")
    schema = _runtime_schema(page)
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("greenhouse"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.status == "needs_attention"
    assert result.code == "submission_click_unconfirmed"
    assert result.submission_state == "uncertain"
    assert page.submit_controls[0].clicks == 1


class FakeRoute:
    def __init__(self, url: str, page: FakePage) -> None:
        self.request = SimpleNamespace(
            url=url,
            frame=page.main_frame,
            is_navigation_request=lambda: True,
        )
        self.aborted = False
        self.continued = False

    async def abort(self, _reason: str) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class TargetClosedError(RuntimeError):
    pass


class ClosingRoute(FakeRoute):
    async def continue_(self) -> None:
        raise TargetClosedError("Target page, context or browser has been closed")


class BrokenRoute(FakeRoute):
    async def continue_(self) -> None:
        raise RuntimeError("routing backend failed")


def test_navigation_guard_aborts_cross_host_redirects() -> None:
    page = FakePage(_task("scan", None).target_url, _fields())
    adapter = get_adapter("greenhouse")
    assert adapter is not None
    asyncio.run(BrowserRuntime._install_navigation_guard(page, adapter))

    blocked = FakeRoute("https://attacker.example/collect", page)
    allowed = FakeRoute("https://job-boards.greenhouse.io/acme/jobs/1", page)
    asyncio.run(page.route_handler(blocked))
    asyncio.run(page.route_handler(allowed))

    assert blocked.aborted and not blocked.continued
    assert allowed.continued and not allowed.aborted


def test_navigation_guard_ignores_page_close_race() -> None:
    page = FakePage(_task("scan", None).target_url, _fields())
    adapter = get_adapter("greenhouse")
    assert adapter is not None
    asyncio.run(BrowserRuntime._install_navigation_guard(page, adapter))

    closing = ClosingRoute("https://job-boards.greenhouse.io/acme/jobs/1", page)
    asyncio.run(page.route_handler(closing))


def test_navigation_guard_propagates_unrelated_route_failures() -> None:
    page = FakePage(_task("scan", None).target_url, _fields())
    adapter = get_adapter("greenhouse")
    assert adapter is not None
    asyncio.run(BrowserRuntime._install_navigation_guard(page, adapter))

    broken = BrokenRoute("https://job-boards.greenhouse.io/acme/jobs/1", page)
    with pytest.raises(RuntimeError, match="routing backend failed"):
        asyncio.run(page.route_handler(broken))


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.contexts = [SimpleNamespace(pages=[page])]
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.connections: list[tuple[str, int]] = []

    async def connect_over_cdp(self, url: str, *, timeout: int) -> FakeBrowser:
        self.connections.append((url, timeout))
        return self.browser


class FakePlaywrightManager:
    def __init__(self, chromium: FakeChromium) -> None:
        self.playwright = SimpleNamespace(chromium=chromium)

    async def __aenter__(self) -> Any:
        return self.playwright

    async def __aexit__(self, *_args: Any) -> None:
        return None


class FakeBrowserbase:
    def __init__(self) -> None:
        self.created: list[tuple[str, bool]] = []
        self.ephemeral: list[bool] = []
        self.timeouts: list[int | None] = []
        self.released: list[str] = []

    def create_session_for_worker(
        self,
        context_id: str,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        **_kwargs: Any,
    ) -> TrustedBrowserSession:
        self.created.append((context_id, keep_alive))
        self.timeouts.append(timeout_seconds)
        return TrustedBrowserSession(
            id="session-one",
            context_id=context_id,
            connect_url="wss://connect.browserbase.com?sessionId=one&token=secret",
        )

    def create_ephemeral_session_for_worker(
        self,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        **_kwargs: Any,
    ) -> TrustedBrowserSession:
        self.ephemeral.append(keep_alive)
        self.timeouts.append(timeout_seconds)
        return TrustedBrowserSession(
            id="session-one",
            context_id=None,
            connect_url="wss://connect.browserbase.com?sessionId=one&token=secret",
        )

    def release_session(self, session_id: str) -> dict[str, Any]:
        self.released.append(session_id)
        return {"id": session_id, "released": True}

    def get_session_live_view(self, session_id: str) -> dict[str, str]:
        return {
            "session_id": session_id,
            "live_view_url": "https://www.browserbase.com/sessions/session-one",
        }


def test_runtime_connects_playwright_over_trusted_cdp_and_releases_scan_session() -> None:
    page = FakePage(_task("scan", None).target_url, _fields())
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    browserbase = FakeBrowserbase()
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(chromium),
    )

    execution = asyncio.run(runtime.execute(_task("scan", None), resume_path=None))

    assert execution.result.code == "application_form_scanned"
    assert chromium.connections == [
        ("wss://connect.browserbase.com?sessionId=one&token=secret", 30_000)
    ]
    assert browserbase.created == [("context-1", False)]
    assert browserbase.timeouts == [90]
    assert browserbase.released == ["session-one"]
    assert browser.closed is True
    assert page.unroute_behaviors == ["ignoreErrors"]


def test_yc_runtime_always_reuses_the_tenant_persistent_browser_context() -> None:
    page = CurrentYcJobPage()
    task = _provider_task(
        "yc", page.target, "scan", None, context_id="tenant-yc-context"
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(
            FakeChromium(browser)
        ),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.code == "application_form_scanned"
    assert browserbase.created == [("tenant-yc-context", False)]
    assert browserbase.ephemeral == []
    assert browserbase.timeouts == [90]
    assert browserbase.released == ["session-one"]


def test_public_form_runtime_uses_ephemeral_browserbase_session() -> None:
    page = FakePage(_task("scan", None).target_url, _fields())
    browserbase = FakeBrowserbase()
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(
            FakeChromium(FakeBrowser(page))
        ),
    )
    task = ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="greenhouse",
        phase="scan",
        target_url="https://job-boards.greenhouse.io/acme/jobs/1",
        context_id=None,
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.code == "application_form_scanned"
    assert browserbase.ephemeral == [False]
    assert browserbase.timeouts == [90]
    assert browserbase.created == []


def test_prefill_keeps_bounded_live_review_session_without_exposing_cdp_url() -> None:
    page = FakePage(_task("scan", None).target_url, _fields())
    schema = _runtime_schema(page)
    task = _task(
        "prefill",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(
            FakeChromium(browser)
        ),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    details = execution.details()
    assert execution.result.code == "review_required"
    assert details["review_session_id"] == "session-one"
    assert details["live_view_url"].startswith("https://")
    assert "connect.browserbase.com" not in repr(details)
    assert browserbase.created == [("context-1", True)]
    assert browserbase.timeouts == [90]
    assert browserbase.released == []
    assert browser.closed is False


def test_google_forms_prefill_keeps_ephemeral_live_view_for_review() -> None:
    target_url = "https://docs.google.com/forms/d/e/example/viewform"
    page = FakePage(target_url, _fields(), submit_count=1)
    schema = _runtime_schema(page, "google_forms")
    task = ResolvedBrowserTask(
        job_id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="google_forms",
        phase="prefill",
        target_url=target_url,
        context_id=None,
        approval=ApprovalSnapshot(
            id=REVISION_ID,
            revision=1,
            schema_hash=schema.schema_hash,
            answers={"Email address": "candidate@example.com"},
        ),
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(
            FakeChromium(browser)
        ),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.code == "review_required"
    assert execution.result.submission_state == "not_attempted"
    assert execution.details()["live_view_url"].startswith("https://")
    assert browserbase.ephemeral == [True]
    assert browserbase.released == []
    assert browser.closed is False
    assert page.submit_controls[0].clicks == 0


@pytest.mark.parametrize(
    ("confirmed_url", "confirmed_body"),
    [
        (
            "https://docs.google.com/forms/d/e/example/viewform",
            "Your response has been recorded.",
        ),
        (
            "https://docs.google.com/forms/d/e/example/formResponse",
            "Application form",
        ),
    ],
)
def test_google_forms_submit_clicks_once_and_requires_google_confirmation(
    confirmed_url: str,
    confirmed_body: str,
) -> None:
    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        submit_count=1,
        confirmed_url=confirmed_url,
        confirmed_body=confirmed_body,
    )
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.status == "succeeded"
    assert execution.result.code == "application_submitted"
    assert execution.result.submission_state == "confirmed"
    assert page.submit_controls[0].clicks == 1
    assert browserbase.ephemeral == [True]
    assert browserbase.released == ["session-one"]
    assert browser.closed is True
    assert "live_view_url" not in execution.details()


def test_google_forms_missing_required_fields_retains_actionable_live_view() -> None:
    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        submit_count=1,
    )
    schema = _runtime_schema(page, "google_forms")
    task = _google_task("submit", schema.schema_hash, answers={})
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))
    details = execution.details()

    assert execution.result.code == "required_answers_missing"
    assert execution.result.submission_state == "not_attempted"
    assert details["missing_fields"] == ["Email address"]
    assert details["live_view_url"].startswith("https://")
    assert page.submit_controls[0].clicks == 0
    assert browserbase.released == []
    assert browser.closed is False


def test_google_forms_unconfirmed_click_is_terminal_and_keeps_live_view() -> None:
    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        submit_count=1,
    )
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.status == "needs_attention"
    assert execution.result.code == "submission_unconfirmed"
    assert execution.result.submission_state == "uncertain"
    assert execution.details()["live_view_url"].startswith("https://")
    assert page.submit_controls[0].clicks == 1
    assert browserbase.released == []
    assert browser.closed is False


def test_google_forms_visible_required_error_is_safe_to_prepare_again() -> None:
    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        submit_count=1,
        confirmed_body="Application form\nThis is a required question",
    )
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.status == "needs_attention"
    assert execution.result.code == "provider_validation_failed"
    assert execution.result.submission_state == "not_attempted"
    assert page.submit_controls[0].clicks == 1
    assert execution.details()["live_view_url"].startswith("https://")
    assert browserbase.released == []
    assert browser.closed is False


def test_google_forms_live_view_failure_never_retries_an_uncertain_click() -> None:
    class FailingLiveViewBrowserbase(FakeBrowserbase):
        def get_session_live_view(self, _session_id: str) -> dict[str, str]:
            raise RuntimeError("live view temporarily unavailable")

    page = FakePage(
        _google_task("scan", None).target_url,
        _fields(),
        submit_count=1,
    )
    page.submit_controls[0].click_error = TimeoutError("response hidden")
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    browserbase = FailingLiveViewBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.code == "submission_click_unconfirmed"
    assert execution.result.submission_state == "uncertain"
    assert page.submit_controls[0].clicks == 1
    assert "live_view_url" not in execution.details()
    assert browserbase.released == ["session-one"]
    assert browser.closed is True


@pytest.mark.parametrize(
    "target_url",
    [
        "https://127.0.0.1/private-application",
        "https://www.ycombinator.com/jobs",
        "https://account.ycombinator.com/authenticate",
        "https://www.workatastartup.com/jobs/12345",
        "https://tenant.workatastartup.com/application",
    ],
)
def test_invalid_custom_company_target_is_rejected_before_a_metered_session(
    target_url: str,
) -> None:
    task = _provider_task(
        "company_form",
        target_url,
        "scan",
        None,
    )

    execution = asyncio.run(
        BrowserRuntime(UnusedBrowserbase()).execute(task, resume_path=None)  # type: ignore[arg-type]
    )

    assert execution.result.status == "needs_attention"
    assert execution.result.code == "provider_url_forbidden"
    assert execution.result.submission_state == "not_attempted"


@pytest.mark.parametrize(
    ("confirmed_body", "expected_code", "expected_state", "retained"),
    [
        ("Thank you for applying", "application_submitted", "confirmed", False),
        ("Application form", "submission_unconfirmed", "uncertain", True),
    ],
)
def test_custom_company_submit_is_exact_host_single_click_and_confirmation_bound(
    confirmed_body: str,
    expected_code: str,
    expected_state: str,
    retained: bool,
) -> None:
    target_url = "https://careers.acme.com/jobs/one/apply"
    page = FakePage(
        target_url,
        _fields(),
        submit_count=1,
        confirmed_body=confirmed_body,
    )
    schema = _runtime_schema(page, "company_form")
    task = _provider_task(
        "company_form",
        target_url,
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.code == expected_code
    assert execution.result.submission_state == expected_state
    assert page.controls[0].filled == "candidate@example.com"
    assert page.submit_controls[0].clicks == 1
    assert browserbase.ephemeral == [True]
    assert bool(execution.live_view_url) is retained
    assert (browserbase.released == []) is retained
    assert browser.closed is (not retained)


def test_custom_company_missing_required_field_pauses_before_click_with_live_view() -> None:
    target_url = "https://careers.acme.com/jobs/one/apply"
    page = FakePage(target_url, _fields(), submit_count=1)
    schema = _runtime_schema(page, "company_form")
    task = _provider_task(
        "company_form",
        target_url,
        "submit",
        schema.schema_hash,
        answers={},
    )
    browserbase = FakeBrowserbase()
    browser = FakeBrowser(page)
    runtime = BrowserRuntime(
        browserbase,
        playwright_factory=lambda: FakePlaywrightManager(FakeChromium(browser)),
    )

    execution = asyncio.run(runtime.execute(task, resume_path=None))

    assert execution.result.code == "required_answers_missing"
    assert execution.details()["missing_fields"] == ["Email address"]
    assert execution.details()["live_view_url"].startswith("https://")
    assert page.submit_controls[0].clicks == 0
    assert browserbase.released == []
    assert browser.closed is False


@pytest.mark.parametrize(
    ("confirmed_body", "expected_code", "expected_state"),
    [
        ("Your application was sent", "application_submitted", "confirmed"),
        ("Message sent", "submission_unconfirmed", "uncertain"),
        ("Application form", "submission_unconfirmed", "uncertain"),
    ],
)
def test_yc_dialog_submit_uses_exact_approved_answers_and_one_final_click(
    confirmed_body: str,
    expected_code: str,
    expected_state: str,
) -> None:
    page = CurrentYcJobPage(
        _yc_fields(),
        submit_count=1,
        confirmed_body=confirmed_body,
    )
    schema = bind_schema_to_target(
        asyncio.run(scan_form(page)), page.application
    )
    task = _provider_task(
        "yc",
        page.target,
        "submit",
        schema.schema_hash,
        answers={"Message to the founders": "I build reliable AI systems."},
        context_id="tenant-yc-context",
    )
    adapter = get_adapter("yc")

    assert adapter is not None
    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            adapter,
            task,
            None,
        )
    )

    assert result.code == expected_code
    assert result.submission_state == expected_state
    assert page.controls[0].filled == "I build reliable AI systems."
    assert page.submit_controls[0].clicks == 1


def test_current_yc_job_follows_only_the_job_bound_account_handoff_and_scans() -> None:
    page = CurrentYcJobPage()
    task = _provider_task("yc", page.target, "scan", None, context_id="tenant-yc")
    adapter = get_adapter("yc")

    assert adapter is not None
    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(page, adapter, task, None)
    )

    assert result.code == "application_form_scanned"
    assert result.form_url == page.application
    assert result.schema is not None
    assert result.schema.public_fields == [
        {
            "key": "application_message",
            "label": "Message to the founders",
            "kind": "textarea",
            "type": "textarea",
            "required": True,
            "disabled": False,
            "prefilled": False,
            "options": [],
            "accepts_resume": False,
        }
    ]
    assert page.visited == [page.target, page.handoff]
    assert page.entry_control.clicks == 0


@pytest.mark.parametrize(
    "target_url",
    [
        "https://www.ycombinator.com/jobs",
        "https://www.ycombinator.com/companies/circleback/jobs",
        "https://account.ycombinator.com/authenticate?continue="
        "https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D91924",
        "https://www.workatastartup.com/application?signup_job_id=91924",
        "https://www.workatastartup.com/jobs/12345",
        "https://workatastartup.com/jobs/12345/",
        "https://www.workatastartup.com/companies/acme/jobs/12345-engineer",
        "https://workatastartup.com/companies/acme/jobs/12345-engineer/",
        "https://www.ycombinator.com/companies/circleback/jobs/"
        "dUH7zjN-software-engineer?utm_source=feed",
    ],
)
def test_yc_worker_rejects_every_non_exact_initial_target_before_navigation(
    target_url: str,
) -> None:
    page = FakePage(target_url, _yc_fields(), submit_count=1)
    task = _provider_task("yc", target_url, "scan", None, context_id="tenant-yc")

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("yc"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "provider_url_forbidden"
    assert page.submit_controls[0].clicks == 0


def test_yc_unknown_visible_fields_fail_closed_before_review_or_submit() -> None:
    fields = _yc_fields() + [
        {
            "dom_index": 1,
            "key": "unexpected_answer",
            "label": "Unexpected screening question",
            "kind": "text",
            "required": False,
            "disabled": False,
            "answered": False,
            "options": [],
            "option_label": "",
            "accept": "",
        }
    ]
    page = CurrentYcJobPage(fields, submit_count=1)
    task = _provider_task(
        "yc", page.target, "scan", None, context_id="tenant-yc-context"
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("yc"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "unsupported_application_fields"
    assert page.submit_controls[0].clicks == 0


def test_yc_schema_change_invalidates_the_sealed_revision_before_fill() -> None:
    original_page = CurrentYcJobPage()
    original_schema = bind_schema_to_target(
        asyncio.run(scan_form(original_page)), original_page.application
    )
    changed = _yc_fields()
    changed[0]["label"] = "Updated message to the hiring team"
    page = CurrentYcJobPage(changed, confirmed_body="Application submitted")
    task = _provider_task(
        "yc",
        page.target,
        "submit",
        original_schema.schema_hash,
        answers={"Message to the founders": "A reviewed message."},
        context_id="tenant-yc-context",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("yc"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "form_schema_changed"
    assert page.controls[0].filled is None
    assert page.submit_controls[0].clicks == 0


def test_yc_submit_requires_one_sealed_approval_before_fill_or_click() -> None:
    page = CurrentYcJobPage(
        _yc_fields(),
        submit_count=1,
        confirmed_body="Application submitted",
    )
    task = _provider_task(
        "yc",
        page.target,
        "submit",
        None,
        context_id="tenant-yc-context",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("yc"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code == "form_approval_required"
    assert page.controls[0].filled is None
    assert page.submit_controls[0].clicks == 0


def test_yc_login_and_security_checkpoint_never_fill_or_submit() -> None:
    target = CurrentYcJobPage.target
    login_page = FakePage(
        target,
        _yc_fields(),
        redirect_url=(
            "https://account.ycombinator.com/authenticate?continue="
            "https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D12345"
        ),
        submit_count=1,
    )
    checkpoint_page = FakePage(
        target,
        _yc_fields(),
        checkpoint_count=1,
        submit_count=1,
    )
    runtime = BrowserRuntime(UnusedBrowserbase())  # type: ignore[arg-type]

    login_result = asyncio.run(
        runtime._run_page(
            login_page,
            get_adapter("yc"),  # type: ignore[arg-type]
            _provider_task("yc", target, "scan", None, context_id="tenant-yc"),
            None,
        )
    )
    checkpoint_result = asyncio.run(
        runtime._run_page(
            checkpoint_page,
            get_adapter("yc"),  # type: ignore[arg-type]
            _provider_task("yc", target, "submit", "0" * 64, context_id="tenant-yc"),
            None,
        )
    )

    assert login_result.code == "provider_login_required"
    assert checkpoint_result.code == "security_checkpoint"
    assert login_page.controls[0].filled is None
    assert checkpoint_page.controls[0].filled is None
    assert login_page.submit_controls[0].clicks == 0
    assert checkpoint_page.submit_controls[0].clicks == 0


def test_yc_ambiguous_final_action_fails_closed_without_clicking() -> None:
    page = CurrentYcJobPage(_yc_fields(), submit_count=2)
    schema = bind_schema_to_target(
        asyncio.run(scan_form(page)), page.application
    )
    task = _provider_task(
        "yc",
        page.target,
        "submit",
        schema.schema_hash,
        answers={"Message to the founders": "A reviewed message."},
        context_id="tenant-yc",
    )

    result = asyncio.run(
        BrowserRuntime(UnusedBrowserbase())._run_page(  # type: ignore[arg-type]
            page,
            get_adapter("yc"),  # type: ignore[arg-type]
            task,
            None,
        )
    )

    assert result.code in {"application_form_not_found", "application_form_ambiguous"}
    assert [control.clicks for control in page.submit_controls] == [0, 0]


class FakeTenantRepository:
    def __init__(
        self,
        bundle: dict[str, Any],
        *,
        responses: dict[str, Any] | None = None,
    ) -> None:
        self.bundle = bundle
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[tuple[str, str]] = []

    async def rpc(self, name: str, params: dict[str, Any]) -> Any:
        self.calls.append((name, params))
        if name == "get_application_job_bundle":
            return [self.bundle]
        if name == "get_application_job_browser_context_binding":
            ciphertext = self.bundle.get("browser_context_id_ciphertext")
            return {
                "browser_context_id_ciphertext": ciphertext,
                "credential_source": "platform" if ciphertext else None,
                "credential_generation": None,
                "credential_epoch": 0 if ciphertext else None,
                "project_fingerprint": (
                    _browserbase_fingerprint("platform-project")
                    if ciphertext
                    else None
                ),
            }
        return self.responses.get(name, True)

    async def download_object(self, bucket: str, path: str) -> bytes:
        self.downloads.append((bucket, path))
        return b"%PDF-1.7\nworker-test"


def _bundle(cipher: TokenCipher) -> dict[str, Any]:
    return {
        "user_id": USER_ID,
        "application_id": APPLICATION_ID,
        "provider": "greenhouse",
        "target_url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "browser_context_id_ciphertext": cipher.encrypt("context-one"),
        "resume": {
            "storage_path": f"{USER_ID}/resume-1.pdf",
            "size_bytes": 20,
            "mime_type": "application/pdf",
        },
    }


def test_public_ats_can_resolve_to_an_ephemeral_session_without_saved_context() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    bundle = _bundle(cipher)
    bundle["browser_context_id_ciphertext"] = None
    repository = FakeTenantRepository(bundle)
    resources = SupabaseTenantResources(repository, cipher)
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="greenhouse",
        kind="application_scan",
    )

    task = asyncio.run(resources.resolve(job, "worker-1"))

    assert task.context_id is None


@pytest.mark.parametrize(
    ("ciphertext", "expected_context"),
    [(None, None), ("saved", "google-context")],
)
def test_google_forms_context_is_optional_but_reused_when_present(
    ciphertext: str | None,
    expected_context: str | None,
) -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    bundle = _bundle(cipher)
    bundle.update(
        {
            "provider": "google_forms",
            "target_url": "https://docs.google.com/forms/d/e/example/viewform",
            "browser_context_id_ciphertext": (
                cipher.encrypt("google-context") if ciphertext else None
            ),
        }
    )
    resources = SupabaseTenantResources(FakeTenantRepository(bundle), cipher)
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="google_forms",
        kind="application_scan",
    )

    task = asyncio.run(resources.resolve(job, "worker-1"))

    assert task.context_id == expected_context


def test_custom_company_form_resolves_without_a_saved_login_context() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    bundle = _bundle(cipher)
    bundle.update(
        {
            "provider": "company_form",
            "target_url": "https://careers.acme.com/jobs/one/apply",
            "browser_context_id_ciphertext": None,
        }
    )
    resources = SupabaseTenantResources(FakeTenantRepository(bundle), cipher)
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="company_form",
        kind="application_scan",
    )

    task = asyncio.run(resources.resolve(job, "worker-1"))

    assert task.context_id is None


@pytest.mark.parametrize(
    ("provider", "target_url"),
    [
        ("wellfound", "https://wellfound.com/jobs/one"),
        (
            "yc",
            "https://www.ycombinator.com/companies/circleback/jobs/dUH7zjN-software-engineer",
        ),
    ],
)
def test_account_provider_still_requires_a_saved_tenant_context(
    provider: str,
    target_url: str,
) -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    bundle = _bundle(cipher)
    bundle.update(
        {
            "provider": provider,
            "target_url": target_url,
            "browser_context_id_ciphertext": None,
        }
    )
    repository = FakeTenantRepository(bundle)
    resources = SupabaseTenantResources(repository, cipher)
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider=provider,
        kind="application_scan",
    )

    with pytest.raises(ManagedBrowserError) as error:
        asyncio.run(resources.resolve(job, "worker-1"))

    assert error.value.code == "provider_connection_required"


def test_tenant_resource_bundle_is_lease_bound_and_resume_is_ephemeral() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    repository = FakeTenantRepository(_bundle(cipher))
    resources = SupabaseTenantResources(repository, cipher)
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="greenhouse",
        kind="application_scan",
    )

    async def exercise() -> None:
        task = await resources.resolve(job, "worker-1")
        assert task.context_id == "context-one"
        async with resources.materialize_resume(task) as resume_path:
            assert resume_path is not None
            assert Path(resume_path).read_bytes().startswith(b"%PDF-")
            temporary = Path(resume_path)
        assert not temporary.exists()

    asyncio.run(exercise())

    assert repository.calls[0] == (
        "get_application_job_bundle",
        {"job_id": JOB_ID, "worker_id": "worker-1"},
    )
    assert repository.downloads == [("resumes", f"{USER_ID}/resume-1.pdf")]


def test_cross_tenant_bundle_is_rejected_before_resume_download() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    bundle = _bundle(cipher)
    bundle["user_id"] = "00000000-0000-0000-0000-000000000099"
    repository = FakeTenantRepository(bundle)
    resources = SupabaseTenantResources(repository, cipher)
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="greenhouse",
        kind="application_scan",
    )

    with pytest.raises(ManagedBrowserError) as error:
        asyncio.run(resources.resolve(job, "worker-1"))

    assert error.value.code == "application_bundle_mismatch"
    assert repository.downloads == []


def test_worker_prefers_lease_bound_tenant_browserbase_credential(
    monkeypatch: Any,
) -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    repository = FakeTenantRepository(
        _bundle(cipher),
        responses={
            "get_application_job_browserbase_credential": {
                "user_id": USER_ID,
                "credential_ciphertext": cipher.encrypt(
                    json.dumps(
                        {
                            "version": 1,
                            "provider": "browserbase",
                            "api_key": "tenant-browserbase-key",
                            "project_id": "tenant-project",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                ),
                "verification_status": "verified",
                "verification_code": None,
                "credential_source": "user",
                "generation": 3,
                "epoch": 3,
                "binding_fingerprint": _browserbase_fingerprint("tenant-project"),
            }
        },
    )
    resources = SupabaseTenantResources(
        repository,
        cipher,
        platform_browserbase_api_key="platform-browserbase-key",
        platform_browserbase_project_id="platform-project",
        resolve_browserbase_byok=True,
    )
    captured: list[tuple[str, str]] = []

    class SelectedClient:
        pass

    def client_factory(api_key: str, project_id: str) -> SelectedClient:
        captured.append((api_key, project_id))
        return SelectedClient()

    monkeypatch.setattr(browser_runtime_module, "BrowserbaseClient", client_factory)
    job = SimpleNamespace(id=JOB_ID, user_id=USER_ID)

    selected = asyncio.run(resources.browserbase_for_job(job, "worker-1"))

    assert isinstance(selected, SelectedClient)
    assert captured == [("tenant-browserbase-key", "tenant-project")]
    assert repository.calls[-1] == (
        "get_application_job_browserbase_credential",
        {"job_id": JOB_ID, "worker_id": "worker-1"},
    )


@pytest.mark.parametrize(
    ("epoch", "binding_fingerprint"),
    [
        (4, _browserbase_fingerprint("tenant-project")),
        (3, _browserbase_fingerprint("a-different-project")),
    ],
)
def test_worker_fails_closed_when_lease_bound_browserbase_binding_is_stale(
    epoch: int, binding_fingerprint: str
) -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    repository = FakeTenantRepository(
        _bundle(cipher),
        responses={
            "get_application_job_browserbase_credential": {
                "user_id": USER_ID,
                "credential_ciphertext": cipher.encrypt(
                    json.dumps(
                        {
                            "version": 1,
                            "provider": "browserbase",
                            "api_key": "tenant-browserbase-key",
                            "project_id": "tenant-project",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                ),
                "verification_status": "verified",
                "verification_code": None,
                "credential_source": "user",
                "generation": 3,
                "epoch": epoch,
                "binding_fingerprint": binding_fingerprint,
            }
        },
    )
    resources = SupabaseTenantResources(
        repository,
        cipher,
        platform_browserbase_api_key="platform-browserbase-key",
        platform_browserbase_project_id="platform-project",
        resolve_browserbase_byok=True,
    )
    job = SimpleNamespace(id=JOB_ID, user_id=USER_ID)

    with pytest.raises(ManagedBrowserError) as error:
        asyncio.run(resources.browserbase_for_job(job, "worker-1"))

    assert error.value.code == "browserbase_credential_reconfiguration_required"


def test_worker_fails_closed_for_unverified_tenant_browserbase_credential() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    repository = FakeTenantRepository(
        _bundle(cipher),
        responses={
            "get_application_job_browserbase_credential": {
                "user_id": USER_ID,
                "credential_ciphertext": cipher.encrypt("not inspected"),
                "verification_status": "unverified",
                "verification_code": "browserbase_unavailable",
                "credential_source": "user",
                "generation": 1,
                "epoch": 1,
                "binding_fingerprint": _browserbase_fingerprint("tenant-project"),
            }
        },
    )
    resources = SupabaseTenantResources(
        repository,
        cipher,
        platform_browserbase_api_key="platform-browserbase-key",
        platform_browserbase_project_id="platform-project",
        resolve_browserbase_byok=True,
    )
    job = SimpleNamespace(id=JOB_ID, user_id=USER_ID)

    with pytest.raises(ManagedBrowserError) as error:
        asyncio.run(resources.browserbase_for_job(job, "worker-1"))

    assert error.value.code == "browserbase_credential_reconfiguration_required"


def test_worker_uses_platform_browserbase_only_when_tenant_row_is_absent(
    monkeypatch: Any,
) -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    repository = FakeTenantRepository(
        _bundle(cipher),
        responses={"get_application_job_browserbase_credential": None},
    )
    resources = SupabaseTenantResources(
        repository,
        cipher,
        platform_browserbase_api_key="platform-browserbase-key",
        platform_browserbase_project_id="platform-project",
        resolve_browserbase_byok=True,
    )
    captured: list[tuple[str, str]] = []

    monkeypatch.setattr(
        browser_runtime_module,
        "BrowserbaseClient",
        lambda api_key, project_id: captured.append((api_key, project_id)) or object(),
    )
    job = SimpleNamespace(id=JOB_ID, user_id=USER_ID)

    asyncio.run(resources.browserbase_for_job(job, "worker-1"))

    assert captured == [("platform-browserbase-key", "platform-project")]


def test_confirmed_submission_uses_exact_lease_bound_record_rpc() -> None:
    cipher = TokenCipher(TokenCipher.generate_key())
    repository = FakeTenantRepository(
        _bundle(cipher),
        responses={"record_application_form_submission": [{"id": REVISION_ID}]},
    )
    resources = SupabaseTenantResources(repository, cipher)
    schema = _runtime_schema(FakePage(_task("scan", None).target_url, _fields()))
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    result = ProviderResult(
        status="succeeded",
        code="application_submitted",
        message="Provider confirmed.",
        provider="greenhouse",
        phase="submit",
        form_url=task.target_url,
        schema=schema,
        filled_count=1,
        submission_state="confirmed",
    )

    assert asyncio.run(resources.record_submission(task, "worker-1", result)) is True
    name, params = repository.calls[-1]
    assert name == "record_application_form_submission"
    assert params["job_id"] == JOB_ID
    assert params["worker_id"] == "worker-1"
    assert params["provider_submission_id"] is None
    assert params["result"]["submission_state"] == "confirmed"


class FakeManagedResources:
    def __init__(self, task: ResolvedBrowserTask, *, recorded: bool) -> None:
        self.task = task
        self.recorded = recorded
        self.record_calls = 0

    async def resolve(self, _job: Any, _worker_id: str) -> ResolvedBrowserTask:
        return self.task

    async def browserbase_for_job(self, _job: Any, _worker_id: str) -> Any:
        return SimpleNamespace(project_id="platform-project")

    async def progress(self, *_args: Any) -> bool:
        return True

    @asynccontextmanager
    async def materialize_resume(self, _task: Any) -> Any:
        yield "/tmp/tenant-resume.pdf"

    async def store_scan(self, *_args: Any) -> bool:
        return True

    async def record_submission(self, *_args: Any) -> bool:
        self.record_calls += 1
        return self.recorded


class FakeManagedRuntime:
    def __init__(self, execution: Any) -> None:
        self.execution = execution

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        callback = _kwargs.get("before_submit")
        assert callback is not None and await callback() is True
        return self.execution


@pytest.mark.parametrize(
    ("recorded", "expected_status", "expected_code"),
    [
        (True, "succeeded", "application_submitted"),
        (False, "needs_attention", "submission_record_unconfirmed"),
    ],
)
def test_clear_submission_is_recorded_before_worker_reports_success(
    recorded: bool, expected_status: str, expected_code: str
) -> None:
    schema = _runtime_schema(FakePage(_task("scan", None).target_url, _fields()))
    task = _task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    provider_result = ProviderResult(
        status="succeeded",
        code="application_submitted",
        message="Provider confirmed.",
        provider="greenhouse",
        phase="submit",
        form_url=task.target_url,
        schema=schema,
        filled_count=1,
        submission_state="confirmed",
    )
    resources = FakeManagedResources(task, recorded=recorded)
    handler = ManagedBrowserJobHandler(
        resources,  # type: ignore[arg-type]
        FakeManagedRuntime(SimpleNamespace(result=provider_result, details=lambda: {})),  # type: ignore[arg-type]
        "worker-1",
        ("greenhouse",),
        handle_job,
    )
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="greenhouse",
        kind="application_submit",
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == expected_status
    assert outcome.code == expected_code
    assert resources.record_calls == 1


def test_google_forms_uncertain_submit_completes_as_needs_attention_without_recording() -> None:
    page = FakePage(_google_task("scan", None).target_url, _fields())
    schema = _runtime_schema(page, "google_forms")
    task = _google_task(
        "submit",
        schema.schema_hash,
        answers={"Email address": "candidate@example.com"},
    )
    provider_result = ProviderResult(
        status="needs_attention",
        code="submission_unconfirmed",
        message="Verify the provider result manually.",
        provider="google_forms",
        phase="submit",
        form_url=task.target_url,
        schema=schema,
        filled_count=1,
        submission_state="uncertain",
    )
    resources = FakeManagedResources(task, recorded=True)
    handler = ManagedBrowserJobHandler(
        resources,  # type: ignore[arg-type]
        FakeManagedRuntime(
            SimpleNamespace(result=provider_result, details=lambda: provider_result.details())
        ),  # type: ignore[arg-type]
        "worker-1",
        ("google_forms",),
        handle_job,
    )
    job = SimpleNamespace(
        id=JOB_ID,
        user_id=USER_ID,
        application_id=APPLICATION_ID,
        provider="google_forms",
        kind="application_submit",
    )

    outcome = asyncio.run(handler(job))

    assert outcome.status == "needs_attention"
    assert outcome.code == "submission_unconfirmed"
    assert outcome.details["submission_state"] == "uncertain"
    assert resources.record_calls == 0
