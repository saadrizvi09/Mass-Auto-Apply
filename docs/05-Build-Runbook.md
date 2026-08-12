# Build, Deployment, and Launch Runbook

## AutoApply Cloud 2.0

**Date:** 2026-08-11

This runbook deploys the Vercel control plane, Supabase backend, credential-free
discovery, and reviewed Browserbase application workflow. Network discovery and browser
automation require a separately running worker; browser providers also require the
exact allowlist and live staging validation described below.

## 1. Prerequisites

- Git repository without committed personal data/secrets.
- Vercel account and CLI.
- Supabase project (Pro recommended for a public production service).
- Google Cloud project and Web application OAuth client for the default
  platform-managed Gmail path; advanced users may instead own their individual Google
  projects/clients.
- Cloudflare account and Turnstile widgets for the exact staging/production hosts.
- Custom production domain with HTTPS.
- Browserbase API key/project ID and a persistent worker host for managed-browser work.
- Controlled test accounts and test job/form URLs for every browser provider enabled.
- Python 3.12 for production-parity local tests.

Do not upload the existing project ZIP directly to Vercel. It contains ignored local
credentials/data in the working directory. Deploy from a clean Git checkout and verify
`.vercelignore` before the first build.

## 2. Create and configure Supabase

1. Create a project in the region closest to the intended Vercel Function region.
2. Save the project URL, browser publishable key, and server secret key.
3. Run the SQL migration through Supabase CLI or the SQL editor:

   ```bash
   supabase link --project-ref <project-ref>
   supabase db push
   ```

4. Confirm all tenant tables show RLS enabled.
5. Confirm `connection_secrets`, `user_google_oauth_clients`,
   `connection_lifecycles`, `oauth_states`, and `provider_send_events` have no `anon`
   or `authenticated` grants/policies.
6. Confirm private bucket `resumes` exists with PDF/6 MB restrictions and policies for
   only `<auth-user-uuid>/resume-1.pdf` through `resume-5.pdf`.
7. Exercise `register_resume` with concurrent registrations and confirm the owned
   Storage object is checked and exactly one résumé is active after each transaction.
8. Confirm the `pg_cron` extension and `autoapply-prune-provider-send-events` job are
   enabled with schedule `17 * * * *`. Run `prune_provider_send_events()` as the
   privileged migration/service role, confirm browser roles cannot execute it, and
   inspect cron run history for a successful run. Treat extension/job creation failure
   as a failed deployment, not a warning.
9. Run Supabase Security Advisor and resolve findings.
10. Confirm `discovery_preferences` and `application_form_revisions` have RLS enabled;
    browser roles can manage only their own discovery preferences and can only read
    their own immutable form revisions.
11. Confirm `application_form_revisions` rejects direct browser writes, and
    `store_application_form_scan`, `update_application_job_progress`,
    `get_application_job_bundle`, and `record_application_form_submission` are
    executable only by `service_role`.
12. Confirm exact approval with `approve_application_form_revision`: stale revision,
    changed schema hash, changed answers, another tenant, and a superseded revision must
    all fail. Both prefill and submit enqueueing must require that latest approval.
13. Confirm `ingest_discovered_jobs` deduplicates by tenant/normalized URL, preserves
    user-edited job content on repeat discovery, enforces the 200-item/2 MiB bounds, and
    rejects ZipRecruiter.

