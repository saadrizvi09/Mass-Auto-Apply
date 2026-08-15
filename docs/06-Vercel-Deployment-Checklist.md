# AutoApply Cloud — Vercel production deployment checklist

Last reviewed: 2026-08-15

Use this file when moving the current local SaaS application to a public domain. It
covers the Vercel control plane, Supabase, Google login, Gmail sending, Turnstile, the
separate worker, Browserbase, and the checks required before opening the product to
other users. The deployed sidebar journey is **Profile → Find jobs → Form Pilot →
Mass Cold Email**. The final destination contains **Build campaign → Review & send**
email-only subtabs. Form Pilot separately owns Google Form scan, browser-side answer
suggestion display backed by an encrypted account Groq credential, exact review, and the user's one explicit **Approve & submit in
background** action. The worker may submit only that immutable approved revision and
may report success only after a fresh provider confirmation. Browserbase Live View is
an observable `needs_attention` fallback, not the normal completion step.

> [!IMPORTANT]
> Vercel hosts the static frontend and short-lived FastAPI control plane. It does not
> host the continuous Python process that polls the Supabase automation queue. A
> complete deployment therefore needs Vercel, Supabase, and a separate persistent
> Docker worker host.

## 1. Record the final production values

Choose one canonical HTTPS origin before configuring any provider. Replace the example
values below once, then use the exact same values in every dashboard.

| Name | Production value |
| --- | --- |
| `PRODUCTION_ORIGIN` | `https://app.example.com` |
| `PRODUCTION_HOSTNAME` | `app.example.com` |
| `SUPABASE_PROJECT_REF` | `tksoeyvlrdmvyuzdunwn` |
| `SUPABASE_URL` | `https://tksoeyvlrdmvyuzdunwn.supabase.co` |
| Supabase Google callback | `https://tksoeyvlrdmvyuzdunwn.supabase.co/auth/v1/callback` |
| Gmail callback | `https://app.example.com/api/v1/oauth/google/callback` |
| Stable staging origin | `https://staging.example.com` or a stable Vercel branch alias |
| Worker host | Name of the persistent Docker/background-worker service |

Do not switch between a `vercel.app` URL and a custom domain after OAuth is configured.
Pick the canonical domain, redirect the other hostname to it, and use only the canonical
origin for `SITE_URL` and Gmail OAuth.

## 2. Understand the hosting limits before launch

- Vercel Functions are stateless and request-bounded. Do not store files, OAuth state,
  sessions, or worker state on the Vercel filesystem.
- Vercel Function request and response bodies are limited to 4.5 MB. The résumé flow
  already uploads directly from the browser to private Supabase Storage, so do not
  change it to proxy PDFs through FastAPI.
- The current worker runs `python -m worker.main` as an infinite Supabase queue poller.
  It has no HTTP port and cannot run as a Vercel Function. Hobby Cron is not a
  replacement for a persistent adaptive queue poller.
- Vercel Hobby is for personal, non-commercial use. A free non-commercial beta can use
  it, but a public commercial product requires Vercel Pro or a different host.
- Browserbase Free is suitable for controlled prototypes, not unrestricted public
  browser automation. Normal Google Forms success does not depend on retaining a Live
  View session; keep-alive is relevant only when an attention fallback must remain
  available. Keep browser automation disabled until account limits, worker capacity,
  and provider canaries support it.
- Browserbase bills every created session for at least one minute. AutoApply closes
  sessions immediately when work finishes and applies a 90-second stall cap; this
  reduces runaway/stalled usage but does not reduce a sub-minute run below the
  one-minute minimum.

