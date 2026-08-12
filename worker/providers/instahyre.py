"""Instahyre application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="instahyre",
    policy=ProviderPolicy(
        provider="instahyre",
        exact_hosts=frozenset({"instahyre.com", "www.instahyre.com"}),
    ),
    submit_selectors=(),
    confirmation_path_patterns=(r"/(?:applications?|candidate/opportunities)/(?:submitted|complete)(?:/|$)",),
    confirmation_text=("application submitted", "successfully applied"),
    form_selectors=('[role="dialog"] form', '[role="dialog"]', "form"),
    application_transition_unsupported_message=(
        "Instahyre requires a job-row identity, View dialog, and reviewed multi-step "
        "application flow before hosted automation can continue."
    ),
    submission_unsupported_message=(
        "Instahyre final submission is disabled until its opportunity-bound "
        "application state machine is available."
    ),
)

__all__ = ["ADAPTER"]
