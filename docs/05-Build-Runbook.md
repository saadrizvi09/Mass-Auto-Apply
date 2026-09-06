# Build, Deployment, and Launch Runbook

## AutoApply Cloud 2.0

**Date:** 2026-08-15

This runbook deploys the Vercel control plane, Supabase backend, encrypted account-scoped BYOK,
discovery, reviewed outreach, and reviewed Browserbase application workflow. The main
sidebar journey is **Profile → Find jobs → Form Pilot → Mass Cold Email**; that last
destination contains email-only **Build campaign → Review & send** subtabs. Form Pilot
owns Google Form answer review and one explicit exact-approval background submit; it
reports success only after provider confirmation and exposes Browserbase Live View only
when the run needs attention. Network discovery and browser automation require a
continuously running worker on a persistent host outside Vercel;
browser providers also require the exact allowlist and live staging validation described
below.

## 1. Prerequisites

- Git repository without committed personal data/secrets.
- Vercel account and CLI.
- Supabase project (Pro recommended for a public production service).
- Google Cloud project and Web application OAuth client for the default
  platform-managed Gmail path; advanced users may instead own their individual Google
  projects/clients.
- Cloudflare account and Turnstile widgets for the exact staging/production hosts.
- Custom production domain with HTTPS.
- A persistent worker host for managed-browser work. A controlled Browserbase API
  key/project ID may be saved as account BYOK; operator Browserbase values are an
  optional platform fallback.
- A controlled Groq test key for encrypted account-scoped BYOK. It is an authenticated
  user input for staging verification, not a deployment secret. Public contact lookup
  requires no provider key.
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

   Verify migrations were applied in filename order through
   `202609050001_outreach_email_queue.sql`. Do not stop at
   `202608130001_google_forms_manual_submit.sql`: it is the temporary fail-closed
   prohibition. The required forward migration `202608130002_google_forms_approved_submit.sql`
   removes that prohibition and installs the exact-approved/required-answer submit gate;
   `202608130003_profile_public_resume_url.sql` adds the separate public résumé URL fact;
   the later profile-sync and terminal-snapshot migrations carry that fact into safe
   unapproved revisions and durably fence any submit click whose outcome is uncertain.

4. Confirm all tenant tables show RLS enabled.
5. Confirm `connection_secrets`, `user_google_oauth_clients`,
   `user_provider_credentials`,
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
    all fail. Prefill and submit must require that latest approval. For Google Forms,
    confirm an incomplete required-answer preflight cannot enqueue submission and that
    the temporary `guard_google_forms_manual_submit` trigger/function is gone while
    `guard_google_forms_approved_submit`, its trigger, and the submit-revision unique
    index exist.
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
5. In Supabase Authentication → URL Configuration, set the hosted project's **Site URL**
   to `https://autoapply-cloud.vercel.app` and add
   `https://autoapply-cloud.vercel.app/**` under Redirect URLs. Keep the exact local URL
   allowed during development and add Preview URLs only when they are intentionally
   enabled. A redirect URL that is not allow-listed is silently replaced by Supabase's
   Site URL, which is why a local Site URL can send a production login to `127.0.0.1`.
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
advanced path to re-save their Google OAuth client, Groq key, and
Browserbase key/project pair. The value must be identical in the API and all workers.

For an existing deployment, apply
`202608150002_user_provider_credentials.sql` before deploying code that resolves stored
credentials, then apply `202608150003_yc_exact_job_automation.sql` and the final
`202609050001_outreach_email_queue.sql` before deploying the corresponding
UI/API/worker. Confirm the credential table has RLS enabled, no browser grants/policies, and a
service-role grant. Deploy API and worker with the same encryption key before the new
frontend. On the first authenticated load, the frontend performs a one-time import of
that user's namespaced legacy Groq browser value through the normal validated
PUT endpoints. It removes a legacy browser copy only after the encrypted account save
succeeds (or when the same credential is already configured); on validation/network
failure it keeps the browser copy, shows a warning, and allows a safe retry. The value
must not be copied across users, logged, placed in a queue, or used as a fallback request
header. Verify no legacy provider-key entry remains after successful import and no
generation route requires transient key headers.

For public contact crawling, apply
`202609060001_public_contact_discovery.sql` before deploying the matching API and
worker. It creates the RLS-protected `job_contacts` evidence table and three
lease-bound RPCs; it adds no contact-provider key or mailbox probe. The worker image
must include `app/saas/contact_discovery.py`; the checked-in `worker/Dockerfile`
copies it. Give each imported role a public HTTPS job URL, and add a company URL or
domain in import metadata when available. An ATS URL may not expose the employer's
own contact page, so zero results can be an honest outcome.

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

