"""Conservative, provider-neutral application-form scanning and filling."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


FieldKind = Literal[
    "text",
    "email",
    "tel",
    "url",
    "number",
    "date",
    "textarea",
    "select",
    "combobox",
    "radio",
    "checkbox",
    "file",
]
ExecutionPhase = Literal["scan", "prefill", "submit"]

_CONTROL_SELECTOR = (
    'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]),'
    'textarea,select,[role="combobox"],[role="radio"],[role="checkbox"]'
)
_NORMALIZE = re.compile(r"[^a-z0-9]+")
_CHECKPOINT_TEXT = re.compile(
    r"(?:verify (?:that )?you are human|security checkpoint|complete the captcha|"
    r"enter (?:the |your )?(?:verification|security) code|two[- ]factor authentication|"
    r"multi[- ]factor authentication)",
    re.IGNORECASE,
)
_PASSIVE_CHECKPOINT_WIDGET_SELECTOR = (
    'iframe[src*="recaptcha" i],iframe[src*="hcaptcha" i],'
    'iframe[src*="challenges.cloudflare" i],[data-sitekey]'
)
_ACTIVE_VERIFICATION_CONTROL_SELECTOR = (
    'input[autocomplete="one-time-code"],input[name*="otp" i]'
)
_LOGIN_CONTROL_SELECTOR = (
    'input[type="password"],input[autocomplete="current-password" i]'
)
_LOGIN_PATH = re.compile(r"/(?:login|signin|sign-in)(?:/|$)", re.IGNORECASE)
_RESUME_FIELD = re.compile(
    r"(?:\bresume\b|\br[ée]sum[ée]\b|\bcv\b|curriculum vitae)",
    re.IGNORECASE,
)

# This script returns form structure only.  It never returns entered values.
_SCAN_SCRIPT = r"""
(root) => {
  const selector = 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]),textarea,select,[role="combobox"],[role="radio"],[role="checkbox"]';
  const scope = root || document;
  const controls = Array.from(scope.querySelectorAll(selector));
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 500);
  const labelledBy = element => {
    const ids = clean(element && element.getAttribute('aria-labelledby'));
    if (!ids) return '';
    return clean(ids.split(/\s+/).map(id => {
      const node = document.getElementById(id);
      return node ? clean(node.innerText || node.textContent) : '';
    }).filter(Boolean).join(' '));
  };
  const groupFor = element => element.closest('fieldset,[role="radiogroup"],[role="group"],[role="listitem"]');
  const groupLabel = element => {
    const group = groupFor(element);
    if (!group) return '';
    const legend = group.querySelector(':scope > legend') || group.querySelector('legend');
    if (legend && clean(legend.innerText || legend.textContent)) return clean(legend.innerText || legend.textContent);
    const aria = labelledBy(group) || clean(group.getAttribute('aria-label'));
    if (aria) return aria;
    const heading = group.querySelector('[role="heading"],h1,h2,h3,h4');
    return heading ? clean(heading.innerText || heading.textContent) : '';
  };
  const labelled = element => {
    const role = clean(element.getAttribute('role')).toLowerCase();
    const rawType = clean(element.getAttribute('type')).toLowerCase();
    if (['radio', 'checkbox'].includes(role || rawType)) {
      const grouped = groupLabel(element);
      if (grouped) return grouped;
    }
    if (element.labels && element.labels.length) {
      const value = clean(element.labels[0].innerText || element.labels[0].textContent);
      if (value) return value;
    }
    const direct = labelledBy(element);
    if (direct) return direct;
    return clean(
      element.getAttribute('aria-label') ||
      element.getAttribute('placeholder') ||
      element.getAttribute('name') ||
      element.getAttribute('id')
    );
  };
  const optionLabel = element => {
    const aria = clean(element.getAttribute('aria-label') || element.getAttribute('data-value'));
    if (aria) return aria;
    if (element.labels && element.labels.length) {
      return clean(element.labels[0].innerText || element.labels[0].textContent);
    }
    return clean(element.getAttribute('value'));
  };
  return controls.slice(0, 150).map((element, index) => {
    const tag = element.tagName.toLowerCase();
    const rawType = clean(element.getAttribute('type')).toLowerCase();
    const role = clean(element.getAttribute('role')).toLowerCase();
    let kind = tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : rawType || 'text';
    if (role === 'combobox' && tag !== 'select') kind = 'combobox';
    if (role === 'radio') kind = 'radio';
    if (role === 'checkbox') kind = 'checkbox';
    if (!['text','email','tel','url','number','date','textarea','select','combobox','radio','checkbox','file'].includes(kind)) kind = 'text';
    const name = clean(element.getAttribute('name'));
    const id = clean(element.getAttribute('id'));
    const label = labelled(element) || `Question ${index + 1}`;
    const group = groupFor(element);
    const groupKey = clean(group && (group.getAttribute('aria-labelledby') || group.getAttribute('name') || group.getAttribute('id')));
    const key = ['radio', 'checkbox'].includes(kind) ? (groupKey || name || label) : (name || id || `field_${index + 1}`);
    let options = [];
    if (tag === 'select') options = Array.from(element.options || []).map(o => clean(o.label || o.textContent || o.value)).filter(Boolean).slice(0, 100);
    if (kind === 'radio' || kind === 'checkbox') options = [optionLabel(element)].filter(Boolean);
    let answered = false;
    if (kind === 'radio') {
      answered = name
        ? controls.some(control => control.type === 'radio' && control.name === name && control.checked)
        : Boolean(element.checked || element.getAttribute('aria-checked') === 'true');
    } else if (kind === 'checkbox') {
      answered = Boolean(element.checked || element.getAttribute('aria-checked') === 'true');
    } else if (kind !== 'file') {
      answered = Boolean(clean(element.value || element.textContent));
    }
    return {
      dom_index: index,
      key,
      label,
      kind,
      required: Boolean(element.required || element.getAttribute('aria-required') === 'true' || (group && group.getAttribute('aria-required') === 'true')),
      disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true' || (group && group.getAttribute('aria-disabled') === 'true')),
      answered,
      options,
      option_label: optionLabel(element),
      accept: clean(element.getAttribute('accept')),
    };
  });
}
"""


def normalize_key(value: str) -> str:
    return _NORMALIZE.sub(" ", value.strip().lower()).strip()


def safe_form_url(value: str) -> str:
    """Remove credentials, query, and fragment before persisting a form URL."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        return ""
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def canonical_form_target(provider: str, value: str) -> str:
    """Preserve only provider-vetted query fields needed to reopen the same form."""

    base = safe_form_url(value)
    if not base:
        return ""
    parsed = urlsplit(value)
    query_keys: dict[str, frozenset[str]] = {
        # Greenhouse embed links identify the posting through these fields.  Common
        # tracking fields such as gh_src/utm_* are deliberately discarded.
        "greenhouse": frozenset({"token", "gh_jid", "for"}),
    }
    allowed = query_keys.get(provider, frozenset())
    if not allowed or not parsed.query:
        return base
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=False,
            strict_parsing=False,
            max_num_fields=50,
        )
    except ValueError:
        return ""
    clean_pairs = sorted(
        (key, value[:512])
        for key, value in pairs
        if key in allowed and value and len(key) <= 64
    )
    clean = urlsplit(base)
    return urlunsplit(
        (clean.scheme, clean.netloc, clean.path, urlencode(clean_pairs), "")
    )


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    provider: str
    exact_hosts: frozenset[str]
    host_suffixes: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ("/",)
    host_path_prefixes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def allows(self, value: str) -> bool:
        if not isinstance(value, str) or not value or len(value) > 4096:
            return False
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        host_allowed = host in self.exact_hosts or any(
            host.endswith(suffix) and host != suffix.removeprefix(".")
            for suffix in self.host_suffixes
        )
        path = parsed.path or "/"
        allowed_paths = self.host_path_prefixes.get(host, self.path_prefixes)
        return bool(
            parsed.scheme == "https"
            and host_allowed
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and any(path.startswith(prefix) for prefix in allowed_paths)
        )


