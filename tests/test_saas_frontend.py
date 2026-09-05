from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
VERCEL_JSON = (ROOT / "vercel.json").read_text(encoding="utf-8")
SUPABASE_CONFIG = (ROOT / "supabase" / "config.toml").read_text(encoding="utf-8")
DEV_COMMAND = (ROOT / "dev.command").read_text(encoding="utf-8")


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


def test_loading_system_is_accessible_recoverable_and_checkpoint_driven() -> None:
    boot = INDEX_HTML.split('id="boot-screen"', 1)[1].split('id="public-site"', 1)[0]
    assert 'aria-live="polite"' in boot
    assert 'aria-busy="true"' in boot
    assert 'id="boot-title"' in boot
    assert 'id="boot-detail"' in boot
    assert 'data-boot-step="service"' in boot
    assert 'data-boot-step="session"' in boot
    assert 'data-boot-step="workspace"' in boot
    assert 'id="boot-retry"' in boot
    assert "function setBootCheckpoint" in APP_JS
    assert "function showBootFailure" in APP_JS
    assert 'byId("boot-retry").addEventListener("click", () => window.location.reload())' in APP_JS
    assert "controller.abort()" in APP_JS
    assert 'new AppError("The workspace service took too long to respond. Try again."' in APP_JS
    assert ".boot-dossier" in STYLES_CSS
    assert ".boot-checkpoints" in STYLES_CSS
    assert ".boot-file-scan" in STYLES_CSS


def test_workspace_loader_stays_up_until_data_is_ready_without_duplicate_opening() -> None:
    workspace_open = APP_JS.split("async function showWorkspace", 1)[1].split(
        "function captchaEnabled", 1
    )[0]
    assert "state.workspaceOpeningPromise" in workspace_open
    assert "state.workspaceOpeningUserId === session.user.id" in workspace_open
    assert "showBootFailure(error)" in workspace_open
    assert workspace_open.index('byId("workspace").hidden = true') < workspace_open.index(
        "await waitForWorkspaceLoad(identity)"
    )
    assert workspace_open.index("await waitForWorkspaceLoad(identity)") < workspace_open.index(
        'byId("workspace").hidden = false'
    )
    assert workspace_open.index('byId("workspace").hidden = false') < workspace_open.index(
        "finishBoot()"
    )
    assert "WORKSPACE_OPEN_TIMEOUT_MS = 30_000" in APP_JS
    assert '"workspace_load_timeout"' in APP_JS


def test_busy_buttons_keep_their_label_and_expose_real_pending_state() -> None:
    busy = APP_JS.split("async function withBusy", 1)[1].split(
        "function isCaptchaProtectedAuthButton", 1
    )[0]
    assert "const originalNodes = Array.from(button.childNodes)" in busy
    assert 'button.setAttribute("aria-busy", "true")' in busy
    assert "setBusyLabel(button, busyLabel)" in busy
    assert 'className: "button-pending-spinner"' in busy
    assert 'attrs: { "aria-hidden": "true" }' in busy
    assert 'button.removeAttribute("aria-busy")' in busy
    assert "button.replaceChildren(...originalNodes)" in busy
    assert "originalAriaLabel" in busy
    assert "button.disabled = originalDisabled" in busy
    assert ".button-pending-spinner" in STYLES_CSS
    assert 'button[data-busy="true"]' in STYLES_CSS