On macOS, `./dev.command` is the normal local launcher after initial setup. It sources
`.env.saas.local`, starts `python -m worker.main` and the reload-enabled FastAPI server,
and stops both when the launcher receives Control-C. The worker is a continuous Python
process that polls the durable Supabase automation queue; it is not an HTTP server.

To test the production container path instead, run the worker separately after the web
API is ready:

```bash
docker build -f worker/Dockerfile -t autoapply-worker .
docker run --env-file .env.worker autoapply-worker
```

CSV/XLSX and referral-digest ingestion can be tested through the web control plane
without Browserbase. In **Form Pilot → Referral digest**, paste the complete forwarded
message; Stage 01 calls authenticated `POST /api/v1/discovery/referrals`, extracts the
numbered Company/Role/Batch/compensation/location blocks, ignores recognized
channel/premium-promotion links, routes Google Forms to the preparation inbox, and makes
email-address applications available in Mass Cold Email. Verify the routing summary
before continuing. Mass Cold Email must contain only those email-channel drafts, never
Google Form revisions. The referral text is limited to 100,000 characters and the
spreadsheet request must remain at or below 4 MB;
résumé PDFs use direct Supabase Storage upload instead. Network discovery needs the
persistent worker but no third-party user credentials. Browser scan, exact-approved
submit, verified confirmation, and any attention fallback need Browserbase;
login-gated providers require user login inside Live View.

Exercise the reviewed workflow in this order:

1. Complete **Profile**, upload/parse the active résumé, review the saved target roles,
   skills, and location, and save a Groq key through
   `PUT /api/v1/provider-credentials/groq`. Confirm only a safe hint/status returns.
2. In **Find jobs**, call `POST /api/v1/discovery/resume-guided`; the API resolves the
   caller's encrypted Groq credential. Groq derives bounded roles/keywords from the résumé and
   profile, the API queues LinkedIn guest and public-feed jobs, and the worker filters
   Telegram/RSS results against those derived search terms. LinkedIn guest discovery is
   unofficial, bounded, and must not use login state or expose an Easy Apply action.
3. In **Form Pilot**, paste a full referral digest through
   `POST /api/v1/discovery/referrals`, or choose **Single form link** to save one Google
   Form through `POST /api/v1/discovery/ats`. Confirm the digest's numbered jobs are
   separated, Company/Role/Batch/compensation/location facts are retained, known
   promotional links are ignored, forms appear in the tenant-scoped inbox from
   `GET /api/v1/discovery/google-forms`, and email applications are available in Mass
   Cold Email. The user explicitly starts preparation. When the scan returns, Form
   Pilot automatically requests one grounded suggestion set with the account's stored
   Groq credential when available. Verify that graduation/passout questions map
   deterministically from the reviewed profile year and résumé/CV link questions use
   only the separately saved public HTTPS résumé URL—not the private PDF path or a
   signed URL. The user reviews/edits the exact revision and chooses **Approve & submit
   in background** once. The API seals it, checks all required answers, and queues one
   idempotent submit. Success appears only after the worker observes fresh provider
   confirmation; an uncertain/login/challenge/schema result becomes `needs_attention`
   and may expose Live View. Parsing, suggestions, and scan never approve or submit on
   their own.
4. In the **Build campaign** subtab of **Mass Cold Email**, generate the résumé-bound
   research prompt, run it in an external AI with web/search access, and upload its
   CSV/XLSX workbook. The importer selects the recognizable data sheet, preserves the
   faithful JD summary and public evidence URLs, and accepts only public/user-supplied email
   strings. It never guesses, probes a mailbox, or sends verification mail.
5. Select no more than 30 roles in order, review the public contact lead, ask Groq to
   draft from the JD and résumé, and switch to **Review & send**. Verify that ATS/form
   applications are excluded, approve every exact draft individually, and confirm the
   final handoff. The API creates durable `send_email` rows; the persistent worker
   sends them after the browser closes with daily-cap, duplicate-recipient, and
   idempotency gates authoritative in Supabase.

This is bounded, user-reviewed orchestration. It is not autonomous or unreviewed bulk
cold email.

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

`BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` are optional only as a pair. They
provide a trusted platform fallback; a claimed job owner's verified encrypted
Browserbase BYOK pair takes priority. If neither source exists, managed-browser work is
unavailable while non-browser product features continue to work.

