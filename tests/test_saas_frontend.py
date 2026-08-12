from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
VERCEL_JSON = (ROOT / "vercel.json").read_text(encoding="utf-8")
SUPABASE_CONFIG = (ROOT / "supabase" / "config.toml").read_text(encoding="utf-8")


def test_every_static_by_id_reference_exists_in_the_document() -> None:
    referenced = set(re.findall(r'byId\("([A-Za-z0-9_-]+)"\)', APP_JS))
    declared = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', INDEX_HTML))
    assert referenced - declared == set()


def test_turnstile_is_fail_closed_for_auth_without_blocking_session_restore() -> None:
    assert "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" in APP_JS
    assert "captchaLoadError" in APP_JS
    assert "const captchaPromise = initialiseCaptcha().catch" in APP_JS
    assert "const result = await state.supabase.auth.getSession()" in APP_JS
    assert APP_JS.index("const captchaPromise = initialiseCaptcha().catch") < APP_JS.index(
        "const result = await state.supabase.auth.getSession()"
    )
    assert "await captchaPromise" in APP_JS
    assert 'options: { captchaToken }' in APP_JS
    assert "https://challenges.cloudflare.com" in VERCEL_JSON


def test_google_identity_login_uses_supabase_without_gmail_permissions() -> None:
    assert 'id="google-signin"' in INDEX_HTML
    assert "Continue with Google" in INDEX_HTML
    social = INDEX_HTML.split('id="auth-social"', 1)[1].split(
        'class="segmented-control"', 1
    )[0]
    assert 'href="/terms.html"' in social
    assert 'href="/privacy.html"' in social

    handler = APP_JS.split('byId("google-signin").addEventListener', 1)[1].split(
        'byId("signin-form").addEventListener', 1
    )[0]
    assert "state.supabase.auth.signInWithOAuth" in handler
    assert 'provider: "google"' in handler
    assert 'redirectTo: new URL("/", window.location.origin).href' in handler
    assert 'scopes: "openid email profile"' in handler
    assert "gmail.send" not in handler
    assert "requireCaptchaToken" not in handler
    assert 'withBusy(button, "Opening Google…"' in handler

    protected_selector = APP_JS.split("function isCaptchaProtectedAuthButton", 1)[1].split(
        "function safeHttpUrl", 1
    )[0]
    assert "google-signin" not in protected_selector


def test_google_identity_callback_errors_are_safe_and_recoverable() -> None:
    callback_handler = APP_JS.split("function consumeAuthCallbackError", 1)[1].split(
        "function captchaSizeForWidth", 1
    )[0]
    assert '["error", "error_code", "error_description"]' in callback_handler
    assert "window.history.replaceState" in callback_handler
    assert "Google sign-in was cancelled." in callback_handler
    assert "Google sign-in could not be completed." in callback_handler
    assert "error_description" not in callback_handler.split("return null;", 1)[-1]

    assert 'byId("auth-social").hidden = show' in APP_JS
    assert 'byId("auth-social").hidden = false' in APP_JS
    assert "const originalNodes = Array.from(button.childNodes)" in APP_JS
    assert "button.replaceChildren(...originalNodes)" in APP_JS


def test_google_identity_provider_is_enabled_without_committing_its_secret() -> None:
    provider = SUPABASE_CONFIG.split("[auth.external.google]", 1)[1].split(
        "[auth.web3.solana]", 1
    )[0]
    assert "enabled = true" in provider
    assert 'secret = "env(SUPABASE_AUTH_EXTERNAL_GOOGLE_CLIENT_SECRET)"' in provider
    assert "GOCSPX-" not in SUPABASE_CONFIG


def test_turnstile_is_responsive_without_clipping_the_challenge() -> None:
    assert 'return width < TURNSTILE_FLEXIBLE_MIN_WIDTH ? "compact" : "flexible"' in APP_JS
    assert "size," in APP_JS
    assert 'window.turnstile.remove(state.captchaWidgetId)' in APP_JS
    assert "#auth-captcha.is-compact" in STYLES_CSS
    assert "overflow: hidden" not in STYLES_CSS[
        STYLES_CSS.index("#auth-captcha {"):STYLES_CSS.index("#auth-captcha-status {")
    ]