def test_worker_progress_uses_real_status_checkpoints_in_a_dismissible_dock() -> None:
    assert 'id="workflow-dock"' in INDEX_HTML
    assert 'id="workflow-dock-checkpoints"' in INDEX_HTML
    assert 'id="workflow-dock-dismiss"' in INDEX_HTML
    assert 'aria-label="Dismiss workflow status"' in INDEX_HTML
    assert 'id="resume-discovery-checkpoints"' in INDEX_HTML
    assert 'id="resume-discovery-progress-bar"' not in INDEX_HTML
    assert "function discoveryCheckpoint" in APP_JS
    assert "function renderWorkflowDock" in APP_JS
    assert "container.dataset.statusSignature === signature" in APP_JS
    assert "state.workflowDockDismissedRunId === runId" in APP_JS
    assert 'announce("Workflow status dismissed. Its full history remains in Activity.")' in APP_JS
    assert "let percent" not in APP_JS
    assert ".workflow-dock" in STYLES_CSS
    assert ".discovery-checkpoint.status-running" in STYLES_CSS
    assert "@media (prefers-reduced-motion: reduce)" in STYLES_CSS


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
        "view-form-pilot",
        "resume-discovery-form",
        "resume-discovery-role-list",
        "resume-discovery-progress",
        "resume-discovery-status",
        "discovery-remote-only",
        "discovery-max-jobs",
        "discovery-time-limit",
        "google-form-intake-form",
        "google-form-url",
        "google-form-intake-status",
        "google-form-queue",
        "google-form-queue-refresh",
        "ats-link-form",
        "ats-board-form",
        "ats-board-links",
        "discovery-job-list",
        "jobs-load-more",
        "application-form-review",
        "form-application-id",
        "form-workflow-progress",
        "form-live-review-link",
        "retry-form-scan",
        "form-revision-answers",
        "form-submit-preflight",
        "form-submit-preflight-status",
        "form-submit-preflight-detail",
        "form-submit-missing-list",
        "submit-form-revision",
        "form-submission-recovery",
        "form-open-original",
        "form-mark-submitted",
        "form-prepare-submission-retry",
    ):
        assert f'id="{identifier}"' in INDEX_HTML
    assert 'body instanceof FormData' in APP_JS
    assert 'schema_hash: revision.schema_hash' in APP_JS
    assert 'form_revision_id: revision.id' in APP_JS
    assert "function latestFormScanJob(applicationId)" in APP_JS
    assert "function renderFormScanPlaceholder(applicationId)" in APP_JS
    assert 'status.textContent = running ? "Capturing form" : "Waiting for worker"' in APP_JS
    assert 'applicationStatus.textContent = running ? "Preparing form" : "Preparation queued"' in APP_JS
    assert 'status.textContent = succeededWithoutRevision ? "No questions captured"' in APP_JS
    assert 'applicationStatus.textContent = "Preparation needs attention"' in APP_JS
    assert 'if (state.selectedFormApplicationId !== applicationId) return;' in APP_JS
    assert 'if (!latestFormRevision(applicationId))' in APP_JS
    assert 'setFormScanRetry(applicationId, true)' in APP_JS
    assert 'byId("retry-form-scan").addEventListener("click"' in APP_JS
    assert 'if (selectedFormApplicationId && !selectedRevision) renderFormRevision(null)' in APP_JS
    assert 'loadJobs(true, identitySnapshot(), true)' in APP_JS
    assert 'state.jobsHasMore' in APP_JS
    assert 'apiRequest("/discovery/ats/boards"' in APP_JS
    assert 'idempotency_key: discoveryRunKey("public-ats")' in APP_JS
    assert 'apiRequest("/discovery/resume-guided"' in APP_JS
    assert 'apiRequest("/discovery/google-forms?limit=100&offset=0"' in APP_JS
    assert 'groq: true' in APP_JS
    assert 'kind="application_submit"' not in INDEX_HTML
    for copy in (
        "Résumé-guided job radar",
        "Find jobs matched to your résumé",
        "LinkedIn",
        "Telegram",
        "RSS",
        "Find matching jobs",
        "Maximum jobs",
        "Search time limit",
        "Form Pilot",
        "Paste referral alert",
        "Ready for preparation",
        "Worker required",
        "no LinkedIn login",
        "private Telegram groups",
    ):
        assert copy in INDEX_HTML
    assert "Add Google Forms directly in Form Pilot" in INDEX_HTML
    assert "100 published jobs from this screen" in INDEX_HTML

    discovery_view = INDEX_HTML.split('id="view-discovery"', 1)[1].split(
        'id="view-form-pilot"', 1
    )[0]
    form_pilot_view = INDEX_HTML.split('id="view-form-pilot"', 1)[1].split(
        'id="view-outreach"', 1
    )[0]
    assert 'id="google-form-queue"' not in discovery_view
    assert 'id="referral-ingest-form"' not in discovery_view
    assert 'id="referral-digest"' not in discovery_view
    assert "Pasted leads" not in discovery_view
    assert "Turn shared messages into jobs" not in discovery_view
    assert 'class="panel discovery-search-panel span-2"' in discovery_view
    assert 'id="google-form-queue"' in form_pilot_view
    assert "Parse leads" in form_pilot_view
    assert "Prepare forms" in form_pilot_view
    assert "Review &amp; submit" in form_pilot_view
    assert "One explicit approval" in form_pilot_view
    assert "Approve &amp; submit in background" in form_pilot_view
    assert 'id="referral-ingest-form"' in form_pilot_view
    assert 'id="referral-digest"' in form_pilot_view
    assert 'id="referral-ingest-status"' in form_pilot_view
    assert 'id="referral-route-summary"' in form_pilot_view
    assert 'data-form-intake-mode="digest"' in form_pilot_view
    assert 'data-form-intake-mode="single"' in form_pilot_view
    assert 'id="google-form-intake-form"' in form_pilot_view
    assert 'id="google-form-url"' in form_pilot_view
    assert "Add &amp; prepare form" in form_pilot_view
    assert 'id="application-form-review"' in form_pilot_view
    assert 'id="form-application-id"' in form_pilot_view

    mass_email_review = INDEX_HTML.split('id="view-applications"', 1)[1].split(
        'id="view-connections"', 1
    )[0]
    assert 'id="application-form-review"' not in mass_email_review
    assert "This queue contains cold-email drafts only" in mass_email_review

    digest_intake = APP_JS.split("async function ingestReferralDigest", 1)[1].split(
        "async function importJobFile", 1
    )[0]
    assert 'apiRequest("/discovery/referrals"' in digest_intake
    assert "loadJobs(true)" in digest_intake
    assert "loadGoogleForms(true)" in digest_intake
    assert "renderReferralRouteSummary(summary)" in digest_intake
    assert 'No form was submitted and no email was sent' in APP_JS

    direct_intake = APP_JS.split("async function addGoogleFormToPilot", 1)[1].split(
        "async function queueAtsBoardDiscovery", 1
    )[0]
    assert 'providerForJob({ apply_url: url }) !== "google_forms"' in direct_intake
    assert 'apiRequest("/discovery/ats"' in direct_intake
    assert 'body: { urls: [url] }' in direct_intake
    assert 'scanJobApplication(savedJob, "google_forms", button)' in direct_intake


