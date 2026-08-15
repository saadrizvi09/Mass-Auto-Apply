"""Strict current YC exact-job application policy.

YC publishes job details on ``ycombinator.com`` but sends the Apply action
through the YC account service to one job-bound Work at a Startup application.
Only a current YC company job-detail URL may enter the managed-browser worker.
The account and Work at a Startup routes remain navigation-only authorities for
that exact job's controlled application handoff and confirmation.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .base import (
    ApplicationPreparation,
    FormSchema,
    ProviderAdapter,
    ProviderPolicy,
    normalize_key,
)


_CURRENT_JOB_PATH = re.compile(
    r"^/companies/[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?/jobs/"
    r"[a-z0-9]{5,64}(?:-[a-z0-9]+)*/?$",
    re.IGNORECASE,
)
_CONFIRMATION_PATH = re.compile(
    r"^/applications?/(?:submitted|complete)/?$", re.IGNORECASE
)
_ACCOUNT_PATHS = frozenset({"/", "/authenticate", "/authenticate/"})
_ACCOUNT_QUERY_KEYS = frozenset(
    {"continue", "defaults[signUpActive]", "defaults[waas_company]"}
)
_MESSAGE_CONTROL_SELECTOR = (
    'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):'
    'not([type="reset"]),textarea,select,[role="combobox"],[role="listbox"],'
    '[role="radio"],[role="checkbox"]'
)


def _parsed_https(value: str) -> Any | None:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port != 443)
        or parsed.fragment
    ):
        return None
    return parsed


def _exact_query(value: str, *, maximum_fields: int) -> list[tuple[str, str]] | None:
    try:
        pairs = parse_qsl(
            value,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=maximum_fields,
        )
    except ValueError:
        return None
    if len(pairs) > maximum_fields or any(not key or not item for key, item in pairs):
        return None
    return pairs


def is_exact_yc_job_url(value: str) -> bool:
    """Accept one current public YC job detail, never a legacy entry URL."""

    parsed = _parsed_https(value)
    if parsed is None or parsed.query:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    return bool(
        host in {"ycombinator.com", "www.ycombinator.com"}
        and _CURRENT_JOB_PATH.fullmatch(parsed.path) is not None
    )


def _application_identity(value: str) -> tuple[str, str] | None:
    """Return the canonical host/job id for one controlled application route."""

    parsed = _parsed_https(value)
    if parsed is None:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if host not in {"workatastartup.com", "www.workatastartup.com"}:
        return None
    if parsed.path not in {"/application", "/application/"}:
        return None
    pairs = _exact_query(parsed.query, maximum_fields=1)
    if pairs is None or len(pairs) != 1 or pairs[0][0] != "signup_job_id":
        return None
    job_id = pairs[0][1]
    if re.fullmatch(r"[1-9]\d{0,18}", job_id) is None:
        return None
    return "www.workatastartup.com", job_id


def _controlled_account_handoff(value: str) -> bool:
    parsed = _parsed_https(value)
    if parsed is None:
        return False
    host = (parsed.hostname or "").rstrip(".").lower()
    if host != "account.ycombinator.com" or parsed.path not in _ACCOUNT_PATHS:
        return False
    pairs = _exact_query(parsed.query, maximum_fields=3)
    if pairs is None or not 1 <= len(pairs) <= 3:
        return False
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)) or not set(keys) <= _ACCOUNT_QUERY_KEYS:
        return False
    values = dict(pairs)
    if _application_identity(values.get("continue", "")) is None:
        return False
    sign_up = values.get("defaults[signUpActive]")
    company = values.get("defaults[waas_company]")
    return bool(
        (sign_up is None or sign_up == "true")
        and (company is None or re.fullmatch(r"[1-9]\d{0,18}", company))
    )


def canonical_yc_form_target(value: str) -> str:
    """Bind a reviewed schema to the exact YC job or signup application id."""

    if is_exact_yc_job_url(value):
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
    identity = _application_identity(value)
    if identity is None:
        return ""
    host, job_id = identity
    return urlunsplit(
        ("https", host, "/application", urlencode({"signup_job_id": job_id}), "")
    )


def yc_schema_issue(schema: FormSchema) -> tuple[str, str] | None:
    """YC's reviewed contract is exactly one editable message textarea."""

    if len(schema.fields) != 1:
        return (
            "unsupported_application_fields",
            "YC displayed application fields outside the reviewed message-only workflow. Complete this application manually.",
        )
    field = schema.fields[0]
    if field.kind != "textarea" or field.disabled:
        return (
            "unsupported_application_fields",
            "YC did not display one editable application message. Complete this application manually.",
        )
    # Do not depend on one volatile label, but reject a field whose explicit
    # label identifies a materially different action.
    identity = normalize_key(f"{field.key} {field.label}")
    if identity and any(
        marker in identity
        for marker in ("password", "verification code", "one time code", "captcha")
    ):
        return (
            "unsupported_application_fields",
            "YC displayed an account or verification field instead of an application message.",
        )
    return None


