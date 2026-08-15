# Technical Specification

## AutoApply Cloud 2.0

**Status:** Implementation baseline
**Date:** 2026-08-15

## 1. Hosted project structure

```text
.
├── app/
│   ├── saas_main.py                 # Vercel-safe FastAPI application
│   └── saas/
│       ├── auth.py                  # Supabase bearer-token dependency
│       ├── config.py                # deployment configuration
│       ├── errors.py                # stable API error envelope
│       ├── store.py                 # Supabase REST/Storage repository
│       ├── schemas.py               # public request/response models
│       ├── crypto.py                # provider credential/token encryption
│       ├── groq.py                  # account-scoped Groq adapter
│       ├── hunter.py                # account-scoped Hunter validation/HR contacts
│       ├── gmail.py                 # web OAuth and Gmail send adapter
│       ├── browser.py               # managed-browser control adapter
│       ├── providers.py             # capability catalog/allowlist
│       └── discovery/               # bounded feeds/import/URL normalization
├── public/
│   ├── index.html                   # auth + onboarding + workspace
│   ├── app.js
│   └── styles.css
├── worker/
│   ├── main.py                      # durable job poller
│   ├── handlers.py                  # provider-safe job handlers
│   ├── browser_runtime.py           # Browserbase Playwright/CDP lifecycle
│   ├── providers/                   # eight host-allowlisted registry adapters; capability-gated
│   ├── Dockerfile
│   └── requirements.txt
├── supabase/
│   └── migrations/
│       └── *_autoapply_cloud.sql    # schema, RLS, Storage, claim RPC
├── tests/
│   └── test_saas_*.py
├── pyproject.toml                   # Python 3.12 + Vercel entrypoint
├── vercel.json
├── .vercelignore
└── .env.example
```

Legacy local modules may remain during migration, but `app.saas_main:app` must not import
`app.main`, SQLite, APScheduler, desktop OAuth, or Playwright.

## 2. Environment variables

### Publicly returned configuration

```dotenv
SUPABASE_URL=https://project-ref.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
SITE_URL=https://example.com
TURNSTILE_SITE_KEY=<public Cloudflare site key>
```

The publishable key is safe to return to the browser only because every exposed table
and Storage bucket is protected by RLS.

`TURNSTILE_SITE_KEY` is also intentionally public. Its matching secret is configured
only in Supabase Authentication → Bot and Abuse Protection; it is not a Vercel variable.

### Server-only Vercel/worker configuration

```dotenv
SUPABASE_SECRET_KEY=sb_secret_...
TOKEN_ENCRYPTION_KEY=<urlsafe-base64 32-byte key>

# Fixed for both platform- and user-managed Google OAuth Web clients.
GOOGLE_REDIRECT_URI=https://example.com/api/v1/oauth/google/callback

# Optional: enables the default platform-managed Gmail path.
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...

GROQ_MODEL=openai/gpt-oss-120b
MAX_RESUME_BYTES=6291456
DEFAULT_DAILY_SEND_CAP=10

# Optional platform fallback for managed-browser application features.
# A validated account-scoped Browserbase credential takes priority.
BROWSERBASE_API_KEY=...
BROWSERBASE_PROJECT_ID=...
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
WORKER_ID=worker-1
WORKER_POLL_SECONDS=2
WORKER_MAX_IDLE_POLL_SECONDS=15
WORKER_LEASE_SECONDS=120
```

The Supabase secret key, encryption key, platform Google client secret, user-managed
Google client credentials, provider tokens, and browser connection URLs must never be
returned by `/config`, embedded in JavaScript, stored in browser storage, or logged.
User-supplied Groq, Hunter, and Browserbase credentials are submitted once through the
authenticated provider-credential API, encrypted as versioned JSON with
`TOKEN_ENCRYPTION_KEY`, and persisted only in the service-role-only
`user_provider_credentials` table. API responses expose only provider status, safe
hints, validation codes, and timestamps—never plaintext or ciphertext.

The worker begins polling at `WORKER_POLL_SECONDS`, exponentially backs off consecutive
empty claims toward `WORKER_MAX_IDLE_POLL_SECONDS`, and resets the idle delay as soon as
it claims work. Successful HTTP transport entries are suppressed; worker lifecycle,
claimed-job, completion, and redacted warning logs remain visible.

The API and worker share `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and
`TOKEN_ENCRYPTION_KEY`; trusted code uses the latter to decrypt only the credential or
context required for the owned request or claimed tenant job. For Browserbase, the
worker first resolves the claimed job owner's validated BYOK API-key/Project-ID pair
and uses the operator `BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID` only when no owned
pair exists. Both sources being absent disables managed-browser work. An empty
`ALLOWED_BROWSER_PROVIDERS` disables all browser jobs; the exact
initial allowlist above enables Greenhouse plus one-page Google Forms scan,
exact-approved submit, and verified-confirmation handling and excludes ZipRecruiter.
Add Lever, Ashby, or Wellfound individually only after its controlled canary passes. YC
has a finished exact saved-job scan/review/sealed-submit state machine but remains
launch-gated pending a controlled signed-in canary. After that canary, add `yc`
explicitly, for example
`ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse,yc`. The
generic exact-host `company_form` adapter is internal/gated and is not a public catalog
or allowlist entry. Leave Cutshort and Instahyre out until their tenant-aware multi-step
state machines are implemented and validated.

`GOOGLE_REDIRECT_URI` is the one fixed Gmail OAuth callback and is required for either
credential path. `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are optional as a pair:
when present they provide the default platform-managed Gmail OAuth client. Without
them, Gmail connection is still available to an authenticated user who saves an
advanced user-managed Web OAuth client, provided the server secret store and Fernet key
are configured. A standard Google Cloud API key is not an OAuth client and is rejected.
These settings do not authorize Google Forms. Users authenticate to Google Forms or
job boards themselves inside Browserbase Live View. The Google Forms connection is
optional: ordinary public forms continue to use an ephemeral browser, while a scanned
form containing a signed-in résumé/CV upload returns `provider_login_required` until
that tenant has connected an isolated Google browser context. Gmail OAuth tokens do
not provide browser cookies and cannot satisfy this requirement. Groq and Hunter are
also account-scoped encrypted credentials rather than worker environment variables;
there is no deployment-wide `GROQ_API_KEY` or `HUNTER_API_KEY`. Hunter contact lookup
remains a foreground, user-initiated API call.