def test_resume_discovery_is_one_click_and_refreshes_results_automatically() -> None:
    assert "DISCOVERY_POLL_INTERVAL_MS = 2_000" in APP_JS
    assert "DISCOVERY_MONITOR_TIMEOUT_MS = 120_000" in APP_JS
    assert "DISCOVERY_QUEUE_REQUEST_TIMEOUT_MS = 55_000" in APP_JS
    assert "max_jobs: Number(byId(\"discovery-max-jobs\")?.value || 10)" in APP_JS
    assert "timeout_seconds: Number(byId(\"discovery-time-limit\")?.value || DISCOVERY_DEFAULT_TIMEOUT_SECONDS)" in APP_JS
    assert "requestDiscoveryCancellation" in APP_JS
    assert "run.timedOut = true" in APP_JS
    assert "Search time limit reached" in APP_JS
    assert "DISCOVERY_TERMINAL_STATUSES" in APP_JS
    assert "monitorResumeDiscoveryRun" in APP_JS
    assert "resumeDiscoveryMonitoring(identity)" in APP_JS
    assert 'apiRequest(`/automation-jobs/${encodeURIComponent(jobId)}`' in APP_JS
    assert "loadJobs(true, identity)" in APP_JS
    assert "loadGoogleForms(true, identity)" in APP_JS
    assert "saveDiscoveryRun(run, identity.userId)" in APP_JS
    assert "Your current search is already running" in APP_JS
    assert "Follow their progress in Activity, then refresh the results" not in APP_JS
    assert "Your search is safely queued" in APP_JS
    assert "restart ./dev.command" not in APP_JS


def test_local_development_launcher_supervises_api_and_worker() -> None:
    assert '"$SAAS_PYTHON" -m worker.main &' in DEV_COMMAND
    assert '--reload-dir "$PROJECT_DIR/app"' in DEV_COMMAND
    assert 'kill -0 "$WORKER_PID"' in DEV_COMMAND
    assert 'kill -0 "$API_PID"' in DEV_COMMAND
    assert "queued work cannot appear stuck" in DEV_COMMAND