def test_auth_card_accent_stays_inside_the_card() -> None:
    accent_rule = STYLES_CSS[
        STYLES_CSS.index(".auth-card::after {"):STYLES_CSS.index(".auth-card h2 {")
    ]
    assert "right: -" not in accent_rule
    assert "bottom: -" not in accent_rule
    assert "height: 44%" not in accent_rule


def test_account_deletion_and_google_revocation_recovery_ui_is_wired() -> None:
    for identifier in (
        "account-deletion-screen",
        "account-deletion-retry-form",
        "account-deletion-confirmation",
        "account-deletion-signout",
        "gmail-revocation-warning",
    ):
        assert f'id="{identifier}"' in INDEX_HTML
    assert 'responseError.code === "account_deletion_in_progress"' in APP_JS
    assert "disconnectResult.revoked === true" in APP_JS
    assert "https://myaccount.google.com/connections" in INDEX_HTML


def test_gmail_supports_platform_and_user_owned_google_oauth_apps() -> None:
    for identifier in (
        "gmail-oauth-setup",
        "gmail-oauth-mode-platform",
        "gmail-oauth-mode-user",
        "gmail-connect-platform",
        "gmail-connect-user",
        "gmail-callback-uri",
        "gmail-copy-callback",
        "gmail-oauth-client-form",
        "gmail-client-id",
        "gmail-client-secret",
        "gmail-oauth-client-summary",
        "gmail-replace-client",
        "gmail-delete-client",
    ):
        assert f'id="{identifier}"' in INDEX_HTML

    for url in (
        "https://console.cloud.google.com/projectcreate",
        "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
        "https://console.cloud.google.com/auth/branding",
        "https://console.cloud.google.com/auth/audience",
        "https://console.cloud.google.com/auth/scopes",
        "https://console.cloud.google.com/auth/clients",
    ):
        assert url in INDEX_HTML

    assert re.search(r'id="gmail-client-secret"[^>]+type="password"', INDEX_HTML)
    assert re.search(r'id="gmail-callback-uri"[^>]+readonly', INDEX_HTML)
    assert "Testing mode reconnects after 7 days." in INDEX_HTML
    assert "never saved in this browser" in INDEX_HTML
    assert 'apiRequest("/connections/google-oauth-client"' in APP_JS
    assert 'method: "PUT"' in APP_JS
    assert 'method: "DELETE"' in APP_JS
    assert "body: { client_id: clientId, client_secret: clientSecret }" in APP_JS
    assert "body: { credential_source: credentialSource }" in APP_JS
    assert "googleOauthConnectedSource" in APP_JS
    assert "navigator.clipboard.writeText(value)" in APP_JS

    save_client = APP_JS.split("async function saveGoogleOauthClient", 1)[1].split(
        "function beginGoogleOauthClientReplacement", 1
    )[0]
    assert "localStorage" not in save_client


def test_supabase_browser_library_is_self_hosted() -> None:
    assert 'from "/vendor/supabase.js"' in APP_JS
    assert "esm.sh" not in APP_JS
    assert (ROOT / "public" / "vendor" / "supabase.js").is_file()


def test_discovery_and_exact_form_review_are_wired_into_workspace() -> None:
    for identifier in (
        "view-discovery",
        "linkedin-discovery-form",
        "discovery-remote-only",
        "public-feeds-run",
        "referral-ingest-form",
        "job-import-form",
        "ats-link-form",
        "ats-board-form",
        "ats-board-links",
        "discovery-job-list",
        "jobs-load-more",
        "application-form-review",
        "form-revision-answers",
        "suggest-form-answers",
        "approve-form-revision",
        "prefill-form-revision",
        "submit-form-revision",
    ):
        assert f'id="{identifier}"' in INDEX_HTML
    assert 'body instanceof FormData' in APP_JS
    assert 'schema_hash: revision.schema_hash' in APP_JS
    assert 'form_revision_id: revision.id' in APP_JS
    assert 'loadJobs(true, identitySnapshot(), true)' in APP_JS
    assert 'state.jobsHasMore' in APP_JS
    assert 'apiRequest("/discovery/ats/boards"' in APP_JS
    assert 'idempotency_key: discoveryRunKey("public-ats")' in APP_JS
    assert 'kind="application_submit"' not in INDEX_HTML
    for copy in (
        "Job Radar",
        "LinkedIn Public Job Finder",
        "Telegram Public Channel Scanner",
        "Fetch LinkedIn jobs",
        "Fetch Telegram &amp; RSS jobs",
        "Worker required",
        "no login or Easy Apply",
        "not private groups",
    ):
        assert copy in INDEX_HTML
    assert "unsupported URLs are skipped" in INDEX_HTML
    assert "100 published jobs from this screen" in INDEX_HTML


