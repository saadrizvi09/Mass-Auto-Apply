"""Greenhouse-hosted application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="greenhouse",
    policy=ProviderPolicy(
        provider="greenhouse",
        exact_hosts=frozenset(
            {
                "boards.greenhouse.io",
                "job-boards.greenhouse.io",
                "boards.eu.greenhouse.io",
                "job-boards.eu.greenhouse.io",
            }
        ),
    ),
    submit_selectors=(
        'button[type="submit"]:text-is("Submit Application"),'
        'button[type="submit"]:text-is("Submit application"),'
        'button[type="submit"]:text-is("Submit"),'
        'input[type="submit"][value*="submit" i]',
    ),
    confirmation_path_patterns=(r"/(?:confirmation|thanks?)(?:/|$)",),
    confirmation_text=("application has been submitted", "thank you for applying"),
    form_selectors=("form#application-form", 'form[action*="application"]', "form"),
    application_entry_selector=(
        'a[href*="/embed/job_app"],a[href$="#app"],a[href*="#application"],'
        'a:text-is("Apply for this job"),a:text-is("Apply for this Job"),'
        'button:text-is("Apply for this job"),button:text-is("Apply for this Job")'
    ),
)

__all__ = ["ADAPTER"]