def test_outreach_is_a_bounded_review_gated_workflow() -> None:
    for identifier in (
        "view-outreach",
        "outreach-prerequisites",
        "public-contact-heading",
        "outreach-job-list",
        "outreach-selection-count",
        "outreach-find-contacts",
        "outreach-contact-results",
        "outreach-create-drafts",
        "outreach-draft-list",
        "outreach-send-approved",
    ):
        assert f'id="{identifier}"' in INDEX_HTML

    outreach_view = INDEX_HTML.split('id="view-outreach"', 1)[1].split(
        'id="view-jobs"', 1
    )[0]
    for step in (
        "Select relevant jobs",
        "Find public contact emails",
        "Create drafts with Groq, then review",
        "Send only approved messages",
    ):
        assert step in outreach_view
    assert "Mass Cold Email" in INDEX_HTML
    assert "No contact API key required" in outreach_view
    assert "does not guess private addresses" in outreach_view
    assert "Select up to 30 imported roles" in outreach_view
    assert "Nothing is sent automatically" in outreach_view
    assert 'id="outreach-research-form"' in outreach_view
    assert 'id="outreach-research-prompt"' in outreach_view
    assert 'id="outreach-import-form"' in outreach_view
    assert 'id="outreach-import-trigger"' in outreach_view
    assert 'id="outreach-generate-prompt"' in outreach_view
    assert 'id="outreach-download-template"' not in outreach_view
    assert "at least 100 source-verified public contacts" in outreach_view

    assert 'headers.set("X-Hunter-Api-Key", key)' not in APP_JS
    assert 'title: "Mass Cold Email"' in APP_JS
    assert 'id="referral-ingest-form"' not in outreach_view
    assert 'id="referral-digest"' not in outreach_view
    assert 'apiRequest(`/provider-credentials/${encodeURIComponent(provider)}`' in APP_JS
    assert '/contacts/public' in APP_JS
    assert "state.outreachSelectedJobIds.size >= 30" in APP_JS
    assert "selected: contacts[0]?.email" not in APP_JS
    assert "contacts.some((contact) => contact.email === job.contact_email)" in APP_JS
    assert 'application?.status === "approved"' in APP_JS
    send_batch = APP_JS.split("async function sendApprovedOutreach", 1)[1].split(
        "function renderOutreach", 1
    )[0]
    assert "await confirmAction({" in send_batch
    assert "Final Gmail handoff" in send_batch
    public_contact_lookup = APP_JS.split("async function findOutreachContacts", 1)[1].split(
        "function renderOutreachDrafts", 1
    )[0]
    assert "confirmAction" not in public_contact_lookup
    assert 'apiRequest(`/jobs/${encodeURIComponent(job.id)}/contacts/public`' in public_contact_lookup
    assert "No mailbox is contacted" in INDEX_HTML
    assert "findButton.disabled = !selected.length" in APP_JS
    assert 'id="outreach-credit-estimate"' in outreach_view
    assert 'idempotency_key: `outreach-batch-${crypto.randomUUID()}`' in APP_JS


def test_outreach_workbook_picker_is_bounded_and_preserves_scroll() -> None:
    assert ".outreach-import-trigger" in STYLES_CSS
    assert "grid-template-columns: minmax(0, 1fr) auto" in STYLES_CSS
    assert "clip-path: inset(50%)" in STYLES_CSS
    assert 'byId("outreach-import-file")?.click()' in APP_JS
    assert "rememberOutreachImportScroll" in APP_JS
    assert 'focus({ preventScroll: true })' in APP_JS


def test_mass_cold_email_contains_build_and_review_subtabs() -> None:
    sidebar = INDEX_HTML.split('<nav class="workspace-nav"', 1)[1].split("</nav>", 1)[0]
    assert 'data-view="outreach"' in sidebar
    assert 'data-view="applications"' not in sidebar
    assert 'id="draft-count-badge"' in sidebar
    assert INDEX_HTML.count('data-mass-email-view="outreach"') == 2
    assert INDEX_HTML.count('data-mass-email-view="applications"') == 2
    assert INDEX_HTML.count('role="tablist"') >= 2
    assert 'data-view-panel="outreach" role="tabpanel"' in INDEX_HTML
    assert 'data-view-panel="applications" role="tabpanel"' in INDEX_HTML
    assert "Build campaign" in INDEX_HTML
    assert "Review &amp; send" in INDEX_HTML
    assert 'url.searchParams.set("tab", "review")' in APP_JS
    assert 'url.searchParams.get("tab") === "review"' in APP_JS
    assert 'button.dataset.view === (massEmailView ? "outreach" : view)' in APP_JS
    assert 'all("[data-mass-email-view]")' in APP_JS
    assert 'button.tabIndex = active ? 0 : -1' in APP_JS
    assert '["ArrowLeft", "ArrowRight", "Home", "End"]' in APP_JS


def test_native_confirmations_use_accessible_modal_and_public_contact_search_runs_directly() -> None:
    for identifier in (
        "action-dialog",
        "action-dialog-title",
        "action-dialog-message",
        "action-dialog-cancel",
        "action-dialog-confirm",
    ):
        assert f'id="{identifier}"' in INDEX_HTML
    dialog = INDEX_HTML.split('id="action-dialog"', 1)[1].split("</dialog>", 1)[0]
    assert 'aria-labelledby="action-dialog-title"' in dialog
    assert 'aria-describedby="action-dialog-message"' in dialog
    assert 'method="dialog"' in dialog
    assert 'value="cancel"' in dialog
    assert 'value="confirm"' in dialog
    assert "window.confirm(" not in APP_JS
    assert "function confirmAction" in APP_JS
    assert "dialog.showModal()" in APP_JS
    assert 'addEventListener("cancel"' in APP_JS
    assert "event.target === dialog" in APP_JS
    assert "trigger?.isConnected" in APP_JS
    assert ".action-dialog::backdrop" in STYLES_CSS
    assert '.action-dialog[data-tone="danger"]' in STYLES_CSS
    public_contact_search = APP_JS.split("async function findOutreachContacts", 1)[1].split(
        "function renderOutreachDrafts", 1
    )[0]
    assert "confirmAction" not in public_contact_search


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
        "profile-resume-url",
    ):
        assert f'id="{identifier}"' in INDEX_HTML
    assert 'class="panel span-2"' not in jobs_view
    assert 'class="panel jobs-library-panel"' in jobs_view
    assert "function renderJobIntelligence" in APP_JS
    assert "/analyze`" in APP_JS
    assert 'linkedin_url: "profile-linkedin"' in APP_JS
    assert 'github_url: "profile-github"' in APP_JS
    assert 'resume_url: nullable("profile-resume-url")' in APP_JS
    assert 'graduation_year: "profile-graduation-year"' in APP_JS
    assert "function isPlaceholderProfileUrl" in APP_JS
    assert "mayReplacePlaceholder" in APP_JS
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
    for selector in (
        ".mass-email-contact-panel",
        ".mass-email-contact-intro",
        ".public-contact-stamp",
    ):
        assert selector in STYLES_CSS


