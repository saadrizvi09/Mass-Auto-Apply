"""Supabase bearer-token verification for private FastAPI routes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .errors import ApiError

_bearer = HTTPBearer(auto_error=False)


def _auth_timestamp(value: Any) -> datetime | None:
    """Parse a trusted Auth timestamp without accepting ambiguous local time."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AuthUser:
    """Verified Supabase identity plus its request-scoped access token."""

    user_id: UUID
    email: str | None
    access_token: str = field(repr=False)
    last_sign_in_at: datetime | None = field(default=None, repr=False)
    user_metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    app_metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def id(self) -> UUID:
        """Compatibility alias for Supabase's ``id`` field."""

        return self.user_id

    @property
    def sub(self) -> str:
        return str(self.user_id)

    @property
    def jwt(self) -> str:
        return self.access_token


class SupabaseAuth:
    """Validate access JWTs against Supabase Auth's current-user endpoint.

    The optional HTTP client supports connection reuse and deterministic tests. It
    must not contain user-specific default headers.
    """

    def __init__(
        self, settings: Settings, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._url = settings.supabase_url.rstrip("/")
        self._publishable_key = settings.supabase_publishable_key
        self._timeout = float(settings.supabase_http_timeout_seconds)
        self._http_client = http_client

    async def authenticate(self, token: str) -> AuthUser:
        """Return a verified user or raise a sanitized :class:`ApiError`."""

        token = token.strip() if isinstance(token, str) else ""
        if (
            not token
            or len(token) > 16_384
            or not token.isascii()
            or re.fullmatch(r"[A-Za-z0-9._~-]+", token) is None
        ):
            raise ApiError(401, "auth_invalid", "The sign-in session is invalid or expired.")
        if not self._url or not self._publishable_key:
            raise ApiError(503, "auth_unavailable", "Authentication is temporarily unavailable.")

        headers = {
            "Accept": "application/json",
            "apikey": self._publishable_key,
            "Authorization": f"Bearer {token}",
        }
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    f"{self._url}/auth/v1/user", headers=headers, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=False
                ) as client:
                    response = await client.get(
                        f"{self._url}/auth/v1/user", headers=headers
                    )
        except httpx.HTTPError:
            raise ApiError(
                503, "auth_unavailable", "Authentication is temporarily unavailable."
            ) from None

        if response.status_code in {401, 403}:
            raise ApiError(401, "auth_invalid", "The sign-in session is invalid or expired.")
        if response.status_code != 200:
            raise ApiError(503, "auth_unavailable", "Authentication is temporarily unavailable.")
        try:
            payload = response.json()
            user_id = UUID(str(payload["id"]))
        except (KeyError, TypeError, ValueError):
            raise ApiError(503, "auth_unavailable", "Authentication is temporarily unavailable.") from None

        email = payload.get("email")
        return AuthUser(
            user_id=user_id,
            email=email if isinstance(email, str) else None,
            access_token=token,
            last_sign_in_at=_auth_timestamp(payload.get("last_sign_in_at")),
            user_metadata=(
                dict(payload["user_metadata"])
                if isinstance(payload.get("user_metadata"), dict)
                else {}
            ),
            app_metadata=(
                dict(payload["app_metadata"])
                if isinstance(payload.get("app_metadata"), dict)
                else {}
            ),
        )

    async def __call__(
        self,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> AuthUser:
        """Use an instance directly in ``Depends(auth)``."""

        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ApiError(
                401,
                "authentication_required",
                "A valid sign-in session is required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await self.authenticate(credentials.credentials)


async def authenticate_bearer(auth: SupabaseAuth, authorization: str | None) -> AuthUser:
    """Framework-neutral helper useful to workers/tests and custom middleware."""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "authentication_required", "A valid sign-in session is required.")
    return await auth.authenticate(authorization[7:])
