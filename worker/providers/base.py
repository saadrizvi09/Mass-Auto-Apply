"""Conservative, provider-neutral application-form scanning and filling."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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
    "listbox",
    "radio",
    "checkbox",
    "file",
]
ExecutionPhase = Literal["scan", "prefill", "submit"]

_CONTROL_SELECTOR = (
    'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]),'
    'textarea,select,[role="combobox"],[role="listbox"],[role="radio"],[role="checkbox"]'
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
_FILE_UPLOAD_PROMPT = re.compile(
    r"(?:\b(?:upload|attach|choose|select|add)\b.{0,40}\b(?:file|document|resume|r[ée]sum[ée]|cv)\b|"
    r"\b(?:file|document|resume|r[ée]sum[ée]|cv)\b.{0,40}\b(?:upload|attach)\b)",
    re.IGNORECASE,
)
_PDF_ACCEPT_TOKENS = frozenset(
    {".pdf", "application/pdf", "application/x-pdf", "application/*", "*/*"}
)
_GOOGLE_PICKER_FRAME_HOSTS = frozenset({"docs.google.com", "drive.google.com"})
_GOOGLE_PICKER_FRAME_PATH = re.compile(r"/(?:picker|upload)(?:/|$)", re.IGNORECASE)
_GOOGLE_PICKER_TRIGGER = re.compile(
    r"^(?:add|upload|choose|select)\s+(?:a\s+)?file$", re.IGNORECASE
)

# This script returns form structure only.  It never returns entered values.
_SCAN_SCRIPT = r"""
(root) => {
  const selector = 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]),textarea,select,[role="combobox"],[role="listbox"],[role="radio"],[role="checkbox"]';
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
    if (['radio', 'checkbox', 'combobox', 'listbox', 'file'].includes(role || rawType)) {
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
    return clean(element.getAttribute('value') || element.innerText || element.textContent);
  };
  return controls.slice(0, 150).map((element, index) => {
    const tag = element.tagName.toLowerCase();
    const rawType = clean(element.getAttribute('type')).toLowerCase();
    const role = clean(element.getAttribute('role')).toLowerCase();
    let kind = tag === 'textarea' ? 'textarea' : tag === 'select' ? 'select' : rawType || 'text';
    if (role === 'combobox' && tag !== 'select') kind = 'combobox';
    if (role === 'listbox' && tag !== 'select') kind = 'listbox';
    if (role === 'radio') kind = 'radio';
    if (role === 'checkbox') kind = 'checkbox';
    if (!['text','email','tel','url','number','date','textarea','select','combobox','listbox','radio','checkbox','file'].includes(kind)) kind = 'text';
    const name = clean(element.getAttribute('name'));
    const id = clean(element.getAttribute('id'));
    const label = labelled(element) || `Question ${index + 1}`;
    const group = groupFor(element);
    const groupedLabel = groupLabel(element);
    const groupKey = clean(group && (group.getAttribute('aria-labelledby') || group.getAttribute('name') || group.getAttribute('id')));
    const key = ['radio', 'checkbox', 'combobox', 'listbox'].includes(kind)
      ? (groupKey || name || id || groupedLabel || label)
      : (name || id || `field_${index + 1}`);
    let options = [];
    if (tag === 'select') options = Array.from(element.options || []).map(o => clean(o.label || o.textContent || o.value)).filter(Boolean).slice(0, 100);
    if (kind === 'listbox') options = Array.from(element.querySelectorAll('[role="option"]')).map(optionLabel).filter(Boolean).slice(0, 100);
    if (kind === 'radio' || kind === 'checkbox') options = [optionLabel(element)].filter(Boolean);
    let answered = false;
    if (kind === 'radio') {
      answered = name
        ? controls.some(control => control.type === 'radio' && control.name === name && control.checked)
        : Boolean(element.checked || element.getAttribute('aria-checked') === 'true');
    } else if (kind === 'checkbox') {
      answered = Boolean(element.checked || element.getAttribute('aria-checked') === 'true');
    } else if (kind === 'listbox') {
      const selected = element.querySelector('[role="option"][aria-selected="true"]');
      answered = Boolean(selected && clean(selected.getAttribute('data-value') || selected.innerText || selected.textContent));
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
    if provider == "company_form" and parsed.query:
        # A user-supplied company site can place the exact application identity in
        # arbitrary query fields after its one same-host Apply transition. Preserve
        # the original encoded ordering so signed URLs continue to reopen, but put
        # a strict ceiling on what can be retained as reviewed form identity.
        if len(parsed.query) > 2_048:
            return ""
        try:
            pairs = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=25,
            )
        except ValueError:
            return ""
        if (
            len(pairs) > 25
            or any(not key or len(key) > 128 or len(item) > 1_024 for key, item in pairs)
        ):
            return ""
        clean = urlsplit(base)
        return urlunsplit(
            (clean.scheme, clean.netloc, clean.path, parsed.query, "")
        )
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
    path_patterns: tuple[str, ...] = ()
    allow_explicit_port: bool = True

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
        path_allowed = any(path.startswith(prefix) for prefix in allowed_paths)
        if self.path_patterns:
            path_allowed = path_allowed and any(
                re.search(pattern, path, re.IGNORECASE)
                for pattern in self.path_patterns
            )
        return bool(
            parsed.scheme == "https"
            and host_allowed
            and parsed.username is None
            and parsed.password is None
            and (port is None or (self.allow_explicit_port and port == 443))
            and path_allowed
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
    provider_picker: bool = False
    picker_index: int | None = field(default=None, repr=False)

    @property
    def is_resume_upload(self) -> bool:
        """Whether this is one explicit native résumé/CV file control."""

        return self.kind == "file" and bool(
            _RESUME_FIELD.search(f"{self.label} {self.key}")
        )

    @property
    def accepts_pdf(self) -> bool:
        """Fail closed when an explicit accept list excludes PDF documents."""

        if self.kind != "file":
            return False
        tokens = {
            item.strip().casefold()
            for item in self.accept.split(",")
            if item.strip()
        }
        # An omitted accept attribute means the native control accepts arbitrary
        # files; Playwright still attaches exactly one validated local PDF.
        return not tokens or bool(tokens & _PDF_ACCEPT_TOKENS)

    def public(self) -> dict[str, Any]:
        result = {
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
            "accepts_resume": self.is_resume_upload and self.accepts_pdf,
        }
        if self.kind == "file":
            # The accept contract participates in the reviewed schema hash. It is
            # safe structural metadata, not an entered value or private file path.
            result["accepted_file_types"] = self.accept
            if self.provider_picker:
                result["upload_mode"] = "google_picker"
        return result


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
        "select", "combobox", "listbox", "radio", "checkbox", "file",
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


def _picker_label_from_text(value: str) -> str:
    """Extract one stable question label without persisting picker/UI copy."""

    lines = [" ".join(line.split())[:500] for line in value.splitlines()]
    candidates = [
        line
        for line in lines
        if line
        and _RESUME_FIELD.search(line)
        and not _GOOGLE_PICKER_TRIGGER.fullmatch(line)
    ]
    return candidates[0] if len(candidates) == 1 else ""


def _has_file_picker_prompt(value: str) -> bool:
    if _FILE_UPLOAD_PROMPT.search(value):
        return True
    return bool(
        _RESUME_FIELD.search(value)
        and any(
            _GOOGLE_PICKER_TRIGGER.fullmatch(" ".join(line.split()))
            for line in value.splitlines()
            if line.strip()
        )
    )


async def _scan_google_provider_pickers(
    scope: Any,
    *,
    start_index: int,
) -> tuple[FormField, ...]:
    """Model Google Forms' picker-only file questions as reviewable fields.

    Google renders the actual ``input[type=file]`` inside a Google-owned picker
    iframe only after the respondent presses ``Add file``.  The scan therefore
    records a synthetic field only when one visible question container says both
    résumé/CV and file upload.  No trigger is clicked during scanning.
    """

    try:
        containers = scope.locator('[role="listitem"]')
        count = min(await containers.count(), max(0, 150 - start_index))
    except Exception:
        return ()

    result: list[FormField] = []
    for picker_index in range(count):
        container = containers.nth(picker_index)
        try:
            if not await container.is_visible():
                continue
            text = (await container.inner_text(timeout=1_000))[:2_000]
            if not _has_file_picker_prompt(text):
                continue
            if await container.locator('input[type="file"]').count() > 0:
                # Native controls are already represented by ``_SCAN_SCRIPT``.
                continue
            label = _picker_label_from_text(text)
            if not label:
                # Keep ambiguous/non-résumé provider pickers in the structural
                # guard below; never invent a résumé destination from nearby text.
                continue
            normalized = normalize_key(label)
            result.append(
                FormField(
                    dom_index=start_index + len(result),
                    key=f"google_picker_{normalized}"[:300],
                    label=label,
                    kind="file",
                    # A required marker is rendered as a literal asterisk in the
                    # Google question copy. Attachment verification is mandatory
                    # even when the question itself is optional.
                    required="*" in text,
                    disabled=False,
                    answered=False,
                    # Google Forms accepts PDFs unless its visible question copy
                    # explicitly states a different file restriction. The worker
                    # validates the real picker input again before attaching.
                    accept="",
                    provider_picker=True,
                    picker_index=picker_index,
                )
            )
        except Exception:
            # The later structural guard will fail closed if an upload-looking
            # widget cannot be inspected consistently.
            continue
    return tuple(result)


async def scan_form(scope: Any, *, provider: str = "") -> FormSchema:
    """Capture controls inside one provider-approved application form root."""

    raw_fields = await scope.evaluate(_SCAN_SCRIPT)
    if not isinstance(raw_fields, list):
        raw_fields = []
    fields = tuple(field for raw in raw_fields if (field := _parse_field(raw)) is not None)
    if provider == "google_forms" and len(fields) < 150:
        fields += await _scan_google_provider_pickers(scope, start_index=len(fields))
    return _schema(fields)


async def resume_upload_guard_issue(
    scope: Any,
    schema: FormSchema,
) -> tuple[str, str] | None:
    """Validate the bounded native résumé-upload contract before approval/fill.

    AutoApply can safely attach the selected private PDF only to one native file
    input whose captured label/key explicitly says résumé or CV. Provider-owned
    pickers (including Google Drive pickers), a required unrelated file, multiple
    possible résumé inputs, and accept lists that exclude PDF stay manual. This
    inspection reads structure and visible labels only; it never reads file values.
    """

    file_fields = tuple(field for field in schema.fields if field.kind == "file")
    resume_fields = tuple(field for field in file_fields if field.is_resume_upload)
    picker_fields = tuple(field for field in file_fields if field.provider_picker)

    if len(resume_fields) > 1:
        return (
            "resume_upload_ambiguous",
            "More than one résumé or CV upload field was found. Attach the file in Live View so the destination is explicit.",
        )
    if resume_fields and not resume_fields[0].accepts_pdf:
        return (
            "resume_upload_unsupported",
            "The résumé upload control does not accept PDF files. Attach a supported file manually in Live View.",
        )
    if any(field.required and field not in resume_fields for field in file_fields):
        return (
            "required_file_upload_unsupported",
            "This form requires a non-résumé file. Add it manually in Live View before submission.",
        )

    supported_picker_index: int | None = None
    if picker_fields:
        if len(picker_fields) != 1 or len(resume_fields) != 1:
            return (
                "resume_upload_ambiguous",
                "The provider file picker could not be bound to exactly one résumé or CV question.",
            )
        picker = picker_fields[0]
        if picker.picker_index is None:
            return (
                "provider_file_picker_unsupported",
                "The résumé picker identity could not be verified safely. Complete the attachment in Live View.",
            )
        supported_picker_index = picker.picker_index
        try:
            container = scope.locator('[role="listitem"]').nth(supported_picker_index)
            if not await container.is_visible():
                raise ValueError("picker question is not visible")
            text = (await container.inner_text(timeout=1_000))[:2_000]
            label = _picker_label_from_text(text)
            if normalize_key(label) != normalize_key(picker.label):
                raise ValueError("picker question label changed")
            triggers = container.locator('[role="button"],button')
            exact_triggers = 0
            for trigger_index in range(min(await triggers.count(), 20)):
                trigger = triggers.nth(trigger_index)
                if not await trigger.is_visible() or not await trigger.is_enabled():
                    continue
                trigger_text = " ".join(
                    (await trigger.inner_text(timeout=1_000)).split()
                )
                if _GOOGLE_PICKER_TRIGGER.fullmatch(trigger_text):
                    exact_triggers += 1
            if exact_triggers != 1:
                raise ValueError("picker trigger is ambiguous")
        except Exception:
            return (
                "provider_file_picker_unsupported",
                "The Google résumé picker could not be identified unambiguously. Complete the attachment in Live View.",
            )

    if not file_fields:
        # A URL field labelled "Resume Link" is common on Google Forms and is
        # not an upload widget.  Avoid probing every dynamic question container
        # (which can detach while Google re-renders) unless the visible form copy
        # contains an actual upload/picker prompt.  If the root itself cannot be
        # inspected, retain the conservative container checks below.
        try:
            visible_form_text = (await scope.inner_text(timeout=2_000))[:100_000]
        except Exception:
            visible_form_text = ""
        if visible_form_text and not _has_file_picker_prompt(visible_form_text):
            return None

    # Some sites render a provider-owned file picker without exposing a native
    # input in the reviewed form. Never click that picker or attempt a nearby
    # input: it may require a separate login, Drive permission, or ambiguous
    # upload destination. A real native input in the same question is supported.
    try:
        containers = scope.locator(
            '[role="listitem"],fieldset,[data-file-upload],.file-upload'
        )
        for index in range(min(await containers.count(), 150)):
            container = containers.nth(index)
            try:
                if not await container.is_visible():
                    continue
                text = (await container.inner_text(timeout=1_000))[:2_000]
                if not _has_file_picker_prompt(text):
                    continue
                native = container.locator('input[type="file"]')
                if await native.count() == 0:
                    if supported_picker_index == index:
                        continue
                    return (
                        "provider_file_picker_unsupported",
                        "This form uses a provider-owned file picker instead of a native upload control. Complete its login or picker in Live View.",
                    )
            except Exception:
                return (
                    "file_upload_inspection_failed",
                    "The form's upload control could not be verified safely. Complete the attachment in Live View.",
                )
    except Exception:
        return (
            "file_upload_inspection_failed",
            "The form's upload controls could not be inspected safely. Complete the attachment in Live View.",
        )
    return None


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


async def _option_has_exact_value(option: Any, wanted: str) -> bool:
    """Match one visible ARIA option without fuzzy or substring guesses."""

    candidates: list[Any] = []
    for attribute in ("data-value", "aria-label", "value"):
        try:
            candidates.append(await option.get_attribute(attribute))
        except Exception:
            continue
    try:
        candidates.append(await option.inner_text())
    except Exception:
        pass
    return any(
        isinstance(candidate, str) and normalize_key(candidate) == wanted
        for candidate in candidates
    )


async def _visible_exact_options(scope: Any, wanted: str) -> list[Any]:
    try:
        options = scope.locator('[role="option"]')
        count = min(await options.count(), 100)
    except Exception:
        return []

    matches: list[Any] = []
    for index in range(count):
        option = options.nth(index)
        try:
            if await option.is_visible() and await _option_has_exact_value(option, wanted):
                matches.append(option)
        except Exception:
            continue
    return matches


async def _choice_selected_exactly(control: Any, option: Any, wanted: str) -> bool:
    """Verify the clicked choice through observable selected state/value."""

    try:
        if (
            await option.get_attribute("aria-selected") == "true"
            and await _option_has_exact_value(option, wanted)
        ):
            return True
    except Exception:
        pass

    # Google Forms keeps the selected option below the listbox after its popup
    # closes. Other ARIA widgets expose the chosen value on the control itself.
    try:
        selected = control.locator('[role="option"][aria-selected="true"]')
        for index in range(min(await selected.count(), 20)):
            if await _option_has_exact_value(selected.nth(index), wanted):
                return True
    except Exception:
        pass

    candidates: list[Any] = []
    for attribute in ("data-value", "aria-valuetext", "value"):
        try:
            candidates.append(await control.get_attribute(attribute))
        except Exception:
            continue
    try:
        candidates.append(await control.input_value())
    except Exception:
        pass
    try:
        candidates.append(await control.inner_text())
    except Exception:
        pass
    return any(
        isinstance(candidate, str) and normalize_key(candidate) == wanted
        for candidate in candidates
    )


async def _fill_aria_choice(
    page: Any,
    control: Any,
    answer: str,
    *,
    editable: bool,
) -> bool:
    wanted = normalize_key(answer)
    if not wanted:
        return False
    if await _choice_selected_exactly(control, control, wanted):
        return True

    if editable:
        try:
            await control.fill(answer)
        except Exception:
            # Some ARIA comboboxes are button-like widgets rather than inputs.
            try:
                await control.click()
            except Exception:
                return False
    else:
        try:
            await control.click()
        except Exception:
            return False
    try:
        await page.wait_for_timeout(250)
    except Exception:
        pass

    # Prefer options owned by the control. Portalled menus are searched on the
    # page only when the control itself exposes no exact candidate.
    matches = await _visible_exact_options(control, wanted)
    if not matches:
        matches = await _visible_exact_options(page, wanted)
    if len(matches) != 1:
        if editable:
            try:
                await control.fill("")
            except Exception:
                pass
        return False

    option = matches[0]
    try:
        await option.click()
    except Exception:
        return False
    for attempt in range(3):
        if await _choice_selected_exactly(control, option, wanted):
            return True
        if attempt < 2:
            try:
                await page.wait_for_timeout(100)
            except Exception:
                pass
    return False


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
        return await _fill_aria_choice(page, control, text, editable=True)
    if field.kind == "listbox":
        if is_many:
            return False
        return await _fill_aria_choice(page, control, text, editable=False)
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


async def _attach_resume_pdf(control: Any, resume_path: str) -> bool:
    """Attach one PDF and verify the native input's observable FileList."""

    try:
        expected_size = Path(resume_path).stat().st_size
    except (OSError, ValueError):
        expected_size = None
    await control.set_input_files(resume_path)
    try:
        evidence = await control.evaluate(
            """
            element => {
              const files = element && element.files ? Array.from(element.files) : [];
              if (files.length !== 1) return { count: files.length };
              const file = files[0];
              return {
                count: 1,
                name: String(file.name || '').slice(0, 255),
                type: String(file.type || '').slice(0, 100),
                size: Number(file.size || 0),
              };
            }
            """
        )
    except Exception:
        return False
    if not isinstance(evidence, Mapping) or evidence.get("count") != 1:
        return False
    name = evidence.get("name")
    mime_type = evidence.get("type")
    size = evidence.get("size")
    return bool(
        isinstance(name, str)
        and name.casefold().endswith(".pdf")
        and mime_type in {"application/pdf", "application/x-pdf"}
        and isinstance(size, (int, float))
        and not isinstance(size, bool)
        and size > 0
        and (expected_size is None or size == expected_size)
    )


async def _exact_google_picker_trigger(container: Any) -> Any | None:
    try:
        triggers = container.locator('[role="button"],button')
        matches: list[Any] = []
        for index in range(min(await triggers.count(), 20)):
            trigger = triggers.nth(index)
            if not await trigger.is_visible() or not await trigger.is_enabled():
                continue
            text = " ".join((await trigger.inner_text(timeout=1_000)).split())
            if _GOOGLE_PICKER_TRIGGER.fullmatch(text):
                matches.append(trigger)
        return matches[0] if len(matches) == 1 else None
    except Exception:
        return None


def _google_picker_frame_url_allowed(value: str, *, page_url: str) -> bool:
    try:
        parsed = urlsplit(urljoin(page_url, value))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").rstrip(".").lower() in _GOOGLE_PICKER_FRAME_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port is None
        and _GOOGLE_PICKER_FRAME_PATH.search(parsed.path or "/")
    )


async def _picker_filename_visible(scope: Any, filename: str) -> bool:
    try:
        body = scope.locator("body")
        text = await body.inner_text(timeout=1_000)
    except Exception:
        try:
            text = await scope.inner_text(timeout=1_000)
        except Exception:
            return False
    return filename.casefold() in text[:100_000].casefold()


async def _google_picker_closed_with_filename(
    page: Any,
    container: Any,
    filename: str,
) -> bool:
    for _ in range(40):
        try:
            frame_nodes = page.locator(
                'iframe.picker-frame,iframe[src*="/picker" i]'
            )
            visible_frames = 0
            for index in range(min(await frame_nodes.count(), 10)):
                if await frame_nodes.nth(index).is_visible():
                    visible_frames += 1
            if visible_frames == 0 and await _picker_filename_visible(container, filename):
                return True
        except Exception:
            pass
        try:
            await page.wait_for_timeout(500)
        except Exception:
            pass
    return False


async def _attach_google_resume_picker(
    page: Any,
    root: Any,
    field: FormField,
    resume_path: str,
) -> bool:
    """Attach one PDF through Google's picker and verify provider-visible state.

    This deliberately supports only the picker shape observed for a signed-in
    Google Forms file-upload question. It never enters credentials and never
    clicks the form's final Submit control. Any DOM/host/action ambiguity returns
    ``False`` so the managed run pauses before submission.
    """

    if field.picker_index is None:
        return False
    try:
        container = root.locator('[role="listitem"]').nth(field.picker_index)
        if not await container.is_visible():
            return False
        text = (await container.inner_text(timeout=1_000))[:2_000]
        if normalize_key(_picker_label_from_text(text)) != normalize_key(field.label):
            return False
        trigger = await _exact_google_picker_trigger(container)
        if trigger is None:
            return False
        await trigger.click()
        await page.wait_for_timeout(500)

        frame_nodes = page.locator(
            'iframe.picker-frame,iframe[src*="/picker" i]'
        )
        frames: list[Any] = []
        for index in range(min(await frame_nodes.count(), 10)):
            frame_node = frame_nodes.nth(index)
            if not await frame_node.is_visible():
                continue
            source = await frame_node.get_attribute("src")
            if not isinstance(source, str) or not _google_picker_frame_url_allowed(
                source, page_url=getattr(page, "url", "")
            ):
                return False
            frames.append(frame_node)
        if len(frames) != 1:
            return False

        picker = frames[0].content_frame
        file_inputs = picker.locator('input[type="file"]')
        if await file_inputs.count() != 1:
            return False
        file_input = file_inputs.nth(0)
        actual_accept = (await file_input.get_attribute("accept") or "").strip()
        if actual_accept:
            tokens = {
                item.strip().casefold()
                for item in actual_accept.split(",")
                if item.strip()
            }
            if not tokens & _PDF_ACCEPT_TOKENS:
                return False
        if not await _attach_resume_pdf(file_input, resume_path):
            return False

        filename = Path(resume_path).name
        # Some picker versions upload immediately. Prefer observing the picker
        # close and the exact filename appear in the original question.
        if await _google_picker_closed_with_filename(page, container, filename):
            return True

        if not await _picker_filename_visible(picker, filename):
            return False
        # Other picker versions require one explicit Upload/Select action after
        # the exact local file is observable. Restrict this to one exact action
        # inside the already host-validated picker iframe.
        action_nodes = picker.locator('[role="button"],button')
        actions: list[Any] = []
        for index in range(min(await action_nodes.count(), 30)):
            action = action_nodes.nth(index)
            if not await action.is_visible() or not await action.is_enabled():
                continue
            action_text = normalize_key(await action.inner_text(timeout=1_000))
            if action_text in {"upload", "select"}:
                actions.append(action)
        if len(actions) != 1:
            return False
        await actions[0].click()
        return await _google_picker_closed_with_filename(page, container, filename)
    except Exception:
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
    attempted_keys: set[str] = set()
    filled_count = 0
    for field in schema.fields:
        if field.disabled:
            continue
        try:
            if field.kind == "file":
                if resume_path and field.is_resume_upload and field.accepts_pdf:
                    attempted_keys.add(normalize_key(field.key))
                    attached = (
                        await _attach_google_resume_picker(
                            page, root or page, field, resume_path
                        )
                        if field.provider_picker
                        else await _attach_resume_pdf(
                            controls.nth(field.dom_index), resume_path
                        )
                    )
                    if attached:
                        filled_keys.add(normalize_key(field.key))
                        filled_count += 1
                continue
            control = controls.nth(field.dom_index)
            answer = _answer_for(field, approved)
            if answer is None:
                continue
            attempted_keys.add(normalize_key(field.key))
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
            (field.required or field.answered or required_key in attempted_keys)
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
        if self.provider == "yc":
            # YC's signed-in job page opens exactly one application dialog whose
            # legacy, observed contract is a message textarea plus one Send/Submit
            # control. Do not treat unrelated page forms or recommendation cards
            # as the application root.
            try:
                textareas = root.locator("textarea")
                visible_textareas = 0
                for index in range(min(await textareas.count(), 20)):
                    if await textareas.nth(index).is_visible():
                        visible_textareas += 1
                finals = root.locator(",".join(self.submit_selectors))
                visible_finals = 0
                for index in range(min(await finals.count(), 10)):
                    candidate = finals.nth(index)
                    if await candidate.is_visible() and await candidate.is_enabled():
                        visible_finals += 1
                return visible_textareas >= 1 and visible_finals == 1
            except Exception:
                return False
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
    "resume_upload_guard_issue",
    "safe_form_url",
    "scan_form",
]
