# Technical Specification

## AutoApply Cloud 2.0

**Status:** Implementation baseline
**Date:** 2026-08-11

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
│       ├── crypto.py                # provider-token encryption
│       ├── groq.py                  # transient BYOK proxy
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

# Managed-browser application features (trusted API/worker only)
BROWSERBASE_API_KEY=...
BROWSERBASE_PROJECT_ID=...
ALLOWED_BROWSER_PROVIDERS=google_forms,greenhouse
WORKER_ID=worker-1
WORKER_POLL_SECONDS=2
WORKER_LEASE_SECONDS=120
```

The Supabase secret key, encryption key, platform Google client secret, user-managed
Google client credentials, provider tokens, and browser connection URLs must never be
returned by `/config`, embedded in JavaScript, stored in browser storage, or logged.

The API and worker share `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, and
`TOKEN_ENCRYPTION_KEY`; the worker uses the latter to decrypt only the context bound to
its claimed tenant job. Browserbase variables are required to create/reuse Live View
contexts. An empty `ALLOWED_BROWSER_PROVIDERS` disables all browser jobs; the exact
initial allowlist above enables only the aligned one-page Google Forms and Greenhouse
handlers and excludes ZipRecruiter. Add Lever, Ashby, or Wellfound individually only
after its controlled canary passes. Leave YC, Cutshort, and Instahyre out until their
tenant-aware multi-step state machines are implemented and validated.

`GOOGLE_REDIRECT_URI` is the one fixed Gmail OAuth callback and is required for either
credential path. `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are optional as a pair:
when present they provide the default platform-managed Gmail OAuth client. Without
them, Gmail connection is still available to an authenticated user who saves an
advanced user-managed Web OAuth client, provided the server secret store and Fernet key
are configured. A standard Google Cloud API key is not an OAuth client and is rejected.
These settings do not authorize Google Forms. Users authenticate to Google Forms or
job boards themselves inside Browserbase Live View. A user's Groq key remains
browser-only and is not a worker environment variable.

## 3. Database schema

The canonical executable schema is the versioned Supabase migration. Logical model:

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
| POST | `/api/v1/resumes/{id}/analyze` | use the transient user Groq key to return allowlisted profile/role suggestions without mutating the profile |
| DELETE | `/api/v1/resumes/{id}` | delete owned résumé row/object, retain application history |

### Groq

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/groq/validate` | validate transient header key |
| POST | `/api/v1/jobs/{id}/draft` | create/update an owned draft |

`X-Groq-Api-Key` is required and marked sensitive. Neither endpoint returns it.

### Jobs/applications

| Method | Path | Result |
|---|---|---|
| GET/POST | `/api/v1/jobs` | bounded own list with transient, explainable résumé-alignment output / create |
| GET/PATCH/DELETE | `/api/v1/jobs/{id}` | own resource |
| GET/POST | `/api/v1/applications` | bounded own list / create |
| GET/PATCH | `/api/v1/applications/{id}` | own resource |
| POST | `/api/v1/applications/{id}/approve` | validate then set approved |
| POST | `/api/v1/applications/{id}/send` | one reviewed Gmail send |

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
| GET | `/api/v1/discovery/sources` | source/provider capabilities and current limits |

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

### Reviewed application forms

| Method | Path | Result |
|---|---|---|
| POST | `/api/v1/jobs/{job_id}/application/scan` | create/reuse ATS application; enqueue field-only scan |
| GET | `/api/v1/applications/{application_id}/form-revisions` | list own immutable revisions |
| POST | `/api/v1/application-form-revisions/{revision_id}/suggest` | return transient, fact-grounded Groq answer suggestions for review |
| POST | `/api/v1/application-form-revisions/{revision_id}/approve` | approve exact latest revision/hash/answers |
| POST | `/api/v1/application-form-revisions/{revision_id}/prefill` | enqueue reviewed prefill; never submit |
| POST | `/api/v1/application-form-revisions/{revision_id}/submit` | separately enqueue exact approved submission |

The API does not accept an `approved=true` boolean in queue payloads. The approval RPC
compares revision ID, revision number, schema hash, answers JSON, ownership, and “latest
revision” under a row lock and seals those exact answers. Prefill/submit revalidate the
same approved revision and provider before enqueueing. After sealing, any changed
answer—and any résumé, URL, or detected-schema change—requires a new scan/revision and
approval.

The suggestion endpoint accepts the browser-held Groq key only in the request header,
omits protected-trait/security questions, returns only captured question keys, and does
not update the revision. The user must still review and explicitly approve the answers.

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

