"""Narrow Browserbase REST adapter for isolated user/provider contexts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests


BROWSERBASE_API_URL = "https://api.browserbase.com/v1"
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)
SESSION_STATUSES = frozenset({"PENDING", "RUNNING", "ERROR", "TIMED_OUT", "COMPLETED"})
_BROWSERBASE_CONNECT_HOST = "connect.browserbase.com"


class BrowserbaseError(RuntimeError):
    """A sanitized Browserbase failure safe to expose through the API layer."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class TrustedBrowserSession:
    """Worker-only session material.

    ``connect_url`` is a bearer credential for the remote browser, so it is
    deliberately omitted from representations.  API routes must continue to use
    :meth:`BrowserbaseClient.create_session`, whose result never contains it.
    """

    id: str
    context_id: str | None
    connect_url: str = field(repr=False)
    status: str | None = None
    expires_at: str | None = None


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrowserbaseError("browserbase_invalid_request", f"{label} is required.")
    return value.strip()


def _identifier(value: str, label: str) -> str:
    clean = _required(value, label)
    if len(clean) > 255 or any(character in clean for character in ("\r", "\n", "\x00")):
        raise BrowserbaseError("browserbase_invalid_request", f"{label} is invalid.")
    return clean


def _status_error(status_code: int) -> BrowserbaseError:
    if status_code in {401, 403}:
        return BrowserbaseError(
            "browserbase_not_authorized",
            "Managed browser service credentials were rejected.",
            status_code=status_code,
        )
    if status_code == 404:
        return BrowserbaseError(
            "browserbase_not_found",
            "The managed browser resource was not found.",
            status_code=status_code,
        )
    if status_code == 409:
        return BrowserbaseError(
            "browserbase_conflict",
            "The managed browser resource is currently in use.",
            status_code=status_code,
        )
    if status_code == 429:
        return BrowserbaseError(
            "browserbase_rate_limited",
            "The managed browser service is rate limiting requests.",
            status_code=status_code,
        )
    if status_code >= 500:
        return BrowserbaseError(
            "browserbase_unavailable",
            "The managed browser service is temporarily unavailable.",
            status_code=status_code,
        )
    return BrowserbaseError(
        "browserbase_request_failed",
        "The managed browser service rejected the request.",
        status_code=status_code,
    )