Keep the allowlist empty until controlled staging validation. The exact initial
allowlist recommendation is:

```text
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
```

This enables only one-page Google Forms and Greenhouse. Add `lever`, `ashby`, or
`wellfound` one at a time after a successful controlled canary. YC has a finished exact
saved-job scan/review/sealed-submit state machine, but must remain out until its
controlled signed-in tenant-aware canary passes. After that canary, use:

```text
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse,yc
```

The exact-host
generic `company_form` adapter is internal/gated and is not a public catalog or allowlist
entry. Leave `cutshort` and `instahyre` out until tenant-aware multi-step application
state machines have been implemented and live-validated. Multi-page or branching Google
Forms remain unsupported.

Users' Groq and Browserbase credentials are not Vercel environment variables.
They are validated, encrypted with `TOKEN_ENCRYPTION_KEY`, and stored in the
service-role-only `user_provider_credentials` table. The browser receives only safe
status/hints and stores no provider secret. `GOOGLE_REDIRECT_URI` is
required for both Gmail OAuth paths. `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are
optional only when the deployment intentionally offers user-managed clients without a
default platform client. These environment values enable Gmail send only; the
Supabase Google login client is configured in Supabase itself. Neither is a
Browserbase/Google Forms credential. `TOKEN_ENCRYPTION_KEY` encrypts Gmail tokens,
user-managed Google client credentials, account-scoped provider credentials, and opaque provider secrets; it must have the
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
- `PUT /api/v1/provider-credentials/groq` validates and persists only encrypted
  ciphertext; refresh returns only a safe hint/status, generation uses the owned key,
  and no plaintext/ciphertext appears in responses, browser storage, or logs;
- `POST /api/v1/discovery/resume-guided` requires a parsed active résumé and verified
  owned Groq credential, returns a bounded search plan, and queues both LinkedIn guest
  and Telegram/RSS discovery without placing the key in a job payload;
- the worker applies the résumé-derived Groq roles/keywords before retaining
  Telegram/RSS results, while bounded LinkedIn guest discovery remains explicitly
  unofficial, credential-free, and separate from Easy Apply;
- a newly loaded eligible Form Pilot revision automatically applies exact saved Profile
  facts first and, when an owned Groq credential exists, requests Groq suggestions once for
  unresolved questions; the review desk has no duplicate AI-fill action, suggestions
  remain unpersisted editable drafts, and never approve or enqueue a form action;
- Groq status survives refresh/sign-out because the encrypted credential is
  account-scoped, another user cannot resolve or infer it, and explicit/account deletion
  removes the service-role-only row;
- job CRUD, draft edit, approval, archive, and application history work;
- `GET /api/v1/discovery/google-forms` is tenant-scoped and deduplicated, and viewing
  the queue never starts a scan; an explicit scan records fields for the existing
  immutable review/approval flow;
- `POST /api/v1/jobs/{id}/contacts/public` enforces job ownership and returns only
  bounded, syntax-checked contact candidates already present in the job record; the
  outreach UI requires an explicit user-selected contact;
- outreach selection cannot exceed 30 jobs; Groq produces drafts only after contacts
  are selected, and editing any draft invalidates its exact-content approval;
- the final outreach action requires a separate confirmation and enqueues approved
  drafts to the persistent Gmail worker; daily-send, duplicate-recipient, and
  idempotency gates still fail closed and a retry cannot create a second message;
- no UI or API path presents this workflow as autonomous or unreviewed bulk cold email;
- pasted referral-digest ingestion enforces its 100,000-character schema bound, filters
  recognized promotional routes, and routes form/email applications without external
  action; CSV/XLSX import separately enforces the 4 MB request cap plus
  row/cell/XLSX-expansion bounds; both normalize provider URLs, deduplicate within the
  tenant, and render imported text without HTML execution;
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
- exact revision/hash/answers approval and complete required-answer preflight are
  enforced; the normal Google Forms UI makes its single submit authorization explicit,
  queues one revision-bound idempotent `application_submit`, and reports success only
  for `application_submitted` plus `submission_state=confirmed`;
- CAPTCHA, MFA, expired login, unknown required fields, and ambiguous confirmation stop
  visibly as `needs_attention`, may expose an allowlisted Live View, and are never
  bypassed, blindly retried, or reported as success;
- Browserbase BYOK requires one API key plus Project ID and validates with read-only
  `GET /v1/projects/{project_id}` without creating a session; an invalid/mismatched pair
  is not stored, a valid owned pair takes priority over the optional platform fallback,
  and the key/project always come from the same source;
- every Browserbase session closes immediately on completion/failure and has a
  90-second stall cap. Confirm in Browserbase Sessions that normal runs finish sooner;
  remember every created session is billed for at least one minute, so the cap reduces
  runaway/stalled usage rather than sub-minute minimum billing;
- `/api/v1/health` reports ready without loading SQLite/Playwright/APScheduler;
- deployment bundle contains none of `.env`, tokens, DBs, PDFs, profiles, browser data,
  logs, screenshots, generated outputs, or backups.

## 8. Worker deployment

The web product is useful without a worker for onboarding, file/URL imports,
drafting, tracking, manual handoff, and reviewed Gmail sends. Deploy the
continuous worker on a persistent process host outside Vercel
before enabling Telegram/RSS, LinkedIn guest discovery, or browser application jobs.
Résumé-guided discovery queues the existing LinkedIn guest collector and passes its
Groq-derived roles/keywords to the Telegram/RSS worker filter. The API decrypts the
account's Groq credential only for the owned request; the key is never placed in the
queue.

### 8.1 Choose a worker host

The current image runs a continuous Python/Supabase queue poller. It needs outbound
HTTPS, a long-running process, automatic restart, and no public port. Use this decision
matrix; “free” is never a production-availability guarantee:

| Option | Current fit | Operational position |
| --- | --- | --- |
| [Northflank Sandbox](https://northflank.com/pricing) | Best managed zero-cost trial for the current container. The Sandbox tier advertises always-on free services, and [Northflank services may be created with no ports](https://northflank.com/docs/v1/application/getting-started/build-and-deploy-your-code). | Use only for testing or a risk-accepted public beta. The provider describes Sandbox as a testing tier; AutoApply does not treat it as production or assume an SLA, permanent capacity, or unchanged free terms. |
| [OCI Always Free Compute](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) | More durable zero-cost, self-managed VM option. Run Docker under `systemd` or an equivalent restart supervisor. | Capacity can be unavailable in the home region, and Oracle may reclaim instances it classifies as idle. Patch the VM, restrict SSH, monitor it, and design for reprovisioning; do not promise uninterrupted free production. |
| [AWS Free Tier](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-FAQ.html) | Technically suitable through EC2/container hosting, but not a durable zero-cost recommendation. | For new customers, the current free plan ends after six months or when credits are exhausted. Set budgets and plan migration or paid operation before expiry; older accounts can have different legacy eligibility. |
| [Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs) | Not compatible with the current infinite poll loop. A Cloud Run job must run tasks and exit. | Future option only after implementing and testing a bounded **drain-and-exit** worker mode, scheduled executions, safe lease/heartbeat shutdown, retry behavior, and a clean success exit. |
| [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-overview) | Not suitable for the current no-port continuous worker. Free Spaces sleep when unused and are designed around an interactive Space lifecycle. | Do not add a fake web server merely to keep the queue poller alive. |
| [Koyeb Free](https://www.koyeb.com/docs/run-and-scale/scale-to-zero) | Not suitable for the current no-port continuous worker. The free Web Service sleeps after one hour without inbound traffic, which a Supabase poller does not receive. | Do not use synthetic traffic to defeat scale-to-zero. Any paid fixed-instance design needs a separate operational review. |
| [Vercel Functions](https://vercel.com/docs/functions/limitations) | Not suitable. Functions are request-bounded and do not host this continuous worker. | Keep Vercel limited to the static frontend and short-lived FastAPI control plane. |

Recheck the linked limits, pricing, acceptable-use terms, and regional capacity at each
deployment. A free tier can change or disappear; monitoring, backups, and a documented
migration path remain required.

### 8.2 Build, configure, and validate

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
WORKER_MAX_IDLE_POLL_SECONDS
WORKER_LEASE_SECONDS
# Required here when the platform-managed Gmail OAuth path is enabled.
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
```