## 3. Database schema

The canonical executable schema is the ordered Supabase migrations. Deployments must
apply through `202608150003_yc_exact_job_automation.sql`. That migration installs the
exact-current-YC-job target contract, tenant-only YC preferences, provider/job binding,
and service-role guards without enabling YC discovery. In particular,
`202608130001_google_forms_manual_submit.sql` is a temporary fail-closed state;
`202608130002_google_forms_approved_submit.sql` is the required forward migration that
removes that prohibition and installs the exact-approved/required-answer submit gate.
Never stop a deployment at `001` or edit migration history destructively. Logical model:

### `profiles`

```sql
user_id uuid primary key references auth.users(id) on delete cascade,
full_name text,
email text,
phone text,
location text,
headline text,
summary text,
years_experience numeric,
work_authorization text,
notice_period text,
college text,
degree text,
graduation_year smallint,
resume_url text, -- user-provided public HTTPS URL, not the private PDF path
linkedin_url text,
github_url text,
portfolio_url text,
education jsonb not null default '[]',
skills jsonb not null default '[]',
preferences jsonb not null default '{}',
onboarding_completed boolean not null default false,
account_status text not null default 'active', -- active | deleting
deletion_started_at timestamptz,
created_at timestamptz not null default now(),
updated_at timestamptz not null default now()
```

`graduation_year` is the deterministic source for recognized graduation/passout/batch
questions. `resume_url` is the deterministic source only for recognized public
résumé/CV link questions. It must be a user-reviewed HTTPS URL and is deliberately
separate from the private `resumes.storage_path`; the API/worker must not substitute a
private path, an expiring signed URL, or a link inferred from the uploaded PDF.

### `user_settings`

```sql
user_id uuid primary key references auth.users(id) on delete cascade,
daily_send_cap integer not null default 10 check (daily_send_cap between 0 and 25),
duplicate_window_days integer not null default 7,
require_review boolean not null default true,
timezone text not null default 'UTC',
created_at timestamptz not null default now(),
updated_at timestamptz not null default now()
```

### `discovery_preferences`

```sql
user_id uuid primary key references auth.users(id) on delete cascade,
enabled_sources text[] not null,
keywords text[] not null default '{}',
excluded_keywords text[] not null default '{}',
locations text[] not null default '{}',
remote_only boolean not null default false,
schedule_enabled boolean not null default false,
schedule_interval_minutes integer not null default 360,
max_results_per_run integer not null default 100,
feed_urls text[] not null default '{}',
metadata jsonb not null default '{}',
created_at timestamptz not null default now(),
updated_at timestamptz not null default now()
```

Allowed source IDs are `telegram`, `rss`, `referral_digest`, `csv`, `xlsx`,
`public_ats`, and `linkedin_guest`. Array cardinality/byte limits, a 15–1440 minute
interval, and a 1–200 result bound are enforced in the migration. The row is
tenant-owned/RLS-protected; source URLs are still revalidated by the worker and are not
a general-purpose fetch proxy.

### `resumes`

```sql
id uuid primary key default gen_random_uuid(),
user_id uuid not null references auth.users(id) on delete cascade,
storage_path text not null,
original_name text not null,
mime_type text not null check (mime_type = 'application/pdf'),
size_bytes bigint not null check (size_bytes between 1 and 6291456),
sha256 text,
parsed_text text,
parse_status text not null default 'uploaded',
parse_error text,
is_active boolean not null default true,
created_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
unique (user_id, storage_path)
```

A partial unique index permits one active résumé per user. The executable registration
RPC further requires one of five exact private object paths,
`<user-id>/resume-1.pdf` through `<user-id>/resume-5.pdf`, verifies the Storage row, and
serializes deactivate/activate changes in one transaction.

### `jobs`

```sql
id uuid primary key default gen_random_uuid(),
user_id uuid not null references auth.users(id) on delete cascade,
source text not null default 'manual',
external_id text,
normalized_url text,
apply_url text,
title text not null,
company text not null,
location text,
description text not null check (char_length(description) between 20 and 25000),
contact_email text,
status text not null default 'saved',
metadata jsonb not null default '{}',
last_discovered_at timestamptz,
archived_at timestamptz,
created_at timestamptz not null default now(),
updated_at timestamptz not null default now()
```

A partial unique index enforces `(user_id, normalized_url)` when URL is non-null.

### `applications`

```sql
id uuid primary key default gen_random_uuid(),
user_id uuid not null references auth.users(id) on delete cascade,
job_id uuid references jobs(id) on delete set null,
channel text not null default 'email',
status text not null default 'draft_pending',
recipient text,
subject text,
body text check (body is null or char_length(body) <= 20000),
content_revision bigint not null default 1,
approved_revision bigint,
approved_at timestamptz,
sent_at timestamptz,
provider_message_id text,
provider_thread_id text,
send_idempotency_key text,
last_error text,
metadata jsonb not null default '{}',
created_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
unique (user_id, send_idempotency_key)
```

Status values: `draft_pending`, `drafted`, `approved`, `queued`, `sent`, `manual`,
`applied`, `rejected`, `interview`, `failed`, `archived`.

### `connections`

User-visible, non-secret integration state:

```sql
id uuid primary key default gen_random_uuid(),
user_id uuid not null references auth.users(id) on delete cascade,
provider text not null,
mode text not null,
status text not null,
external_account_id text,
display_name text,
scopes text[] not null default '{}',
expires_at timestamptz,
last_verified_at timestamptz,
metadata jsonb not null default '{}',
created_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
unique (user_id, provider)
```

### `connection_secrets`

Server-only; RLS enabled with no browser policies and explicit grants revoked from
`anon`/`authenticated`:

```sql
connection_id uuid primary key references connections(id) on delete cascade,
user_id uuid not null references auth.users(id) on delete cascade,
access_token_ciphertext text,
refresh_token_ciphertext text,
browser_context_id_ciphertext text,
browser_session_id_ciphertext text,
token_type text,
created_at timestamptz not null default now(),
updated_at timestamptz not null default now()
```

### `user_google_oauth_clients`

Server-only encrypted credentials for the advanced per-user Google Web OAuth client.
RLS is enabled, all `public`/`anon`/`authenticated` privileges are revoked, and only the
service role may access the table:

