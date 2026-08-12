# AutoApply Cloud

AutoApply Cloud is a multi-user job-application workspace designed for Vercel and
Supabase. Each user signs in, uploads a private PDF résumé, maintains their own profile
and job pipeline, drafts tailored outreach with a browser-held Groq key, connects Gmail
through OAuth, reviews every message, and tracks applications.

The hosted application is the supported product entrypoint. The earlier single-user
desktop implementation remains in the repository only as migration/reference code and
is never imported by the Vercel application.

## Product capabilities

| Capability | Launch behavior |
|---|---|
| Authentication | Supabase email/password sign-up, confirmation, sign-in, reset, and sign-out, with Turnstile on public auth flows and recent sign-in enforcement for account deletion |
| Tenant isolation | Every user-owned database row and résumé object is protected by Supabase RLS |
| Résumés | Up to five fixed private Storage slots, atomic registration/activation, PDF parsing, and conservative profile suggestions |
| Groq | Per-user key persists in that browser's `localStorage`; sign-out preserves it, while explicit removal/account deletion clears it |
| Discovery | Credential-free Telegram public previews/RSS, pasted referral digests, CSV/XLSX imports, individual ATS URL detection, bounded Greenhouse/Lever/Ashby public-board enumeration, and bounded LinkedIn guest-job discovery |
| Jobs | Review/deduplicate discovered roles, save job descriptions and URLs, edit/archive records, and create tailored drafts |
| Applications | Edit, approve, send once through Gmail, and retain status/history |
| Gmail | Google Web OAuth with `openid email profile gmail.send`; platform-managed by default, with an advanced per-user Web OAuth client option; encrypted credentials/tokens and disconnect/revoke |
| LinkedIn | Bounded unofficial guest-page discovery plus manual handoff; Easy Apply remains excluded unless an official candidate-apply partnership is obtained |
| Browser application work | Greenhouse and one-page Google Forms have aligned scan → approved prefill → explicit-submit handlers. Lever and Ashby mappings passed read-only live scan canaries but remain disabled pending controlled prefill/submit tests; Wellfound still needs its signed-in canary. YC, Cutshort, and Instahyre are connection-only and fail safely until tenant-aware multi-step handlers are implemented. |

LinkedIn guest discovery uses a bounded, throttled, unofficial public endpoint. It can
stop working if LinkedIn changes or blocks that endpoint, and it never signs a user in
or submits Easy Apply. LinkedIn login is not presented as application authorization:
LinkedIn OpenID Connect identifies a member but does not grant candidate Easy Apply
access. The product does not ship credential scraping, CAPTCHA bypass, or unsupported
LinkedIn application automation.

## Discovery and application review

Users can bring jobs into their own workspace without supplying third-party credentials:

- fetch configured Telegram public-channel previews and RSS feeds through bounded HTTP
  requests;
- paste a referral digest and normalize the job/form links it contains;
- import CSV or XLSX files up to 4 MB with flexible job-column headings;
- detect supported public application providers from submitted URLs;
- enumerate published jobs from up to 8 official Greenhouse, Lever, or Ashby company
  boards through their credential-free public APIs; and
- run a deliberately bounded and throttled LinkedIn guest-job search.

Network discovery is durable worker work; paste/file normalization is a bounded ingest.
Every result is normalized, deduplicated per tenant, and shown for review. Public ATS
board discovery enumerates only the company-board URLs the user supplies; it is not an
internet-wide job-search engine. No LinkedIn account cookie or password is collected.
The 4 MB spreadsheet cap leaves margin under Vercel's Function payload limit. Résumé
PDFs still upload directly to Supabase Storage and keep their separate 6 MiB limit.

Application automation uses sealed form revisions. A scan records the exact fields,
proposed answers, résumé selection, and schema hash. The first approval atomically seals
the exact reviewed answers on that revision. After sealing, any field, answer, résumé,
or schema change requires a new revision. Prefill and submit are separate queue
operations; submission requires the approved revision and never follows automatically
from scanning or prefilling. Before approval, the user may ask Groq for factual answer
suggestions grounded in the owned profile, linked résumé, and job; suggestions remain
browser-visible drafts and never approve themselves.

