"""Reviewed managed-browser provider registry used only by the worker.

Provider modules intentionally contain policy and conservative selector candidates,
not claims that a live third-party page was validated.  The shared adapter refuses
cross-provider redirects, fills only exact approved answers, and never guesses.
"""

from __future__ import annotations

from .ashby import ADAPTER as ASHBY
from .base import ProviderAdapter
from .cutshort import ADAPTER as CUTSHORT
from .google_forms import ADAPTER as GOOGLE_FORMS
from .greenhouse import ADAPTER as GREENHOUSE
from .instahyre import ADAPTER as INSTAHYRE
from .lever import ADAPTER as LEVER
from .wellfound import ADAPTER as WELLFOUND
from .yc import ADAPTER as YC


_ADAPTERS: tuple[ProviderAdapter, ...] = (
    GOOGLE_FORMS,
    GREENHOUSE,
    LEVER,
    ASHBY,
    YC,
    WELLFOUND,
    CUTSHORT,
    INSTAHYRE,
)
ADAPTERS = {adapter.provider: adapter for adapter in _ADAPTERS}


def get_adapter(provider: str) -> ProviderAdapter | None:
    if not isinstance(provider, str):
        return None
    return ADAPTERS.get(provider.strip().lower())


__all__ = ["ADAPTERS", "ProviderAdapter", "get_adapter"]