```sql
user_id uuid primary key references auth.users(id) on delete cascade,
client_id_ciphertext text not null,
client_secret_ciphertext text not null,
generation bigint not null check (generation > 0),
created_at timestamptz not null default now(),
updated_at timestamptz not null default now()
```

The status API decrypts only server-side to validate the record and derive a masked
client-ID hint. It never returns either ciphertext, the full client ID, or the client
secret. `save_user_google_oauth_client` and `delete_user_google_oauth_client` take the
Gmail lifecycle advisory lock, reject an existing Gmail connection or pending provider
send, advance the generation, and invalidate all pending Google OAuth states. Account
deletion removes this row by cascade. Gmail disconnect removes the token connection but
leaves this separate client configuration until the user deletes it.

### `user_provider_credentials`

Server-only encrypted BYOK credentials for `groq`, `hunter`, and `browserbase`. RLS is
enabled with no browser policy; privileges are revoked from `public`, `anon`, and
`authenticated`, and the service role is the only database role with access:

```sql
user_id uuid not null references auth.users(id) on delete cascade,
provider text not null check (provider in ('groq', 'hunter', 'browserbase')),
credential_ciphertext text not null,
verification_status text not null, -- verified | unverified | invalid
verification_code text,
verified_at timestamptz,
generation bigint not null check (generation > 0),
created_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
primary key (user_id, provider)
```

The ciphertext is a versioned JSON envelope encrypted with `TOKEN_ENCRYPTION_KEY`.
Groq and Hunter contain one provider key; Browserbase contains both the API key and
Project ID. Save/delete RPCs use an account/provider advisory lock, increment the
generation on replacement, write a secret-free audit event, and cascade on account
deletion. Replacing or deleting Browserbase BYOK is rejected while that account has an
active browser job or retained managed-browser context, because those remote resources
belong to the original Browserbase project.

### `oauth_states`

Server-only, single-use rows containing a hash of the random state, bound user/provider,
per-user connection generation, return path, encrypted PKCE verifier, short expiry,
`credential_source` (`platform` or `user`), and nullable
`credential_generation`. A platform state has no credential generation; a user state
must carry the exact positive generation. Consuming the state deletes it atomically.

### `connection_lifecycles`

Server-only `(user_id, provider)` generation/status rows.
`create_google_oauth_state_v2` verifies the selected credential binding, advances the
Gmail generation, and removes older states. `begin_google_disconnect` invalidates
pending states before external revocation, and `finish_google_disconnect` deletes the
connection only if the same generation is still disconnecting. This serializes
credential changes, reconnect, callback, and disconnect races.

### `automation_jobs`

```sql
id uuid primary key default gen_random_uuid(),
user_id uuid not null references auth.users(id) on delete cascade,
application_id uuid references applications(id) on delete set null,
form_revision_id uuid references application_form_revisions(id) on delete set null,
kind text not null,
provider text,
status text not null default 'queued',
payload jsonb not null default '{}',
progress jsonb not null default '{}',
result jsonb,
error_code text,
error_message text,
idempotency_key text not null,
attempts integer not null default 0,
max_attempts integer not null default 3,
run_after timestamptz not null default now(),
locked_by text,
locked_at timestamptz,
lease_expires_at timestamptz,
cancel_requested_at timestamptz,
created_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
unique (user_id, idempotency_key)
```

Additional durable kinds are `discover_public_feeds`, `discover_linkedin_guest`,
`discover_public_ats`, `application_scan`, `application_prefill`, and
`application_submit`. Form jobs validate that `application_id`, `form_revision_id`
(when required), provider, and résumé all belong to the authenticated user before
insertion. The queue stores references and bounded parameters, never a substitute
answer set or approval flag.
`discover_public_feeds` uses the internal aggregate provider `public_feeds` for one
bounded Telegram/RSS run; it is not a connectable user account/provider.
`discover_public_ats` similarly uses aggregate provider `public_ats` and accepts only
canonical official Greenhouse, Lever, or Ashby board URLs.

### `application_form_revisions`

```sql
id uuid primary key default gen_random_uuid(),
user_id uuid not null references auth.users(id) on delete cascade,
application_id uuid not null references applications(id) on delete cascade,
job_id uuid not null references jobs(id) on delete restrict,
resume_id uuid not null references resumes(id) on delete restrict,
provider text not null,
form_url text not null,
revision bigint not null,
schema_hash text not null,
question_schema jsonb not null,
answers jsonb not null default '{}',
status text not null default 'scanned',
approved_revision bigint,
approved_schema_hash text,
approved_at timestamptz,
submitted_at timestamptz,
provider_submission_id text,
submission_result jsonb,
last_error text,
created_at timestamptz not null default now(),
updated_at timestamptz not null default now(),
unique (application_id, revision)
```

Provider is restricted to `google_forms`, `greenhouse`, `lever`, `ashby`, `yc`,
`wellfound`, `cutshort`, or `instahyre`. The immutable trigger rejects changes to
ownership, job/application/résumé references, provider/URL, revision number, schema
hash/schema, or creation time. Proposed answers may change only in the same transaction
as first approval; they and the approval tuple are immutable afterward. Only bounded
lifecycle/result fields may otherwise advance. A new scan appends the next revision and
supersedes prior unsubmitted revisions. Exact approval requires the latest revision
number, its 64-character schema hash, and the answers JSON the user reviewed. An
identical approval retry is idempotent, while changed answers on a sealed revision fail.

Statuses are `scanned`, `prefilled`, `approved`, `submitted`, `needs_attention`,
`failed`, and `superseded`. Browser roles have read-own access but cannot insert/update
the table directly. The worker writes scans/results only through lease-bound service RPCs.

### `send_events`, `provider_send_events`, `answer_bank`, and `audit_events`

`send_events`, `answer_bank`, and `audit_events` are tenant-owned. Answer uniqueness is
`(user_id, normalized_question)`. Send events contain provider IDs/outcomes but never
message bodies or tokens. Audit metadata is redacted and bounded.

`provider_send_events` is the deliberate non-tenant exception: it has no user or
application foreign key and is inaccessible to browser roles. It stores only a random
send-event UUID, `gmail`, domain-separated SHA-256 hashes of the Google subject and
recipient, outcome/error code, timestamps, and expiry. It enforces a provider-account
rolling 24-hour cap and duplicate window across account recreation. The schema requires
expiry within 90 days of creation, and reservations prune expired rows.