def test_groq_validation_displays_the_provider_status_instead_of_a_generic_error() -> None:
    save_credential = APP_JS.split("async function saveProviderCredential", 1)[1].split(
        "async function deleteProviderCredential", 1
    )[0]
    assert 'method: "PUT"' in save_credential
    assert 'setCredentialMessage(provider, errorMessage(error' in save_credential
    assert "Save &amp; validate" in INDEX_HTML
    assert 'id="credential-groq-status"' in INDEX_HTML
    assert '"Groq did not accept this key."' not in APP_JS


def test_account_credential_vault_supports_groq_and_browserbase_byok() -> None:
    connections_view = INDEX_HTML.split('id="view-connections"', 1)[1].split(
        'id="view-automation"', 1
    )[0]
    for identifier in (
        "provider-credential-vault",
        "credential-vault-status",
        "credential-card-groq",
        "credential-groq-api-key",
        "credential-card-browserbase",
        "credential-browserbase-api-key",
        "credential-browserbase-project-id",
    ):
        assert f'id="{identifier}"' in connections_view

    assert connections_view.index('id="provider-credential-vault"') < connections_view.index(
        'id="connection-list"'
    )
    assert 'href="https://www.browserbase.com/sign-up"' in connections_view
    assert 'href="https://www.browserbase.com/settings"' in connections_view
    assert 'href="https://www.browserbase.com/overview"' in connections_view
    assert 'href="https://docs.browserbase.com/welcome/getting-started"' in connections_view
    assert "Copy API key" in connections_view
    assert "Copy Project ID" in connections_view
    assert 'type="password"' in connections_view
    assert "Usage is charged to your Browserbase account" in connections_view
    assert ".credential-card-grid" in STYLES_CSS

    load_credentials = APP_JS.split("async function loadProviderCredentials", 1)[1].split(
        "function providerCredentialVerifiedCopy", 1
    )[0]
    assert 'apiRequest("/provider-credentials"' in load_credentials
    assert "Array.isArray(data?.items)" in APP_JS
    save_credentials = APP_JS.split("async function saveProviderCredential", 1)[1].split(
        "async function deleteProviderCredential", 1
    )[0]
    assert 'method: "PUT"' in save_credentials
    assert 'provider === "browserbase" ? { project_id: projectId }' in save_credentials
    delete_credentials = APP_JS.split("async function deleteProviderCredential", 1)[1].split(
        "function focusProviderCredential", 1
    )[0]
    assert 'method: "DELETE"' in delete_credentials
    assert 'headers.set("X-Groq-Api-Key"' not in APP_JS
    assert 'headers.set("X-Hunter-Api-Key"' not in APP_JS
    assert 'provider !== "browserbase" || credential.verification_status === "verified"' in APP_JS
    normalize_credentials = APP_JS.split("function normalizedProviderCredential", 1)[1].split(
        "function replaceProviderCredentialState", 1
    )[0]
    assert "{ ...candidate" not in normalize_credentials
    assert "candidate.credential_ciphertext" not in normalize_credentials
    assert "candidate.api_key" not in normalize_credentials
    assert "api_key:" not in normalize_credentials
    assert "const keyHint = rawHint" in normalize_credentials
    for safe_field in (
        "verification_status",
        "verification_code",
        "verified_at",
        "updated_at",
        "key_hint",
        "project_id_hint",
        "requires_reconfiguration",
    ):
        assert safe_field in normalize_credentials
    clear_private = APP_JS.split("function clearPrivateState", 1)[1].split(
        "function setSession", 1
    )[0]
    assert "state.providerCredentials = { groq: {}, browserbase: {} }" in clear_private
    assert "state.providerCredentialMigrationUserId = null" in clear_private