@dataclass(frozen=True, slots=True)
class FormField:
    dom_index: int
    key: str
    label: str
    kind: FieldKind
    required: bool
    disabled: bool
    answered: bool
    options: tuple[str, ...] = ()
    option_label: str = ""
    accept: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            # ``type`` is the stable UI/Groq contract; ``kind`` keeps the worker's
            # explicit DOM classification available to future adapters.
            "type": self.kind,
            "required": self.required,
            "disabled": self.disabled,
            # Never expose the current value.  The flag forces the user to enter
            # and approve an exact replacement before the worker may submit it.
            "prefilled": self.answered,
            "options": list(self.options),
            "accepts_resume": self.kind == "file",
        }


@dataclass(frozen=True, slots=True)
class FormSchema:
    fields: tuple[FormField, ...]
    schema_hash: str

    @property
    def public_fields(self) -> list[dict[str, Any]]:
        return _public_fields(self.fields)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: Literal["succeeded", "needs_attention"]
    code: str
    message: str
    provider: str
    phase: ExecutionPhase
    form_url: str
    schema: FormSchema | None = None
    filled_count: int = 0
    missing_required: tuple[str, ...] = ()
    submission_state: Literal["not_attempted", "confirmed", "uncertain"] = "not_attempted"

    def details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "phase": self.phase,
            "form_url": self.form_url,
            "filled_count": self.filled_count,
            "missing_required": list(self.missing_required),
            "submission_state": self.submission_state,
        }
        if self.schema is not None:
            details.update(
                {
                    "schema_hash": self.schema.schema_hash,
                    "field_count": len(self.schema.fields),
                    "question_schema": self.schema.public_fields,
                }
            )
        return details


