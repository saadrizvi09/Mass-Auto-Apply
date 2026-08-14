"""YC Work at a Startup application policy."""

from .base import ProviderAdapter, ProviderPolicy


ADAPTER = ProviderAdapter(
    provider="yc",
    policy=ProviderPolicy(
        provider="yc",
        exact_hosts=frozenset({"www.workatastartup.com", "workatastartup.com"}),
        path_patterns=(
            r"^/(?:jobs/\d[^/]*|companies/[^/]+/jobs/\d[^/]*)(?:/.*)?$",
            r"^/applications?/(?:submitted|complete)(?:/|$)",
        ),
    ),
    submit_selectors=(
        'button:text-is("Send"),button:text-is("Send application"),'
        'button:text-is("Send Application"),button[type="submit"]:text-is("Submit"),'
        'button:text-is("Submit"),button:text-is("Submit application"),'
        'button:text-is("Submit Application")',
    ),
    confirmation_path_patterns=(r"/(?:applications?)/(?:submitted|complete)(?:/|$)",),
    confirmation_text=(
        "application submitted",
        "your application was sent",
        "message sent",
        "your message has been sent",
    ),
    form_selectors=('[role="dialog"] form', '[role="dialog"]'),
    login_redirect_hosts=frozenset({"account.ycombinator.com"}),
    application_entry_selector=(
        'a:text-is("Apply"),button:text-is("Apply"),'
        'a:text-is("Apply now"),button:text-is("Apply now"),'
        'a:text-is("Apply Now"),button:text-is("Apply Now")'
    ),
)

__all__ = ["ADAPTER"]
