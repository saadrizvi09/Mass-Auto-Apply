"""Small async Supabase REST and Storage client with explicit auth modes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from .config import Settings
from .errors import ApiError

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_FILTER_OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "is"})
_SAFE_DATABASE_ERRORS: dict[str, tuple[int, str, str]] = {
    "application_not_found": (
        404,
        "application_not_found",
        "The requested application was not found.",
    ),
    "application_not_sendable": (
        409,
        "application_not_sendable",
        "The application must be reviewed and approved before sending.",
    ),
    "application_content_locked": (
        409,
        "application_content_locked",
        "Queued or sent message content cannot be changed.",
    ),
    "application_revision_conflict": (
        409,
        "application_revision_conflict",
        "The draft changed after it was displayed. Review the latest version.",
    ),
    "application_status_conflict": (
        409,
        "application_status_conflict",
        "The application changed state. Refresh it before continuing.",
    ),
    "application_channel_invalid": (
        409,
        "application_channel_invalid",
        "Only email applications can use the Gmail approval and send flow.",
    ),
    "automation_queue_full": (
        429,
        "automation_queue_full",
        "Too many automation jobs are already active. Wait for one to finish.",
    ),
    "automation_daily_limit_reached": (
        429,
        "automation_daily_limit_reached",
        "The daily automation job limit has been reached.",
    ),
    "contact_jobs_invalid": (
        422,
        "contact_jobs_invalid",
        "Select saved jobs from this workspace.",
    ),
    "contact_limits_invalid": (
        422,
        "contact_limits_invalid",
        "Contact discovery limits are outside the allowed range.",
    ),
    "provider_automation_unavailable": (
        409,
        "provider_automation_unavailable",
        "This provider supports a manual handoff only.",
    ),
    "yc_exact_job_url_required": (
        409,
        "yc_exact_job_url_required",
        "Save one exact YC job-detail URL before starting the application.",
    ),
    "yc_exact_job_url_changed": (
        409,
        "yc_exact_job_url_changed",
        "The saved YC job URL changed. Scan and review it again.",
    ),
    "yc_application_identity_required": (
        409,
        "yc_application_identity_required",
        "Scan this exact YC job before preparing or submitting its application.",
    ),
    "yc_application_identity_changed": (
        409,
        "yc_application_identity_changed",
        "YC resolved a different application for this job. Rebind the exact job and scan it again.",
    ),
    "yc_application_identity_invalid": (
        409,
        "yc_application_identity_invalid",
        "YC did not resolve a trusted application identity for this exact job.",
    ),
    "yc_application_identity_untrusted": (
        409,
        "yc_application_identity_untrusted",
        "The YC application identity must be established by a fresh job scan.",
    ),
    "yc_application_identity_immutable": (
        409,
        "yc_application_identity_immutable",
        "The YC application identity is sealed. Rebind the exact job before scanning again.",
    ),
    "browser_start_rate_limited": (
        429,
        "browser_start_rate_limited",
        "Managed-browser starts are temporarily limited. Try again later.",
    ),
    "browser_connection_operation_in_progress": (
        409,
        "browser_connection_operation_in_progress",
        "Managed-browser disconnect cleanup is in progress. Retry disconnecting before starting again.",
    ),
    "browser_connection_operation_stale": (
        409,
        "browser_connection_operation_stale",
        "The managed-browser connection changed. Refresh and try again.",
    ),
    "browser_connection_invalid": (
        422,
        "browser_connection_invalid",
        "The managed-browser connection request is invalid.",
    ),
    "provider_credential_invalid": (
        422,
        "provider_credential_invalid",
        "The provider credential is invalid.",
    ),
    "browserbase_jobs_active": (
        409,
        "browserbase_jobs_active",
        "Wait for active managed-browser jobs to finish before changing Browserbase credentials.",
    ),
    "browserbase_disconnect_required": (
        409,
        "browserbase_disconnect_required",
        "Disconnect saved browser logins before changing Browserbase credentials.",
    ),
    "browserbase_connection_operation_in_progress": (
        409,
        "browserbase_connection_operation_in_progress",
        "Wait for the managed-browser connection operation to finish before changing Browserbase credentials.",
    ),
    "browserbase_credential_binding_stale": (
        409,
        "browserbase_credential_binding_stale",
        "The saved browser context belongs to a different Browserbase credential. Abandon it locally if remote cleanup is no longer possible.",
    ),
    "browserbase_abandon_confirmation_invalid": (
        422,
        "browserbase_abandon_confirmation_invalid",
        "Confirm that unreachable remote Browserbase data should be abandoned.",
    ),
    "resume_parse_rate_limited": (
        429,
        "resume_parse_rate_limited",
        "Résumé parsing is temporarily limited. Try again shortly.",
    ),
    "resume_object_not_found": (
        404,
        "resume_object_not_found",
        "The uploaded résumé was not found.",
    ),
    "oauth_start_rate_limited": (
        429,
        "oauth_start_rate_limited",
        "Google connection attempts are temporarily limited. Try again later.",
    ),
    "connection_operation_in_progress": (
        409,
        "connection_operation_in_progress",
        "A Gmail disconnect is in progress. Retry disconnecting before reconnecting.",
    ),
    "connection_operation_stale": (
        409,
        "connection_operation_stale",
        "The Gmail connection changed. Refresh and try again.",
    ),
    "groq_request_rate_limited": (
        429,
        "groq_request_rate_limited",
        "Groq requests are temporarily limited. Try again later.",
    ),
    "tenant_row_quota_reached": (
        429,
        "tenant_row_quota_reached",
        "This workspace has reached its stored-item limit.",
    ),
    "google_refresh_token_required": (
        409,
        "google_refresh_token_required",
        "Google did not return offline access. Reconnect and approve access again.",
    ),
    "google_scope_missing": (
        409,
        "google_scope_missing",
        "The required Gmail send permission was not granted.",
    ),
    "google_account_already_connected": (
        409,
        "google_account_already_connected",
        "This Google account is already connected to another AutoApply account.",
    ),
    "account_deletion_in_progress": (
        409,
        "account_deletion_in_progress",
        "Account deletion is in progress. Retry deletion or contact support.",
    ),
    "account_operation_in_progress": (
        409,
        "account_operation_in_progress",
        "A Gmail send is still being resolved. Wait or reconcile it before deleting the account.",
    ),
    "account_automation_jobs_running": (
        409,
        "account_automation_jobs_running",
        "Account deletion requested cancellation of active work. Retry after the running worker stops.",
    ),
    "send_reservation_not_found": (
        404,
        "send_reservation_not_found",
        "No send reservation was found for this application.",
    ),
    "send_reconciliation_too_early": (
        409,
        "send_reconciliation_too_early",
        "Wait before resolving an unconfirmed send so the active request can finish.",
    ),
    "daily_send_cap_reached": (
        429,
        "daily_send_cap_reached",
        "Your daily send limit has been reached.",
    ),
    "provider_daily_send_cap_reached": (
        429,
        "provider_daily_send_cap_reached",
        "This Gmail account has reached the product send limit for the last 24 hours.",
    ),
    "gmail_not_connected": (
        409,
        "gmail_not_connected",
        "Connect Gmail before sending.",
    ),
    "gmail_send_in_progress": (
        409,
        "gmail_send_in_progress",
        "A Gmail send is still being resolved. Wait or reconcile it before changing the connection.",
    ),
    "gmail_disconnect_required": (
        409,
        "gmail_disconnect_required",
        "Disconnect Gmail before replacing or deleting its Google OAuth app.",
    ),
    "google_connection_must_disconnect": (
        409,
        "gmail_disconnect_required",
        "Disconnect Gmail before replacing or deleting its Google OAuth app.",
    ),
    "google_oauth_client_invalid": (
        422,
        "google_oauth_client_invalid",
        "The Google OAuth client credentials are invalid.",
    ),
    "google_oauth_client_not_configured": (
        409,
        "google_oauth_client_not_configured",
        "Save your Google OAuth app before connecting Gmail with it.",
    ),
    "google_oauth_client_not_found": (
        404,
        "google_oauth_client_not_configured",
        "No user-managed Google OAuth app is saved.",
    ),
    "google_oauth_client_stale": (
        409,
        "google_oauth_client_stale",
        "The Google OAuth app changed. Start the Gmail connection again.",
    ),
    "duplicate_recipient_window": (
        409,
        "duplicate_recipient_window",
        "A message was already reserved for this recipient recently.",
    ),
    "idempotency_key_conflict": (
        409,
        "idempotency_key_conflict",
        "This idempotency key was already used for another request.",
    ),
    "send_in_progress": (
        409,
        "send_in_progress",
        "This send is already in progress. Check its status before retrying.",
    ),
    "form_revision_not_found": (
        404,
        "form_revision_not_found",
        "The captured application form revision was not found.",
    ),
    "owned_form_revision_not_found": (
        404,
        "form_revision_not_found",
        "The captured application form revision was not found.",
    ),
    "job_not_found": (404, "job_not_found", "The requested job was not found."),
    "application_already_submitted": (
        409,
        "application_already_submitted",
        "This application is already recorded as submitted.",
    ),
    "application_operation_in_progress": (
        409,
        "application_operation_in_progress",
        "Another browser operation is already running for this application.",
    ),
    "form_approval_required": (
        409,
        "form_approval_required",
        "Review and approve the latest captured form revision before continuing.",
    ),
    "form_revision_stale": (
        409,
        "form_revision_stale",
        "The provider form changed. Scan and review its latest revision.",
    ),
    "form_revision_locked": (
        409,
        "form_revision_locked",
        "This form revision is being used by another browser operation.",
    ),
    "form_approval_sealed": (
        409,
        "form_approval_sealed",
        "This reviewed form revision is sealed. Run a new scan to change it.",
    ),
    "form_approval_immutable": (
        409,
        "form_approval_sealed",
        "This reviewed form revision is sealed. Run a new scan to change it.",
    ),
    "form_revision_limit_reached": (
        429,
        "form_revision_limit_reached",
        "This application has reached its captured-form revision limit.",
    ),
    "form_submission_resolution_invalid": (
        422,
        "form_submission_resolution_invalid",
        "Choose a valid outcome for this uncertain form submission.",
    ),
    "form_submission_resolution_stale": (
        409,
        "form_submission_resolution_stale",
        "This form changed while its submission outcome was being resolved. Refresh and review it again.",
    ),
    "form_submission_not_uncertain": (
        409,
        "form_submission_not_uncertain",
        "This form does not have an uncertain submission to resolve.",
    ),
    "form_submission_resolution_conflict": (
        409,
        "form_submission_resolution_conflict",
        "This submission was already resolved with a different outcome.",
    ),
    "form_submission_resolution_required": (
        409,
        "form_submission_resolution_required",
        "Verify the uncertain submission outcome before preparing this form again.",
    ),
    "form_submit_attempt_exists": (
        409,
        "form_submit_attempt_exists",
        "This approved form revision already has a submission attempt. Resolve its outcome before retrying.",
    ),
    "provider_connection_required": (
        409,
        "provider_connection_required",
        "Connect this provider before running the application workflow.",
    ),
    "active_resume_not_found": (
        409,
        "active_resume_required",
        "Upload and activate a résumé before continuing.",
    ),
    "resume_not_found": (
        409,
        "active_resume_required",
        "The résumé linked to this form revision is no longer available.",
    ),
    "form_approval_input_invalid": (
        422,
        "form_approval_invalid",
        "The reviewed form answers are invalid.",
    ),
    "form_scan_invalid": (
        422,
        "form_scan_invalid",
        "The captured provider form could not be validated.",
    ),
}

FilterScalar = str | int | float | bool | UUID | date | datetime | Decimal | None
FilterValue = FilterScalar | tuple[str, FilterScalar]


def _identifier(value: str, kind: str = "identifier") -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid Supabase {kind}")
    return value


def _scalar(value: FilterScalar) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _filter(value: FilterValue) -> str:
    if isinstance(value, tuple):
        if len(value) != 2 or value[0] not in _FILTER_OPERATORS:
            raise ValueError("Invalid Supabase filter operator")
        return f"{value[0]}.{_scalar(value[1])}"
    if value is None:
        return "is.null"
    return f"eq.{_scalar(value)}"


def _object_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\x00" in path:
        raise ValueError("Invalid Storage object path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Invalid Storage object path")
    return quote(path, safe="/")


def _bucket_name(value: str) -> str:
    if not isinstance(value, str) or not _BUCKET.fullmatch(value):
        raise ValueError("Invalid Storage bucket")
    return value


def _storage_prefix(value: str) -> str:
    if not isinstance(value, str) or len(value) > 1024 or value.startswith("/"):
        raise ValueError("Invalid Storage object prefix")
    if not value:
        return value
    parts = value.rstrip("/").split("/")
    if any(
        part in {"", ".", ".."} or any(ord(character) < 32 for character in part)
        for part in parts
    ):
        raise ValueError("Invalid Storage object prefix")
    return "/".join(parts)


@dataclass(slots=True, repr=False)
class StoreClient:
    """A request-scoped Supabase client bound to a user JWT or server secret."""

    _base_url: str
    _api_key: str = field(repr=False)
    _authorization: str | None = field(repr=False)
    _mode: str
    _timeout: float
    _http_client: httpx.AsyncClient | None = field(default=None, repr=False)

    @property
    def mode(self) -> str:
        return self._mode

    def __repr__(self) -> str:
        return f"StoreClient(mode={self._mode!r})"

    def _headers(self, extras: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "apikey": self._api_key,
            **dict(extras or {}),
        }
        if self._authorization:
            headers["Authorization"] = f"Bearer {self._authorization}"
        return headers

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._http_client is not None:
                return await self._http_client.request(
                    method, url, timeout=self._timeout, **kwargs
                )
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False
            ) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise ApiError(
                503, "data_store_unavailable", "The data service is temporarily unavailable."
            ) from None

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        try:
            error_payload = response.json()
            safe_error = (
                _SAFE_DATABASE_ERRORS.get(error_payload.get("message", ""))
                if isinstance(error_payload, dict)
                else None
            )
        except ValueError:
            safe_error = None
        if safe_error is not None:
            raise ApiError(*safe_error)
        status_map = {
            400: (400, "data_invalid", "The data request is invalid."),
            401: (401, "auth_invalid", "The sign-in session is invalid or expired."),
            403: (403, "data_forbidden", "Access to this data is not permitted."),
            404: (404, "not_found", "The requested resource was not found."),
            406: (404, "not_found", "The requested resource was not found."),
            409: (409, "data_conflict", "The data conflicts with an existing record."),
            413: (413, "request_too_large", "The uploaded object is too large."),
            422: (422, "data_invalid", "The data request is invalid."),
            429: (429, "data_rate_limited", "The data service rate limit was reached."),
        }
        status, code, message = status_map.get(
            response.status_code,
            (503, "data_store_unavailable", "The data service is temporarily unavailable."),
        )
        raise ApiError(status, code, message)

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ApiError(
                503, "data_store_invalid_response", "The data service returned an invalid response."
            ) from None

    async def fetch_many(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Mapping[str, FilterValue] | None = None,
        order: str | None = None,
        limit: int | None = 100,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Select a bounded list. Plain filter values are encoded as ``eq``."""

        table = _identifier(table, "table")
        params: dict[str, str | int] = {"select": columns}
        for name, value in (filters or {}).items():
            params[_identifier(name, "column")] = _filter(value)
        if order:
            params["order"] = order
        if limit is not None:
            if not 0 <= limit <= 1000:
                raise ValueError("Supabase list limit must be between 0 and 1000")
            params["limit"] = limit
        if offset is not None:
            if offset < 0:
                raise ValueError("Supabase list offset cannot be negative")
            params["offset"] = offset
        response = await self._send(
            "GET",
            f"{self._base_url}/rest/v1/{table}",
            headers=self._headers(),
            params=params,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise ApiError(503, "data_store_invalid_response", "The data service returned an invalid response.")
        return payload

    async def fetch_one(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Mapping[str, FilterValue] | None = None,
        required: bool = False,
    ) -> dict[str, Any] | None:
        """Select at most one record without relying on PostgREST 406 semantics."""

        rows = await self.fetch_many(
            table, columns=columns, filters=filters, limit=1
        )
        if rows:
            return rows[0]
        if required:
            raise ApiError(404, "not_found", "The requested resource was not found.")
        return None

    async def insert(
        self,
        table: str,
        values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        returning: bool = True,
    ) -> Any:
        table = _identifier(table, "table")
        prefer = "return=representation" if returning else "return=minimal"
        response = await self._send(
            "POST",
            f"{self._base_url}/rest/v1/{table}",
            headers=self._headers({"Content-Type": "application/json", "Prefer": prefer}),
            json=values,
        )
        self._raise_for_status(response)
        return self._json(response)

    async def upsert(
        self,
        table: str,
        values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        on_conflict: str | None = None,
        ignore_duplicates: bool = False,
        returning: bool = True,
    ) -> Any:
        table = _identifier(table, "table")
        resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
        prefer = f"resolution={resolution},return={'representation' if returning else 'minimal'}"
        params = {"on_conflict": on_conflict} if on_conflict else None
        response = await self._send(
            "POST",
            f"{self._base_url}/rest/v1/{table}",
            headers=self._headers({"Content-Type": "application/json", "Prefer": prefer}),
            params=params,
            json=values,
        )
        self._raise_for_status(response)
        return self._json(response)

    async def update(
        self,
        table: str,
        values: Mapping[str, Any],
        *,
        filters: Mapping[str, FilterValue],
        returning: bool = True,
    ) -> Any:
        table = _identifier(table, "table")
        if not filters:
            raise ValueError("Updates require at least one filter")
        params = {_identifier(k, "column"): _filter(v) for k, v in filters.items()}
        prefer = "return=representation" if returning else "return=minimal"
        response = await self._send(
            "PATCH",
            f"{self._base_url}/rest/v1/{table}",
            headers=self._headers({"Content-Type": "application/json", "Prefer": prefer}),
            params=params,
            json=values,
        )
        self._raise_for_status(response)
        return self._json(response)

    async def delete(
        self,
        table: str,
        *,
        filters: Mapping[str, FilterValue],
        returning: bool = True,
    ) -> Any:
        table = _identifier(table, "table")
        if not filters:
            raise ValueError("Deletes require at least one filter")
        params = {_identifier(k, "column"): _filter(v) for k, v in filters.items()}
        prefer = "return=representation" if returning else "return=minimal"
        response = await self._send(
            "DELETE",
            f"{self._base_url}/rest/v1/{table}",
            headers=self._headers({"Prefer": prefer}),
            params=params,
        )
        self._raise_for_status(response)
        return self._json(response)

    async def rpc(self, function: str, params: Mapping[str, Any] | None = None) -> Any:
        """Call a PostgREST RPC using the names from the SQL function signature."""

        function = _identifier(function, "function")
        response = await self._send(
            "POST",
            f"{self._base_url}/rest/v1/rpc/{function}",
            headers=self._headers({"Content-Type": "application/json"}),
            json=dict(params or {}),
        )
        self._raise_for_status(response)
        return self._json(response)

    async def upload_object(
        self,
        bucket: str,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        upsert: bool = False,
        cache_control: str | None = None,
    ) -> Any:
        bucket = _bucket_name(bucket)
        headers = {
            "Content-Type": content_type,
            "x-upsert": "true" if upsert else "false",
        }
        if cache_control:
            headers["Cache-Control"] = cache_control
        response = await self._send(
            "POST",
            f"{self._base_url}/storage/v1/object/{bucket}/{_object_path(path)}",
            headers=self._headers(headers),
            content=data,
        )
        self._raise_for_status(response)
        return self._json(response)

    async def download_object(self, bucket: str, path: str) -> bytes:
        bucket = _bucket_name(bucket)
        response = await self._send(
            "GET",
            f"{self._base_url}/storage/v1/object/{bucket}/{_object_path(path)}",
            headers=self._headers({"Accept": "application/octet-stream"}),
        )
        self._raise_for_status(response)
        return response.content

    async def delete_objects(self, bucket: str, paths: Sequence[str]) -> Any:
        bucket = _bucket_name(bucket)
        if not paths:
            return []
        safe_paths = [path for path in paths]
        for path in safe_paths:
            _object_path(path)
        response = await self._send(
            "DELETE",
            f"{self._base_url}/storage/v1/object/{bucket}",
            headers=self._headers({"Content-Type": "application/json"}),
            json={"prefixes": safe_paths},
        )
        self._raise_for_status(response)
        return self._json(response)

    async def list_objects(
        self,
        bucket: str,
        *,
        prefix: str = "",
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """List one Storage folder using bounded, deterministic pagination."""

        bucket = _bucket_name(bucket)
        prefix = _storage_prefix(prefix)
        if not 1 <= limit <= 1000:
            raise ValueError("Storage list limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("Storage list offset cannot be negative")
        body: dict[str, Any] = {
            "prefix": prefix,
            "limit": limit,
            "offset": offset,
            "sortBy": {"column": "name", "order": "asc"},
        }
        if search is not None:
            if not isinstance(search, str) or len(search) > 256:
                raise ValueError("Invalid Storage search value")
            body["search"] = search
        response = await self._send(
            "POST",
            f"{self._base_url}/storage/v1/object/list/{bucket}",
            headers=self._headers({"Content-Type": "application/json"}),
            json=body,
        )
        self._raise_for_status(response)
        payload = self._json(response)
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ApiError(
                503,
                "data_store_invalid_response",
                "The data service returned an invalid response.",
            )
        return payload

    async def delete_object(self, bucket: str, path: str) -> Any:
        return await self.delete_objects(bucket, [path])

    async def object_exists(self, bucket: str, path: str) -> bool:
        bucket = _bucket_name(bucket)
        response = await self._send(
            "HEAD",
            f"{self._base_url}/storage/v1/object/{bucket}/{_object_path(path)}",
            headers=self._headers({"Accept": "application/octet-stream"}),
        )
        if response.status_code == 404:
            return False
        self._raise_for_status(response)
        return True

    async def delete_auth_user(
        self, user_id: UUID | str, *, should_soft_delete: bool = False
    ) -> None:
        """Permanently or softly delete one Supabase Auth user in secret mode."""

        if self._mode != "secret":
            raise ApiError(
                403,
                "admin_operation_forbidden",
                "This server operation is not permitted for a user session.",
            )
        try:
            clean_user_id = str(UUID(str(user_id)))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid Auth user identifier") from None
        response = await self._send(
            "DELETE",
            f"{self._base_url}/auth/v1/admin/users/{clean_user_id}",
            headers=self._headers({"Content-Type": "application/json"}),
            json={"should_soft_delete": bool(should_soft_delete)},
        )
        if response.status_code == 404:
            return
        self._raise_for_status(response)

    select = fetch_many


class SupabaseStore:
    """Factory that makes authentication mode explicit at every call site."""

    def __init__(
        self, settings: Settings, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._url = settings.supabase_url.rstrip("/")
        self._publishable_key = settings.supabase_publishable_key
        self._secret_key = settings.supabase_secret_key
        self._timeout = float(settings.supabase_http_timeout_seconds)
        self._http_client = http_client

    def user(self, access_token: str) -> StoreClient:
        """Bind operations to a verified user's JWT so Postgres RLS remains active."""

        if not self._url or not self._publishable_key:
            raise ApiError(503, "data_store_unavailable", "The data service is not configured.")
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or not access_token.strip().isascii()
            or any(character.isspace() for character in access_token.strip())
        ):
            raise ApiError(401, "authentication_required", "A valid sign-in session is required.")
        return StoreClient(
            self._url,
            self._publishable_key,
            access_token.strip(),
            "user",
            self._timeout,
            self._http_client,
        )

    def secret(self) -> StoreClient:
        """Bind operations to the server secret; never expose this client to a browser."""

        if not self._url or not self._secret_key:
            raise ApiError(503, "data_store_unavailable", "The server data service is not configured.")
        # New opaque `sb_secret_...` keys authenticate through `apikey` and are not
        # JWTs. Legacy service-role JWTs still require the Bearer header.
        authorization = None if self._secret_key.startswith("sb_secret_") else self._secret_key
        return StoreClient(
            self._url,
            self._secret_key,
            authorization,
            "secret",
            self._timeout,
            self._http_client,
        )

    as_user = user
    as_secret = secret