`prune_provider_send_events()` is service-only and scheduled by Supabase Postgres Cron
as `autoapply-prune-provider-send-events` at minute 17 of every hour. Limit/duplicate
queries also require `expires_at > now()`, so an expired row stops affecting behavior
before the next cron run; the reservation hot path deletes expired rows as a second
cleanup path. Migration failure to enable `pg_cron` is a deployment failure. Cron run
failures can delay physical deletion without extending logical/enforcement expiry, so
`cron.job_run_details` must be monitored.

## 4. RLS policy template

Every browser-visible tenant table enables RLS and has explicit role policies:

```sql
alter table public.jobs enable row level security;

create policy "jobs_select_own"
on public.jobs for select to authenticated
using ((select auth.uid()) = user_id);

create policy "jobs_insert_own"
on public.jobs for insert to authenticated
with check ((select auth.uid()) = user_id);

create policy "jobs_update_own"
on public.jobs for update to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy "jobs_delete_own"
on public.jobs for delete to authenticated
using ((select auth.uid()) = user_id);
```

Indexes on `user_id` and `(user_id, status)` support policy predicates. Server-only
tables revoke all privileges from browser roles in addition to RLS.
`discovery_preferences` uses full own-row RLS. `application_form_revisions` grants only
own-row `SELECT` to browser roles; exact approval is exposed solely through
`approve_application_form_revision`, while scan/progress/submission RPCs are
service-role only and validate the active worker lease.

## 5. Storage

Private bucket: `resumes`.

Object path:

```text
<auth.uid()>/resume-[1-5].pdf
```

Storage policies compare `(storage.foldername(name))[1]` to `auth.uid()::text`, require
exactly one folder segment and one of the five slot filenames, and require an active
account. An advisory-lock quota check caps private objects at five under concurrent
uploads. Bucket configuration restricts PDF and 6 MB.

## 6. HTTP API

All routes are under `/api/v1`. Private routes require a Supabase bearer JWT.

### Public

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/config` | Supabase URL/publishable key, public feature flags |
| GET | `/api/v1/health` | build/runtime/config readiness without tenant calls |
| GET | `/api/v1/providers` | capability catalog; no private connection status |

### Profile and résumé

| Method | Path | Result |
|---|---|---|
| GET/PATCH | `/api/v1/profile` | own typed profile |
| GET/PATCH | `/api/v1/settings` | own typed settings |
| POST | `/api/v1/resumes/register` | atomically verify/register/activate uploaded owned slot |
| GET | `/api/v1/resumes` | own metadata list |
| POST | `/api/v1/resumes/{id}/parse` | bounded PDF text/annotation-link extraction and conservative contact suggestions |
| POST | `/api/v1/resumes/{id}/analyze` | resolve the owned Groq credential and return allowlisted profile/role suggestions without mutating the profile |
| DELETE | `/api/v1/resumes/{id}` | delete owned résumé row/object, retain application history |

### Account-scoped provider credentials

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/provider-credentials` | list safe status/hints for the caller's Groq, Hunter, and Browserbase credentials |
| PUT | `/api/v1/provider-credentials/{provider}` | validate, encrypt, and create/replace one owned credential; `provider` is `groq`, `hunter`, or `browserbase` |
| DELETE | `/api/v1/provider-credentials/{provider}` | delete one owned encrypted credential after lifecycle guards pass |

The PUT body is `{ "api_key": "..." }` for Groq/Hunter and
`{ "api_key": "...", "project_id": "..." }` for Browserbase. It validates before
persistence and then discards the request plaintext. Browserbase validation calls
`GET https://api.browserbase.com/v1/projects/{project_id}` with `X-BB-API-Key`, requires
HTTP 200 and a matching returned project ID, and does **not** create a browser session
or consume browser minutes. The list/PUT responses expose only safe hints,
verification status/code, generation, and timestamps. The browser clears credential
input controls after the request and retains no provider secret.

New Browserbase users can register at
<https://www.browserbase.com/sign-up>. Existing users obtain their API key from
<https://www.browserbase.com/settings> and their Project ID from
<https://www.browserbase.com/overview>.

### Groq

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/jobs/{id}/draft` | resolve the owned Groq credential and create/update an owned draft |

Groq generation routes resolve and decrypt the authenticated user's verified credential
just in time. No key header, query parameter, queue payload, or provider key appears in
the response.

### Hunter

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/jobs/{id}/contacts/hunter?limit=1..10` | search bounded HR contacts for the owned job's company |

The endpoint resolves the authenticated user's verified encrypted Hunter credential.
The adapter uses Hunter's `X-API-Key` provider header, never a query parameter; applies
fixed timeouts; does not cache/log/return the key or provider error body; and returns
only bounded domain, email, name, position, confidence, and verification status fields.
The endpoint loads the owned job before provider lookup and requires its company name.
It exposes no durable Hunter job or unattended company search.

### Jobs/applications

| Method | Path | Result |
|---|---|---|
| GET/POST | `/api/v1/jobs` | bounded own list with transient, explainable résumé-alignment output / create |
| GET/PATCH/DELETE | `/api/v1/jobs/{id}` | own resource |
| GET/POST | `/api/v1/applications` | bounded own list / create |
| GET/PATCH | `/api/v1/applications/{id}` | own resource |
| POST | `/api/v1/applications/{id}/approve` | validate then set approved |
| POST | `/api/v1/applications/{id}/send` | one reviewed Gmail send |
| GET/PATCH | `/api/v1/providers/yc/preferences` | read/update optional tenant YC query/remote/limit preferences; never fetch, scrape, discover, or enqueue YC provider work |

Job fit is a deterministic relevance aid, not a hiring prediction. It compares the
owned parsed résumé and profile skills/target roles with each owned job's title and
description, returns a bounded score, matched skills, missing detected skills, and an
explanation, and never writes those transient fields into the job row. Groq résumé
analysis fills only blank browser form fields; the user reviews and saves the profile.

### Discovery and import