## 8. Gmail OAuth credential and token encryption

`TOKEN_ENCRYPTION_KEY` is an operator-managed 32-byte URL-safe base64 key. Provider
tokens are encrypted using authenticated encryption (Fernet in the initial Python
implementation). Token ciphertext is stored only in `connection_secrets`; user Web
OAuth client-ID and client-secret ciphertext is stored only in
`user_google_oauth_clients`. Both tables are service-role-only.

Key rotation is an operational migration: decrypt with current/previous key, encrypt
with new key, verify, then remove previous. Losing all encryption keys makes tokens and
user-managed client credentials unrecoverable; affected users must re-save their client
and reconnect.

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
| Google Forms | URL/referral detection | aligned `managed_browser` for one-page forms only; multi-page/branching forms unsupported |
| Greenhouse | individual URL detection plus bounded official public-board enumeration | aligned `managed_browser`; controlled staging still required |
| Lever / Ashby | individual URL detection plus bounded official public-board enumeration | read-only live scan passed; controlled prefill/submit canaries still required |
| Wellfound | URL/import detection | safe mapping pending a signed-in controlled canary |
| YC / Cutshort / Instahyre | URL/import detection | connection-only; application jobs fail safely pending tenant-aware multi-step state machines |
| Gmail | None | official OAuth reviewed send |

ZipRecruiter has no hosted catalog entry, URL classification, lifecycle, accepted queue
provider, or worker adapter. Generic unsupported URLs remain manual-only.

## 11. Worker claim protocol

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

Browser handlers use Playwright over Browserbase CDP. `get_application_job_bundle`
returns only the tenant/application/job/revision/résumé/context bundle associated with
the active lease. `store_application_form_scan` appends a revision;
`update_application_job_progress` persists redacted progress; and
`record_application_form_submission` commits applied/submitted state only after an
exact approved revision receives provider confirmation. These RPCs are service-role
only and reject an expired lease or mismatched worker ID.

The provider layer has one host/redirect-policy adapter per registry provider. Public
forms may use an ephemeral Browserbase session; login-gated adapters reuse the encrypted
context bound to the tenant/provider. Only enabled application handlers scan fields,
prefill approved answers/résumé, and recheck schema/approval before one submit action.
Connection-only or unsupported multi-step paths return `needs_attention` without
attempting an application. CAPTCHA, MFA, login/security challenge, an unknown required
field, and uncertain confirmation do the same. Mocked adapter tests are not evidence
that live provider markup or terms are unchanged; operator Browserbase credentials and
controlled test accounts/jobs are required before adding a provider to the production
allowlist.

## 12. Frontend storage contract

Keys:

```text
autoapply.groq_api_key.v2.<auth-user-uuid>
autoapply.ui_preferences.v1
```

The Groq key input uses `type=password`, is never inserted into DOM HTML, is not copied
to query strings, and is deleted on the user's command. Sign-out intentionally leaves
the user-namespaced key in that browser. Explicit key removal, successful account
deletion, site-data clearing, or browser-profile deletion removes it. A different
signed-in user cannot read it through the application. Supabase Auth manages its own
namespaced session values.

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
- Groq transient-key redaction tests.
- Groq sign-out/account-deletion browser-storage lifecycle tests.
- Send approval/idempotency/tenant and persistent provider-ledger quota tests.
- OAuth-generation callback/reconnect/disconnect race and revocation-failure tests.
- OAuth credential-source/generation binding tests proving stale callbacks and refreshes
  cannot switch between platform and user-managed clients.
- Queue claim/lease/retry/cancellation concurrency tests.
- Discovery normalization, Telegram/RSS SSRF/redirect/byte/item bounds, referral text,
  CSV/XLSX malformed/row/cell bounds, and tenant idempotency tests.
- LinkedIn guest page/result/throttle bounds and explicit Easy Apply exclusion tests.
- Exact immutable form-revision, stale hash/answers, supersession, ownership, and
  service-RPC lease-binding tests.
- Separate scan/prefill/submit tests for implemented Browserbase handlers, plus registry,
  host-allowlist, connection-only, CAPTCHA/MFA, unknown-field, and ambiguous-confirmation
  fail-closed tests across the eight registered providers.
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
Browser-provider launch additionally requires live staging validation with real
Browserbase credentials, controlled provider accounts/jobs, and current provider terms;
mocked tests cannot satisfy that gate.