Supabase records scheduled jobs in `cron.job` and run status in
`cron.job_run_details`; see the [Supabase Cron guide](https://supabase.com/docs/guides/cron).

For public sign-up:

- enable email confirmation;
- configure custom SMTP (the built-in provider is development-limited);
- enable CAPTCHA/Turnstile and appropriate Auth rate limits;
- configure the exact production Site URL and approved redirect URLs;
- add preview redirects intentionally rather than permitting an unrestricted production
  wildcard.

Official references: [production checklist](https://supabase.com/docs/guides/deployment/going-into-prod),
[redirect URLs](https://supabase.com/docs/guides/auth/redirect-urls), and
[CAPTCHA](https://supabase.com/docs/guides/auth/auth-captcha).

### Configure Continue with Google

Account login must use Supabase Auth's Google provider. This produces the same
Supabase user, JWT session, profile bootstrap, and RLS identity as password sign-in;
it must not request `gmail.send`.

1. In Google Auth Platform → Clients, create a **Web application** OAuth client for
   account login. Add `http://127.0.0.1:8000` and the exact deployed origin under
   Authorized JavaScript origins.
2. In Supabase Authentication → Sign In / Providers → Google, copy the hosted callback
   URL. For this project it is:

   ```text
   https://tksoeyvlrdmvyuzdunwn.supabase.co/auth/v1/callback
   ```

3. Add that exact value to the Google client's Authorized redirect URIs. It is not the
   application Gmail callback at `/api/v1/oauth/google/callback`.
4. On the Supabase Google provider page, enable the provider and save the Web client ID
   and secret. They stay in Supabase; they are not Vercel variables.
5. In Supabase Authentication → URL Configuration, keep the exact local URL allowed
   during development and add Preview/Production URLs before testing those deployments.
6. Test both a new Google account and an existing verified password account. Confirm
   both reach one tenant-isolated workspace and that the Google consent screen asks
   only for `openid`, email, and profile access.

The Gmail flow in the next section is a separate consent grant. A separate Google Web
client within the same Cloud project is recommended so identity-only login and
`gmail.send` callbacks remain independently auditable.

### Safe Turnstile/Supabase CAPTCHA rollout

Use separate Supabase projects and Turnstile widgets for staging and production. The
Turnstile **site key** is public and is returned by `/api/v1/config`; the Turnstile
**secret** belongs only in Supabase Auth.

1. Create a Turnstile widget whose hostname allowlist contains the exact staging host.
2. Set `TURNSTILE_SITE_KEY` in the Vercel Preview environment and deploy while CAPTCHA
   enforcement is still disabled in the staging Supabase project.
3. Confirm the explicit widget loads, can issue a token, and is not blocked by CSP.
4. Configure the matching secret and enable Turnstile in Supabase Authentication → Bot
   and Abuse Protection.
5. In a fresh browser, test sign-up, sign-in, failed-login retry, and password reset.
   Each auth attempt must consume/reset the prior challenge.
6. Repeat for production: create/verify the production hostname widget, deploy the
   production site key first, verify it is live, and only then enable CAPTCHA in the
   production Supabase project.
7. Immediately run production auth smoke tests. If the widget fails, disable Supabase
   CAPTCHA before rolling back the frontend so users are not left with an enforced
   challenge the deployed client cannot provide.

Do not enable Supabase CAPTCHA first, put the Turnstile secret in Vercel, reuse a secret
across unrelated environments, or wildcard untrusted hosts. Keep
`https://challenges.cloudflare.com` in `script-src` and `frame-src` unless a stricter
nonce-based policy has been tested. References:
[Supabase CAPTCHA](https://supabase.com/docs/guides/auth/auth-captcha),
[Turnstile widget lifecycle](https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/),
and [Turnstile CSP](https://developers.cloudflare.com/turnstile/reference/content-security-policy/).

## 3. Configure Google OAuth for Gmail

AutoApply supports two credential sources:

- **Platform-managed (default):** the deployment operator configures one Google OAuth
  client in server environment variables. Users only click **Connect Gmail** and grant
  access; they do not supply Google Cloud credentials.
- **User-managed (advanced):** an authenticated user creates a separate Google Web
  OAuth client, saves its ID/secret in AutoApply, explicitly selects it, and then signs
  into Gmail. The user owns that Google project's consent screen, test-user list,
  verification, quotas, and credential rotation.

For either owner, configure Google using the official console:

1. [Create or select a Google Cloud project](https://console.cloud.google.com/projectcreate).
2. [Enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
3. Configure [Branding](https://console.cloud.google.com/auth/branding),
   [Audience](https://console.cloud.google.com/auth/audience), and
   [Data Access](https://console.cloud.google.com/auth/scopes). An External app needs an
   owned homepage, privacy policy, terms, support email, and verified production domain.
4. Add only launch scopes:

   ```text
   openid
   email
   profile
   https://www.googleapis.com/auth/gmail.send
   ```

5. Open [Google Auth Platform → Clients](https://console.cloud.google.com/auth/clients),
   choose **Create client**, and select **Web application**, not Desktop. A standard
   Google Cloud API key is not sufficient for Gmail OAuth. Copy the client ID and secret
   when Google shows them; the full secret may not be displayed again.
6. Register the exact fixed redirect URI displayed by AutoApply. For the supplied local
   configuration it is:

   ```text
   http://127.0.0.1:8000/api/v1/oauth/google/callback
   ```

   For production it is exactly:

   ```text
   https://your-domain.example/api/v1/oauth/google/callback
   ```

   Copy the active deployment's `GOOGLE_REDIRECT_URI` exactly; scheme, hostname, port,
   path, and trailing-slash behavior must match. Do not accept or invent a per-user
   callback.
7. For the platform path, put the client ID/secret only in local/Vercel server
   environment variables. For the advanced path, the user pastes them only into the
   authenticated Gmail connection panel. AutoApply encrypts both values before a
   service-role-only database write and never returns the saved secret or persists it
   in the browser.
8. While an External app is in **Testing**, add every intended Gmail account under
   **Test users**. Because AutoApply requests `gmail.send`, each test user's grant and
   refresh token expire seven days after consent. Reconnect after expiry; Google's
   identity-only exception does not apply.
9. Before using a client as a public production app, its Google-project owner must
   publish it and complete brand/domain and sensitive-scope verification for the exact
   domain and scopes. `gmail.send` is sensitive rather than restricted, so do not imply
   that Testing or an unverified warning is a public launch configuration.

References: [web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server),
[consent configuration](https://developers.google.com/workspace/guides/configure-oauth-consent),
[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[audience/test-user rules](https://support.google.com/cloud/answer/15549945), and
[token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

User instructions shown by the product:

1. Use the default **Platform-managed client**, when available, unless there is a reason
   to own a separate Google project.
2. For the advanced option, follow the linked Google steps, paste the Web OAuth client
   ID/secret, save, and select **My OAuth client**. Do not paste an API key.
3. Click **Connect Gmail** and select the account that should send applications.
4. Review and grant “Send email on your behalf.”
5. Return to AutoApply and verify the displayed address.
6. Review every draft before sending.
7. Use **Disconnect** to remove the token connection. Disconnect Gmail before replacing
   or deleting a saved user-managed OAuth client; disconnect alone deliberately leaves
   that client configuration until the user chooses **Delete client**.

Connection lifecycle expectations:

- every OAuth start advances a per-user generation and invalidates prior state;
- every state binds the selected platform/user credential source and, for a user client,
  its exact credential generation;
- only the callback carrying the current lifecycle and credential generation may save
  tokens, and refresh may not fall back to another client;
- saving/replacing/deleting a user client shares the lifecycle lock, invalidates pending
  states, and fails until Gmail is disconnected and pending provider sends are resolved;
- disconnect invalidates pending callbacks before asking Google to revoke;
- local encrypted tokens are deleted even when Google's revocation response is
  unavailable; in that case the user must also remove AutoApply under Google Account →
  Security → third-party access;
- test concurrent start/callback/disconnect requests in staging, not only the happy
  path.

Never ask users for a Gmail password, app password, `credentials.json`, `token.json`, or
Google API key. The advanced path accepts only a Web OAuth client ID and secret.

## 4. Generate token encryption material

Generate one production key in a trusted terminal/password manager:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Store it as `TOKEN_ENCRYPTION_KEY` in Vercel and the worker. Do not commit or expose it.
Back it up securely; losing it requires every user to reconnect Gmail and users of the
advanced path to re-save their Google OAuth client.

## 5. Local SaaS development

Create `.env.saas.local` from the example and fill only development project values.
Then:

```bash
python3.12 -m venv .venv-saas
source .venv-saas/bin/activate
python -m pip install -r requirements-dev.txt
uvicorn app.saas_main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Keep the legacy personal `.env` separate; hosted modules
do not read it for user data.

Test:

```bash
python -m pytest
```

Run integration/RLS tests against a disposable Supabase project, never production.

For Telegram/RSS or LinkedIn guest discovery and any Browserbase flow, run the worker in
a second terminal after the web API is ready:

```bash
docker build -f worker/Dockerfile -t autoapply-worker .
docker run --env-file .env.worker autoapply-worker
```

Pasted referral-digest and CSV/XLSX ingestion can be tested through the web control
plane without Browserbase. The spreadsheet request must remain at or below 4 MB;
résumé PDFs use direct Supabase Storage upload instead. Network discovery needs the
persistent worker but no third-party user credentials. Browser scan/prefill/submit needs
Browserbase plus a user login performed inside Live View.

## 6. Configure Vercel

Link the project:

```bash
vercel link
```

Add Preview and Production variables with the correct scope:

```text
SUPABASE_URL
SUPABASE_PUBLISHABLE_KEY
SUPABASE_SECRET_KEY
SITE_URL
TURNSTILE_SITE_KEY
TOKEN_ENCRYPTION_KEY
GOOGLE_REDIRECT_URI
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GROQ_MODEL
MAX_RESUME_BYTES
DEFAULT_DAILY_SEND_CAP
BROWSERBASE_API_KEY
BROWSERBASE_PROJECT_ID
ALLOWED_BROWSER_PROVIDERS
```

Keep the allowlist empty until controlled staging validation. The exact initial
allowlist recommendation is:

```text
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
```

This enables only one-page Google Forms and Greenhouse. Add `lever`, `ashby`, or
`wellfound` one at a time after a successful controlled canary. Leave `yc`, `cutshort`,
and `instahyre` out until tenant-aware multi-step application state machines have been
implemented and live-validated. Multi-page or branching Google Forms remain unsupported.

The user's Groq key is not a Vercel environment variable. `GOOGLE_REDIRECT_URI` is
required for both Gmail OAuth paths. `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are
optional only when the deployment intentionally offers user-managed clients without a
default platform client. These environment values enable Gmail send only; the
Supabase Google login client is configured in Supabase itself. Neither is a
Browserbase/Google Forms credential. `TOKEN_ENCRYPTION_KEY` encrypts Gmail tokens,
user-managed Google client credentials, and opaque provider secrets; it must have the
same value in trusted API and worker environments.

Deploy a preview:

```bash
vercel deploy
```

After changing any environment value, create a new deployment; existing deployments do
not inherit the change.

## 7. Preview verification gate

Do not promote until all pass:

- public landing/config/health load;
- sign-up confirmation, sign-in, refresh, password reset, and sign-out;
- Turnstile is required for sign-in/sign-up/reset, a token is reset after each attempt,
  and the Turnstile secret never appears in Vercel config or browser responses;
- User A cannot read or mutate User B data through API or direct Supabase calls;
- only the five exact owned PDF slots upload, a sixth/nested/cross-user path fails, and
  concurrent upload/registration leaves at most five objects and exactly one active row;
- résumé parsing rejects wrong MIME, oversized, malformed, and cross-user objects;
- deleting a résumé removes its object/metadata without deleting application history;
- account deletion rejects a session whose last sign-in is older than ten minutes and
  succeeds after signing out and completing the normal protected sign-in flow again;
- Groq key validates, generates a draft, survives refresh, and is absent from DB/logs;
- pre-approval Groq form suggestions use only captured non-sensitive question keys,
  remain unpersisted drafts, and never approve or enqueue a form action;
- Groq key survives ordinary sign-out for the same user/browser, is not shown to another
  signed-in user, and is removed by explicit deletion and account deletion;
- job CRUD, draft edit, approval, archive, and application history work;
- pasted referral-digest ingestion and CSV/XLSX import enforce the 4 MB request cap and
  row/cell/XLSX-expansion bounds, normalize provider URLs, deduplicate within the tenant,
  and render imported text without HTML execution;
- Telegram/RSS fetching rejects unallowlisted/private/link-local/metadata destinations,
  revalidates redirects, and stops at byte/item limits;
- bounded LinkedIn guest discovery uses no account context, stops on throttle/challenge,
  and offers no Easy Apply queue/action;
- public ATS board discovery accepts only exact Greenhouse/Lever/Ashby hosted URLs,
  uses official unauthenticated GET endpoints, omits unlisted Ashby posts, fairly
  interleaves multiple boards, and stops at the documented body/board/result limits;
- Gmail state replay/cross-user callback fails;
- an older OAuth callback cannot save after a newer start or disconnect; simultaneous
  callback/disconnect leaves one deterministic lifecycle and no resurrected connection;
- platform-managed OAuth is the default when configured, while an explicitly selected
  user-managed Web client completes the same fixed callback flow;
- Google OAuth client status returns only availability, the fixed callback, and a
  masked hint—never a saved ID/secret or ciphertext—and browser roles cannot read the
  client table;
- an API key, malformed/non-Web client ID, or request-supplied redirect/scope/endpoint
  is rejected;
- replacement/deletion of a user-managed client fails while Gmail is connected, then
  succeeds after disconnect and makes an older callback stale;
- corrupt/missing/stale user credentials fail closed during callback and refresh rather
  than silently falling back to the platform client;
- Gmail connects a test account, sends one approved test message once, and disconnects;
- forced Google revocation failure still removes local token rows and surfaces the
  Google Account removal instruction;
- duplicate send retry does not produce a second Gmail message;
- provider-account/recipient hash caps survive a delete/recreate test, expose no user or
  plaintext recipient, reject browser-role access, and enforce a 90-day maximum logical
  expiry independent of physical cleanup timing;
- expired provider-ledger rows stop affecting limits immediately; the service-only prune
  function, hourly cron run, and reservation cleanup remove them, with cron failures
  visible to operations;
- LinkedIn capability text separates unofficial guest discovery from manual/
  partner-required Easy Apply and never offers a LinkedIn submit action;
- Google Forms, Greenhouse, Lever, Ashby, YC, Wellfound, Cutshort, and Instahyre are the
  only managed-browser provider IDs; ZipRecruiter is absent from catalog, UI, lifecycle,
  accepted queue payloads, and worker registry;
- each enabled application handler's scan records fields without filling/submitting;
  first approval seals its exact reviewed answers, and every later answer change or
  résumé/URL/schema change requires a new revision;
- exact revision/hash/answers approval is enforced; prefill fills only the reviewed
  revision and stops; submit requires a separate user action and confirmed provider
  success;
- CAPTCHA, MFA, expired login, unknown required fields, and ambiguous confirmation stop
  visibly as `needs_attention` and are never bypassed or reported as success;
- `/api/v1/health` reports ready without loading SQLite/Playwright/APScheduler;
- deployment bundle contains none of `.env`, tokens, DBs, PDFs, profiles, browser data,
  logs, screenshots, generated outputs, or backups.

## 8. Worker deployment

The web product is useful without a worker for onboarding, pasted/file imports,
drafting, tracking, manual handoff, and reviewed single Gmail sends. Deploy the worker
before enabling Telegram/RSS, LinkedIn guest discovery, or browser application jobs.

Build/run from `worker/Dockerfile` on a persistent-process platform:

```bash
docker build -f worker/Dockerfile -t autoapply-worker .
docker run --env-file .env.worker autoapply-worker
```

The root `.dockerignore` deliberately allowlists only worker/SaaS source required by
the Dockerfile. Verify it before every remote build so local `.env` files, résumés,
browser profiles, databases, and Git history never enter the builder context.

Required worker variables:

```text
SUPABASE_URL
SUPABASE_SECRET_KEY
TOKEN_ENCRYPTION_KEY
WORKER_ID
WORKER_POLL_SECONDS
WORKER_LEASE_SECONDS
```

Managed browser additionally requires:

```text
BROWSERBASE_API_KEY
BROWSERBASE_PROJECT_ID
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
```

The Browserbase plan must support `keepAlive`: prefill disconnects the worker while the
bounded Live View remains available for human review, then the session expires or is
released. A plan without keep-alive cannot provide that review handoff as implemented.

Start with `ALLOWED_BROWSER_PROVIDERS` empty. After controlled validation, use only
`google_forms,greenhouse`; Google Forms validation must use a one-page form. Enable
Lever, Ashby, or Wellfound one at a time after reviewing its current terms and testing
scan, immutable approval, prefill-only behavior, explicit submission, confirmation
evidence, cancellation/idempotency, and disclosure. Do not enable YC, Cutshort, or
Instahyre until their tenant-aware multi-step application handlers exist and pass the
same gate.
Users enter passwords and MFA directly in Browserbase Live View; do not ask them to send
credentials to the operator or enter credentials in an AutoApply form.

Worker health checks:

- it claims one synthetic job;
- a second worker cannot claim the same active lease;
- killing the worker requeues after lease expiry;
- cancellation is observed;
- result/error data is redacted;
- provider secrets never appear in logs.
- persisted browser context A cannot be leased by another user/provider or concurrent
  job, and public ephemeral sessions are not reused across tenants;
- navigation and every redirect stay within the adapter's HTTPS host allowlist;
- a stale/mismatched form revision cannot be filled or submitted;
- scan, prefill, and submit are visible as separate job kinds;
- CAPTCHA/MFA/challenges and uncertain submit confirmation become `needs_attention`.

Mocked tests are necessary but not live-provider evidence. Public enablement remains
blocked until the operator supplies Browserbase credentials, a continuous worker host,
and controlled test accounts/jobs and records a passing staging run for every enabled
provider. Third-party markup and policies can change after deployment, so keep one-click
provider disablement through the allowlist.

## 9. Production promotion

Do not treat a locally parsed migration or mocked test as production evidence. First
apply the exact migration to a real, disposable staging Supabase project and run the
cross-tenant RLS/Data API tests, private Storage slot/quota tests, concurrent résumé
registration, OAuth generation/disconnect races, send reservation/idempotency/provider
ledger tests, account deletion cleanup, and multi-worker lease/claim tests.

1. Complete Google brand/domain and sensitive-scope verification for the default
   platform-managed OAuth client on the exact production domain/scopes. Confirm the
   product accurately warns advanced-client owners that Testing grants for
   `gmail.send` expire after seven days and that they must complete equivalent
   verification before publishing their own client for public production use.
2. Replace every placeholder in privacy/terms/support/deletion content with the
   operator's reviewed legal name/entity, postal address, active support/privacy/legal
   contacts, actual effective dates, governing terms, applicable rights/notices, and
   provider/processing details. None are inferred by this repository.
3. Verify the exact Vercel production domain/environment and Supabase Site URL, redirect
   allowlist, key types, RLS/Storage policies, SMTP, Turnstile, backups, budgets, and
   monitoring. If a custom Supabase domain changes browser origins, update/test CSP.
4. Run the staging migration/test gate above, then apply the reviewed migration to
   production with a rollback/forward-compatibility plan.
5. Deploy and verify production auth and Google callback URLs.
6. Send only to controlled test recipients during final validation.
7. Promote:

   ```bash
   vercel deploy --prod
   ```

8. Monitor auth failures, OAuth callbacks, send outcomes, discovery partial/throttle
   results, queue age, worker leases, Browserbase session failures, provider challenges,
   and provider rate errors, plus the provider-ledger cron job's last successful
   run/failures.

## 10. Operational rules

- Never disable RLS to fix an application bug.
- Never expose or use the Supabase secret key in the browser.
- Never log request authorization/Groq/provider headers.
- Never increase Gmail scopes casually; scope changes can require re-verification.
- Never accept a per-user OAuth redirect/scope/endpoint or treat an API key as a Gmail
  OAuth client. Never expose a stored user client ID/secret or ciphertext to the
  browser.
- Never replace/delete a user-managed Google OAuth client while Gmail remains connected;
  disconnect first so no stored refresh token depends on the old client secret.
- Never retry an ambiguous external send/apply without reconciliation.
- Never delete or weaken the pseudonymous provider ledger to let account recreation
  evade provider caps; never extend its enforced 90-day maximum logical expiry. Alert
  and remediate when hourly cleanup fails or physical deletion is delayed.
- Never enable LinkedIn candidate automation without official written partner access.
- Never describe bounded LinkedIn guest discovery as an official API or Easy Apply.
- Never bypass CAPTCHA/MFA or retry an ambiguous submit as though nothing happened.
- Never add ZipRecruiter to the hosted provider list without a new reviewed product
  decision, migration, adapter, and staging gate.
- Treat remote browser contexts as credentials and delete them on disconnect/account
  deletion.

## 11. Rollback and incident response

- Roll back the Vercel deployment independently of database schema.
- Migrations must be forward-compatible through one application release; avoid immediate
  destructive column removal.
- On token-encryption-key exposure: stop provider actions, rotate key, re-encrypt stored
  tokens, and require reconnect where integrity is uncertain.
- On Supabase secret-key exposure: rotate immediately, audit all tenant tables/storage,
  and invalidate worker/API deployments.
- On suspected cross-tenant access: disable private APIs, preserve redacted audit logs,
  assess affected rows/objects, fix both API scope and RLS, then notify as required.
- On Groq browser-key exposure: instruct the affected user to revoke it in Groq Console;
  the server has no stored copy to rotate.
- On Google revocation failure: confirm local encrypted rows are gone, keep the UI
  warning visible, and direct the user to remove access from their Google Account.
- On a user-managed Google client-secret exposure: disconnect Gmail, rotate/delete the
  OAuth client in Google Cloud, delete or replace it in AutoApply only after disconnect,
  and reconnect. Do not assume deleting it in AutoApply revokes Google's copy.

## 12. Public-launch checklist

- [ ] Clean Git deployment artifact
- [ ] Supabase RLS and Storage policies tested
- [ ] Real staging migration plus RLS/Storage/OAuth/send/deletion/concurrency tests passed
- [ ] Provider-ledger hourly prune job enabled, privilege-tested, and monitored
- [ ] Custom SMTP and staged Turnstile/CAPTCHA rollout verified
- [ ] Platform Google OAuth client verified for the exact domain and `gmail.send`
- [ ] Advanced user-client flow shows official setup links, fixed callback, seven-day
      Testing expiry, verification ownership, encrypted-storage disclosure, and
      disconnect-before-delete rule
- [ ] Operator identity/address/contact/law/effective-date placeholders replaced and reviewed
- [ ] Privacy/terms/support/deletion pages live
- [ ] Vercel production environment complete
- [ ] Cross-tenant and send-idempotency tests passing
- [ ] Monitoring/budgets/backups enabled
- [ ] No unsupported provider marketing or controls
- [ ] Persistent worker deployed and lease recovery/monitoring verified
- [ ] Browserbase key/project configured only in trusted environments
- [ ] Initial `google_forms,greenhouse` allowlist reviewed; unsupported providers and ZipRecruiter absent
- [ ] Live scan/prefill/submit validation recorded for every enabled provider
- [ ] Telegram/RSS and LinkedIn guest network bounds/partial failures exercised