| Method | Path | Result |
|---|---|---|
| GET/PATCH | `/api/v1/discovery/preferences` | read/update bounded own discovery settings |
| POST | `/api/v1/discovery/referrals` | parse and ingest bounded pasted referral text |
| POST | `/api/v1/discovery/import` | parse/ingest one `.csv` or `.xlsx`, maximum 4 MB |
| POST | `/api/v1/discovery/ats` | detect/ingest supported public application URLs in bounded text |
| POST | `/api/v1/discovery/ats/boards` | enqueue bounded official Greenhouse/Lever/Ashby public-board enumeration |
| POST | `/api/v1/discovery/public-feeds` | enqueue bounded Telegram/RSS discovery |
| POST | `/api/v1/discovery/linkedin` | enqueue bounded unofficial LinkedIn guest discovery |
| POST | `/api/v1/discovery/resume-guided` | use the owned Groq credential with the active parsed résumé to return a bounded plan and enqueue LinkedIn guest plus filtered Telegram/RSS jobs |
| GET | `/api/v1/discovery/google-forms` | paginate the tenant's deduplicated direct/metadata-discovered Google Forms review queue |
| GET | `/api/v1/discovery/sources` | source/provider capabilities and current limits |

Form Pilot Stage 01 sends the full referral message to
`POST /api/v1/discovery/referrals` as `{ "text": "..." }`. `ReferralDigestIngest`
accepts 20–100,000 characters; parsing is synchronous, stateless, tenant-scoped, and
does not call Groq or fetch pasted URLs. The parser splits numbered postings, extracts
Company, Role, Batch, CTC/Stipend/compensation, Location, application URL/email,
subject, and CC fields, and classifies each route as `form`, `email`, `ats`, or `url`.
Recognized WhatsApp/channel, Telegram, Topmate, paid-group, and premium-referral links
or tails are excluded before persistence. The response includes `items`, `count`,
`inserted`, `updated`, and a routing `summary`; no browser job or provider action is
enqueued.

Public ATS board enumeration derives a fixed provider endpoint from an exact hosted
board URL. It uses Greenhouse's unauthenticated Job Board GET API, Lever's public
Postings list API, or Ashby's public Job Postings API; redirects, arbitrary hosts,
unlisted Ashby posts, responses over 4 MB, more than 8 boards, and more than 200 saved
results are rejected or truncated. The durable queue kind is `discover_public_ats`.

The API rejects spreadsheet bodies above 4 MB before parsing, leaving margin below
Vercel's 4.5 MB Function payload limit. XLSX archives also receive expansion-ratio/size,
row, and cell bounds and are read without macro/formula execution. Résumé PDFs follow a
different path: direct browser-to-Supabase Storage upload with the existing 6 MiB cap.
The paste/import responses report inserted, refreshed-duplicate, and rejected counts.
The authenticated `ingest_discovered_jobs(jsonb)` RPC accepts at most 200 normalized
candidates/2 MiB JSON, preserves user-edited job content on normalized-URL conflict,
updates discovery metadata/time, and refuses `ziprecruiter` as a hosted source.

Telegram/RSS, public ATS board, and LinkedIn guest runs return HTTP 202 automation jobs. Public-feed
network fetching uses an allowlisted source catalog, HTTPS redirect validation,
timeouts, response-byte/item limits, and sanitized errors. LinkedIn guest search is
unofficial, page/result bounded, has no account context, and cannot create an
application-submit job.

The résumé-guided request body accepts optional location (120 characters),
`remote_only`, `linkedin_limit` 1–25, `feed_limit` 1–200, and a caller idempotency key.
It requires an owned active résumé with `parse_status=parsed`, reserves one Groq
generation, derives at most 5 roles, 12 keywords, and 20 combined search terms, and
chooses request location → saved discovery location → profile location → `India`.
It returns those fields plus two public automation-job views; child keys append
`:linkedin` and `:feeds`. Only the bounded terms enter the feed payload. The worker
validates them and filters searchable Telegram/RSS title/company/location/description
text before interleaving sources, deduplication, and the global cap. Manual feed runs
without terms remain unfiltered. LinkedIn receives only the first role and retains its
unofficial guest-only behavior.

The Google Forms queue accepts `limit` 1–100 and `offset` 0–1000. It combines owned jobs
whose direct apply URL classifies as `google_forms` with form URLs nested in owned job
metadata, normalizes/deduplicates by URL, emits `job:<job-id>` or a URL-digest-backed
`form:<digest>` stable ID, and links the latest application for direct saved jobs. A GET
has no scan side effect; metadata-only forms must be saved before the user explicitly
requests the existing scan/revision-review workflow.
Referral rows whose route is `google_forms` therefore appear in this queue after the
Stage 01 refresh. Rows with an application email remain normal tenant-owned jobs with
their contact and optional subject metadata, making them selectable in Mass Cold Email;
ingestion itself creates no draft and sends no message.