def test_contextual_groq_form_and_public_contact_lookup_use_the_active_flow() -> None:
    profile_view = INDEX_HTML.split('id="view-profile"', 1)[1].split(
        'id="view-discovery"', 1
    )[0]
    outreach_view = INDEX_HTML.split('id="view-outreach"', 1)[1].split(
        'id="view-jobs"', 1
    )[0]
    assert 'id="groq-form"' in profile_view
    assert 'data-provider-credential="groq"' in profile_view
    assert 'id="groq-key"' in profile_view
    assert 'Add or replace your Groq key' in profile_view
    assert 'id="public-contact-heading"' in outreach_view
    assert 'id="hunter-setup-form"' not in outreach_view
    assert 'data-provider-credential="hunter"' not in outreach_view
    assert 'all("[data-provider-credential]").forEach' in APP_JS
    assert "saveGroqKey" not in APP_JS
    assert '/contacts/hunter' not in APP_JS
    assert "localStorage.setItem(storageKey, value)" not in APP_JS


def test_account_switch_clears_typed_secrets_and_restores_only_masked_server_status() -> None:
    clear_private = APP_JS.split("function clearPrivateState", 1)[1].split(
        "function setSession", 1
    )[0]
    set_session = APP_JS.split("function setSession", 1)[1].split(
        "function identitySnapshot", 1
    )[0]
    load_workspace = APP_JS.split("async function loadWorkspace", 1)[1].split(
        "function updateUserIdentity", 1
    )[0]
    normalize_credentials = APP_JS.split("function normalizedProviderCredential", 1)[1].split(
        "function replaceProviderCredentialState", 1
    )[0]

    assert 'all("[data-provider-credential]").forEach((form) => form.reset())' in clear_private
    assert "if (nextUserId !== state.identityUserId)" in set_session
    assert "clearPrivateState();" in set_session
    assert "loadProviderCredentials(true, identity)" in load_workspace
    assert "key_hint: keyHint" in normalize_credentials
    assert "candidate.api_key" not in normalize_credentials
    assert "candidate.credential_ciphertext" not in normalize_credentials
    assert "input.value = credential" not in APP_JS


def test_legacy_browser_keys_migrate_once_and_are_removed_only_after_server_save() -> None:
    migration = APP_JS.split("async function migrateLegacyProviderCredentials", 1)[1].split(
        "async function loadProviderCredentials", 1
    )[0]
    assert "state.providerCredentialMigrationUserId === identity.userId" in migration
    assert "getLegacyGroqKey(identity.userId)" in migration
    assert "getLegacyHunterKey(identity.userId)" not in migration
    assert 'method: "PUT"' in migration
    assert migration.index('await apiRequest(`/provider-credentials/${encodeURIComponent(provider)}`') < migration.index(
        "removeLegacyProviderKey(provider, identity.userId);",
        migration.index('await apiRequest(`/provider-credentials/${encodeURIComponent(provider)}`'),
    )
    assert "The browser copy was kept so you can retry safely." in APP_JS
    assert "BROWSERBASE_STORAGE_PREFIX" not in APP_JS


def test_local_frontend_assets_are_versioned_to_avoid_stale_validation_code() -> None:
    assert 'href="/styles.css?v=20260905.2"' in INDEX_HTML
    assert 'src="/app.js?v=20260905.2"' in INDEX_HTML


def test_ziprecruiter_is_not_presented_in_hosted_frontend() -> None:
    assert "ziprecruiter" not in INDEX_HTML.lower()
    assert "ziprecruiter" not in APP_JS.lower()


def test_browser_prefill_live_view_is_rendered_only_for_browserbase_https() -> None:
    assert "safeBrowserbaseLiveViewUrl" in APP_JS
    assert 'job.result?.live_view_url' in APP_JS
    assert 'text: "Open live review"' in APP_JS
    assert 'host.endsWith(".browserbase.com")' in APP_JS


