"""Google OAuth and Gmail sending primitives for the hosted application.

Token persistence and encryption intentionally live outside this adapter.  Returned
token dictionaries are allowlisted so Google error payloads are never propagated.
"""
from __future__ import annotations

import base64
import os
import re
from collections.abc import Sequence
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote, urlencode

import requests


GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
DEFAULT_GOOGLE_SCOPES: tuple[str, ...] = ("openid", "email", "profile", GMAIL_SEND_SCOPE)
DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)
MAX_PDF_BYTES = 6_291_456


class GoogleProviderError(RuntimeError):
    """A secret-free Google provider error with a stable machine code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoogleProviderError("google_invalid_request", f"{label} is required.")
    return value.strip()


def _json_object(response: requests.Response, code: str, message: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError) as exc:
        raise GoogleProviderError(code, message) from exc
    if not isinstance(payload, dict):
        raise GoogleProviderError(code, message)
    return payload


def _oauth_status_error(status_code: int) -> GoogleProviderError:
    if status_code in {400, 401, 403}:
        return GoogleProviderError(
            "google_oauth_rejected",
            "Google rejected the authorization request. Reconnect the Gmail account.",
        )
    if status_code == 429:
        return GoogleProviderError("google_rate_limited", "Google is rate limiting requests. Try again later.")
    if status_code >= 500:
        return GoogleProviderError("google_unavailable", "Google is temporarily unavailable.")
    return GoogleProviderError("google_request_failed", "Google could not complete the request.")


def build_google_authorization_url(
    client_id: str,
    redirect_uri: str,
    state: str,
    *,
    scopes: Sequence[str] | None = None,
    code_challenge: str | None = None,
    login_hint: str | None = None,
) -> str:
    """Build the web-server OAuth URL for Gmail send access."""

    selected_scopes = tuple(scopes or DEFAULT_GOOGLE_SCOPES)
    if not selected_scopes or any(not isinstance(scope, str) or not scope.strip() for scope in selected_scopes):
        raise GoogleProviderError("google_invalid_request", "At least one Google OAuth scope is required.")
    params: dict[str, str] = {
        "client_id": _required(client_id, "Google client ID"),
        "redirect_uri": _required(redirect_uri, "Google redirect URI"),
        "state": _required(state, "OAuth state"),
        "response_type": "code",
        "scope": " ".join(scope.strip() for scope in selected_scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        # Google may return a refresh token only on consent; the stored token is
        # subsequently preserved by refresh_google_access_token.
        "prompt": "consent",
    }
    if code_challenge:
        params["code_challenge"] = _required(code_challenge, "PKCE code challenge")
        params["code_challenge_method"] = "S256"
    if login_hint:
        params["login_hint"] = _required(login_hint, "Google login hint")
    return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode(params)}"


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    try:
        response = requests.post(
            GOOGLE_TOKEN_URL,
            data=form,
            headers={"Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise GoogleProviderError("google_oauth_timeout", "Google authorization timed out.") from exc
    except requests.RequestException as exc:
        raise GoogleProviderError("google_unavailable", "Google could not be reached.") from exc
    if not 200 <= response.status_code < 300:
        raise _oauth_status_error(response.status_code)
    payload = _json_object(response, "google_invalid_response", "Google returned an invalid token response.")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GoogleProviderError("google_invalid_response", "Google returned an invalid token response.")

    result: dict[str, Any] = {"access_token": access_token}
    for field in ("refresh_token", "token_type", "scope", "id_token"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            result[field] = value
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in >= 0:
        result["expires_in"] = expires_in
    return result


def exchange_google_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    *,
    code_verifier: str | None = None,
) -> dict[str, Any]:
    """Exchange a single-use authorization code for allowlisted token fields."""

    form = {
        "code": _required(code, "Google authorization code"),
        "client_id": _required(client_id, "Google client ID"),
        "client_secret": _required(client_secret, "Google client secret"),
        "redirect_uri": _required(redirect_uri, "Google redirect URI"),
        "grant_type": "authorization_code",
    }
    if code_verifier:
        form["code_verifier"] = _required(code_verifier, "PKCE code verifier")
    return _token_request(form)


def refresh_google_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Refresh access and preserve the stored refresh token when Google omits it."""

    existing_refresh_token = _required(refresh_token, "Google refresh token")
    result = _token_request(
        {
            "refresh_token": existing_refresh_token,
            "client_id": _required(client_id, "Google client ID"),
            "client_secret": _required(client_secret, "Google client secret"),
            "grant_type": "refresh_token",
        }
    )
    result.setdefault("refresh_token", existing_refresh_token)
    return result