### Reviewed application forms

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/jobs/{job_id}/application/scan` | create/reuse ATS application; enqueue field-only scan |
| GET | `/api/v1/applications/{application_id}/form-revisions` | list own immutable revisions |
| POST | `/api/v1/application-form-revisions/{revision_id}/suggest` | resolve the owned Groq credential and return fact-grounded answer suggestions; the browser calls this automatically once when an eligible revision loads and retains a retry path |
| POST | `/api/v1/application-form-revisions/{revision_id}/approve` | approve exact latest revision/hash/answers |
| POST | `/api/v1/application-form-revisions/{revision_id}/prefill` | enqueue a fill-only diagnostic/canary job for an already approved revision; never activates Submit and is not the normal Google Forms completion path |
| POST | `/api/v1/application-form-revisions/{revision_id}/submit` | enqueue an idempotent worker submission for an exact latest approved revision whose required-answer preflight is complete; this is the normal Google Forms path after the UI's explicit combined approval action |

The API does not accept an `approved=true` boolean in queue payloads. The approval RPC
compares revision ID, revision number, schema hash, answers JSON, ownership, and “latest
revision” under a row lock and seals those exact answers. Prefill and submit revalidate
the same approved revision and provider before enqueueing; Google Forms submit also
carries a bounded complete required-answer preflight. After sealing, any changed
answer—and any résumé, URL, or detected-schema change—requires a new scan/revision and
approval.

The suggestion endpoint resolves the owned verified Groq credential, omits
protected-trait/security questions, returns only captured question keys, and does not
update the revision. When Form Pilot loads a newly scanned, unsealed revision with a
Groq credential available, it makes one deduplicated automatic request, applies the returned
values as browser-visible draft answers, and leaves all fields editable. Missing keys,
quota/model errors, or ungrounded fields do not block manual completion; the user can
retry. The user must still review and explicitly approve the answers. The Groq key and
suggestion object are never added to an automation-job payload.

For Google Forms, Form Pilot composes exact approval and submit enqueue behind one
explicit **Approve & submit in background** action. The worker rescans the same
allowlisted target, verifies URL/schema/latest approval and complete required answers,
fills only the sealed answers and selected résumé, activates exactly one unambiguous
Submit control, and waits for freshly observed confirmation. Only a result with
`code=application_submitted` and `submission_state=confirmed` is success. Login/MFA/
CAPTCHA, an unknown required field, schema change, ambiguous control, timeout, or
uncertain confirmation returns `needs_attention` without blind retry. That fallback may
carry an allowlisted retained Browserbase Live View URL; successful runs do not require
Live View.

A captured native `input[type=file]` is résumé-capable only when its label or key
explicitly identifies a résumé/CV and its `accept` contract permits PDF. The structural
accept contract is part of the sealed schema hash. The worker resolves the revision's
tenant-owned private résumé through the lease-bound service RPC, downloads it to a
short-lived local directory, attaches exactly one PDF, verifies the input's observable
`FileList` name, MIME type, and byte count against the materialized file, and removes
the temporary file when the run ends. It never exposes a Storage
path or signed URL to the browser UI. Multiple résumé controls, required unrelated file
controls, PDF-excluding inputs, and provider-owned pickers (including a Google picker
that requires a separate account login) fail closed to `needs_attention`; the worker
also fails with `file_upload_inspection_failed` if widget structure cannot be verified,
and does not click or guess a picker destination. Text fields named “Resume Link” remain
separate and receive only the reviewed public HTTPS `profiles.resume_url` value.

### Connections and OAuth

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/connections` | catalog merged with own status |
| GET | `/api/v1/connections/google-oauth-client` | platform/user availability, fixed callback, masked own-client status; never secret material |
| PUT | `/api/v1/connections/google-oauth-client` | validate and encrypt own Web client ID/secret; requires Gmail disconnected |
| DELETE | `/api/v1/connections/google-oauth-client` | delete own saved client and invalidate pending states; requires Gmail disconnected |
| POST | `/api/v1/oauth/google/start` | short-lived authorization URL; optional `credential_source` is `platform` or `user`, default `platform` |
| GET | `/api/v1/oauth/google/callback` | consume current generation, exchange/save, redirect |
| DELETE | `/api/v1/connections/gmail` | serialize disconnect, best-effort revoke, delete local connection |
| POST | `/api/v1/connections/{provider}/browser/start` | allowlisted Live View URL |
| POST | `/api/v1/connections/{provider}/browser/complete` | close login session and mark state unverified/needs attention |
| DELETE | `/api/v1/connections/{provider}` | delete context/metadata |

### Account lifecycle

| Method | Path | Result |
|---|---|---|
| DELETE | `/api/v1/account` | sign-in age ≤10 minutes + confirm email + `DELETE`; clean providers/Storage/Auth user |

Account deletion is retriable. It requires any pending Gmail dispatch to be finalized or
reconciled first, then prevents new private mutations, attempts provider cleanup,
enumerates/deletes all owned résumé objects (including unregistered orphans), and deletes
the Auth user.
Tenant rows cascade. Pseudonymous `provider_send_events` hashes remain only until their
bounded expiry for abuse prevention.