@dataclass(frozen=True, slots=True)
class ApplicationPreparation:
    """Result of opening one provider's application form without submitting it."""

    root: Any | None
    code: str
    message: str
    transitioned: bool = False

    @property
    def ready(self) -> bool:
        return self.root is not None and self.code == "application_form_ready"


def _clean(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:maximum]


def _parse_field(raw: Any) -> FormField | None:
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    if kind not in {
        "text", "email", "tel", "url", "number", "date", "textarea",
        "select", "combobox", "radio", "checkbox", "file",
    }:
        return None
    index = raw.get("dom_index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 150:
        return None
    key = _clean(raw.get("key"), 300)
    label = _clean(raw.get("label"), 500)
    if not key or not label:
        return None
    raw_options = raw.get("options", [])
    if not isinstance(raw_options, list):
        raw_options = []
    options = tuple(
        option
        for option in (_clean(value, 300) for value in raw_options)
        if option
    )[:100]
    return FormField(
        dom_index=index,
        key=key,
        label=label,
        kind=kind,
        required=raw.get("required") is True,
        disabled=raw.get("disabled") is True,
        answered=raw.get("answered") is True,
        options=options,
        option_label=_clean(raw.get("option_label"), 300),
        accept=_clean(raw.get("accept"), 300),
    )


def _schema(fields: Sequence[FormField]) -> FormSchema:
    public = _public_fields(fields)
    canonical = json.dumps(public, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return FormSchema(
        fields=tuple(fields),
        schema_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def bind_schema_to_target(schema: FormSchema, target_url: str) -> FormSchema:
    """Bind reviewed fields to the exact canonical provider form identity."""

    canonical = json.dumps(
        {"fields": schema.public_fields, "target_url": target_url},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return FormSchema(
        fields=schema.fields,
        schema_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _public_fields(fields: Sequence[FormField]) -> list[dict[str, Any]]:
    """Collapse native radio/checkbox controls into one reviewable question."""

    result: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    group_sizes: dict[tuple[str, str, str], int] = {}
    for field in fields:
        public = field.public()
        if field.kind not in {"radio", "checkbox"}:
            result.append(public)
            continue
        identity = (field.kind, normalize_key(field.key), normalize_key(field.label))
        existing = grouped.get(identity)
        if existing is None:
            grouped[identity] = public
            group_sizes[identity] = 1
            result.append(public)
            existing = public
        else:
            group_sizes[identity] += 1
            existing["required"] = bool(existing["required"] or field.required)
            for option in field.options:
                if option not in existing["options"]:
                    existing["options"].append(option)
        if field.kind == "radio":
            existing["type"] = "radio"
        elif group_sizes[identity] > 1:
            existing["type"] = "multiselect"
    return result


async def scan_form(scope: Any) -> FormSchema:
    """Capture controls inside one provider-approved application form root."""

    raw_fields = await scope.evaluate(_SCAN_SCRIPT)
    if not isinstance(raw_fields, list):
        raw_fields = []
    fields = tuple(field for raw in raw_fields if (field := _parse_field(raw)) is not None)
    return _schema(fields)


async def checkpoint_present(
    page: Any,
    *,
    include_passive_widgets: bool = True,
) -> bool:
    """Return true only for an active, user-facing security checkpoint.

    ATS pages commonly preload invisible reCAPTCHA/hCaptcha iframes and hidden
    ``data-sitekey`` containers. Some also render a passive visible CAPTCHA widget
    beside an otherwise readable form. Scans and reviewed prefills may continue
    around that passive widget, but final submission treats it as a checkpoint.
    OTP controls and explicit checkpoint copy always pause every phase.
    """

    selectors = [_ACTIVE_VERIFICATION_CONTROL_SELECTOR]
    if include_passive_widgets:
        selectors.append(_PASSIVE_CHECKPOINT_WIDGET_SELECTOR)
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(min(await locator.count(), 50)):
                try:
                    if await locator.nth(index).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    try:
        body = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        body = ""
    return bool(_CHECKPOINT_TEXT.search(body[:200_000]))


def _approved_lookup(answers: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in answers.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 500:
            continue
        if isinstance(value, (str, bool, int, float)) and not isinstance(value, complex):
            if isinstance(value, str) and len(value) > 10_000:
                continue
            result[normalize_key(key)] = value
        elif (
            isinstance(value, list)
            and len(value) <= 100
            and all(isinstance(item, str) and len(item) <= 300 for item in value)
        ):
            result[normalize_key(key)] = tuple(value)
    return result


def _answer_for(field: FormField, approved: Mapping[str, Any]) -> Any | None:
    for candidate in (field.key, field.label):
        answer = approved.get(normalize_key(candidate))
        if answer is not None:
            return answer
    return None


def _boolean_answer(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        clean = normalize_key(value)
        if clean in {"yes", "true", "checked", "agree", "accepted", "1"}:
            return True
        if clean in {"no", "false", "unchecked", "decline", "0"}:
            return False
    return None


async def _fill_one(page: Any, control: Any, field: FormField, answer: Any) -> bool:
    is_many = isinstance(answer, tuple)
    text = (
        ""
        if is_many
        else str(answer).strip() if not isinstance(answer, bool) else ("true" if answer else "false")
    )
    many = tuple(str(value).strip() for value in answer) if is_many else ()
    if field.kind in {"text", "email", "tel", "url", "number", "date", "textarea"}:
        if is_many:
            return False
        await control.fill(text)
        return True
    if field.kind == "combobox":
        if is_many:
            return False
        await control.fill(text)
        try:
            await page.wait_for_timeout(250)
        except Exception:
            pass
        options = page.locator('[role="option"]')
        matches: list[Any] = []
        for index in range(min(await options.count(), 100)):
            option = options.nth(index)
            try:
                option_text = (await option.inner_text()).strip()
                if await option.is_visible() and option_text.casefold() == text.casefold():
                    matches.append(option)
            except Exception:
                continue
        if len(matches) == 1:
            await matches[0].click()
            return True
        await control.fill("")
        return False
    if field.kind == "select":
        requested = many or (text,)
        matches = [
            option
            for wanted in requested
            for option in field.options
            if option.casefold() == wanted.casefold()
        ]
        if len(matches) != len(requested):
            return False
        await control.select_option(label=matches if is_many else matches[0])
        return True
    if field.kind == "radio":
        if is_many:
            return False
        if not field.option_label or field.option_label.casefold() != text.casefold():
            return False
        try:
            await control.check()
        except Exception:
            # Google Forms and similar SPAs expose ARIA radio controls rather than
            # native inputs.  The approved option is still matched exactly first.
            await control.click()
        return True
    if field.kind == "checkbox":
        desired = (
            field.option_label.casefold() in {value.casefold() for value in many}
            if is_many and field.option_label
            else _boolean_answer(answer)
        )
        if desired is None and field.option_label:
            desired = field.option_label.casefold() == text.casefold()
        if desired is None:
            return False
        if field.required and not desired:
            return False
        try:
            await control.set_checked(desired)
        except Exception:
            # ARIA checkboxes have no native checked setter.  Toggle only when the
            # observable state differs from the exact approved boolean/option set.
            try:
                current = (await control.get_attribute("aria-checked")) == "true"
            except Exception:
                current = False
            if current != desired:
                await control.click()
        # In a checkbox group, unchecked alternatives do not prove that the
        # approved selection matched this question.  Count only selected options.
        return desired if is_many else True
    return False


async def fill_approved(
    page: Any,
    schema: FormSchema,
    answers: Mapping[str, Any],
    *,
    resume_path: str | None,
    root: Any | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Fill exact approved values only; return count and unresolved required labels."""

    approved = _approved_lookup(answers)
    controls = (root or page).locator(_CONTROL_SELECTOR)
    filled_keys: set[str] = set()
    filled_count = 0
    for field in schema.fields:
        if field.disabled:
            continue
        try:
            control = controls.nth(field.dom_index)
            if field.kind == "file":
                resume_field = bool(
                    _RESUME_FIELD.search(f"{field.label} {field.key}")
                )
                if resume_path and resume_field:
                    await control.set_input_files(resume_path)
                    filled_keys.add(normalize_key(field.key))
                    filled_count += 1
                continue
            answer = _answer_for(field, approved)
            if answer is None:
                continue
            if await _fill_one(page, control, field, answer):
                filled_keys.add(normalize_key(field.key))
                filled_count += 1
        except Exception:
            # A changed or unsupported control stays unfilled and is surfaced below
            # if required.  Never guess or switch to a nearby field.
            continue

    missing: list[str] = []
    seen_required: set[str] = set()
    for field in schema.fields:
        required_key = normalize_key(field.key)
        if (
            (field.required or field.answered)
            and required_key not in filled_keys
            and required_key not in seen_required
        ):
            seen_required.add(required_key)
            missing.append(field.label[:200])
    return filled_count, tuple(missing[:30])


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    provider: str
    policy: ProviderPolicy
    submit_selectors: tuple[str, ...]
    confirmation_path_patterns: tuple[str, ...]
    confirmation_text: tuple[str, ...]
    form_selectors: tuple[str, ...] = ("form",)
    login_redirect_hosts: frozenset[str] = frozenset()
    # A single union selector keeps DOM de-duplication in Playwright.  It is used
    # only when no unambiguous application form is already visible and may cause
    # at most one click before the form is scanned again.
    application_entry_selector: str | None = None
    # Some providers expose a deterministic, same-job query transition instead
    # of a unique Apply control (for example Wellfound's application dialog).
    application_entry_query: tuple[tuple[str, str], ...] = ()
    # Authenticated multi-step providers stay explicit instead of guessing at a
    # control that might itself submit an application.
    application_transition_unsupported_message: str | None = None
    submission_unsupported_message: str | None = None

    def allows_url(self, value: str) -> bool:
        return self.policy.allows(value)

    def login_required(self, value: str) -> bool:
        try:
            return bool(_LOGIN_PATH.search(urlsplit(value).path))
        except ValueError:
            return False

    def is_login_redirect(self, value: str) -> bool:
        try:
            parsed = urlsplit(value)
            return (
                parsed.scheme == "https"
                and (parsed.hostname or "").rstrip(".").lower()
                in self.login_redirect_hosts
            )
        except ValueError:
            return False

    @staticmethod
    async def _visible_login_prompt(scope: Any) -> bool:
        """Recognize a rendered password prompt without reading entered values."""

        try:
            locator = scope.locator(_LOGIN_CONTROL_SELECTOR)
            for index in range(min(await locator.count(), 20)):
                try:
                    if await locator.nth(index).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _looks_like_application_root(self, root: Any) -> bool:
        # Authenticated providers can render a sign-in dialog with application
        # copy (for example, "Sign in to apply").  A password prompt is never an
        # application form and must not enter the scan/fill pipeline.
        if await self._visible_login_prompt(root):
            return False
        try:
            control_count = await root.locator(_CONTROL_SELECTOR).count()
        except Exception:
            return False
        if not 1 <= control_count <= 150:
            return False
        if self.provider == "google_forms":
            return True
        try:
            text = (await root.inner_text(timeout=2_000))[:100_000]
        except Exception:
            text = ""
        application_words = bool(
            re.search(
                r"\b(?:apply|application|resume|r[ée]sum[ée]|curriculum vitae|cover letter|work authori[sz]ation)\b",
                text,
                re.IGNORECASE,
            )
        )
        try:
            resume_controls = await root.locator(
                'input[type="file"],input[name*="resume" i],input[name*="cv" i]'
            ).count()
        except Exception:
            resume_controls = 0
        try:
            email_controls = await root.locator(
                'input[type="email"],input[name*="email" i]'
            ).count()
        except Exception:
            email_controls = 0
        if resume_controls:
            return True
        if self.provider in {"yc", "wellfound", "cutshort", "instahyre"}:
            return application_words
        return bool(application_words and email_controls)

    async def _form_root_state(self, page: Any) -> tuple[Any | None, bool]:
        """Return the form root and whether provider form roots were ambiguous."""

        for selector in self.form_selectors:
            try:
                locator = page.locator(selector)
                raw_count = await locator.count()
                if raw_count > 10:
                    return None, True
                matches: list[Any] = []
                for index in range(raw_count):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible() and await self._looks_like_application_root(
                            candidate
                        ):
                            matches.append(candidate)
                    except Exception:
                        continue
            except Exception:
                continue
            if len(matches) == 1:
                return matches[0], False
            if len(matches) > 1:
                return None, True
        return None, False

    async def find_form_root(self, page: Any) -> Any | None:
        """Return one unambiguous provider-scoped application form container."""

        root, _ambiguous = await self._form_root_state(page)
        return root

    @staticmethod
    def _preparation(
        code: str,
        message: str,
        *,
        root: Any | None = None,
        transitioned: bool = False,
    ) -> ApplicationPreparation:
        return ApplicationPreparation(
            root=root,
            code=code,
            message=message,
            transitioned=transitioned,
        )

    async def open_application(
        self,
        page: Any,
        *,
        include_passive_checkpoints: bool = True,
    ) -> ApplicationPreparation:
        """Open at most one provider-approved application entry point.

        The hook accepts an already-visible form. Otherwise it follows one exact
        allowlisted application URL or clicks one visible/enabled provider-specific
        dialog control, then requires one unambiguous application form. It never
        advances form pages and never looks for a final Submit control.
        """

        current_url = getattr(page, "url", "")
        if self.is_login_redirect(current_url) or self.login_required(current_url):
            return self._preparation(
                "provider_login_required",
                "Sign in to this provider connection before continuing.",
            )
        if await self._visible_login_prompt(page):
            return self._preparation(
                "provider_login_required",
                "A provider sign-in prompt requires your attention before applying.",
            )
        if await checkpoint_present(
            page,
            include_passive_widgets=include_passive_checkpoints,
        ):
            return self._preparation(
                "security_checkpoint",
                "A CAPTCHA, MFA prompt, or security checkpoint requires your attention.",
            )
        if self.application_transition_unsupported_message:
            return self._preparation(
                "provider_transition_unsupported",
                self.application_transition_unsupported_message,
            )
        root, ambiguous = await self._form_root_state(page)
        if ambiguous:
            return self._preparation(
                "application_form_ambiguous",
                "More than one possible application form was visible; choose the form manually.",
            )
        if root is not None:
            return self._preparation(
                "application_form_ready",
                "The provider application form is ready.",
                root=root,
            )

        deterministic_target = ""
        if self.application_entry_query:
            try:
                parsed = urlsplit(current_url)
                query = dict(
                    parse_qsl(
                        parsed.query,
                        keep_blank_values=True,
                        strict_parsing=False,
                        max_num_fields=50,
                    )
                )
                query.update(self.application_entry_query)
                deterministic_target = urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        urlencode(sorted(query.items())),
                        "",
                    )
                )
            except (TypeError, ValueError):
                deterministic_target = ""
            if not self.allows_url(deterministic_target):
                return self._preparation(
                    "provider_redirect_blocked",
                    "The provider application transition could not be constructed safely.",
                )
        elif not self.application_entry_selector:
            return self._preparation(
                "application_form_not_found",
                "No provider application form was found on this page.",
            )

        candidates: list[Any] = []
        if not deterministic_target:
            try:
                locator = page.locator(self.application_entry_selector)
                raw_count = await locator.count()
                if raw_count > 10:
                    return self._preparation(
                        "application_entry_ambiguous",
                        "Too many possible application entry controls were visible.",
                    )
                for index in range(raw_count):
                    candidate = locator.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        candidates.append(candidate)
            except Exception:
                return self._preparation(
                    "application_entry_unavailable",
                    "The provider application entry control could not be inspected safely.",
                )

            if not candidates:
                return self._preparation(
                    "application_form_not_found",
                    "No unambiguous provider application form or Apply control was found.",
                )

            href_targets: list[str | None] = []
            for candidate in candidates:
                try:
                    raw_href = await candidate.get_attribute("href")
                except Exception:
                    raw_href = None
                if isinstance(raw_href, str) and raw_href.strip():
                    target = urljoin(current_url, raw_href.strip())
                    if not self.allows_url(target):
                        return self._preparation(
                            "provider_redirect_blocked",
                            "The provider Apply link points outside its approved hosts.",
                        )
                    href_targets.append(target)
                else:
                    href_targets.append(None)

            navigable = [target for target in href_targets if target is not None]
            if navigable:
                if len(navigable) != len(candidates) or len(set(navigable)) != 1:
                    return self._preparation(
                        "application_entry_ambiguous",
                        "More than one distinct Apply control was visible; choose one manually.",
                    )
                # Duplicate links to the exact same allowlisted application URL
                # are one transition, not multiple choices. Navigate once rather
                # than clicking a possibly duplicated page control.
                deterministic_target = navigable[0]
            elif len(candidates) != 1:
                return self._preparation(
                    "application_entry_ambiguous",
                    "More than one possible Apply control was visible; choose one manually.",
                )

        try:
            if deterministic_target:
                await page.goto(
                    deterministic_target,
                    wait_until="domcontentloaded",
                    timeout=8_000,
                )
            else:
                await candidates[0].click()
        except Exception:
            return self._preparation(
                "application_entry_unconfirmed",
                "The Apply control was attempted, but the application form could not be observed.",
                transitioned=True,
            )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        try:
            await page.wait_for_timeout(750)
        except Exception:
            pass

        current_url = getattr(page, "url", "")
        if self.is_login_redirect(current_url) or self.login_required(current_url):
            return self._preparation(
                "provider_login_required",
                "Sign in to this provider connection before continuing.",
                transitioned=True,
            )
        if not self.allows_url(current_url):
            return self._preparation(
                "provider_redirect_blocked",
                "The provider application entry point left its approved hosts.",
                transitioned=True,
            )
        if await self._visible_login_prompt(page):
            return self._preparation(
                "provider_login_required",
                "The provider opened a sign-in prompt instead of an application form.",
                transitioned=True,
            )
        if await checkpoint_present(
            page,
            include_passive_widgets=include_passive_checkpoints,
        ):
            return self._preparation(
                "security_checkpoint",
                "A CAPTCHA, MFA prompt, or security checkpoint requires your attention.",
                transitioned=True,
            )

        root, ambiguous = await self._form_root_state(page)
        if ambiguous:
            return self._preparation(
                "application_form_ambiguous",
                "The Apply control opened more than one possible application form.",
                transitioned=True,
            )
        if root is None:
            return self._preparation(
                "application_form_not_found",
                "The Apply control did not open a supported application form.",
                transitioned=True,
            )
        return self._preparation(
            "application_form_ready",
            "The provider application form is ready.",
            root=root,
            transitioned=True,
        )

    async def find_submit(self, page: Any, root: Any | None = None) -> Any | None:
        scope = root or page
        candidates: list[Any] = []
        for selector in self.submit_selectors:
            try:
                locator = scope.locator(selector)
                for index in range(min(await locator.count(), 3)):
                    candidate = locator.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        candidates.append(candidate)
            except Exception:
                continue
        # Multiple final-action candidates are ambiguous.  Refuse to choose one.
        return candidates[0] if len(candidates) == 1 else None

    async def confirmed(self, page: Any) -> bool:
        try:
            path = urlsplit(page.url).path
        except ValueError:
            path = ""
        if any(re.search(pattern, path, re.IGNORECASE) for pattern in self.confirmation_path_patterns):
            return True
        try:
            body = (await page.locator("body").inner_text(timeout=2_000)).casefold()
        except Exception:
            return False
        return any(marker.casefold() in body for marker in self.confirmation_text)


__all__ = [
    "ApplicationPreparation",
    "ExecutionPhase",
    "FormField",
    "FormSchema",
    "ProviderAdapter",
    "ProviderPolicy",
    "ProviderResult",
    "bind_schema_to_target",
    "canonical_form_target",
    "checkpoint_present",
    "fill_approved",
    "normalize_key",
    "safe_form_url",
    "scan_form",
]
