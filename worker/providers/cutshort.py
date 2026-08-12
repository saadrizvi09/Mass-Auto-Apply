"""Cutshort application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="cutshort",
    policy=ProviderPolicy(
        provider="cutshort",
        exact_hosts=frozenset({"cutshort.io", "www.cutshort.io"}),
    ),
    submit_selectors=(),
    confirmation_path_patterns=(r"/(?:applications?)/(?:submitted|complete)(?:/|$)",),
    confirmation_text=("application submitted", "application successful"),
    form_selectors=('[role="dialog"] form', '[role="dialog"]', "form"),
    application_transition_unsupported_message=(
        "Cutshort uses a multi-step resume and screening wizard. Hosted automation "
        "will not advance it until every step has a reviewed schema revision."
    ),
    submission_unsupported_message=(
        "Cutshort final submission is disabled until its reviewed multi-step "
        "application state machine is available."
    ),
)

__all__ = ["ADAPTER"]