### Durable jobs

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/automation-jobs` | enqueue permitted owned operation; HTTP 202 |
| GET | `/api/v1/automation-jobs` | own bounded history |
| GET | `/api/v1/automation-jobs/{id}` | own status/result |
| POST | `/api/v1/automation-jobs/{id}/cancel` | durable cancellation request |

`enqueue_automation_job` preserves its five-argument RPC contract, copies an owned
`payload.form_revision_id` into the foreign-key column after validation, and rejects a
provider/kind mismatch. Both prefill and submit require the latest exact approved form
revision. ZipRecruiter is rejected even when supplied directly to the RPC.

## 7. Error contract

```json
{
  "error": {
    "code": "resume_invalid_pdf",
    "message": "The uploaded file is not a readable PDF.",
    "request_id": "uuid"
  }
}
```

Provider details are mapped to safe codes. Raw provider responses, keys, tokens, and
message content are never returned in errors.

## 8. Credential and token encryption

`TOKEN_ENCRYPTION_KEY` is an operator-managed 32-byte URL-safe base64 key shared by the
API and every worker. Provider tokens and credentials are encrypted using authenticated
encryption (Fernet in the initial Python implementation). Token/context ciphertext is
stored only in `connection_secrets`; user Web OAuth client-ID/client-secret ciphertext
is stored only in `user_google_oauth_clients`; Groq, Hunter, and Browserbase encrypted
JSON envelopes are stored only in `user_provider_credentials`. All three tables are
service-role-only.

Key rotation is an operational migration: decrypt with current/previous key, encrypt
with new key, verify, then remove previous. Losing all encryption keys makes tokens and
user-managed credentials unrecoverable; affected users must re-save provider/OAuth
credentials and reconnect. Never replace this variable independently on Vercel and the
worker.

## 9. Gmail send algorithm

1. Authenticate user and load owned application/job/resume/Gmail connection.
2. Require `status=approved`, non-empty recipient/subject/body, and active résumé when
   attachment is requested.
3. Atomically reserve `(user_id, idempotency_key)` and enforce daily/duplicate limits.
   The same transaction locks the pseudonymous Gmail-account hash, enforces a hard
   provider-account ceiling of 25 reservations per rolling 24 hours across recreated
   accounts, and inserts a maximum-90-day provider-ledger row.
4. Refresh Google access token if needed with the credential source/generation saved on
   the connection; never fall back to a different client. Preserve the stored refresh
   token when Google omits a replacement.
5. Construct RFC-compliant MIME, attach PDF, base64url encode, and call
   `users.messages.send`.
6. Persist provider IDs and terminal `sent` status.
7. On an ambiguous timeout, mark `needs_attention` and reconcile; do not blindly resend.

### 9.1 Reviewed outreach client orchestration

The browser provides convenience orchestration without creating a bulk-send API:

1. Build an in-memory selection of at most 10 owned, non-archived jobs.
2. Require a parsed active résumé, verified account-scoped Groq/Hunter credentials, and a connected
   Gmail account. Show projected Hunter credit use inline before the user explicitly
   starts the search; no additional confirmation dialog is required for that lookup.
3. For each selected job, call the owned Hunter contact endpoint sequentially with
   `limit=5`; stop visibly on quota exhaustion. Let the user choose one returned
   contact and persist only that chosen email on the job.
4. For each chosen contact, call the existing Groq draft endpoint sequentially. The
   resulting application remains editable and unapproved.
5. Open the second **Review & send** subtab inside the **Mass Cold Email** destination.
   The sidebar exposes no separate review item. Editing content invalidates prior
   approval; each exact email application revision must be individually approved
   through the existing API. Filter both subtabs to email-channel applications. Form
   Pilot keeps ATS/form revisions, answer controls, approval-bound submit progress,
   verified confirmation, and any needs-attention Live View fallback inside its own
   destination.
6. Gather at most 10 selected applications that are currently approved and have a
   recipient, display an irreversible-action confirmation listing the targets, then
   call `POST /api/v1/applications/{id}/send` sequentially with a unique idempotency key
   and `attach_resume=true`.
7. Report each result and stop safely on daily/provider cap or Gmail reauthorization
   errors. The API remains authoritative for ownership, approval, recipient duplicate,
   daily/provider quota, and idempotency enforcement on every call.

Selections and Hunter candidates are browser memory only. There is no autonomous send
scheduler, batch provider call, inherited approval, or unreviewed bulk cold-email path.

## 10. Provider catalog

```json
{
  "id": "linkedin",
  "label": "LinkedIn",
  "mode": "partner_required",
  "available": false,
  "can_connect": false,
  "can_auto_apply": false,
  "reason": "LinkedIn does not provide a self-serve candidate apply API."
}
```

Catalog fields are stable so the UI never infers support from missing environment
variables. `ALLOWED_BROWSER_PROVIDERS` can enable only registry entries declared
`managed_browser`; it cannot override `partner_required` entries.

Hosted catalog matrix:

| Provider | Discovery | Application mode |
|---|---|---|
| LinkedIn | Bounded unofficial guest jobs | `partner_required` Easy Apply; manual handoff only |
| Google Forms | URL/referral detection | one-page scan → browser-auto-suggest → explicit exact-approved background submit → verified confirmation; Live View only for `needs_attention`; multi-page/branching forms unsupported |
| Greenhouse | individual URL detection plus bounded official public-board enumeration | aligned `managed_browser`; controlled staging still required |
| Lever / Ashby | individual URL detection plus bounded official public-board enumeration | read-only live scan passed; controlled submit canaries still required |
| Wellfound | URL/import detection | safe mapping pending a signed-in controlled canary |
| YC | Exact user-saved current public job URL only; no YC search/scraping/discovery | finished tenant-isolated Browserbase BYOK scan → résumé/Groq-grounded immutable review → sealed single submit → fresh-confirmation verification; operator-allowlist gated until signed-in canary |
| Generic company form | No public catalog/discovery entry | exact-host adapter exists only as a gated controlled-canary path; it is not enabled public functionality |
| Cutshort / Instahyre | URL/import detection | connection-only; application jobs fail safely pending tenant-aware multi-step state machines |
| Gmail | None | official OAuth reviewed send |

ZipRecruiter has no hosted catalog entry, URL classification, lifecycle, accepted queue
provider, or worker adapter. Generic unsupported URLs remain manual-only.

## 11. Worker claim protocol

`python -m worker.main` must run continuously on a persistent process host outside
Vercel. A Vercel Function invocation cannot host or replace this queue poller.

The migration defines a `claim_automation_job(worker_id, lease_seconds, kinds)` RPC
using `FOR UPDATE SKIP LOCKED`. It requeues expired leases, selects one due queued job,
sets `running`, increments attempts, and returns the row. Completion/failure updates
match both job ID and lease owner. A separate tenant-scoped
`cancel_automation_job(job_id)` RPC cancels queued work immediately or signals the
current lease holder to stop cooperatively.

Worker loop:

```python
while running:
    job = await store.claim_job(worker_id)
    if not job:
        wait_with_jitter()
        continue
    handler = registry.get(job.kind)
    result = await handler.execute(job)
    # exact leased transition; ambiguous submit never reports success
    await store.complete(job.id, worker_id, redact(result))
