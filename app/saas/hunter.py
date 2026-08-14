"""Stateless Hunter API primitives for account checks and HR contact search.

The caller supplies a transient Hunter API key for each operation.  Hunter V2
supports header authentication, so this adapter deliberately uses ``X-API-Key``
instead of the documented ``api_key`` query parameter.  Keys are never logged,
cached, returned, or copied into URLs.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from numbers import Real
from typing import Any

import requests


HUNTER_API_BASE_URL = "https://api.hunter.io/v2"
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)
MAX_API_KEY_LENGTH = 512
MAX_CONTACTS = 10

_MAX_COMPANY_LENGTH = 200
_MAX_DOMAIN_LENGTH = 253
_MAX_EMAIL_LENGTH = 320
_MAX_NAME_LENGTH = 200
_MAX_POSITION_LENGTH = 240
_MAX_PLAN_NAME_LENGTH = 100
_MAX_RESET_DATE_LENGTH = 40
_MAX_QUOTA_VALUE = 1_000_000_000_000
_QUOTA_TYPES = ("credits", "searches", "verifications")
_QUOTA_FIELDS = ("used", "available", "remaining")
_VERIFICATION_STATUSES = frozenset({"valid", "accept_all", "unknown"})
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class HunterProviderError(RuntimeError):
    """A stable, secret-free Hunter failure suitable for API error mapping."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _clean_key(key: str) -> str:
    if not isinstance(key, str) or not key.strip():
        raise HunterProviderError("hunter_missing_key", "Enter a Hunter API key.")
    clean = key.strip()
    if (
        len(clean) > MAX_API_KEY_LENGTH
        or not clean.isascii()
        or any(ord(character) < 33 for character in clean)
    ):
        raise HunterProviderError("hunter_invalid_key", "The Hunter API key is invalid.")
    return clean


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key, "Accept": "application/json"}


def _status_error(status_code: int) -> HunterProviderError:
    if status_code == 401:
        return HunterProviderError(
            "hunter_invalid_key",
            "The Hunter API key was rejected.",
            status_code=status_code,
        )
    if status_code == 403:
        return HunterProviderError(
            "hunter_forbidden",
            "Hunter denied access for this API key.",
            status_code=status_code,
        )
    if status_code == 429:
        return HunterProviderError(
            "hunter_quota_exhausted",
            "The Hunter account has reached its usage limit.",
            status_code=status_code,
        )
    if status_code >= 500:
        return HunterProviderError(
            "hunter_unavailable",
            "Hunter is temporarily unavailable.",
            status_code=status_code,
        )
    return HunterProviderError(
        "hunter_request_failed",
        "Hunter rejected the request.",
        status_code=status_code,
    )