The managed-browser registry contains exactly `google_forms`, `greenhouse`, `lever`,
`ashby`, `yc`, `wellfound`, `cutshort`, and `instahyre`, but registry membership does
not mean that all eight have an end-to-end application handler. Greenhouse and
one-page Google Forms are aligned; multi-page or branching Google Forms are unsupported.
Lever and Ashby have fail-closed mappings whose read-only live scans passed on
2026-08-11, but their prefill/submit confirmation gates still require controlled test
postings. Wellfound still requires a signed-in canary. YC, Cutshort, and Instahyre
support an isolated user login/context only; their
workers return `needs_attention` until tenant-aware multi-step state machines are built
and validated. ZipRecruiter is intentionally not part of the hosted product. CAPTCHA,
MFA, login expiry, ambiguous confirmation, or an unrecognized required question also
produces `needs_attention`; the worker never bypasses or guesses through those
conditions.

Each account may own only `<user-id>/resume-1.pdf` through
`<user-id>/resume-5.pdf` in the private `resumes` bucket. The browser uploads directly;
the registration RPC then verifies that the owned object exists and transactionally
deactivates the previous active résumé while activating the selected slot. Deleting a
résumé removes that object and its résumé metadata, not the user's application history.

## Architecture

```text
Browser (public SPA)
  ├─ Supabase Auth session
  ├─ private direct résumé upload
  └─ browser-only Groq key
            │ bearer JWT
            ▼
Vercel FastAPI control plane
  ├─ validates the current Supabase user
  ├─ accesses tenant rows with the user's JWT/RLS
  ├─ runs foreground Groq drafting
  ├─ ingests bounded paste/file discovery
  └─ manages Google OAuth and reviewed Gmail sends
            │
            ├─ Supabase Postgres + private Storage
            └─ persistent worker
                 ├─ Telegram/RSS + LinkedIn guest discovery
                 ├─ Greenhouse/Lever/Ashby public-board discovery
                 └─ Browserbase scan/prefill/submit
```

No SQLite database, local browser profile, APScheduler job, or process-global user state
is used by `app.saas_main:app`.

## Local cloud-mode setup

Use Python 3.12:

```bash
python3.12 -m venv .venv-saas
source .venv-saas/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env.saas.local
```

Create a Supabase project, apply the migration, and put development values in
`.env.saas.local`:

```bash
supabase link --project-ref YOUR_PROJECT_REF
supabase db push
set -a; source .env.saas.local; set +a
uvicorn app.saas_main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The health and public shell still load when configuration
is incomplete, and private/provider actions report which setup is missing.

## Google account login

**Continue with Google** uses Supabase Auth and creates the same Supabase session as
email/password sign-in. It requests identity scopes only; it does not grant Gmail
access. Configure it outside the application environment:

1. Create a Google OAuth client of type **Web application** and add the local and
   production origins under Authorized JavaScript origins.
2. Add the callback displayed by Supabase Authentication → Sign In / Providers →
   Google under Authorized redirect URIs. Hosted projects use
   `https://YOUR_PROJECT_REF.supabase.co/auth/v1/callback`.
3. Enable Google on that Supabase provider page and paste the Web client ID/secret.
4. Add `http://127.0.0.1:8000/**`, any preview URL pattern, and the exact production
   URL to Supabase Authentication → URL Configuration as appropriate.

No Google login secret belongs in `public/app.js` or an application environment
variable. The same Google Cloud project may contain the separate Gmail client below;
using separate Web clients makes their callbacks and scopes easier to audit.

## Google/Gmail sending setup

Normal users do not need a Google Cloud credential. When the deployment provides
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, **Connect Gmail** uses that
platform-managed OAuth client by default. The deployment operator owns its consent
screen, verification, quotas, and credential rotation.

An advanced user may instead bring a separate Google **Web application OAuth client**.
This is useful for a private deployment or a user who wants to own the Google project,
but it requires Google Cloud configuration:

