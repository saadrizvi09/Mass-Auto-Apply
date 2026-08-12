"""YC Work at a Startup application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="yc",
    policy=ProviderPolicy(
        provider="yc",
        exact_hosts=frozenset({"www.workatastartup.com", "workatastartup.com"}),
    ),
    submit_selectors=(),
    confirmation_path_patterns=(r"/(?:applications?)/(?:submitted|complete)(?:/|$)",),
    confirmation_text=("application submitted", "your application was sent"),
    form_selectors=('[role="dialog"] form', '[role="dialog"]', "form"),
    login_redirect_hosts=frozenset({"account.ycombinator.com"}),
    application_transition_unsupported_message=(
        "YC requires a connected, signed-in provider session and a validated "
        "application-dialog transition before hosted automation can continue."
    ),
    submission_unsupported_message=(
        "YC final submission is disabled until its signed-in application dialog "
        "has passed a controlled provider canary."
    ),
)

__all__ = ["ADAPTER"]