```

Browser handlers use Playwright over Browserbase CDP. Before creating a session, the
worker resolves the claimed tenant's verified Browserbase BYOK pair; only when none is
configured may it use the optional platform fallback pair. It never mixes an API key
from one source with a Project ID from another. `get_application_job_bundle` returns
only the tenant/application/job/revision/résumé/context bundle associated with the
active lease. Every browser session has a 90-second stall cap and is closed as soon as
work completes or fails. This reduces minutes for stalled runs, but Browserbase bills a
minimum of one minute for every created session, so a successful sub-minute run still
uses one browser minute. `store_application_form_scan` appends a revision;
`update_application_job_progress` persists redacted progress; and
`record_application_form_submission` commits applied/submitted state only after an
exact approved revision receives provider confirmation. These RPCs are service-role
only and reject an expired lease or mismatched worker ID.

The provider layer has one host/redirect-policy adapter per registry provider. Public
forms may use an ephemeral Browserbase session; login-gated adapters reuse the encrypted
context bound to the tenant/provider. Google Forms is deliberately both public and
optionally connectable: the stored context is reused when present, but
`connection_required=false` remains the catalog contract for ordinary public forms.
Only enabled application handlers scan fields,
fill approved answers/résumé, and recheck URL, schema, approval, and required answers
before a provider action. For Google Forms, the normal `application_submit` handler
activates exactly one unambiguous Submit control and succeeds only after fresh provider
confirmation (`application_submitted`/`confirmed`). YC and the dynamically
exact-host-bound `company_form` adapter have different launch states. YC is a finished
exact-job state machine but remains operator-allowlist gated until its signed-in canary;
the generic adapter remains an internal controlled-canary code path.

The YC handler accepts only one exact current public job-detail URL already saved by the
tenant. It rejects search, listing, account, generic application, and unsupported legacy
targets before creating a Browserbase session. The API, migration, and worker also
reserve every `ycombinator.com` and `workatastartup.com` root/subdomain from the generic
company-form adapter. Login occurs inside that tenant's
isolated persistent BYOK context. Playwright in this separate continuously running
worker opens the bound job, scans only visible job fields, and applies only an approved
immutable revision grounded in the owned profile/résumé and reviewable Groq
suggestions. It activates one unique submit control once and requires a fresh YC
confirmation. Query, remote, and limit preferences are storage/display/matching values
only and never cause a YC request, crawl, discovery job, or bulk application. Vercel
does not launch Chromium, connect to Browserbase CDP, or run this worker.
Connection-only or unsupported multi-step paths return `needs_attention` without
attempting an application. CAPTCHA, MFA, login/security challenge, an unknown required
field, changed schema, and uncertain confirmation do the same and may retain Live View
only for user attention. No ambiguous submit is blindly retried. Mocked adapter tests are not evidence
that live provider markup or terms are unchanged; account BYOK or operator-fallback
Browserbase credentials and controlled test accounts/jobs are required before adding a provider to the production
allowlist.

## 12. Frontend storage contract

Application-owned browser storage contains only non-secret interface preferences such
as `autoapply.ui_preferences.v1`; Supabase Auth manages its own namespaced session
values. Groq, Hunter, and Browserbase keys/project IDs must not be written to
`localStorage`, `sessionStorage`, IndexedDB, cookies, URLs, DOM HTML, or analytics.
Credential inputs use `type=password` where appropriate, are posted once over HTTPS to
the authenticated provider-credential endpoint, and are cleared immediately after the
request. Subsequent views use only safe status/hints from
`GET /api/v1/provider-credentials`. Outreach selection/contact candidates remain
in-memory UI state and reset with the authenticated workspace state.

The sole transition exception is a one-time authenticated import of namespaced legacy
Groq/Hunter values created by the previous release. The client submits each value to
the normal validated PUT endpoint and deletes the legacy copy only after successful
encrypted save (or when that provider is already configured). A failed import retains
the copy and shows a retry warning; it must never fall back to a key header. New saves
never write provider credentials to browser storage.

All imported/provider text is rendered through DOM `textContent`, not HTML templates,
to prevent stored XSS.

## 13. Vercel configuration

- Python entrypoint: `app.saas_main:app`.
- Runtime: Python 3.12.
- Static files live in `public/**` for CDN delivery.
- CSP permits Turnstile only from `https://challenges.cloudflare.com`; custom Supabase
  domains/origins must be reflected exactly in production policy where required.
- API bundle excludes tests, local browser data, PDFs, logs, databases, generated
  output, legacy screenshots, and backups.
- No startup scheduler or database migration runs in Function lifespan.
- Deployment region should be close to the Supabase project.

## 14. Required test groups

- Pure reuse regression tests.
- API auth/error/schema tests with injected fake Auth/Store/Providers.
- Cross-tenant API and Supabase RLS integration tests.
- OAuth state, token encryption, refresh preservation, revoke/disconnect tests.
- Platform-default and user-managed-client status/save/delete tests, including Web
  client validation, API-key rejection, encrypted/server-only persistence, secret
  redaction, fixed redirect, and disconnect-before-replace/delete behavior.
- Resume path/MIME/size/PDF tests.
- Five-slot Storage quota and concurrent transactional registration tests.
- Provider-credential API/migration tests covering Groq/Hunter/Browserbase validation,
  authenticated ownership, versioned encryption, service-role-only persistence, safe
  hints/status, replacement generation, deletion guards, and account cascade.
- Frontend tests proving new Groq, Hunter, and Browserbase secrets never enter browser
  storage, credential inputs are cleared after save, and legacy Groq/Hunter import
  removes a namespaced browser copy only after successful encrypted persistence while
  retaining it with a warning on failure.
- Hunter adapter/API tests covering stored-key secrecy, account-quota allowlisting,
  HR-only 1–10 contact bounds, safe provider failures, and tenant ownership.
- Send approval/idempotency/tenant and persistent provider-ledger quota tests.
- OAuth-generation callback/reconnect/disconnect race and revocation-failure tests.
- OAuth credential-source/generation binding tests proving stale callbacks and refreshes
  cannot switch between platform and user-managed clients.
- Queue claim/lease/retry/cancellation concurrency tests.
- Discovery normalization, Telegram/RSS SSRF/redirect/byte/item bounds, referral text,
  CSV/XLSX malformed/row/cell bounds, and tenant idempotency tests.
- Résumé-guided API/privacy/failure tests, public-feed search-term filtering, and
  tenant-scoped Google Forms queue deduplication/application-link/pagination tests.
- LinkedIn guest page/result/throttle bounds and explicit Easy Apply exclusion tests.
- Frontend max-10 selection and staging checks for inline projected Hunter credit use,
  an explicit search click, contact choice, per-application exact approval, final
  confirmation, and sequential reuse of the gated Gmail single-send endpoint.
- Exact immutable form-revision, stale hash/answers, supersession, ownership, and
  service-RPC lease-binding tests.
- Google Forms scan/suggestion/exact-submit tests, including automatic account-scoped Groq
  request deduplication, deterministic graduation/public-résumé mapping, latest approved
  revision and required-answer gates, single submit activation, fresh confirmation,
  idempotency, and needs-attention-only Live View fallback. Provider-specific
  worker-submit tests apply only where that capability is separately enabled. Registry,
  host-allowlist, connection-only,
  CAPTCHA/MFA, unknown-field, and ambiguous-confirmation paths fail closed across the
  eight registered providers.
- Hosted-provider-registry tests proving ZipRecruiter is absent everywhere.
- Vercel import smoke test asserting no local-singleton imports.
- Account-deletion tests for stale-session rejection and recent-sign-in success.

These unit/static tests do not replace the launch gate: apply the real migration to a
separate staging Supabase project and exercise RLS, private Storage, OAuth lifecycle,
send reservations, account deletion, and queue/worker concurrency before production.
For Gmail preview, use named Google test users and verify the disclosed seven-day
expiration for a Testing app requesting `gmail.send`. Public use of the operator client
also requires publication plus brand/domain and sensitive-scope verification; users who
publish their own OAuth project own the equivalent Google obligations.
Browser-provider launch additionally requires live staging validation with a real
account-scoped Browserbase BYOK pair and the optional platform fallback (if configured),
controlled provider accounts/jobs, and current provider terms. Validation must prove
that saving the pair uses only the read-only project endpoint and creates no session;
worker tests must prove BYOK priority, same-source key/project pairing, immediate close,
and the 90-second stall cap. Mocked tests cannot satisfy that gate.
YC remains operator-allowlist gated until a signed-in exact-job canary proves URL
rejection, tenant context isolation, visible-field scan, immutable grounded review,
single sealed submit, fresh confirmation, and fail-closed outcomes. The exact-host
generic company-form adapter remains an internal gated canary. Implementation code
alone is not enablement evidence.
