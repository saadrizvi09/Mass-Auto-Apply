"""Google Forms policy and conservative final-action signals."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="google_forms",
    policy=ProviderPolicy(
        provider="google_forms",
        exact_hosts=frozenset({"docs.google.com", "forms.gle"}),
        path_prefixes=("/forms/",),
        # Google-owned short links redirect to docs.google.com/forms.  Keep the
        # broad short-link path rule scoped to forms.gle rather than all Google Docs.
        host_path_prefixes={"forms.gle": ("/",)},
    ),
    # Candidate only; the shared adapter still requires exactly one visible control.
    submit_selectors=('div[role="button"]:has-text("Submit")',),
    confirmation_path_patterns=(r"/forms/.*/formResponse$",),
    confirmation_text=("your response has been recorded", "response submitted"),
    form_selectors=('form[action*="formResponse"]', "form"),
    login_redirect_hosts=frozenset({"accounts.google.com"}),
    # Google Forms URLs already open the first form page. Deliberately do not
    # click Next: multi-page forms require another reviewed schema revision.
    application_entry_selector=None,
)

__all__ = ["ADAPTER"]
