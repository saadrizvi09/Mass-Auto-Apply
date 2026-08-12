"""Lever-hosted application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="lever",
    policy=ProviderPolicy(
        provider="lever",
        exact_hosts=frozenset({"jobs.lever.co", "jobs.eu.lever.co"}),
    ),
    submit_selectors=(
        'button#btn-submit[data-qa="btn-submit"]:text-is("Submit application"),'
        'button[type="submit"]:text-is("Submit Application"),'
        'button[type="submit"]:text-is("Submit application"),'
        'button[type="submit"]:text-is("Submit"),'
        'input[type="submit"][value*="submit" i]',
    ),
    confirmation_path_patterns=(r"/(?:thanks?|confirmation)(?:/|$)",),
    confirmation_text=("application has been submitted", "thank you for applying"),
    form_selectors=("form.application-form", 'form[action*="apply"]', "form"),
    application_entry_selector=(
        'a[href$="/apply"],a[href*="/apply?"],'
        'a:text-is("Apply for this job"),button:text-is("Apply for this job")'
    ),
)

__all__ = ["ADAPTER"]