1. [Create or select a Google Cloud project](https://console.cloud.google.com/projectcreate).
2. [Enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
3. Configure [Branding](https://console.cloud.google.com/auth/branding),
   [Audience](https://console.cloud.google.com/auth/audience), and
   [Data Access](https://console.cloud.google.com/auth/scopes). Request only `openid`,
   `email`, `profile`, and `https://www.googleapis.com/auth/gmail.send`.
4. Open [Google Auth Platform → Clients](https://console.cloud.google.com/auth/clients),
   create an OAuth client, and select **Web application** (not Desktop). Copy the client
   ID and secret when Google shows them; the full secret may not be displayed again.
5. Register the single callback shown by AutoApply exactly. For this repository's local
   setup it is `http://127.0.0.1:8000/api/v1/oauth/google/callback`; production uses
   `https://YOUR_DOMAIN/api/v1/oauth/google/callback`. Scheme, host, port, path, and
   trailing-slash behavior must match `GOOGLE_REDIRECT_URI` exactly.
6. In AutoApply's Gmail connection panel, open the advanced user-managed option, paste
   the OAuth client ID and client secret, save them, select that credential source, and
   then connect the Gmail account.

A standard Google Cloud API key is not sufficient: Gmail authorization requires an
OAuth Web client ID/secret and a separate consent grant from the Gmail account. User
OAuth client credentials are encrypted before database storage, are accessible only to
the trusted server role, and are never returned to the browser after save. Disconnect
Gmail before replacing or deleting them. OAuth state records bind the selected
credential source and generation so a callback or later refresh cannot silently switch
clients.

For an External consent screen in **Testing**, add every Gmail account under **Test
users**. Because AutoApply requests `gmail.send`, a test user's authorization and
refresh token expire seven days after consent; reconnecting is expected. For public
production use, the Google-project owner must publish the app and complete brand/domain
and sensitive-scope verification for the exact production domain and scopes.

Whether using the default or advanced path, the user chooses the sender account,
reviews the requested send-only permission, and returns to AutoApply. Never ask for a
Gmail password, app password, `credentials.json`, desktop `token.json`, or API key.

OAuth start, callback, reconnect, and disconnect operations use a per-user generation
so an older callback cannot recreate a connection after a newer flow or disconnect.
Disconnect deletes the local encrypted token record and makes a best-effort Google
revocation request. If Google cannot confirm revocation, the UI instructs the user to
remove AutoApply from their Google Account as well.

To prevent account recreation from resetting send caps, the database retains
domain-separated SHA-256 hashes of the Google provider account and recipient alongside
a random send-event identifier. These rows contain no user ID, email address, message,
or OAuth token, and expire no later than 90 days after creation (normally sooner,
according to the configured duplicate-recipient window). They may therefore remain
briefly after account deletion solely for provider-level rate and duplicate prevention.
Expired rows stop participating in either control immediately; an hourly database job
and the next send-reservation hot path physically prune expired rows. Physical deletion
can lag if the cron job fails, so its run history is a production monitoring gate.

## Bot protection

AutoApply uses Cloudflare Turnstile through Supabase Auth for sign-in, sign-up, and
password-reset requests. Create a Turnstile widget for every exact staging/production
hostname, then use this rollout order so auth is not accidentally locked out:

1. Test the full configuration with a separate staging Supabase project.
2. Set the public `TURNSTILE_SITE_KEY` in the matching Vercel environment and deploy
   while Supabase CAPTCHA enforcement is still disabled.
3. Verify the widget loads under the deployed Content Security Policy and the public
   config reports CAPTCHA ready.
4. Put the matching Turnstile **secret** only in Supabase Authentication → Bot and
   Abuse Protection, then enable CAPTCHA.
5. Immediately test sign-in, sign-up, and password reset in a new browser session.

Never put the Turnstile secret in Vercel or browser code. Do not enable Supabase CAPTCHA
before the site-key deployment is live. See the
[Supabase CAPTCHA guide](https://supabase.com/docs/guides/auth/auth-captcha),
[Turnstile widget lifecycle](https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/),
and [Turnstile CSP requirements](https://developers.cloudflare.com/turnstile/reference/content-security-policy/).

## Deploy to Vercel

Follow the complete, provider-by-provider
[Vercel production deployment checklist](docs/06-Vercel-Deployment-Checklist.md) before
adding a public domain or promoting a deployment.

The project uses Vercel's native FastAPI detection through
`[tool.vercel].entrypoint = "app.saas_main:app"`. Static files under `public/` are served
from Vercel's CDN.

Configure these Production and Preview variables:

```text
SUPABASE_URL
SUPABASE_PUBLISHABLE_KEY
SUPABASE_SECRET_KEY
SITE_URL
TURNSTILE_SITE_KEY
TOKEN_ENCRYPTION_KEY
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_REDIRECT_URI
GROQ_MODEL
MAX_RESUME_BYTES
DEFAULT_DAILY_SEND_CAP
OAUTH_STATE_TTL_SECONDS
SUPABASE_HTTP_TIMEOUT_SECONDS
BROWSERBASE_API_KEY
BROWSERBASE_PROJECT_ID
ALLOWED_BROWSER_PROVIDERS
```

Keep the allowlist empty until controlled staging validation. The current initial
allowlist recommendation is exactly:

```text
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
```

Add `lever`, `ashby`, or `wellfound` individually only after its controlled canary has
passed. Leave `yc`, `cutshort`, and `instahyre` out until their tenant-aware multi-step
state machines exist and pass live staging. `google_forms` covers one-page forms only.

The Browserbase values are required only for managed-browser login and application
jobs. `GOOGLE_REDIRECT_URI` is the deployment's one fixed Gmail callback. The
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` variables are optional only when every
Gmail user will configure the advanced user-managed client; setting both enables the
default platform-managed path. These values do not enable Supabase Google account
login and do not connect Google Forms.
`TOKEN_ENCRYPTION_KEY` is required for Gmail token storage and user-managed OAuth
client storage, and is shared by trusted API/worker environments for encrypted provider
context identifiers.

Then deploy a preview and promote only after the runbook checks pass:

```bash
vercel link
vercel deploy
vercel deploy --prod
```

A code-complete deployment is not automatically launch-ready. Promotion remains
blocked until a real staging migration has passed RLS, Storage, send-reservation,
OAuth-generation, deletion, discovery-boundary, immutable-revision, and queue concurrency
tests; each enabled browser provider has passed a controlled Browserbase staging run;
the platform Google project has been verified for the exact production domain and
`gmail.send` scope before default public use (and user-managed project owners are shown
their equivalent Testing/verification obligations);
Vercel/Supabase/worker production configuration is verified; and the operator has
replaced every legal/support placeholder with reviewed, operator-specific terms,
privacy, deletion, and contact information.

`.vercelignore` excludes local secrets, PDFs, SQLite files, browser profiles,
screenshots, generated output, tests, legacy launchers, and the persistent worker from
the web-function bundle. Always deploy from a clean Git checkout rather than uploading
this working directory with its historical personal files.

## Persistent worker

Vercel hosts the short-lived control plane. Network discovery and browser automation
jobs are leased from Supabase by `worker/main.py`, which must run continuously on a
persistent-process host; a Vercel Function cannot replace it:

```bash
docker build -f worker/Dockerfile -t autoapply-worker .
docker run --env-file .env.worker autoapply-worker
```

The worker needs `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`,
`WORKER_ID`, `WORKER_POLL_SECONDS`, and `WORKER_LEASE_SECONDS`. Browser work additionally
needs `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, and the exact provider allowlist
above. Keep the allowlist empty until staging has exercised Greenhouse and one-page
Google Forms, then begin with only those two entries.

The root `.dockerignore` is deny-by-default and sends only `worker/` plus the SaaS
modules copied by `worker/Dockerfile` to the build daemon. Keep that allowlist in sync
with Dockerfile `COPY` statements; never replace it with a broad build context on a
remote builder.

Code and mocked selectors cannot validate a live provider. Before public enablement the
operator must supply Browserbase credentials on a plan that supports keep-alive review
sessions, a persistent worker host, and controlled test accounts/jobs for every enabled
provider. Users then sign in themselves inside
Browserbase Live View; neither the operator nor AutoApply asks for their passwords.

## Tests

```bash
source .venv-saas/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

The suite includes legacy pure-logic regression tests plus hosted schema, API,
encryption, provider, OAuth/MIME, PDF, discovery, immutable form-revision, worker, and
tenant-boundary tests. Mocked provider tests do not replace the live staging gate.

## Documentation

- [Research and decision record](docs/00-Research-and-Decision-Record.md)
- [Product requirements](docs/01-PRD%20%281%29.md)
- [Software requirements](docs/02-SRS.md)
- [Architecture](docs/03-Architecture.md)
- [Technical specification](docs/04-Technical-Spec.md)
- [Build and launch runbook](docs/05-Build-Runbook.md)

The Apple Silicon launchers are documented in [README_MAC.md](README_MAC.md) and run the
same cloud-mode entrypoint locally. The earlier single-user application remains only as
reference code and uses `requirements-legacy.txt` when invoked explicitly.
