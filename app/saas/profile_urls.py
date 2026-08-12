"""Shared validation helpers for profile links."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


_PLACEHOLDER_PATH_SEGMENTS = {
    "changeme",
    "insertlink",
    "inserturl",
    "replaceme",
    "yourhandle",
    "yourname",
    "yourprofile",
    "yourusername",
}


def is_placeholder_profile_url(value: str | None) -> bool:
    """Return whether a URL path contains an obvious template placeholder."""

    if not isinstance(value, str) or not value.strip():
        return False
    try:
        path = unquote(urlsplit(value.strip()).path)
    except ValueError:
        return False
    segments = {
        re.sub(r"[^a-z0-9]", "", segment.lower())
        for segment in path.split("/")
        if segment
    }
    return bool(segments & _PLACEHOLDER_PATH_SEGMENTS)


__all__ = ["is_placeholder_profile_url"]