def test_jobs_fit_desk_uses_both_columns_and_resume_profile_controls() -> None:
    jobs_view = INDEX_HTML.split('id="view-jobs"', 1)[1].split(
        'id="view-applications"', 1
    )[0]
    for identifier in (
        "job-fit-summary",
        "job-role-directions",
        "jobs-groq-status",
        "jobs-resume-status",
        "jobs-profile-status",
        "job-sort",
        "profile-from-resume",
        "profile-college",
        "profile-degree",
        "profile-graduation-year",
    ):
        assert f'id="{identifier}"' in INDEX_HTML
    assert 'class="panel span-2"' not in jobs_view
    assert 'class="panel jobs-library-panel"' in jobs_view
    assert "function renderJobIntelligence" in APP_JS
    assert "/analyze`" in APP_JS
    assert 'linkedin_url: "profile-linkedin"' in APP_JS
    assert 'github_url: "profile-github"' in APP_JS
    assert 'graduation_year: "profile-graduation-year"' in APP_JS
    assert ".fit-desk" in STYLES_CSS
    assert ".job-fit-meter" in STYLES_CSS


def test_resume_and_groq_setup_are_consolidated_on_profile() -> None:
    profile_view = INDEX_HTML.split('id="view-profile"', 1)[1].split(
        'id="view-discovery"', 1
    )[0]
    profile_form_position = profile_view.index('id="profile-form"')
    for identifier in (
        "profile-foundation-heading",
        "groq-form",
        "groq-key",
        "validate-groq",
        "delete-groq",
        "resume-upload-form",
        "resume-file",
        "resume-list",
        "resume-suggestions",
        "apply-suggestions",
    ):
        marker = f'id="{identifier}"'
        assert marker in profile_view
        assert profile_view.index(marker) < profile_form_position

    assert 'id="view-assets"' not in INDEX_HTML
    assert 'data-view="assets"' not in INDEX_HTML
    assert 'data-view-panel="assets"' not in INDEX_HTML
    assert "Résumé & AI" not in INDEX_HTML
    assert 'switchView("assets")' not in APP_JS
    assert 'if (view === "assets") view = "profile";' in APP_JS
    assert ".profile-source-grid" in STYLES_CSS


def test_groq_validation_displays_the_provider_status_instead_of_a_generic_error() -> None:
    assert 'groq_model_forbidden: { label: "Model blocked", tone: "status-warning" }' in APP_JS
    assert 'groq_rate_limited: { label: "Rate limited", tone: "status-warning" }' in APP_JS
    assert 'typeof payload?.message === "string"' in APP_JS
    assert 'error?.code === "groq_request_rate_limited"' in APP_JS
    assert 'pill.textContent = temporarilyLimited ? "Wait a moment" : "Validation failed"' in APP_JS
    assert '"Groq did not accept this key."' not in APP_JS


def test_local_frontend_assets_are_versioned_to_avoid_stale_validation_code() -> None:
    assert 'href="/styles.css?v=20260813.1"' in INDEX_HTML
    assert 'src="/app.js?v=20260813.1"' in INDEX_HTML


def test_ziprecruiter_is_not_presented_in_hosted_frontend() -> None:
    assert "ziprecruiter" not in INDEX_HTML.lower()
    assert "ziprecruiter" not in APP_JS.lower()


def test_browser_prefill_live_view_is_rendered_only_for_browserbase_https() -> None:
    assert "safeBrowserbaseLiveViewUrl" in APP_JS
    assert 'job.result?.live_view_url' in APP_JS
    assert 'text: "Open live review"' in APP_JS
    assert 'host.endsWith(".browserbase.com")' in APP_JS
