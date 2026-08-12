"""Wellfound application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="wellfound",
    policy=ProviderPolicy(
        provider="wellfound",
        exact_hosts=frozenset({"wellfound.com", "www.wellfound.com"}),
    ),
    submit_selectors=(
        'button[type="submit"]:text-is("Submit Application"),'
        'button[type="submit"]:text-is("Submit application"),'
        'button[type="submit"]:text-is("Send application"),'
        'button[type="submit"]:text-is("Apply"),'
        'input[type="submit"][value*="submit" i]',
    ),
    confirmation_path_patterns=(r"/(?:applications?)/(?:submitted|complete)(?:/|$)",),
    confirmation_text=("application submitted", "your application was sent"),
    form_selectors=('[role="dialog"] form', '[role="dialog"]', "form"),
    # The current-job route itself provides a provider-owned deterministic
    # transition; broad Apply selectors also match recommended jobs.
    application_entry_query=(("autoOpenApplication", "true"),),
)

__all__ = ["ADAPTER"]