def test_form_pilot_auto_suggests_and_submits_one_reviewed_revision_in_background() -> None:
    assert 'apiRequest("/applications?channel=email&limit=50"' in APP_JS
    assert 'apiRequest("/applications?channel=ats&limit=50"' in APP_JS
    assert "formRevisionCanAutoSuggest" in APP_JS
    assert "maybeSuggestFormAnswers(applicationId, latest)" in APP_JS
    assert "state.formSuggestionAttempts.add(revision.id)" in APP_JS
    assert 'byId("form-application-id").value' in APP_JS
    assert "formRevisionPreflight" in APP_JS
    assert "refreshFormSubmitPreflight" in APP_JS
    assert "approveAndSubmitFormRevision" in APP_JS
    assert '/approve`' in APP_JS
    assert '/submit`' in APP_JS
    assert 'idempotency_key: `form-submit-${revision.id}`' in APP_JS
    assert "formSubmissionIsVerified" in APP_JS
    assert 'job.result?.code === "application_submitted"' in APP_JS
    assert 'job.result?.submission_state === "confirmed"' in APP_JS
    assert "safeBrowserbaseLiveViewUrl(result.live_view_url)" in APP_JS
    assert 'id="submit-form-revision"' in INDEX_HTML
    assert "Approve &amp; submit in background" in INDEX_HTML
    assert "Verified success" in APP_JS
    assert "Needs attention" in APP_JS
    assert "openFilledFormForFinalSubmit" not in APP_JS
    assert "reserveFormReviewWindow" not in APP_JS
    assert 'window.open("about:blank", "_blank")' in APP_JS  # provider-login Live View only
    assert '/prefill`' not in APP_JS
    assert 'id="approve-form-revision"' not in INDEX_HTML
    assert 'id="prefill-form-revision"' not in INDEX_HTML
    assert "queueFormRevisionStage" not in APP_JS
    assert "click Google’s Submit button yourself" not in APP_JS
    assert ".form-submit-preflight" in STYLES_CSS
    assert ".form-submit-route" in STYLES_CSS
    assert ".form-answer-row.has-preflight-error" in STYLES_CSS


def test_uncertain_form_submission_has_an_explicit_non_duplicate_recovery_path() -> None:
    assert 'result.submission_state === "uncertain"' in APP_JS
    assert "showFormSubmissionRecovery(revision, result)" in APP_JS
    assert 'safeHttpUrl(revision.form_url || result.form_url)' in APP_JS
    assert 'resolveFormSubmissionOutcome("submitted"' in APP_JS
    assert 'resolveFormSubmissionOutcome("not_submitted"' in APP_JS
    assert '/resolve-submission`' in APP_JS
    assert "expected_revision: Number(revision.revision)" in APP_JS
    assert "schema_hash: revision.schema_hash" in APP_JS
    assert 'await confirmAction({' in APP_JS
    assert "window.confirm(" not in APP_JS
    assert "I verified it was submitted" in INDEX_HTML
    assert "I verified it was not submitted — prepare retry" in INDEX_HTML
    assert "No action here submits the form again." in INDEX_HTML
    assert ".form-submission-recovery" in STYLES_CSS
    assert 'revision?.status === "submitted"' in APP_JS
    assert 'result?.submission_state === "confirmed"' in APP_JS
    assert "Submission recorded after your verification" in APP_JS
    assert "resolution.rescan_required !== false" in APP_JS
    assert "Fresh scan starting" in APP_JS
    assert 'result.submission_state === "not_attempted"' in APP_JS
    assert "setFormScanRetry(state.selectedFormApplicationId, true)" in APP_JS
    assert "baselineRevisionId" in APP_JS
    assert 'scanStatus === "succeeded"' in APP_JS
    assert "scanJob.progress?.scan_revision_id" in APP_JS
    assert "revision.id !== baselineRevisionId" in APP_JS
    assert "job.form_revision_id || job.payload?.form_revision_id" in APP_JS
    assert "applicationRevisions.length === 1" in APP_JS


def test_form_recovery_rescan_keeps_the_fallback_revision_locked() -> None:
    assert "formRecoveryScanApplicationIds: new Set()" in APP_JS
    assert "function formPreparationIsActive(applicationId)" in APP_JS
    assert "state.formRecoveryScanApplicationIds.has(applicationId)" in APP_JS

    preflight = APP_JS.split("function refreshFormSubmitPreflight", 1)[1].split(
        "function renderFormRevision", 1
    )[0]
    assert "if (formPreparationIsActive(applicationId))" in preflight
    assert 'button.textContent = running ? "Capturing current form…" : "Waiting for current form…"' in preflight
    assert "button.disabled = true" in preflight
    assert "Nothing can be submitted during this refresh." in preflight

    recovery = APP_JS.split("async function resolveFormSubmissionOutcome", 1)[1].split(
        "function formRevisionAnswers", 1
    )[0]
    assert "state.formRecoveryScanApplicationIds.add(applicationId)" in recovery
    assert "loadApplicationFormRevisions(applicationId, true, false, identity)" in recovery
    assert "await scanJobApplication(job, providerForJob(job) || revision.provider || \"google_forms\", null)" in recovery
    assert "window.setTimeout" not in recovery
    assert recovery.index("state.formRecoveryScanApplicationIds.add(applicationId)") < recovery.rindex(
        "loadApplicationFormRevisions(applicationId, true, false, identity)"
    )

    submit = APP_JS.split("async function approveAndSubmitFormRevision", 1)[1].split(
        "function updateApplicationCharacterCount", 1
    )[0]
    assert submit.count("formPreparationIsActive(applicationId)") >= 3
    assert "Current form is still loading" in submit
    assert "Nothing was queued; review the newly captured fields" in submit