def get_google_userinfo(access_token: str) -> dict[str, Any]:
    """Return only the account fields used to display a Gmail connection."""

    token = _required(access_token, "Google access token")
    try:
        response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise GoogleProviderError("google_timeout", "Google account lookup timed out.") from exc
    except requests.RequestException as exc:
        raise GoogleProviderError("google_unavailable", "Google could not be reached.") from exc
    if not 200 <= response.status_code < 300:
        raise _oauth_status_error(response.status_code)
    payload = _json_object(response, "google_invalid_response", "Google returned invalid account data.")
    result: dict[str, Any] = {}
    for field in ("sub", "email", "name", "given_name", "family_name", "picture", "locale"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            result[field] = value
    if isinstance(payload.get("email_verified"), bool):
        result["email_verified"] = payload["email_verified"]
    if not isinstance(result.get("sub"), str) or not isinstance(result.get("email"), str):
        raise GoogleProviderError("google_invalid_response", "Google returned invalid account data.")
    return result


def revoke_google_token(token: str) -> dict[str, bool]:
    """Revoke an access or refresh token without echoing it back."""

    clean_token = _required(token, "Google token")
    try:
        response = requests.post(
            GOOGLE_REVOKE_URL,
            data={"token": clean_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise GoogleProviderError("google_revoke_timeout", "Google token revocation timed out.") from exc
    except requests.RequestException as exc:
        raise GoogleProviderError("google_unavailable", "Google could not be reached.") from exc
    if not 200 <= response.status_code < 300:
        raise _oauth_status_error(response.status_code)
    return {"revoked": True}


def _safe_header(value: str, label: str) -> str:
    clean = _required(value, label)
    if "\r" in clean or "\n" in clean:
        raise GoogleProviderError("gmail_invalid_message", f"{label} contains an invalid line break.")
    return clean


def _recipient_header(value: str | Sequence[str], label: str) -> str:
    """Return exactly one RFC mailbox.

    AutoApply reserves, reviews, and accounts for one recipient per send.  Keeping
    this invariant at the provider boundary prevents a directly-written database
    value from turning one reservation into a multi-recipient fan-out.
    """

    raw_values = [value] if isinstance(value, str) else list(value)
    if len(raw_values) != 1:
        raise GoogleProviderError("gmail_invalid_recipient", f"{label} is required.")
    cleaned = [_safe_header(item, label) for item in raw_values if isinstance(item, str)]
    if len(cleaned) != len(raw_values):
        raise GoogleProviderError("gmail_invalid_recipient", f"{label} contains an invalid address.")
    parsed = getaddresses(cleaned)
    if (
        len(parsed) != 1
        or not parsed[0][1]
        or not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", parsed[0][1])
    ):
        raise GoogleProviderError("gmail_invalid_recipient", f"{label} contains an invalid address.")
    return cleaned[0]


def _attachment_filename(filename: str) -> str:
    clean = _safe_header(filename, "Attachment filename")
    clean = os.path.basename(clean.replace("\\", "/"))[:180]
    if not clean or clean in {".", ".."}:
        clean = "resume.pdf"
    if not clean.lower().endswith(".pdf"):
        clean += ".pdf"
    return clean


def build_gmail_mime(
    to: str | Sequence[str],
    subject: str,
    body: str,
    *,
    sender: str | None = None,
    cc: str | Sequence[str] | None = None,
    pdf_bytes: bytes | None = None,
    pdf_filename: str = "resume.pdf",
) -> bytes:
    """Construct an RFC-compliant plain-text message with an optional PDF."""

    if not isinstance(body, str) or not body.strip():
        raise GoogleProviderError("gmail_invalid_message", "Email body is required.")
    message = EmailMessage(policy=SMTP)
    message["To"] = _recipient_header(to, "Recipient")
    message["Subject"] = _safe_header(subject, "Email subject")
    if sender is not None:
        message["From"] = _recipient_header(sender, "Sender")
    if cc is not None:
        message["Cc"] = _recipient_header(cc, "Cc recipient")
    message.set_content(body)

    if pdf_bytes is not None:
        if not isinstance(pdf_bytes, bytes):
            raise GoogleProviderError("gmail_invalid_attachment", "The résumé attachment must be PDF bytes.")
        if not pdf_bytes or len(pdf_bytes) > MAX_PDF_BYTES:
            raise GoogleProviderError(
                "gmail_invalid_attachment",
                "The résumé attachment must be a non-empty PDF no larger than 6 MB.",
            )
        message.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=_attachment_filename(pdf_filename),
        )
    return message.as_bytes()


def send_gmail_message(
    access_token: str,
    to: str | Sequence[str],
    subject: str,
    body: str,
    *,
    sender: str | None = None,
    cc: str | Sequence[str] | None = None,
    pdf_bytes: bytes | None = None,
    pdf_filename: str = "resume.pdf",
    user_id: str = "me",
) -> dict[str, Any]:
    """Send one MIME message and return only stable Gmail identifiers."""

    token = _required(access_token, "Google access token")
    mime_bytes = build_gmail_mime(
        to,
        subject,
        body,
        sender=sender,
        cc=cc,
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_filename,
    )
    encoded = base64.urlsafe_b64encode(mime_bytes).decode("ascii")
    url = f"{GMAIL_API_BASE_URL}/users/{quote(_required(user_id, 'Gmail user ID'), safe='@')}/messages/send"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"raw": encoded},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        # Once dispatch has started, connection resets and timeouts cannot prove that
        # Gmail did not accept the MIME body. Never label these safe-to-retry.
        raise GoogleProviderError(
            "gmail_send_ambiguous",
            "Gmail did not confirm the send. Check message status before retrying.",
        ) from exc
    if not 200 <= response.status_code < 300:
        if response.status_code in {401, 403}:
            raise GoogleProviderError("gmail_reauthorization_required", "Reconnect Gmail before sending.")
        if response.status_code == 429:
            raise GoogleProviderError("gmail_rate_limited", "Gmail is rate limiting sends. Try again later.")
        if response.status_code >= 500:
            raise GoogleProviderError(
                "gmail_send_ambiguous",
                "Gmail did not confirm the send. Check message status before retrying.",
            )
        raise GoogleProviderError("gmail_send_failed", "Gmail rejected the message.")

    try:
        payload = _json_object(
            response, "gmail_invalid_response", "Gmail returned an invalid send response."
        )
    except GoogleProviderError as exc:
        raise GoogleProviderError(
            "gmail_send_ambiguous",
            "Gmail accepted the request but did not return a usable confirmation.",
        ) from exc
    message_id = payload.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise GoogleProviderError(
            "gmail_send_ambiguous",
            "Gmail accepted the request but did not return a usable confirmation.",
        )
    result: dict[str, Any] = {"id": message_id}
    thread_id = payload.get("threadId")
    if isinstance(thread_id, str) and thread_id:
        result["thread_id"] = thread_id
    label_ids = payload.get("labelIds")
    if isinstance(label_ids, list):
        result["label_ids"] = [item for item in label_ids if isinstance(item, str)]
    return result


__all__ = [
    "DEFAULT_GOOGLE_SCOPES",
    "GMAIL_SEND_SCOPE",
    "GoogleProviderError",
    "build_gmail_mime",
    "build_google_authorization_url",
    "exchange_google_code",
    "get_google_userinfo",
    "refresh_google_access_token",
    "revoke_google_token",
    "send_gmail_message",
]