class YcProviderAdapter(ProviderAdapter):
    """Provider adapter with query-aware route and message-root validation."""

    def allows_url(self, value: str) -> bool:
        if is_exact_yc_job_url(value) or _application_identity(value) is not None:
            return True
        if _controlled_account_handoff(value):
            return True
        parsed = _parsed_https(value)
        if parsed is None or parsed.query:
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        return bool(
            host in {"workatastartup.com", "www.workatastartup.com"}
            and _CONFIRMATION_PATH.fullmatch(parsed.path)
        )

    async def _looks_like_application_root(self, root: Any) -> bool:
        if await self._visible_login_prompt(root):
            return False
        try:
            controls = root.locator(_MESSAGE_CONTROL_SELECTOR)
            if not 1 <= await controls.count() <= 150:
                return False
            textareas = root.locator("textarea")
            visible_textareas = 0
            for index in range(min(await textareas.count(), 20)):
                if await textareas.nth(index).is_visible():
                    visible_textareas += 1
            finals = root.locator(",".join(self.submit_selectors))
            visible_finals = 0
            for index in range(min(await finals.count(), 10)):
                if await finals.nth(index).is_visible():
                    visible_finals += 1
            return visible_textareas == 1 and visible_finals == 1
        except Exception:
            return False

    async def open_application(
        self,
        page: Any,
        *,
        include_passive_checkpoints: bool = True,
    ) -> ApplicationPreparation:
        """Use YC's job-bound account handoff on current public job pages.

        The current site also renders generic ``/apply`` navigation links. Those
        are intentionally excluded because they are not bound to the selected
        job. Work at a Startup is admitted only after the exact current job's
        controlled account handoff reaches its numeric application identity.
        """

        try:
            host = (urlsplit(getattr(page, "url", "")).hostname or "").lower()
        except ValueError:
            host = ""
        if host in {"ycombinator.com", "www.ycombinator.com"}:
            scoped = replace(
                self,
                application_entry_selector=(
                    'a[href*="account.ycombinator.com/authenticate"]'
                    '[href*="signup_job_id"]'
                ),
            )
            return await ProviderAdapter.open_application(
                scoped,
                page,
                include_passive_checkpoints=include_passive_checkpoints,
            )
        return await ProviderAdapter.open_application(
            self,
            page,
            include_passive_checkpoints=include_passive_checkpoints,
        )


ADAPTER = YcProviderAdapter(
    provider="yc",
    # The subclass performs the exact path/query validation.  The policy remains
    # descriptive and intentionally cannot widen that override.
    policy=ProviderPolicy(
        provider="yc",
        exact_hosts=frozenset(
            {
                "ycombinator.com",
                "www.ycombinator.com",
                "workatastartup.com",
                "www.workatastartup.com",
                "account.ycombinator.com",
            }
        ),
    ),
    submit_selectors=(
        'button:text-is("Send"),button:text-is("Send application"),'
        'button:text-is("Send Application"),button[type="submit"]:text-is("Submit"),'
        'button:text-is("Submit application"),button:text-is("Submit Application")',
    ),
    confirmation_path_patterns=(r"^/applications?/(?:submitted|complete)/?$",),
    confirmation_text=(
        "application submitted",
        "your application was sent",
    ),
    form_selectors=(
        '[role="dialog"] form',
        '[role="dialog"]',
        "main form",
    ),
    login_redirect_hosts=frozenset({"account.ycombinator.com"}),
    application_entry_selector=(
        'a:text-is("Apply"),button:text-is("Apply"),'
        'a:text-is("Apply now"),button:text-is("Apply now"),'
        'a:text-is("Apply Now"),button:text-is("Apply Now")'
    ),
)


__all__ = [
    "ADAPTER",
    "YcProviderAdapter",
    "canonical_yc_form_target",
    "is_exact_yc_job_url",
    "yc_schema_issue",
]