def _trusted_connect_url(value: Any) -> str:
    """Validate Browserbase's credential-bearing CDP endpoint for trusted workers."""

    if not isinstance(value, str) or not value or len(value) > 8192:
        raise BrowserbaseError(
            "browserbase_invalid_response",
            "The managed browser service returned an invalid worker connection.",
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BrowserbaseError(
            "browserbase_invalid_response",
            "The managed browser service returned an invalid worker connection.",
        ) from exc
    hostname = (parsed.hostname or "").lower()
    trusted_host = hostname == _BROWSERBASE_CONNECT_HOST or hostname.endswith(
        ".browserbase.com"
    )
    if (
        parsed.scheme != "wss"
        or not trusted_host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.query
        or parsed.fragment
    ):
        raise BrowserbaseError(
            "browserbase_invalid_response",
            "The managed browser service returned an invalid worker connection.",
        )
    return value


class BrowserbaseClient:
    """Create and release Browserbase contexts/sessions without leaking connect URLs."""

    def __init__(
        self,
        api_key: str,
        project_id: str,
        *,
        base_url: str = BROWSERBASE_API_URL,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        http: requests.Session | None = None,
    ) -> None:
        self._api_key = _required(api_key, "Browserbase API key")
        self.project_id = _required(project_id, "Browserbase project ID")
        self.base_url = _required(base_url, "Browserbase API URL").rstrip("/")
        if (
            not isinstance(timeout, tuple)
            or len(timeout) != 2
            or any(not isinstance(value, (int, float)) or value <= 0 for value in timeout)
        ):
            raise BrowserbaseError("browserbase_invalid_request", "Browserbase timeout is invalid.")
        self.timeout = (float(timeout[0]), float(timeout[1]))
        self._http = http or requests.Session()

    def validate_project(self) -> dict[str, Any]:
        """Verify that this key can access exactly the configured project.

        Project validation is a read-only request and therefore does not create a
        metered browser session.  Only a small allowlist is returned; provider
        account data and the API key never cross the adapter boundary.
        """

        project_id = _identifier(self.project_id, "Browserbase project ID")
        payload = self._request(
            "GET", f"/projects/{quote(project_id, safe='')}"
        )
        if payload.get("id") != project_id:
            raise BrowserbaseError(
                "browserbase_project_mismatch",
                "The Browserbase API key does not match the selected project.",
            )
        result: dict[str, Any] = {
            "valid": True,
            "status": "ready",
        }
        name = payload.get("name")
        if isinstance(name, str) and name and len(name) <= 200 and not any(
            character in name for character in ("\r", "\n", "\x00")
        ):
            result["project_name"] = name
        concurrency = payload.get("concurrency")
        if isinstance(concurrency, int) and not isinstance(concurrency, bool) and concurrency >= 0:
            result["concurrency"] = concurrency
        default_timeout = payload.get("defaultTimeout")
        if (
            isinstance(default_timeout, int)
            and not isinstance(default_timeout, bool)
            and default_timeout >= 0
        ):
            result["default_timeout"] = default_timeout
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        headers = {"X-BB-API-Key": self._api_key, "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self._http.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise BrowserbaseError(
                "browserbase_timeout",
                "The managed browser service timed out.",
            ) from exc
        except requests.RequestException as exc:
            raise BrowserbaseError(
                "browserbase_unavailable",
                "The managed browser service could not be reached.",
            ) from exc
        if not 200 <= response.status_code < 300:
            raise _status_error(response.status_code)
        if allow_empty and (response.status_code == 204 or not getattr(response, "content", b"")):
            return {}
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid response.",
            ) from exc
        if not isinstance(payload, dict):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid response.",
            )
        return payload

    def create_context(self) -> dict[str, str]:
        """Create an encrypted persistent context; return only its opaque ID."""

        payload = self._request("POST", "/contexts", json_body={"projectId": self.project_id})
        context_id = payload.get("id")
        if not isinstance(context_id, str) or not context_id:
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid context.",
            )
        return {"id": context_id}

    def _create_session_payload(
        self,
        context_id: str | None,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        user_metadata: Mapping[str, str] | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """Create a remote session and retain its response inside the adapter."""

        clean_context_id = (
            _identifier(context_id, "Browserbase context ID")
            if context_id is not None
            else None
        )
        if timeout_seconds is not None and (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 60 <= timeout_seconds <= 21_600
        ):
            raise BrowserbaseError(
                "browserbase_invalid_request",
                "Browserbase session timeout must be between 60 and 21600 seconds.",
            )
        body: dict[str, Any] = {
            "projectId": self.project_id,
            "keepAlive": bool(keep_alive),
        }
        if clean_context_id is not None:
            body["browserSettings"] = {
                "context": {"id": clean_context_id, "persist": True},
            }
        if timeout_seconds is not None:
            body["timeout"] = timeout_seconds
        if user_metadata is not None:
            if not isinstance(user_metadata, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in user_metadata.items()
            ):
                raise BrowserbaseError(
                    "browserbase_invalid_request",
                    "Browserbase user metadata must contain string keys and values.",
                )
            body["userMetadata"] = dict(user_metadata)

        payload = self._request("POST", "/sessions", json_body=body)
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid session.",
            )
        return clean_context_id, payload

    def create_session(
        self,
        context_id: str,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        user_metadata: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a session while keeping its CDP credential inside this adapter."""

        clean_context_id, payload = self._create_session_payload(
            context_id,
            keep_alive=keep_alive,
            timeout_seconds=timeout_seconds,
            user_metadata=user_metadata,
        )
        assert clean_context_id is not None
        session_id = payload["id"]
        result: dict[str, Any] = {"id": session_id, "context_id": clean_context_id}
        status = payload.get("status")
        if isinstance(status, str):
            result["status"] = status
        expires_at = payload.get("expiresAt")
        if isinstance(expires_at, str):
            result["expires_at"] = expires_at
        return result

    def create_session_for_worker(
        self,
        context_id: str,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        user_metadata: Mapping[str, str] | None = None,
    ) -> TrustedBrowserSession:
        """Create a session and return its CDP URL only to a trusted worker.

        This method is intentionally separate from the API-safe method above so a
        future serializer cannot accidentally expose ``connectUrl`` to a browser.
        """

        clean_context_id, payload = self._create_session_payload(
            context_id,
            keep_alive=keep_alive,
            timeout_seconds=timeout_seconds,
            user_metadata=user_metadata,
        )
        assert clean_context_id is not None
        status = payload.get("status")
        if status is not None and (not isinstance(status, str) or status not in SESSION_STATUSES):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid session.",
            )
        expires_at = payload.get("expiresAt")
        if expires_at is not None and (
            not isinstance(expires_at, str)
            or not expires_at
            or len(expires_at) > 64
            or any(character in expires_at for character in ("\r", "\n", "\x00"))
        ):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid session.",
            )
        return TrustedBrowserSession(
            id=payload["id"],
            context_id=clean_context_id,
            connect_url=_trusted_connect_url(payload.get("connectUrl")),
            status=status,
            expires_at=expires_at,
        )

    def create_ephemeral_session_for_worker(
        self,
        *,
        keep_alive: bool = False,
        timeout_seconds: int | None = None,
        user_metadata: Mapping[str, str] | None = None,
    ) -> TrustedBrowserSession:
        """Create a worker-only session without a persistent provider context."""

        clean_context_id, payload = self._create_session_payload(
            None,
            keep_alive=keep_alive,
            timeout_seconds=timeout_seconds,
            user_metadata=user_metadata,
        )
        assert clean_context_id is None
        status = payload.get("status")
        if status is not None and (not isinstance(status, str) or status not in SESSION_STATUSES):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid session.",
            )
        expires_at = payload.get("expiresAt")
        if expires_at is not None and (
            not isinstance(expires_at, str)
            or not expires_at
            or len(expires_at) > 64
            or any(character in expires_at for character in ("\r", "\n", "\x00"))
        ):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid session.",
            )
        return TrustedBrowserSession(
            id=payload["id"],
            context_id=None,
            connect_url=_trusted_connect_url(payload.get("connectUrl")),
            status=status,
            expires_at=expires_at,
        )

    def get_session(self, session_id: str) -> dict[str, str]:
        """Return a strictly allowlisted lifecycle summary for one session.

        Browserbase's raw response can contain browser connection URLs, a Selenium
        endpoint, a signing key, resource metrics, and arbitrary user metadata.
        None of those fields are needed by API routes that only verify lifecycle
        and context binding, so they must not cross this adapter boundary.
        """

        clean_session_id = _identifier(session_id, "Browserbase session ID")
        payload = self._request(
            "GET",
            f"/sessions/{quote(clean_session_id, safe='')}",
        )
        status = payload.get("status")
        if (
            payload.get("id") != clean_session_id
            or payload.get("projectId") != self.project_id
            or not isinstance(status, str)
            or status not in SESSION_STATUSES
        ):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid session.",
            )

        result = {"id": clean_session_id, "status": status}
        context_id = payload.get("contextId")
        if context_id is not None:
            if (
                not isinstance(context_id, str)
                or not context_id
                or len(context_id) > 255
                or any(character in context_id for character in ("\r", "\n", "\x00"))
            ):
                raise BrowserbaseError(
                    "browserbase_invalid_response",
                    "The managed browser service returned an invalid session.",
                )
            result["context_id"] = context_id

        for remote_name, local_name in (("expiresAt", "expires_at"), ("endedAt", "ended_at")):
            value = payload.get(remote_name)
            if value is None:
                continue
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 64
                or any(character in value for character in ("\r", "\n", "\x00"))
            ):
                raise BrowserbaseError(
                    "browserbase_invalid_response",
                    "The managed browser service returned an invalid session.",
                )
            result[local_name] = value
        return result

    def get_session_live_view(self, session_id: str) -> dict[str, str]:
        """Return embeddable Live View URLs, never the browser WebSocket URL."""

        clean_session_id = _identifier(session_id, "Browserbase session ID")
        payload = self._request(
            "GET",
            f"/sessions/{quote(clean_session_id, safe='')}/debug",
        )
        fullscreen_url = payload.get("debuggerFullscreenUrl")
        framed_url = payload.get("debuggerUrl")
        if not isinstance(fullscreen_url, str) or not fullscreen_url.startswith("https://"):
            raise BrowserbaseError(
                "browserbase_invalid_response",
                "The managed browser service returned an invalid Live View.",
            )
        result = {"session_id": clean_session_id, "live_view_url": fullscreen_url}
        if isinstance(framed_url, str) and framed_url.startswith("https://"):
            result["framed_live_view_url"] = framed_url
        return result

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """Request early release, treating a remote 404 as idempotent success."""

        clean_session_id = _identifier(session_id, "Browserbase session ID")
        try:
            payload = self._request(
                "POST",
                f"/sessions/{quote(clean_session_id, safe='')}",
                json_body={"status": "REQUEST_RELEASE", "projectId": self.project_id},
            )
        except BrowserbaseError as exc:
            if exc.status_code != 404:
                raise
            return {"id": clean_session_id, "released": True, "already_absent": True}
        result: dict[str, Any] = {
            "id": clean_session_id,
            "released": True,
            "already_absent": False,
        }
        status = payload.get("status")
        if isinstance(status, str) and status in SESSION_STATUSES:
            result["status"] = status
        return result

    # The REST operation is called "Update a Session" but release_session is a
    # clearer alias for callers that do not model it as deletion.
    release_session = delete_session

    def delete_context(self, context_id: str) -> dict[str, Any]:
        """Delete a context, treating a remote 404 as idempotent success."""

        clean_context_id = _identifier(context_id, "Browserbase context ID")
        try:
            self._request(
                "DELETE",
                f"/contexts/{quote(clean_context_id, safe='')}",
                allow_empty=True,
            )
        except BrowserbaseError as exc:
            if exc.status_code != 404:
                raise
            return {"id": clean_context_id, "deleted": True, "already_absent": True}
        return {"id": clean_context_id, "deleted": True, "already_absent": False}


__all__ = [
    "BROWSERBASE_API_URL",
    "SESSION_STATUSES",
    "BrowserbaseClient",
    "BrowserbaseError",
    "TrustedBrowserSession",
]