def test_old_form_submission_monitor_cannot_overwrite_a_new_revision() -> None:
    assert "function formRevisionIsCurrent(revision, applicationId = state.selectedFormApplicationId)" in APP_JS
    current_guard = APP_JS.split("function formRevisionIsCurrent", 1)[1].split(
        "function renderFormSubmissionJob", 1
    )[0]
    assert "state.selectedFormApplicationId === applicationId" in current_guard
    assert "state.selectedFormRevisionId === revision.id" in current_guard
    assert "latestFormRevision(applicationId)?.id === revision.id" in current_guard

    renderer = APP_JS.split("function renderFormSubmissionJob", 1)[1].split(
        "function refreshFormSubmitPreflight", 1
    )[0]
    assert "!formRevisionIsCurrent(revision, applicationId)" in renderer
    assert "formPreparationIsActive(applicationId)" in renderer

    submit = APP_JS.split("async function approveAndSubmitFormRevision", 1)[1].split(
        "function updateApplicationCharacterCount", 1
    )[0]
    assert "if (formRevisionIsCurrent(revision, applicationId))" in submit
    assert "if (!formRevisionIsCurrent(revision, applicationId)) return;" in submit


def test_captured_google_listboxes_render_as_exact_reviewable_options() -> None:
    assert '["select", "combobox", "listbox", "radio", "dropdown", "multiselect", "checkbox"]' in APP_JS
    assert 'control = createElement("select"' in APP_JS


def test_explicit_company_form_marker_exposes_the_reviewed_scan_action() -> None:
    assert "job?.metadata?.application_provider" in APP_JS
    assert '"company_form"' in APP_JS


def test_public_resume_url_is_distinct_from_private_resume_upload() -> None:
    profile_view = INDEX_HTML.split('id="view-profile"', 1)[1].split(
        'id="view-discovery"', 1
    )[0]
    assert 'id="profile-resume-url"' in profile_view
    assert 'name="resume_url"' in profile_view
    assert "Public résumé link" in profile_view
    assert "separate from the private PDF" in profile_view
    assert '"profile-resume-url": profile.resume_url' in APP_JS
    assert 'resume_url: nullable("profile-resume-url")' in APP_JS
    completeness = APP_JS.split("function renderProfileCompleteness", 1)[1].split(
        "async function saveProfile", 1
    )[0]
    assert '["Public résumé link", byId("profile-resume-url").value]' in completeness
    resume_apply = APP_JS.split("function applyResumeSuggestions", 1)[1].split(
        "function renderJobIntelligence", 1
    )[0]
    assert "profile-resume-url" not in resume_apply


def test_form_pilot_merges_server_profile_answers_without_an_extra_resume_input() -> None:
    form_pilot_view = INDEX_HTML.split('id="view-form-pilot"', 1)[1].split(
        'id="view-outreach"', 1
    )[0]
    assert 'id="profile-resume-url"' not in form_pilot_view
    assert 'const profileAnswers = revision.profile_answers' in APP_JS
    assert 'const answers = { ...storedAnswers, ...cachedAnswers, ...profileAnswers }' in APP_JS
    assert "Sealed revisions deliberately receive an empty profile_answers object" in APP_JS
    auto_suggest = APP_JS.split("function formRevisionCanAutoSuggest", 1)[1].split(
        "function formControlHasAnswer", 1
    )[0]
    assert "getGroqKey()" not in auto_suggest
    request_suggest = APP_JS.split("async function requestFormSuggestions", 1)[1].split(
        "async function maybeSuggestFormAnswers", 1
    )[0]
    assert '...(credentialConfigured("groq") ? { groq: true } : {})' in request_suggest
    assert 'id="suggest-form-answers"' not in form_pilot_view
    assert "Regenerate with Groq" not in form_pilot_view
    assert 'byId("suggest-form-answers").addEventListener' not in APP_JS


def test_jobs_library_removes_duplicate_manual_editor_but_keeps_form_pilot() -> None:
    jobs_view = INDEX_HTML.split('id="view-jobs"', 1)[1].split(
        'id="view-applications"', 1
    )[0]
    assert 'id="job-form"' not in jobs_view
    assert 'id="yc-application-desk"' not in jobs_view
    assert "Imported roles" in jobs_view
    assert "Draft with Groq" in APP_JS
    assert "scanJobApplication" in APP_JS
    assert ".yc-application-route" in STYLES_CSS