Use `WORKER_POLL_SECONDS=2` and `WORKER_MAX_IDLE_POLL_SECONDS=15` as the starting
configuration. Consecutive empty claims back off toward 15 seconds, while a claimed job
resets the delay so the worker can drain queued work without an artificial pause. Normal
successful HTTP polling is intentionally absent from the console; job lifecycle entries
and redacted warnings remain logged.

Managed browser additionally requires the allowlist and either an account BYOK pair or
this optional platform fallback pair:

```text
BROWSERBASE_API_KEY
BROWSERBASE_PROJECT_ID
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
```

Users create a Browserbase account at <https://www.browserbase.com/sign-up>, copy the
API key from <https://www.browserbase.com/settings>, and copy the Project ID from
<https://www.browserbase.com/overview>. Saving the pair calls Browserbase's read-only
`GET /v1/projects/{project_id}` endpoint with `X-BB-API-Key`, requires the returned ID
to match, and creates no session or browser-minute charge. The worker resolves the
claimed job owner's encrypted BYOK pair first and uses the platform pair only when no
owned pair exists.

Close every session immediately after success/failure and retain a 90-second stall cap.
This lowers usage for stuck work and makes the worst ordinary runaway session about two
minutes, but it does not make successful sub-minute work free: Browserbase bills every
created session for at least one minute. Consult the current
[cost-optimization guide](https://docs.browserbase.com/optimizations/cost/cost-optimization)
and [pricing page](https://www.browserbase.com/pricing) before launch.

Normal verified Google Forms completion does not require `keepAlive`. If the product
offers a retained Live View after `needs_attention`, the Browserbase plan and session
policy must support that bounded fallback; otherwise show the failure without claiming
that the form was submitted.

Google Forms login is optional at the provider-catalog level. A public form with no
file-upload question uses an ephemeral session and must not be blocked on a connection.
When a reviewed Google schema includes one explicit native résumé/CV PDF input or one
explicit Google `Add file` résumé/CV picker, prefill/submit must return
`provider_login_required` before opening the upload unless the tenant has an isolated
saved Google Browserbase context. Re-run the exact approved revision after the user
completes that connection. Do not treat Gmail OAuth as browser login state.

Start with `ALLOWED_BROWSER_PROVIDERS` empty. After controlled validation, use only
`google_forms,greenhouse`; Google Forms validation must use a one-page form and prove
the worker submits only the exact approved complete revision and recognizes only fresh
confirmation as success. Include both native and Google-picker résumé/CV upload
canaries. The picker canary must prove that one visible résumé/CV question opens one
host-validated Google picker iframe, exposes one PDF-compatible file input, receives
only the revision's owned private PDF, reports the same filename/MIME/byte length, and
shows that filename back in the original question after the picker closes. No test or
canary may click the form's final Submit control. Multiple résumé controls, an unrelated
required file, a non-PDF picker, unexpected frame host/path, unverified filename/upload,
login, MFA, or CAPTCHA must stop without a submit click. Mocked picker tests do not
replace this controlled signed-in live canary; keep Google picker upload launch-gated
until the current provider markup passes it.
Enable Lever, Ashby, or Wellfound one at a time after reviewing
its current terms and testing scan, immutable approval, submit-control uniqueness,
confirmation evidence, cancellation/idempotency, and disclosure. YC has a finished
exact-job state machine but remains gated until its controlled signed-in canary proves:

- only an exact current public YC job-detail URL is accepted;
- search/listing/account/generic/unsupported targets fail before session creation;
- the owning tenant's persistent Browserbase BYOK context is reused for YC sign-in;
- Playwright in the separate continuous worker scans the visible fields, uses an
  immutable résumé/Groq-grounded review, performs one sealed submit, and requires a
  fresh provider confirmation; and
- uncertain confirmation, changed schema, login/MFA/CAPTCHA, or an ambiguous control
  fails closed without a blind retry.

After that canary, add `yc` to the allowlist. Its optional query/remote/limit preferences
must remain display/matching-only and must never fetch, scrape, discover, or bulk-apply.
Vercel must not launch Chromium or run the worker. Keep the exact-host company-form
adapter gated until its controlled end-to-end canary passes;
do not enable Cutshort or Instahyre until their tenant-aware multi-step application
handlers exist and pass the same gate.
Users enter passwords and MFA directly in Browserbase Live View; do not ask them to send
credentials to the operator or enter credentials in an AutoApply form.

References: [Google Forms file-upload requirements](https://support.google.com/docs/answer/15473134),
[Browserbase uploads](https://docs.browserbase.com/platform/browser/files/uploads),
[Browserbase contexts](https://docs.browserbase.com/platform/browser/core-features/contexts),
and [Playwright Python file uploads](https://playwright.dev/python/docs/input#upload-files).

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
- Google Forms scan and exact-approved submit are visible as separate job kinds; the
  normal UI queues one `application_submit`, and Activity exposes verified confirmation
  or the needs-attention Live View fallback;
- CAPTCHA/MFA/challenges and uncertain submit confirmation become `needs_attention`.

Mocked tests are necessary but not live-provider evidence. Public enablement remains
blocked until a validated account BYOK pair or optional operator fallback is available,
a continuous worker host and controlled test accounts/jobs exist, and a passing staging
run is recorded for every enabled provider. Third-party markup and policies can change
after deployment, so keep one-click provider disablement through the allowlist.

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
   results, résumé-guided queue failures, public-contact extraction outcomes, queue
   age, worker leases, Browserbase session failures,
   provider challenges, and provider rate errors, plus the provider-ledger cron job's
   last successful run/failures.

## 10. Operational rules

- Never disable RLS to fix an application bug.
- Never expose or use the Supabase secret key in the browser.
- Never log request authorization, provider-credential request bodies, decrypted
  credential envelopes, Browserbase CDP URLs, or other provider headers.
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
- Never turn the max-30 outreach assistant into autonomous/unreviewed bulk cold email:
  retain an explicit external-research import, public-contact review, user contact
  choice, exact-draft approval, final send confirmation, durable queue, and all
  daily/duplicate/idempotency gates. Never probe mailboxes or send throwaway-account
  verification messages.
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
  tokens and Groq/Browserbase credential envelopes, and require reconnect or
  credential re-entry where integrity is uncertain.
- On Supabase secret-key exposure: rotate immediately, audit all tenant tables/storage,
  and invalidate worker/API deployments.
- On suspected cross-tenant access: disable private APIs, preserve redacted audit logs,
  assess affected rows/objects, fix both API scope and RLS, then notify as required.
- On Groq credential exposure: delete the affected encrypted credential from AutoApply,
  instruct the user to revoke/rotate it with the provider, audit credential access and
  generation changes, and save the replacement through the authenticated API. There is
  no deployment-wide Groq or contact-provider key.
- On Browserbase BYOK exposure: stop the user's browser jobs, disconnect retained
  contexts, delete the AutoApply credential, rotate the key in Browserbase Settings,
  then validate/save the new key with the same Project ID. Rotate the operator fallback
  separately if it was involved.
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
- [ ] Continuous worker deployed on a persistent host outside Vercel and lease recovery/
      monitoring verified
- [ ] Account Browserbase BYOK setup links/status are present; API key and Project ID
      validate through read-only project lookup without session creation, remain
      encrypted/service-role-only, and take priority over the optional trusted fallback
- [ ] Browser sessions close immediately and the 90-second stall cap is verified;
      product copy discloses the one-minute minimum charge per created session
- [ ] Initial `google_forms,greenhouse` allowlist reviewed; unsupported providers and ZipRecruiter absent
- [ ] Live stage-appropriate validation recorded for every enabled provider; Google
      Forms specifically submits only the exact approved complete revision and requires
      fresh confirmation, while Live View appears only for `needs_attention`
- [ ] Telegram/RSS and LinkedIn guest network bounds/partial failures exercised
- [ ] Résumé-guided discovery tested with encrypted account-scoped Groq BYOK and worker-side
      Telegram/RSS relevance filtering
- [ ] Google Forms queue tested for tenant isolation/deduplication, automatic stored-credential
      suggestion generation after scan, deterministic graduation/public-résumé mapping,
      exact review, one approval-bound idempotent submit, verified confirmation, and
      needs-attention-only Live View fallback
- [ ] Native and Google-picker résumé upload tested with the lease-bound owned PDF;
      the Google path requires exactly one explicit résumé/CV question, one trusted
      picker frame/input, matching filename/MIME/bytes, and provider-visible completion;
      PDF-incompatible, ambiguous, unrelated required-file, untrusted/unverified picker,
      login, and CAPTCHA cases all stop before final submission and never expose a
      Storage path or signed URL
- [ ] Google Forms is optionally connectable (`can_connect=true`,
      `connection_required=false`): ordinary public forms stay ephemeral, native and
      Google-picker résumé uploads fail with `provider_login_required` without a
      context, and the same approved revision reuses only that tenant's saved Google
      context
- [ ] Public-contact extraction tested with owned job records, syntax-only status,
      tenant ownership, and no mailbox/verification side effects
- [ ] Max-30 reviewed outreach tested end to end: external-AI workbook import, public
      contact choice, exact-draft approvals, final confirmation, durable queue, and
      gated Gmail sends; no ATS/form application appears in either Mass Cold Email subtab
