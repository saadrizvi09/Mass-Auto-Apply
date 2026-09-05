"""Environment-only configuration for stateless Vercel and worker processes."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.saas.providers import HOSTED_FORM_AUTOMATION_PROVIDERS


class SettingsError(ValueError):
    """Raised for invalid deployment configuration without exposing its value."""


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_VERCEL_ENVIRONMENTS = frozenset({"production", "preview", "development"})


def _integer(
    values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} is outside its supported range")
    return value


def _url(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip().rstrip("/")
    if not value:
        return ""
    parsed = urlparse(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if not parsed.hostname or parsed.scheme not in ({"http", "https"} if local else {"https"}):
        raise SettingsError(f"{name} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SettingsError(f"{name} must not contain credentials, a query, or a fragment")
    return value


def _is_loopback_url(value: str) -> bool:
    """Return whether an already-validated URL points at local development."""

    return urlparse(value).hostname in _LOOPBACK_HOSTS


def _vercel_origin(values: Mapping[str, str]) -> str:
    """Resolve Vercel's request origin when a deployment value was mis-copied.

    Vercel exposes host-only system variables, while local configuration normally
    uses a full URL.  Normalising the system value here keeps a copied local
    ``SITE_URL`` from leaking into OAuth redirects in a deployed function.
    Explicit non-loopback ``SITE_URL`` still wins over this fallback.
    """

    environment = values.get("VERCEL_ENV", "").strip().lower()
    if environment not in _VERCEL_ENVIRONMENTS:
        return ""
    names = (
        ("VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_URL")
        if environment == "production"
        else ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL")
    )
    for name in names:
        raw = values.get(name, "").strip()
        if not raw:
            continue
        candidate = raw if "://" in raw else f"https://{raw}"
        return _url({name: candidate}, name)
    return ""


def _valid_fernet_key(value: str) -> bool:
    if not value or not value.isascii():
        return False
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, TypeError, binascii.Error):
        return False
    return len(decoded) == 32


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed deployment settings; secret fields are omitted from ``repr``."""

    supabase_url: str = ""
    supabase_publishable_key: str = field(default="", repr=False)
    supabase_secret_key: str = field(default="", repr=False)
    site_url: str = ""
    token_encryption_key: str = field(default="", repr=False)
    google_client_id: str = ""
    google_client_secret: str = field(default="", repr=False)
    google_redirect_uri: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    max_resume_bytes: int = 6 * 1024 * 1024
    default_daily_send_cap: int = 150
    oauth_state_ttl_seconds: int = 600
    browserbase_api_key: str = field(default="", repr=False)
    browserbase_project_id: str = ""
    allowed_browser_providers: tuple[str, ...] = ()
    worker_id: str = "worker-1"
    worker_poll_seconds: int = 2
    supabase_http_timeout_seconds: int = 15
    turnstile_site_key: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build settings without reading local files or mutating process state."""

        values = os.environ if environ is None else environ
        site_url = _url(values, "SITE_URL")
        deployment_origin = _vercel_origin(values)
        if (not site_url or _is_loopback_url(site_url)) and deployment_origin:
            site_url = deployment_origin
        google_redirect = _url(values, "GOOGLE_REDIRECT_URI")
        if (not google_redirect or _is_loopback_url(google_redirect)) and site_url:
            google_redirect = f"{site_url}/api/v1/oauth/google/callback"
        allowed = tuple(
            dict.fromkeys(
                item.strip().lower()
                for item in values.get("ALLOWED_BROWSER_PROVIDERS", "").split(",")
                if item.strip()
            )
        )
        turnstile_site_key = values.get("TURNSTILE_SITE_KEY", "").strip()
        if turnstile_site_key and (
            len(turnstile_site_key) > 200
            or not turnstile_site_key.isascii()
            or any(character.isspace() or ord(character) < 33 for character in turnstile_site_key)
        ):
            raise SettingsError("TURNSTILE_SITE_KEY is invalid")
        return cls(
            supabase_url=_url(values, "SUPABASE_URL"),
            supabase_publishable_key=(
                values.get("SUPABASE_PUBLISHABLE_KEY")
                or values.get("SUPABASE_ANON_KEY")
                or ""
            ).strip(),
            supabase_secret_key=(
                values.get("SUPABASE_SECRET_KEY")
                or values.get("SUPABASE_SERVICE_ROLE_KEY")
                or ""
            ).strip(),
            site_url=site_url,
            token_encryption_key=values.get("TOKEN_ENCRYPTION_KEY", "").strip(),
            google_client_id=values.get("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=values.get("GOOGLE_CLIENT_SECRET", "").strip(),
            google_redirect_uri=google_redirect,
            groq_model=values.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
            or "openai/gpt-oss-120b",
            max_resume_bytes=_integer(
                values, "MAX_RESUME_BYTES", 6 * 1024 * 1024, 1, 6 * 1024 * 1024
            ),
            default_daily_send_cap=_integer(
                values, "DEFAULT_DAILY_SEND_CAP", 150, 0, 150
            ),
            oauth_state_ttl_seconds=_integer(
                values, "OAUTH_STATE_TTL_SECONDS", 600, 120, 1800
            ),
            browserbase_api_key=values.get("BROWSERBASE_API_KEY", "").strip(),
            browserbase_project_id=values.get("BROWSERBASE_PROJECT_ID", "").strip(),
            allowed_browser_providers=allowed,
            worker_id=values.get("WORKER_ID", "worker-1").strip() or "worker-1",
            worker_poll_seconds=_integer(values, "WORKER_POLL_SECONDS", 2, 1, 300),
            supabase_http_timeout_seconds=_integer(
                values, "SUPABASE_HTTP_TIMEOUT_SECONDS", 15, 1, 120
            ),
            turnstile_site_key=turnstile_site_key,
        )

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_publishable_key)

    @property
    def google_configured(self) -> bool:
        """Whether this deployment provides its own Google OAuth app."""

        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_redirect_uri
            and _valid_fernet_key(self.token_encryption_key)
        )

    @property
    def google_byoc_ready(self) -> bool:
        """Whether users can safely store and use their own Google OAuth app."""

        return bool(
            self.google_redirect_uri
            and self.secret_store_configured
            and _valid_fernet_key(self.token_encryption_key)
        )

    @property
    def gmail_connection_available(self) -> bool:
        """Whether at least one supported Gmail OAuth setup path is available."""

        return self.google_configured or self.google_byoc_ready

    @property
    def browserbase_configured(self) -> bool:
        return bool(self.browserbase_api_key and self.browserbase_project_id)

    @property
    def provider_credential_store_ready(self) -> bool:
        """Whether tenant BYOK values can be stored encrypted server-side."""

        return self.secret_store_configured and self.token_encryption_configured

    @property
    def managed_browser_available(self) -> bool:
        """Whether platform or tenant Browserbase credentials can run jobs."""

        return self.browserbase_configured or self.provider_credential_store_ready

    @property
    def secret_store_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_secret_key)

    @property
    def token_encryption_configured(self) -> bool:
        return _valid_fernet_key(self.token_encryption_key)

    def public_config(self) -> dict[str, object]:
        """Return only configuration explicitly safe to embed in the browser."""

        return {
            "supabase_url": self.supabase_url,
            "supabase_publishable_key": self.supabase_publishable_key,
            "site_url": self.site_url,
            "captcha": {
                "enabled": bool(self.turnstile_site_key),
                "provider": "turnstile" if self.turnstile_site_key else None,
                "site_key": self.turnstile_site_key or None,
            },
            "feature_flags": {
                "gmail": self.gmail_connection_available,
                "gmail_platform_oauth": self.google_configured,
                "gmail_user_oauth_clients": self.google_byoc_ready,
                "managed_browser": self.managed_browser_available
                and bool(self.allowed_browser_providers),
                "browserbase_byok": self.provider_credential_store_ready,
                "groq_byok": True,
                "groq_model": self.groq_model,
                "resume_upload": True,
                "resume_bucket": "resumes",
                "max_resume_bytes": self.max_resume_bytes,
                "credential_free_discovery": True,
                "linkedin_guest_discovery": True,
                "managed_application_review": self.managed_browser_available
                and bool(
                    HOSTED_FORM_AUTOMATION_PROVIDERS.intersection(
                        self.allowed_browser_providers
                    )
                ),
            },
        }

    def readiness(self) -> dict[str, bool]:
        """Return presence-only health flags; values never contain secrets."""

        return {
            "supabase": self.supabase_configured,
            "server_store": self.secret_store_configured,
            "token_encryption": self.token_encryption_configured,
            "captcha": bool(self.turnstile_site_key),
            "gmail": self.gmail_connection_available,
            "gmail_platform_oauth": self.google_configured,
            "gmail_user_oauth_clients": self.google_byoc_ready,
            "managed_browser": self.browserbase_configured,
            "browserbase_byok": self.provider_credential_store_ready,
        }

    def require_supabase(self) -> None:
        if not self.supabase_configured:
            raise SettingsError("Supabase public configuration is incomplete")

    def require_secret_store(self) -> None:
        if not self.secret_store_configured:
            raise SettingsError("Supabase server configuration is incomplete")


def get_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Return a fresh immutable settings object (safe for tests and warm functions)."""

    return Settings.from_env(environ)


load_settings = get_settings