References: [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi),
[Function limits](https://vercel.com/docs/functions/limitations),
[Hobby plan](https://vercel.com/docs/plans/hobby), and
[Cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing).

## 3. Complete these repository changes before the first deploy

- [ ] Review `git status`. Most of the hosted SaaS files are currently new/untracked.
      Add and commit every intended production file before using Git-connected Vercel.
- [ ] Do not deploy this dirty working tree directly. Create a reviewed deployment
      branch or commit containing `public/`, `app/saas/`, `app/saas_main.py`,
      `supabase/`, `worker/`, `vercel.json`, `pyproject.toml`, `.python-version`, and
      `.vercelignore`.
- [ ] Confirm `.env`, `.env.*`, PDFs, browser profiles, databases, and OAuth exports
      are excluded from the deployment bundle. The obsolete tracked `auth_link.html`
      artifact has been removed and is ignored so it cannot be recommitted.
- [ ] Replace every `[REQUIRED BEFORE PUBLIC LAUNCH]` value in `public/privacy.html` and
      `public/terms.html`: operator/entity, dates, address, support/privacy contacts,
      jurisdiction, provider/retention details, and refund terms if applicable.
- [ ] Keep the Vercel project root at the repository root (`.`). Do not select `public/`
      or `app/` as the project root.
- [ ] Keep Python 3.12. The repository pins it in `.python-version` and `pyproject.toml`.
- [x] Keep the FastAPI entry point as `app.saas_main:app`.
- [x] Keep the Vercel Function region at Mumbai (`"regions": ["bom1"]`) because the
      Supabase project is in Mumbai.
- [ ] Inspect the actual bundle before uploading:

  ```bash
  vercel deploy --dry --format=json
  ```

References: [Vercel regions](https://vercel.com/docs/regions),
[`.vercelignore`](https://vercel.com/docs/deployments/vercel-ignore), and
[dry-run deployments](https://vercel.com/changelog/dry-run-deployments-with-vercel-cli).

## 4. Rotate the credentials exposed during development

The Supabase secret credential, Browserbase API key, and Google OAuth client secret were
shared in development conversation/screens. Rotate all three before any public deploy.
Do not reuse the pasted values.

- [ ] Supabase Dashboard → Project Settings → API Keys: create/rotate the server secret.
- [ ] Browserbase Dashboard → Settings: rotate the API key.
- [ ] Google Cloud Console → Google Auth Platform → Clients: rotate the OAuth client
      secret or create fresh production clients.
- [ ] Replace the rotated values in Vercel and the worker host.
- [ ] Redeploy after changing Vercel variables. Environment changes affect only new
      deployments.
- [ ] Generate one new Fernet `TOKEN_ENCRYPTION_KEY`, save it in a password manager, and
      use the identical value in Vercel and every worker replica:

  ```bash
  python3.12 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  ```

Do not casually rotate or lose `TOKEN_ENCRYPTION_KEY` after launch. Existing encrypted
Gmail tokens, user-owned Google client secrets, managed-browser context identifiers,
and account-scoped Groq/Hunter/Browserbase credentials would become unreadable. Users
would need to reconnect and re-enter provider credentials.

Reference: [Vercel secret rotation](https://vercel.com/docs/environment-variables/rotating-secrets).

## 5. Apply and verify Supabase migrations

From the repository root:

```bash
supabase login
supabase link --project-ref tksoeyvlrdmvyuzdunwn
supabase db push --dry-run
supabase db push
supabase db lint --linked --level warning
```

- [ ] Confirm every migration through
      `202608150003_yc_exact_job_automation.sql` was applied in filename order.
- [ ] Confirm `user_provider_credentials` has RLS enabled, no browser policy or
      `public`/`anon`/`authenticated` grant, and only `service_role` access. Its allowed
      providers must be exactly `groq`, `hunter`, and `browserbase`.
- [ ] Confirm save/delete RPCs increment credential generations, write only secret-free
      audit metadata, cascade on account deletion, and block Browserbase replacement or
      deletion while owned browser jobs/contexts are active.
- [ ] Do not stop at `202608130001_google_forms_manual_submit.sql`. That migration was a
      temporary fail-closed prohibition. The forward migration
      `202608130002_google_forms_approved_submit.sql` removes it and installs the strict
      latest-revision/required-answer gate for Google Forms background submission.
- [ ] Confirm `202608130002_google_forms_approved_submit.sql` created
      `guard_google_forms_approved_submit`, the
      `automation_jobs_google_forms_approved_submit` trigger, and the
      `automation_jobs_one_submit_revision_key_idx` unique partial index; confirm the
      superseded `guard_google_forms_manual_submit` function/trigger no longer exists.
- [ ] Confirm `202608130003_profile_public_resume_url.sql` added
      `profiles.resume_url` with the public HTTPS constraint and authenticated update
      grant. This column is not a private Storage object path or an expiring signed URL.
- [ ] Confirm `202608130004_company_form_provider.sql` and
      `202608130005_profile_form_answer_sync.sql` are installed. The latter's refresh
      RPC must remain service-role-only and must refuse active, submitted, or uncertain
      application attempts.
- [ ] Confirm `202608150003_yc_exact_job_automation.sql` rejects non-job YC targets,
      binds every YC target/revision/job to one tenant-owned exact current public URL,
      keeps YC preferences tenant-only and non-executable, and allows only
      `application_scan`, `application_prefill`, and `application_submit` provider work.
      Confirm it also removes old generic company-form bindings for every YC-owned root
      or subdomain and prevents those hosts from being rebound through `company_form`.
- [ ] Confirm `202608150004_profile_form_answer_count_lint.sql` is installed and the
      linked Supabase schema linter no longer reports `jsonb_object_length(jsonb)` in
      `refresh_application_form_profile_answers_for_user`.
- [ ] Confirm `202608140001_form_submit_attention_snapshot.sql` installed the
      service-role-only `complete_application_form_submit_attention` RPC. An uncertain
      or provider-confirmed click must atomically persist a durable form-revision fence
      and close the worker job; deleting an old terminal queue row must not make that
      revision reusable.
- [ ] Confirm all public tenant tables have Row Level Security enabled.
- [ ] Confirm the `resumes` Storage bucket is private, PDF-only, 6 MB maximum, and uses
      user-prefixed fixed slots.
- [ ] Confirm authenticated users cannot directly mutate protected résumé,
      application, connection-secret, OAuth-state, or automation tables outside the
      intended APIs/RPCs.
- [ ] Run Supabase Security Advisor and resolve every actionable finding.
- [ ] Confirm the provider-send retention cron exists at `17 * * * *`.
- [ ] Keep the Data API enabled, but disable **Automatically expose new tables** and
      explicitly review RLS/grants whenever adding a table.

`supabase db push` does not configure Auth provider credentials, SMTP, CAPTCHA, Site URL,
or redirect URLs. Configure those separately in the dashboard.

Reference: [Supabase production checklist](https://supabase.com/docs/guides/deployment/going-into-prod).

## 6. Configure Supabase Auth URLs

Supabase Dashboard → Authentication → URL Configuration:

- [ ] Set **Site URL** to the exact canonical origin:

  ```text
  https://app.example.com
  ```

- [ ] Add the production return URL(s) to **Redirect URLs**. The current UI returns to
      `/` for Google login and uses `/?recovery=1` for password recovery. A production
      allowlist entry can be:

  ```text
  https://app.example.com/**
  ```

- [ ] Keep local development as an additional entry, not as the Site URL:

  ```text
  http://127.0.0.1:8000/**
  ```

- [ ] If preview login is intentionally enabled, use Supabase's documented Vercel
      pattern and restrict it to your own account/team slug:

  ```text
  https://*-YOUR_VERCEL_TEAM_SLUG.vercel.app/**
  ```

Use an exact production domain. Do not broadly wildcard attacker-controlled domains.
Prefer a separate staging Supabase project over sharing production data with previews.

Reference: [Supabase redirect URLs](https://supabase.com/docs/guides/auth/redirect-urls).

## 7. Configure the two Google OAuth flows separately

AutoApply uses two different Google authorizations. Use separate Google Cloud projects
for the cleanest production boundary (or at minimum separate Web OAuth clients). Google
consent-screen scopes and verification are project-level, so a dedicated identity-only
project is the most reliable way to keep sign-in independent from Gmail verification.

| Flow | Purpose | Credentials live in | Scopes | Exact Google redirect URI |
| --- | --- | --- | --- | --- |
| Supabase Google login | Create/sign in to AutoApply | Supabase Auth provider settings | `openid`, `email`, `profile` | `https://tksoeyvlrdmvyuzdunwn.supabase.co/auth/v1/callback` |
| Gmail connection | Send a reviewed email from the user's Gmail | Vercel `GOOGLE_*` variables, or encrypted user-owned client | identity + `https://www.googleapis.com/auth/gmail.send` | `https://app.example.com/api/v1/oauth/google/callback` |

### 7.1 Supabase “Continue with Google” client

Google Cloud Console → Google Auth Platform → Clients → Web application:

- [ ] Add the production JavaScript origin:

  ```text
  https://app.example.com
  ```

- [ ] Add the exact Supabase callback—no trailing slash:

  ```text
  https://tksoeyvlrdmvyuzdunwn.supabase.co/auth/v1/callback
  ```

- [ ] Put this client's ID and secret in Supabase Dashboard → Authentication →
      Providers → Google. Do not put the Supabase Google-login secret in Vercel.
- [ ] Keep this client identity-only. It must not request `gmail.send`.
- [ ] Prefer a dedicated identity-only Google Cloud project. Reusing the Gmail project's
      consent screen can make ordinary sign-in inherit its sensitive-scope verification
      state even when the authorization request itself is identity-only.

Reference: [Supabase Google login](https://supabase.com/docs/guides/auth/social-login/auth-google).

### 7.2 Gmail-send OAuth client

Google Cloud Console:

- [ ] Enable the Gmail API.
- [ ] Create a Web application OAuth client for the production app.
- [ ] Add the production origin:

  ```text
  https://app.example.com
  ```

- [ ] Add the exact FastAPI callback—no trailing slash:

  ```text
  https://app.example.com/api/v1/oauth/google/callback
  ```

- [ ] Configure only identity scopes and `gmail.send`; do not request read, modify, or
      full-mailbox scopes.
- [ ] Set Audience to External.
- [ ] During local testing, add every tester email under **Audience → Test users**.
- [ ] Before public launch, publish the app and complete Google's sensitive-scope
      verification for `gmail.send`.
- [ ] Verify the root domain through Google Search Console.
- [ ] Provide a public homepage, privacy policy, and terms page on the verified domain.
- [ ] Prepare Google's requested scope justification and a demo video of the complete
      Gmail consent/send/disconnect flow.

While the Gmail app is in Testing, only added testers can authorize it and Gmail refresh
tokens normally expire after seven days. The normal identity-only Supabase Google login
does not need the Gmail permission.

References: [Google web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server),
[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), and
[Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies).

### 7.3 Google error quick fixes

| Google screen/error | Meaning | Fix |
| --- | --- | --- |
| `Error 403: access_denied`, developer-approved testers only | OAuth app is in Testing and the account is not allowlisted | Audience → Test users → add the exact email, save, then retry |
| “Google hasn't verified this app” | Tester reached a sensitive-scope unverified flow | A developer/tester may use Advanced → continue for testing; public users must wait for verification |
| `redirect_uri_mismatch` | The URI sent by AutoApply does not exactly match the Google client | Add the exact Supabase or Gmail callback from the table above; check scheme, host, path, port, and trailing slash |

## 8. Configure production Turnstile

Cloudflare Dashboard → Turnstile:

- [ ] Create a separate production widget. Do not use Cloudflare's dummy/test keys.
- [ ] Add `app.example.com` as a hostname without scheme, path, or port.
- [ ] Put the public site key in Vercel as `TURNSTILE_SITE_KEY`.
- [ ] Put the matching secret only in Supabase Dashboard → Authentication → Bot and
      Abuse Protection.
- [ ] Never put the Turnstile secret in Vercel, `.env.example`, or browser JavaScript.
- [ ] Deploy and verify the public widget first; only then enable CAPTCHA enforcement in
      Supabase, so a bad hostname/key cannot lock out every user.
- [ ] Use separate widgets for local, staging, and production environments.

References: [Turnstile setup](https://developers.cloudflare.com/turnstile/get-started/),
[hostname management](https://developers.cloudflare.com/turnstile/additional-configuration/hostname-management/),
and [Supabase CAPTCHA](https://supabase.com/docs/guides/auth/auth-captcha).

## 9. Configure production SMTP in Supabase

Supabase's default Auth mail service is not suitable for a public product: it is
development-limited, best-effort, and normally sends only to project-team addresses.

- [ ] Configure a custom SMTP provider in Supabase Authentication settings.
- [ ] Set host, port, username, password, sender address, and sender name.
- [ ] Configure SPF, DKIM, and DMARC for the sender domain.
- [ ] Test signup confirmation, email change, magic link/OTP if enabled, and password
      reset from an address that is not a Supabase project member.
- [ ] Review and raise Auth email rate limits only after abuse protection is working.

Reference: [Supabase custom SMTP](https://supabase.com/docs/guides/auth/auth-smtp).

## 10. Create the Vercel project

- [ ] Import the reviewed Git repository or run `vercel link` at the repository root.
- [ ] Leave Framework Preset/build/output settings on automatic FastAPI detection.
- [ ] Confirm the Python entrypoint is `app.saas_main:app`.
- [ ] Confirm the Function region is Mumbai (`bom1`).
- [ ] Add and verify the canonical custom domain.
- [ ] Keep frontend and API on the same Vercel origin. The current app intentionally has
      no cross-origin API configuration.
- [ ] Confirm Fluid Compute is enabled for upstream calls that may take tens of seconds.
- [ ] Add HSTS only after the final HTTPS domain and subdomain policy are stable.

Reference: [Vercel custom domains](https://vercel.com/docs/domains/set-up-custom-domain).

## 11. Add Vercel environment variables

Add variables through Vercel Project Settings or interactive `vercel env add`. Never
paste secret values into a shell command that will be saved in history.

### Required control-plane variables

| Variable | Production value/pattern | Secret? | Notes |
| --- | --- | --- | --- |
| `SUPABASE_URL` | `https://tksoeyvlrdmvyuzdunwn.supabase.co` | No | Returned by `/api/v1/config` |
| `SUPABASE_PUBLISHABLE_KEY` | Current `sb_publishable_…` value | No | Safe browser credential; RLS remains mandatory |
| `SUPABASE_SECRET_KEY` | Newly rotated server secret | Yes | Never expose through `/api/v1/config` |
| `SITE_URL` | `https://app.example.com` | No | Exact canonical origin; no trailing slash |
| `TURNSTILE_SITE_KEY` | Production widget site key | No | Public key only |
| `TOKEN_ENCRYPTION_KEY` | New backed-up Fernet key | Yes | Must exactly match the worker |
| `GOOGLE_REDIRECT_URI` | `https://app.example.com/api/v1/oauth/google/callback` | No | Exact fixed Gmail callback |

### Gmail platform client

| Variable | Secret? | Notes |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` | No | Gmail-send Web client, not Supabase login client |
| `GOOGLE_CLIENT_SECRET` | Yes | Gmail-send client secret |

Both are required for the default “Use AutoApply's Google app” Gmail path. They may be
left empty only if the product intentionally supports user-owned Google clients alone.

### Optional tuning and managed-browser variables

| Variable | Suggested initial value | Secret? |
| --- | --- | --- |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | No |
| `MAX_RESUME_BYTES` | `6291456` | No |
| `DEFAULT_DAILY_SEND_CAP` | `10` | No |
| `OAUTH_STATE_TTL_SECONDS` | `600` | No |
| `SUPABASE_HTTP_TIMEOUT_SECONDS` | `15` | No |
| `BROWSERBASE_API_KEY` | Rotated Browserbase key | Yes |
| `BROWSERBASE_PROJECT_ID` | Browserbase project ID | No |
| `ALLOWED_BROWSER_PROVIDERS` | Empty for first production deploy | No |

The Browserbase environment pair is an optional platform fallback. For every claimed
browser job, the worker prefers that account owner's validated encrypted BYOK pair and
uses the fallback only when no owned credential exists. Configure both fallback values
or neither; never combine a user API key with the platform Project ID (or vice versa).

Before rotating an operator fallback after launch, stop new managed-browser work,
drain or cancel every queued/running browser job, and disconnect all retained managed
browser connections while the old key still works. Only then replace both fallback
values together in the API and worker environments and redeploy them. A Browserbase
context belongs to the project that created it; replacing the fallback first can leave
the product unable to delete an old-project context with the new key.

Do not configure these in Vercel:

- `GROQ_API_KEY`: each user supplies a key encrypted in their service-role-only account
  credential row.
- `HUNTER_API_KEY`: each user supplies a key encrypted in the same account-scoped
  store; contact search resolves it only for the authenticated owned request.
- `SUPABASE_JWKS_URL`: the application does not consume it.
- Turnstile secret: it belongs in Supabase Auth.
- Supabase Google-login secret: it belongs in Supabase Auth.
- Worker identity/polling variables: they belong on the worker host.

The local Supabase provider secret is named
`SUPABASE_AUTH_EXTERNAL_GOOGLE_CLIENT_SECRET`; this avoids confusing it with the
application's Gmail-send `GOOGLE_CLIENT_SECRET`.

Reference: [Vercel environment variables](https://vercel.com/docs/environment-variables).

## 12. Use a stable staging environment

Random Vercel preview URLs cannot reliably share one fixed `SITE_URL`, Gmail callback,
and Turnstile hostname.

- [ ] Prefer a separate staging Supabase project, Google clients, Turnstile widget, and
      encryption key.
- [ ] Use a stable staging custom domain or stable Vercel branch alias.
- [ ] Do not expose production secrets to untrusted pull-request previews.
- [ ] If ephemeral previews are enabled, leave platform Gmail OAuth and managed-browser
      execution disabled there.
- [ ] Never share production data with a preview that uses a different
      `TOKEN_ENCRYPTION_KEY`.

## 13. Validate and deploy Vercel

Run locally first:

```bash
python3.12 -m venv .venv-saas
source .venv-saas/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
npm --prefix frontend ci
npm --prefix frontend run check
```

Then deploy a preview:

```bash
npm install -g vercel@latest
vercel login
vercel link
vercel env ls preview
vercel env ls production
vercel deploy --dry --format=json
vercel deploy
```

After the stable staging domain passes the smoke tests:

```bash
vercel deploy --prod
```

Any environment-variable change requires a new deployment.

## 14. Deploy the worker separately

The worker is a continuous Python/Supabase queue poller with no HTTP port or built-in
health endpoint. Locally, `./dev.command` starts it alongside FastAPI and stops both on
Control-C. Hosted deployment needs outbound HTTPS, an always-running Docker/background
process, and automatic restart.

### 14.1 Select the hosting model

| Option | Decision for the current worker |
| --- | --- |
| [Northflank Sandbox](https://northflank.com/pricing) | Recommended only for free testing or a risk-accepted public beta. Its Sandbox tier advertises always-on free services, and [a Northflank service can run without ports](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code), which matches this container. It is explicitly not AutoApply's production recommendation; assume no SLA or permanent free entitlement. |
| [OCI Always Free Compute](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) | More durable zero-cost, self-managed option. Run the container under a restart supervisor, patch and secure the VM, and monitor it. Home-region capacity can be unavailable and Oracle may reclaim instances it classifies as idle, so keep a reprovisioning plan and do not promise uninterrupted free production. |
| [AWS Free Tier](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-FAQ.html) | Temporary option, not an indefinite free-worker plan. New-customer free plans currently end after six months or when credits are exhausted; configure budgets and a migration/paid plan before expiry. Legacy-account eligibility can differ. |
| [Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs) | Future architecture only. Jobs run tasks and exit, so the current infinite loop is incompatible. First implement a bounded **drain-and-exit** mode with scheduled invocations, safe lease/heartbeat shutdown, retries, and a clean terminal exit. |
| [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-overview) | Do not use for the current worker. Free Spaces sleep when unused and follow an interactive web-app lifecycle, while this process exposes no port. |
| [Koyeb Free](https://www.koyeb.com/docs/run-and-scale/scale-to-zero) | Do not use for the current worker. Its free Web Service scales to zero after one hour without inbound traffic; this outbound Supabase poller has no traffic-driven wake path. Do not generate synthetic traffic to avoid sleep. |
| [Vercel Functions](https://vercel.com/docs/functions/limitations) | Do not use for the current worker. Functions are request-bounded; Vercel remains the frontend and short-lived API control plane. |

Northflank's free Sandbox is therefore the simplest managed beta test, not production.
OCI Always Free is the more durable zero-cost path when self-management is acceptable,
but it still has capacity/reclamation risk. Recheck current limits, pricing, terms, and
regional availability immediately before deploying; no provider guarantees perpetual
free production hosting.

### 14.2 Build and configure

Build from the repository root because the Dockerfile also copies `app/saas/`:

```bash
docker build -f worker/Dockerfile -t autoapply-worker .
docker run --env-file .env.worker autoapply-worker
```

Do not set `worker/` as the Docker build root.

### Worker variables

```dotenv
SUPABASE_URL=https://tksoeyvlrdmvyuzdunwn.supabase.co
SUPABASE_SECRET_KEY=ROTATED_SERVER_SECRET
SUPABASE_HTTP_TIMEOUT_SECONDS=15

WORKER_ID=autoapply-worker-prod-1
WORKER_POLL_SECONDS=2
WORKER_MAX_IDLE_POLL_SECONDS=15
WORKER_LEASE_SECONDS=120
WORKER_HEARTBEAT_SECONDS=20
WORKER_LOG_LEVEL=INFO

TOKEN_ENCRYPTION_KEY=SAME_FERNET_KEY_AS_VERCEL
# Optional platform fallback; omit both when relying exclusively on user BYOK.
BROWSERBASE_API_KEY=ROTATED_BROWSERBASE_KEY
BROWSERBASE_PROJECT_ID=YOUR_BROWSERBASE_PROJECT_ID
ALLOWED_BROWSER_PROVIDERS=
```

The worker checks quickly while work is active, then exponentially backs off consecutive
empty claims toward `WORKER_MAX_IDLE_POLL_SECONDS`. Any claimed job resets the idle
delay. A healthy idle deployment does not print each successful Supabase HTTP request;
monitor worker lifecycle, claimed-job, completion, and warning entries instead.

- [ ] Give every worker replica a unique `WORKER_ID`.
- [ ] Use the exact Vercel encryption key and provider allowlist. If a platform
      Browserbase fallback is configured, use the exact same pair in trusted API/worker
      environments.
- [ ] A discovery-only worker needs Supabase URL/secret and worker identity; managed
      browser jobs additionally need encryption and either the job owner's verified
      Browserbase BYOK pair or the optional fallback settings.
- [ ] Start with `ALLOWED_BROWSER_PROVIDERS` empty.
- [ ] Once Browserbase capacity and controlled canaries pass, begin with only:

  ```dotenv
  ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
  ```

- [ ] Canary every other provider independently before adding it. YC has a finished
      exact saved-job state machine but remains launch-gated pending a controlled
      signed-in end-to-end canary. After it passes, add it explicitly:

  ```dotenv
  ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse,yc
  ```

- [ ] Keep the generic exact-host `company_form` adapter internal/gated. It is not a
      public provider catalog or allowlist entry; do not advertise or enable it from
      code presence alone.
- [ ] Before enabling Google Forms résumé upload, connect one isolated test Google
      Browserbase context and run a non-submitting file-upload canary. It must find
      exactly one explicit résumé/CV `Add file` question, keep the picker on an exact
      trusted Google picker host/path, expose exactly one PDF-compatible input, attach
      only the lease-bound private PDF, verify matching filename/MIME/byte length, and
      observe that filename in the original question after the picker closes. A missing
      context, login/MFA/CAPTCHA, multiple upload destinations, non-PDF restriction,
      unexpected iframe, or unverified completion must stop before the form's Submit
      control. Mocked tests are not evidence that current Google markup passed.
- [ ] Monitor process uptime, oldest queued job age, expired leases, repeated queue
      errors, Browserbase 429s, and session failures.
- [ ] Test SIGTERM recovery: an interrupted job should be reclaimable after lease expiry.

Credential-free Telegram/RSS, LinkedIn guest discovery, and public ATS discovery use the
worker but do not require Browserbase. `POST /api/v1/discovery/resume-guided` resolves
the caller's encrypted account Groq credential to derive bounded search terms, then
queues LinkedIn guest and public-feed work without the key. The worker filters
Telegram/RSS results against those terms. LinkedIn guest discovery remains unofficial,
bounded, credential-free, and unrelated to Easy Apply. Managed-browser scan,
approval-bound submission, verified confirmation, and any needs-attention Live View do
require Browserbase.

Vercel hosts only the bounded API/UI and queue endpoints. It must never launch Chromium,
connect Playwright to Browserbase, or run the queue poller. A separate persistent worker
uses Playwright over Browserbase CDP with the claimed tenant's BYOK credential and
isolated provider context.

Browserbase account setup:

1. Create a free account at <https://www.browserbase.com/sign-up>.
2. Copy the API key from <https://www.browserbase.com/settings>.
3. Copy the Project ID from <https://www.browserbase.com/overview>.
4. Save both through `PUT /api/v1/provider-credentials/browserbase`.

Validation must call read-only
`GET https://api.browserbase.com/v1/projects/{project_id}` with `X-BB-API-Key`, require
HTTP 200 plus a matching response ID, and create no session. Verify worker sessions use
the owned pair before the fallback, close immediately, and stop at the 90-second stall
cap. Browserbase's one-minute minimum still applies to each created session.

References: [Browserbase pricing](https://www.browserbase.com/pricing) and
[Browserbase cost optimization](https://docs.browserbase.com/optimizations/cost/cost-optimization),
[project validation API](https://docs.browserbase.com/reference/api/get-a-project), and
[Browserbase contexts](https://docs.browserbase.com/platform/browser/core-features/contexts).

## 15. Production smoke test

### Public and security checks

- [ ] `/`, `/app.js`, `/styles.css`, `/privacy.html`, and `/terms.html` return 200.
- [ ] `/api/v1/config`, `/api/v1/health`, `/api/v1/providers`, and
      `/api/openapi.json` return expected responses.
- [ ] `/api/v1/profile` without a bearer token returns 401.
- [ ] `/api/v1/config` contains the exact production Site URL and no secret, OAuth token,
      encryption key, user Groq/Hunter/Browserbase secret, project ID, or ciphertext.
- [ ] Confirm provider-credential request bodies, decrypted envelopes, ciphertext,
      Browserbase CDP URLs, and provider headers are absent from function, worker,
      proxy, analytics, and observability logs; no user Groq/Hunter key is configured in
      Vercel or the worker host.
- [ ] Confirm CSP, referrer, permissions, nosniff, and frame-deny headers on public pages.
- [ ] Confirm API responses are `private, no-store`.
- [ ] Inspect every `/api/v1/health` check. `status=ready` alone does not prove Gmail,
      Browserbase, encryption, provider allowlists, or a live worker.

### User journey checks

- [ ] Email signup, confirmation, password reset, session restore, and signout work.
- [ ] Supabase “Continue with Google” works without Gmail permissions.
- [ ] Turnstile expiration/retry works and a bad challenge fails closed.
- [ ] Two test accounts cannot read each other's profiles, résumés, jobs, applications,
      connections, or activity.
- [ ] PDF résumé upload goes directly to Supabase Storage; parse/analyze/delete work.
- [ ] Résumé analysis fills only blank profile fields for review, including LinkedIn,
      GitHub, college, degree, and passout year when explicitly present.
- [ ] A common passout/year-of-graduation question is filled deterministically from
      `profiles.graduation_year`. A recognized résumé/CV link question is filled only
      from the user's separately saved public HTTPS `profiles.resume_url`; the private
      uploaded PDF path, a signed Storage URL, and a guessed/extracted link are never
      substituted for it.
- [ ] Jobs are ranked by explainable résumé alignment and the UI states that this is not
      a hiring prediction.
- [ ] `PUT /api/v1/provider-credentials/groq` validates and stores only encrypted
      ciphertext; `GET /api/v1/provider-credentials` exposes only a safe hint/status,
      and owned résumé analysis/drafting works without a key in browser storage,
      responses, logs, or another account.
- [ ] From **Find jobs**, `POST /api/v1/discovery/resume-guided` with the verified owned
      Groq credential requires a parsed active résumé, returns a bounded plan, and
      queues LinkedIn guest plus Telegram/RSS work without placing the key in the job.
- [ ] The same **Find matching jobs** screen polls both returned automation-job IDs,
      prevents duplicate clicks while they are active, and automatically refreshes jobs
      and Form Pilot when they reach a terminal state. Activity remains diagnostics, not
      a required step in the normal journey.
- [ ] The worker retains Telegram/RSS matches using the résumé-derived Groq terms;
      LinkedIn guest results remain bounded/unofficial and expose no login or Easy Apply
      behavior.
- [ ] Saving a YC target accepts only an exact current public YC job-detail URL and
      rejects YC search pages, company listings, account pages, generic application
      URLs, and unsupported historical shapes before any Browserbase session exists.
- [ ] `GET/PATCH /api/v1/providers/yc/preferences` isolates each tenant's optional
      query/remote/limit values. Reading or changing them performs no YC request and
      never fetches, scrapes, discovers, queues provider work, or enables bulk apply.
- [ ] In the signed-in YC canary, the worker resolves the claimed tenant's Browserbase
      BYOK pair, reuses only that tenant's persistent YC context, and connects Playwright
      over CDP outside Vercel. A second tenant cannot observe or lease that context.
- [ ] The YC scan opens only the saved exact job, captures its visible job-bound fields,
      and produces an immutable résumé/Groq-grounded revision for explicit review. The
      sealed revision authorizes one unique submit activation and success appears only
      after a fresh YC confirmation.
- [ ] For YC, login/MFA/CAPTCHA, an unknown required field, changed job/schema,
      ambiguous submit control, timeout, or uncertain confirmation fails closed as
      `needs_attention` without a blind retry or a claim that the application succeeded.
- [ ] In **Form Pilot**, `GET /api/v1/discovery/google-forms` returns only the signed-in
      tenant's deduplicated inbox. Loading it performs no scan; the user explicitly
      chooses **Prepare form**. Poll the returned scan job until the immutable revision
      is ready; Activity is diagnostics rather than a required navigation step.
- [ ] When an eligible unapproved revision loads, Form Pilot automatically applies
      deterministic Profile facts and calls the suggestion endpoint at most once. A
      stored account Groq credential is used only for unresolved questions; the review desk has
      no duplicate AI-fill button. Suggestions remain editable/browser-visible, exclude
      protected or unknown keys, never grant approval, and missing keys/Groq failure
      leave unknown fields for explicit review.
- [ ] Saving `profiles.resume_url` creates a new unapproved revision only when the
      previous snapshot is safe to supersede. An approved revision with an uncertain
      submit click remains immutable and cannot be refreshed or retried automatically.
- [ ] A textual Resume/CV link field receives only the saved public HTTPS Profile URL.
      One explicit native PDF-compatible Resume/CV upload receives the tenant's active
      private PDF, and the worker verifies filename, MIME, and byte size after attaching.
      Google Forms may instead expose one Google-owned Resume/CV picker; it is allowed
      only with the tenant's saved Google browser context, an allowlisted picker frame,
      one PDF input, and an exact visible filename confirmation. Ambiguous, unrelated
      required-file, untrusted-picker, and incompatible-file cases stop before submission.
- [ ] Google Forms remains optionally connectable: an ordinary public form uses an
      ephemeral browser, while a signed-in file-upload form returns
      `provider_login_required` until that tenant completes the isolated Google browser
      login in Connections. Gmail OAuth does not substitute for browser cookies.
- [ ] In **Form Pilot → Referral digest**, paste a representative full numbered hiring
      message. `POST /api/v1/discovery/referrals` extracts Company, Role, Batch,
      CTC/Stipend/compensation, Location, form URLs, and application emails; filters
      WhatsApp/channel/Topmate or premium-group promotion links; reports routing counts;
      sends Google Forms to Form Pilot; and makes email applications available in Mass
      Cold Email. Confirm that parsing neither submits a form nor sends an email.
- [ ] Pasting a forms.gle or docs.google.com/forms URL into Form Pilot saves it through
      `POST /api/v1/discovery/ats`, queues preparation only after the explicit button
      press, and rejects non-Google ATS URLs from this dedicated input.
- [ ] Google Forms found by résumé-guided discovery or imported links are routed to
      Form Pilot. Referral-digest intake stays in Form Pilot Stage 01 rather than adding
      a second pasted-leads panel to **Find jobs**.
- [ ] Form Pilot keeps the captured questions, suggestion review, exact approval,
      submit progress, confirmation, and any needs-attention handoff in Form Pilot. It does not move an ATS/
      Google Form application into **Mass Cold Email**.
- [ ] The normal Google Forms action is one explicit **Approve & submit in background**
      click. It seals the exact latest revision, refuses incomplete required answers,
      and queues one idempotent `application_submit` job bound to that revision. It does
      not require a separate prefill or activity-page click.
- [ ] The worker rescans the allowlisted target, rejects a changed schema or stale
      approval, fills only the sealed answers and selected résumé, activates exactly one
      unambiguous Submit control, and waits for a freshly observed provider confirmation.
      UI success requires `code=application_submitted` and
      `submission_state=confirmed`; a queued/running job is never described as submitted.
- [ ] Login/MFA/CAPTCHA, an unknown required field, missing public résumé URL, changed
      form, ambiguous submit control, timeout, or uncertain confirmation becomes
      `needs_attention` without a blind retry. Only that fallback may expose the
      allowlisted Browserbase Live View so the user can inspect and continue safely.
- [ ] `PUT /api/v1/provider-credentials/browserbase` requires an API key and Project ID,
      validates them using the read-only project endpoint without creating a session,
      and stores only an encrypted envelope. Status returns safe hints only.
- [ ] A tenant browser job uses that tenant's Browserbase BYOK pair before the optional
      platform fallback, never mixes credential sources, closes the session immediately
      on every terminal path, and enforces the 90-second stall cap. The UI explains
      that each created session is billed for at least one minute.
- [ ] A user's Hunter key validates through
      `PUT /api/v1/provider-credentials/hunter`, persists only as encrypted ciphertext,
      and is resolved only for owned `POST /api/v1/jobs/{id}/contacts/hunter` requests;
      no plaintext/ciphertext appears in browser storage, responses, logs, Vercel, or
      the worker environment.
- [ ] **Mass Cold Email** is the only related sidebar item and exposes **Build campaign**
      and **Review & send** as subtabs. Build campaign enforces a ten-job maximum, shows
      projected Hunter credit use inline before the explicit lookup, and requires the
      user to choose a contact before Groq drafting. Both subtabs list email-channel
      applications only; no ATS/form revision or form-answer editor appears there.
- [ ] In the second **Review & send** subtab, every exact draft is approved individually;
      edits invalidate approval, and a separate final confirmation precedes sending.
- [ ] Approved outreach sends run sequentially through the existing Gmail endpoint;
      daily-cap, duplicate-recipient, and idempotency gates remain authoritative, and a
      retry cannot create a second message.
- [ ] Product copy and behavior never represent the reviewed max-ten workflow as
      autonomous or unreviewed bulk cold email.
- [ ] Gmail connect, reviewed send, disconnect, and Google-side revocation work.
- [ ] CSV/XLSX/referral/ATS imports work within their documented limits.
- [ ] A synthetic worker job is claimed, heartbeated, completed, and visible in Activity.
- [ ] Account deletion requires recent authentication and removes private Storage data
      and provider state.
- [ ] Check desktop, tablet, and mobile layouts with keyboard navigation.

## 16. Cutover and rollback

Recommended order:

1. Rotate credentials and save the encryption key.
2. Commit/review the deployment branch and legal pages.
3. Apply/lint Supabase migrations through
   `202608150003_yc_exact_job_automation.sql`; verify the provider-credential
   no-browser-access contract and YC exact-target/service-role guards before deploying
   the corresponding API/worker code.
4. Configure Supabase Site URL, redirects, Google provider, SMTP, and Turnstile.
5. Configure Google identity and Gmail clients.
6. Deploy the stable staging Vercel build.
7. Deploy and validate the continuous worker on a persistent host outside Vercel.
8. Run two-user, OAuth/Gmail, queue, exact-approved Google Forms, and signed-in YC
   exact-job smoke tests.
9. Promote/deploy production.
10. Keep browser provider allowlists empty until capacity and canaries pass. Add `yc`
    only after its signed-in exact-job canary; the custom company-form adapter remains
    gated even though implementation code exists.

If cutover fails:

- Roll back to the previous Vercel deployment.
- Pause/stop the worker so it cannot claim new jobs.
- Clear `ALLOWED_BROWSER_PROVIDERS` before investigating browser failures.
- Do not roll back database migrations destructively. Apply a reviewed forward-fix
  migration.
- Revoke a compromised provider credential, update every environment, and redeploy.
- Preserve `TOKEN_ENCRYPTION_KEY` unless deliberately invalidating all encrypted
  connections/provider credentials and requiring every user to reconnect or re-enter
  keys.

For an upgrade from the prior browser-local Groq/Hunter design, deploy and verify the
table/API before the new frontend. On a user's first authenticated load, the frontend
imports only that user's namespaced legacy values through the normal validated PUT
endpoints. It deletes each browser copy only after a successful encrypted account save
(or when that provider is already configured); validation/network failure keeps the
copy, displays a retry warning, and must not send it as a fallback key header. Test
successful import, failure retention, user isolation, and removal of legacy entries.
Browserbase BYOK begins empty per account; the optional platform fallback keeps
controlled browser features available during gradual adoption.

## Official reference index

- [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi)
- [Vercel Python runtime](https://vercel.com/docs/functions/runtimes/python)
- [Vercel environment variables](https://vercel.com/docs/environment-variables)
- [Vercel domains](https://vercel.com/docs/domains/working-with-domains/add-a-domain)
- [Supabase Google login](https://supabase.com/docs/guides/auth/social-login/auth-google)
- [Supabase redirect URLs](https://supabase.com/docs/guides/auth/redirect-urls)
- [Supabase SMTP](https://supabase.com/docs/guides/auth/auth-smtp)
- [Supabase production checklist](https://supabase.com/docs/guides/deployment/going-into-prod)
- [Google OAuth web-server flow](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/get-started/)
- [Browserbase pricing](https://www.browserbase.com/pricing)
- [Browserbase sign up](https://www.browserbase.com/sign-up)
- [Browserbase API-key settings](https://www.browserbase.com/settings)
- [Browserbase project overview](https://www.browserbase.com/overview)
- [Browserbase get-project API](https://docs.browserbase.com/reference/api/get-a-project)
- [Browserbase cost optimization](https://docs.browserbase.com/optimizations/cost/cost-optimization)
- [Northflank pricing](https://northflank.com/pricing)
- [Northflank build and deploy](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code)
- [OCI Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [AWS Free Tier FAQ](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-FAQ.html)
- [Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs)
- [Hugging Face Spaces lifecycle](https://huggingface.co/docs/hub/spaces-overview)
- [Koyeb scale-to-zero](https://www.koyeb.com/docs/run-and-scale/scale-to-zero)
