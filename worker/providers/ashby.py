"""Ashby-hosted application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="ashby",
    policy=ProviderPolicy(
        provider="ashby",
        exact_hosts=frozenset({"jobs.ashbyhq.com"}),
    ),
    submit_selectors=(
        'button.ashby-application-form-submit-button:text-is("Submit Application"),'
        'button.ashby-application-form-submit-button:text-is("Submit application"),'
        'button[type="submit"]:text-is("Submit Application"),'
        'button[type="submit"]:text-is("Submit application"),'
        'button[type="submit"]:text-is("Submit"),'
        'input[type="submit"][value*="submit" i]',
    ),
    confirmation_path_patterns=(r"/(?:application/)?(?:submitted|confirmation|thanks?)(?:/|$)",),
    confirmation_text=(
        "application submitted",
        "your application was successfully submitted",
        "thank you for applying",
    ),
    form_selectors=(
        '#form[role="tabpanel"]',
        ".ashby-application-form-container",
        '[data-testid*="application-form" i]',
        "form",
    ),
    application_entry_selector=(
        'a[href$="/application"],a[href*="/application?"],'
        'a:text-is("Apply Now"),a:text-is("Apply now"),'
        'button:text-is("Apply Now"),button:text-is("Apply now")'
    ),
)

__all__ = ["ADAPTER"]