def _get(path: str, key: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Make one bounded Hunter request without exposing provider error bodies."""

    try:
        request_kwargs: dict[str, Any] = {
            "headers": _headers(key),
            "timeout": DEFAULT_TIMEOUT,
        }
        if params is not None:
            request_kwargs["params"] = dict(params)
        response = requests.get(f"{HUNTER_API_BASE_URL}{path}", **request_kwargs)
    except requests.Timeout:
        raise HunterProviderError("hunter_timeout", "Hunter timed out. Try again later.") from None
    except requests.RequestException:
        raise HunterProviderError(
            "hunter_unavailable",
            "Hunter could not be reached. Try again later.",
        ) from None

    if not 200 <= response.status_code < 300:
        raise _status_error(response.status_code)
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        raise HunterProviderError(
            "hunter_invalid_response",
            "Hunter returned an invalid response.",
        ) from None
    if not isinstance(payload, dict):
        raise HunterProviderError(
            "hunter_invalid_response",
            "Hunter returned an invalid response.",
        )
    return payload


def _normalized_text(value: Any, *, secret: str) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split()).strip()
    if not clean or secret.casefold() in clean.casefold():
        return None
    return clean


def _bounded_text(value: Any, limit: int, *, secret: str) -> str | None:
    clean = _normalized_text(value, secret=secret)
    if clean is None:
        return None
    return clean[:limit]


def _quota_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    numeric = min(max(numeric, 0.0), float(_MAX_QUOTA_VALUE))
    if numeric.is_integer():
        return int(numeric)
    return numeric


def _quota_summary(data: Mapping[str, Any], *, secret: str) -> dict[str, Any]:
    """Return only small, display-safe account and current-period usage fields."""

    summary: dict[str, Any] = {}
    plan_name = _bounded_text(data.get("plan_name"), _MAX_PLAN_NAME_LENGTH, secret=secret)
    reset_date = _bounded_text(data.get("reset_date"), _MAX_RESET_DATE_LENGTH, secret=secret)
    if plan_name is not None:
        summary["plan_name"] = plan_name
    if reset_date is not None:
        summary["reset_date"] = reset_date

    raw_requests = data.get("requests")
    requests_summary: dict[str, dict[str, int | float]] = {}
    if isinstance(raw_requests, Mapping):
        for request_type in _QUOTA_TYPES:
            raw_bucket = raw_requests.get(request_type)
            if not isinstance(raw_bucket, Mapping):
                continue
            bucket: dict[str, int | float] = {}
            for field in _QUOTA_FIELDS:
                number = _quota_number(raw_bucket.get(field))
                if number is not None:
                    bucket[field] = number
            if "remaining" not in bucket and "used" in bucket and "available" in bucket:
                bucket["remaining"] = max(0, bucket["available"] - bucket["used"])
            if bucket:
                requests_summary[request_type] = bucket
    summary["requests"] = requests_summary
    return summary


def validate_hunter_key(key: str) -> dict[str, Any]:
    """Validate a caller-held key using Hunter's free account endpoint.

    Both successful and failed results are safe to return to a settings screen.
    Account identity, provider error bodies, and the supplied key are omitted.
    """

    try:
        clean_key = _clean_key(key)
    except HunterProviderError as error:
        return {"valid": False, "status": error.code, "message": str(error)}

    try:
        payload = _get("/account", clean_key)
    except HunterProviderError as error:
        return {"valid": False, "status": error.code, "message": str(error)}

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return {
            "valid": False,
            "status": "hunter_invalid_response",
            "message": "Hunter returned invalid account data.",
        }
    return {
        "valid": True,
        "status": "ready",
        "quota": _quota_summary(data, secret=clean_key),
    }


def _clean_domain(value: Any, *, secret: str) -> str | None:
    clean = _normalized_text(value, secret=secret)
    if clean is None or len(clean) > _MAX_DOMAIN_LENGTH:
        return None
    clean = clean.rstrip(".").lower()
    if (
        not clean
        or any(character in clean for character in ("/", "\\", "@", "?", "#", ":"))
        or any(character.isspace() for character in clean)
    ):
        return None
    labels = clean.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        return None
    return clean


def _required_search_target(
    domain: str | None,
    company: str | None,
    *,
    secret: str,
) -> tuple[str, str]:
    if domain is not None and (not isinstance(domain, str) or not domain.strip()):
        raise HunterProviderError("hunter_invalid_request", "Hunter domain is invalid.")
    if domain is not None:
        clean_domain = _clean_domain(domain, secret=secret)
        if clean_domain is None:
            raise HunterProviderError("hunter_invalid_request", "Hunter domain is invalid.")
        return "domain", clean_domain

    clean_company = _normalized_text(company, secret=secret)
    if clean_company is None or len(clean_company) > _MAX_COMPANY_LENGTH:
        raise HunterProviderError(
            "hunter_invalid_request",
            "A company or domain is required for Hunter search.",
        )
    return "company", clean_company


def _contact_email(value: Any, *, secret: str) -> str | None:
    clean = _normalized_text(value, secret=secret)
    if clean is None or len(clean) > _MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.fullmatch(clean):
        return None
    return clean.lower()


def _confidence(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    if not math.isfinite(numeric):
        return 0
    numeric = min(max(numeric, 0.0), 100.0)
    return int(numeric) if numeric.is_integer() else numeric


def _verification_status(value: Any, *, secret: str) -> str:
    clean = _bounded_text(value, 32, secret=secret)
    if clean is None:
        return "unknown"
    normalized = clean.lower()
    return normalized if normalized in _VERIFICATION_STATUSES else "unknown"


def _contact(item: Mapping[str, Any], domain: str | None, *, secret: str) -> dict[str, Any] | None:
    email = _contact_email(item.get("value"), secret=secret)
    if email is None:
        return None

    first_name = _bounded_text(item.get("first_name"), _MAX_NAME_LENGTH, secret=secret)
    last_name = _bounded_text(item.get("last_name"), _MAX_NAME_LENGTH, secret=secret)
    combined_name = " ".join(part for part in (first_name, last_name) if part)
    if not combined_name:
        combined_name = _bounded_text(item.get("name"), _MAX_NAME_LENGTH, secret=secret) or ""
    combined_name = combined_name[:_MAX_NAME_LENGTH]

    position = _bounded_text(item.get("position"), _MAX_POSITION_LENGTH, secret=secret) or ""
    verification = item.get("verification")
    raw_status = verification.get("status") if isinstance(verification, Mapping) else None
    contact_domain = domain or _clean_domain(email.rsplit("@", 1)[1], secret=secret) or ""
    return {
        "email": email,
        "name": combined_name,
        "position": position,
        "confidence": _confidence(item.get("confidence")),
        "verification_status": _verification_status(raw_status, secret=secret),
        "domain": contact_domain,
    }


def search_hunter_contacts(
    key: str,
    *,
    domain: str | None = None,
    company: str | None = None,
    limit: int = MAX_CONTACTS,
) -> dict[str, Any]:
    """Find at most ten HR contacts for one Hunter-resolved company or domain."""

    clean_key = _clean_key(key)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CONTACTS:
        raise HunterProviderError(
            "hunter_invalid_request",
            "Hunter contact limit must be between 1 and 10.",
        )
    target_name, target_value = _required_search_target(domain, company, secret=clean_key)
    params: dict[str, str | int] = {
        target_name: target_value,
        "department": "hr",
        "limit": limit,
    }
    payload = _get("/domain-search", clean_key, params=params)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise HunterProviderError(
            "hunter_invalid_response",
            "Hunter returned invalid contact data.",
        )

    resolved_domain = _clean_domain(data.get("domain"), secret=clean_key)
    if resolved_domain is None and target_name == "domain":
        resolved_domain = target_value
    raw_contacts = data.get("emails")
    if not isinstance(raw_contacts, list):
        raise HunterProviderError(
            "hunter_invalid_response",
            "Hunter returned invalid contact data.",
        )

    contacts: list[dict[str, Any]] = []
    for item in raw_contacts:
        if not isinstance(item, Mapping):
            continue
        clean_contact = _contact(item, resolved_domain, secret=clean_key)
        if clean_contact is not None:
            contacts.append(clean_contact)
        if len(contacts) >= limit:
            break
    return {"domain": resolved_domain, "contacts": contacts}


__all__ = [
    "DEFAULT_TIMEOUT",
    "HUNTER_API_BASE_URL",
    "HunterProviderError",
    "search_hunter_contacts",
    "validate_hunter_key",
]
