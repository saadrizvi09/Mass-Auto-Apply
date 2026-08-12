"""Small allowlisted HTTP boundary used by credential-free discovery."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx


class DiscoveryFetchError(RuntimeError):
    """A public discovery source could not be fetched safely."""


def require_allowed_https_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Reject user-controlled schemes, ports, credentials, and non-allowlisted hosts."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Discovery URL is malformed") from exc
    allowed = {host.strip().lower().rstrip(".") for host in allowed_hosts}
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or hostname not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("Discovery URL is not on the HTTPS source allowlist")
    return url


def fetch_text(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    timeout_seconds: float = 12.0,
    max_bytes: int = 1_000_000,
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch one trusted public page without redirects and with a hard body limit."""

    require_allowed_https_url(url, allowed_hosts)
    request_headers = {
        "User-Agent": "AutoApplyCloud-Discovery/1.0 (+public-job-import)",
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9",
        **(headers or {}),
    }
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        ) as client:
            with client.stream("GET", url, headers=request_headers) as response:
                if response.status_code != 200:
                    raise DiscoveryFetchError(
                        f"Discovery source returned HTTP {response.status_code}"
                    )
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise DiscoveryFetchError("Discovery response is too large")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise DiscoveryFetchError("Discovery response is too large")
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, "replace")
    except DiscoveryFetchError:
        raise
    except httpx.HTTPError as exc:
        raise DiscoveryFetchError("Discovery source could not be reached") from exc


__all__ = ["DiscoveryFetchError", "fetch_text", "require_allowed_https_url"]
