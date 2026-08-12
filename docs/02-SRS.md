# Software Requirements Specification

## AutoApply Cloud 2.0

**Status:** Implementation baseline
**Date:** 2026-08-11

## 1. System boundary

AutoApply Cloud consists of:

1. a static browser application served by Vercel;
2. a stateless FastAPI API deployed as a Vercel Function;
3. Supabase Auth, Postgres, and private Storage;
4. Google OAuth and Gmail API integration;
5. credential-free discovery/import adapters;
6. a durable job table and separately deployable Python worker; and
7. optional managed-browser contexts for explicitly permitted providers.

The legacy local application remains a development/reference implementation and is not
loaded by the Vercel entrypoint.

## 2. Actors

- **Anonymous visitor:** may view the landing page, product configuration, health, terms,
  and privacy information.
- **Authenticated user:** owns a profile, résumés, connections, jobs, applications,
  drafts, answer bank, audit history, and queued work.
- **Worker:** trusted service authenticated with the Supabase secret key; may claim jobs
  but must bind every operation to the claimed row's stored `user_id`.
- **Operator:** configures Vercel/Supabase/Google/Browserbase and reviews operational
  telemetry. Operator actions are out of the public API unless explicitly documented.

## 3. Functional requirements

### AUTH — product authentication

- **AUTH-01:** Supabase Auth shall support email/password sign-up and sign-in.
- **AUTH-02:** All private API endpoints shall require `Authorization: Bearer <JWT>`.
- **AUTH-03:** The API shall derive identity only from a verified Supabase token.
- **AUTH-04:** A request-supplied `user_id` shall never select authorization scope.
- **AUTH-05:** Expired/invalid tokens shall return HTTP 401; ownership failures shall
  return 404 or 403 without leaking another tenant's record.
- **AUTH-06:** Authenticated responses shall use `Cache-Control: private, no-store`.
- **AUTH-07:** Sign-out shall clear the browser session; account deletion shall remove
  tenant rows and private objects and attempt to revoke supported provider tokens.
- **AUTH-08:** When CAPTCHA is configured, sign-in, sign-up, and password reset shall
  require a fresh Cloudflare Turnstile token passed to Supabase Auth.
- **AUTH-09:** The public configuration shall expose only the Turnstile site key. The
  matching secret shall exist only in Supabase Auth configuration.

### PROF — profile and onboarding

- **PROF-01:** A new auth user shall receive an empty profile and default user settings.
- **PROF-02:** Users shall read/update only their own typed profile fields.
- **PROF-03:** The API shall return onboarding completion/missing-field state.
- **PROF-04:** Hard-coded owner-specific geography, education, work authorization, and
  experience assumptions shall not be used in generated answers.

### RES — résumé

- **RES-01:** The browser shall upload PDFs directly to the private `resumes` bucket.
- **RES-02:** Object paths shall be exactly `<auth-user-uuid>/resume-N.pdf`, where `N`
  is an integer from 1 through 5; no nested path or sixth slot shall be accepted.
- **RES-03:** Launch uploads shall accept only PDF MIME/type and at most 6 MB.
- **RES-04:** Registration shall transactionally verify the owned Storage object,
  serialize concurrent registration, update its résumé metadata, deactivate every
  other active résumé, and activate exactly the selected slot.
- **RES-05:** Parsing shall read the owned Storage object, extract text with `pypdf`, and
  return suggested profile fields without silently overwriting non-blank user values.
- **RES-06:** Replacing/deleting a résumé shall not expose another user's object.
- **RES-07:** Deleting a résumé shall remove that Storage object and résumé metadata but
  shall not delete or rewrite existing job, application, draft, send, or audit history.

### AI — browser-persistent Groq key and drafting

- **AI-01:** The frontend shall save the user's Groq key in origin-scoped local storage.
- **AI-02:** The key shall be masked after entry and removable by the user.
- **AI-03:** Authenticated AI requests shall send it as `X-Groq-Api-Key` over HTTPS.
- **AI-04:** The backend shall not store or log the key and shall reject missing or
  malformed keys without echoing them.
- **AI-05:** Key validation shall call a minimal Groq endpoint and return only valid/
  invalid/provider status.
- **AI-06:** Draft generation shall use the configured production model, the user's
  profile/résumé, and the selected JD; it shall not invent unsupported experience.
- **AI-07:** A generated draft shall remain editable and unsent until explicitly
  approved.
- **AI-08:** Background jobs shall never assume access to a browser-only Groq key.
- **AI-09:** Groq local-storage keys shall be namespaced by authenticated user ID.
  Sign-out shall preserve the signed-out user's key; explicit removal and successful
  account deletion shall remove it. Clearing browser site data also removes it.

### JOB — jobs and applications

- **JOB-01:** Users shall create, list, retrieve, update, and archive their jobs.
- **JOB-02:** Jobs shall support source, external ID, URL, title, company, location, JD,
  contact email, and arbitrary non-secret source metadata.
- **JOB-03:** `(user_id, normalized_url)` shall prevent duplicate job ingestion when a
  normalized URL is present.
- **JOB-04:** Each application shall belong to one user and optionally one job.
- **JOB-05:** Status transitions shall be validated and timestamped.
- **JOB-06:** Every ID lookup/update/delete shall include ownership scope.
- **JOB-07:** Lists shall be bounded and support cursor/limit parameters.

### DISC — credential-free discovery and import

- **DISC-01:** Authenticated users shall ingest Telegram public-channel previews, RSS,
  pasted referral digests, CSV files, and XLSX files without third-party credentials.
- **DISC-02:** Telegram/RSS network requests shall use explicit scheme/host validation,
  redirect revalidation, connect/read timeouts, byte limits, item limits, and a bounded
  source catalog; discovered URLs shall not become arbitrary server-side fetch targets.
- **DISC-03:** Pasted digest content and imported cells shall be treated as untrusted text,
  normalized into typed job candidates, and rendered only as text in the browser.
- **DISC-04:** The hosted CSV/XLSX endpoint and UI shall enforce a 4 MB upload cap plus
  row-count, cell-length, and XLSX expansion limits, reject unsupported formats, and map
  documented flexible headings without macro or formula execution. Résumé PDFs shall
  continue to bypass Vercel payloads through direct Supabase Storage upload.
- **DISC-05:** Public ATS detection shall classify known job/application URLs as
  `google_forms`, `greenhouse`, `lever`, `ashby`, `yc`, `wellfound`, `cutshort`, or
  `instahyre`; it shall not claim exhaustive internet search.
- **DISC-05A:** Public ATS board discovery shall accept at most eight exact hosted
  Greenhouse, Lever, or Ashby URLs, derive fixed official GET endpoints without
  redirects, omit unlisted posts, and save at most 200 normalized jobs per run.
- **DISC-06:** LinkedIn guest-job discovery shall use no login/account cookie, remain
  unofficial, be throttled and bounded by pages/results, and stop on a block, challenge,
  or rate-limit response.
- **DISC-07:** LinkedIn guest discovery shall create reviewable job records only and
  shall never open, fill, or submit LinkedIn Easy Apply.
- **DISC-08:** Network discovery shall run as durable jobs with kinds
  `discover_public_feeds`, `discover_linkedin_guest`, and `discover_public_ats`; pasted
  and file ingestion may run synchronously only within documented request limits.
- **DISC-09:** Every accepted candidate shall be tenant-owned and idempotently
  deduplicated by normalized URL when present. One tenant's discovery preferences,
  source cursors, or results shall never affect another tenant.

### MAIL — Gmail connection and reviewed sending

- **MAIL-01:** Connect Gmail shall use Google's web Authorization Code flow.
- **MAIL-02:** The authorization request shall include random state, offline access, and
  only launch scopes: `openid email profile gmail.send`.
- **MAIL-03:** OAuth state shall be single-use, expire quickly, and bind to the initiating
  authenticated user.
- **MAIL-04:** Google tokens shall be encrypted before server-side persistence.
- **MAIL-05:** A refresh response that omits `refresh_token` shall preserve the existing
  encrypted refresh token.
- **MAIL-06:** Connection status shall expose provider/account/scopes/expiry, never token
  material.
- **MAIL-07:** Disconnect shall revoke when possible and permanently delete token rows.
- **MAIL-08:** Only an explicitly approved application with recipient, subject, and body
  may be sent.
- **MAIL-09:** A send idempotency key shall make retries at-most-once at the product
  layer.
- **MAIL-10:** The active user's PDF shall be attached when configured.
- **MAIL-11:** Per-user daily cap, duplicate-recipient window, and audit event shall be
  enforced transactionally.
- **MAIL-12:** Public launch shall not expose inbox/reply reading routes.
- **MAIL-13:** OAuth start shall advance a per-user generation. Only the current
  generation may save a callback, and starting disconnect shall invalidate pending
  states before any external revocation request.
- **MAIL-14:** Reconnect/callback/disconnect changes shall be serialized. A repeated
  disconnect shall safely continue the current generation rather than race a callback.
- **MAIL-15:** Google revocation is best-effort. Local token deletion shall complete
  even if Google cannot confirm revocation, and the UI shall instruct the user to
  remove access from Google Account settings.
- **MAIL-16:** A server-only abuse ledger shall retain domain-separated SHA-256 hashes
  of the Google provider subject and recipient, a random send-event ID, outcome, and
  expiry—never a user ID, plaintext address, message, or token—to prevent account
  recreation from resetting provider caps and duplicate windows.
- **MAIL-17:** Provider-ledger rows shall expire no later than 90 days after creation,
  may outlive account deletion until expiry, and shall be inaccessible to browser roles.
  Limit queries shall ignore expired rows immediately; an hourly service-only database
  job and the reservation hot path shall perform physical cleanup. Cron failures shall
  be monitored because they can delay physical deletion beyond logical expiry.
- **MAIL-18:** When an operator-managed Google OAuth Web client is configured, it shall
  be the default credential source. The product may also offer an explicitly selected
  advanced user-managed Web OAuth client; a Google Cloud API key shall not satisfy this
  requirement.
- **MAIL-19:** Both credential sources shall use the single fixed
  `GOOGLE_REDIRECT_URI`. No request shall supply a redirect URI, requested scope,
  authorization endpoint, or token endpoint.
- **MAIL-20:** A user-managed client ID and secret shall be encrypted before persistence
  in a service-role-only table with RLS and no browser grants. Status may report that a
  client exists and masked identification, but no saved secret or ciphertext shall be
  returned to the browser, logged, or placed in browser storage.
- **MAIL-21:** OAuth state shall bind the exact credential source and the current
  user-client generation. Callback exchange and later refresh shall use only that bound
  client; missing, corrupt, deleted, or stale credentials shall fail closed and require
  a new connection.
- **MAIL-22:** Saving, replacing, or deleting a user-managed client shall share the
  Gmail lifecycle lock, invalidate pending OAuth states, and fail while a Gmail
  connection or provider send still depends on it. The user shall disconnect Gmail
  before replacement or deletion.
- **MAIL-23:** Setup guidance shall link to Google Cloud project, Gmail API, consent, and
  Web-client pages and display the exact callback to copy. It shall disclose that an
  External app in Testing must list test users and that a `gmail.send` grant/refresh
  token expires seven days after consent.
- **MAIL-24:** Public production guidance shall distinguish preview testing from
  publication and Google's brand/domain and sensitive-scope verification expectations
  for the owner of each OAuth project.

### CONN — provider connection center

- **CONN-01:** The API shall return a catalog with mode, status, availability, required
  configuration, and plain-language limitations for each provider.
- **CONN-02:** `linkedin` shall report `partner_required` for hosted apply automation.
- **CONN-03:** LinkedIn profile/manual handoff may be used without storing LinkedIn
  credentials or cookies.
- **CONN-04:** A managed-browser login may start only for an operator allowlisted
  provider and when worker/browser configuration is complete.
- **CONN-05:** Managed browser contexts shall be isolated by `(user_id, provider)` and
  protected by a one-active-job lock.
- **CONN-06:** Login/MFA input occurs inside the managed provider's Live View, not an
  AutoApply password form.
- **CONN-07:** Users shall be able to delete a remote context and mark the connection
  disconnected.
- **CONN-08:** The only hosted managed-browser provider IDs shall be `google_forms`,
  `greenhouse`, `lever`, `ashby`, `yc`, `wellfound`, `cutshort`, and `instahyre`.
- **CONN-09:** ZipRecruiter shall not appear in the hosted provider catalog, connection
  controls, accepted automation payloads, or worker handler registry.
- **CONN-10:** A provider adapter shall remain unavailable until Browserbase is
  configured, the operator allowlists it, and its current flow is validated with a
  controlled test account/job.
- **CONN-11:** Connection availability shall not imply application capability. The
  catalog shall report Greenhouse and one-page Google Forms as the initial aligned
  handlers; Lever and Ashby as read-only-scan validated but pending controlled
  prefill/submit canaries, Wellfound as pending a signed-in canary; and YC,
  Cutshort, and Instahyre as connection-only until tenant-aware multi-step state
  machines exist. Multi-page or branching Google Forms shall remain unavailable.

### FORM — immutable application-form review

- **FORM-01:** `application_scan` shall inspect one owned job/application URL and persist
  a new form revision containing the detected provider, target URL, exact field schema,
  proposed answer values, selected résumé ID, and deterministic content/schema hashes.
- **FORM-02:** Form ownership, provider/URL, résumé, detected schema, and revision number
  are immutable. The first approval may atomically replace proposed answers with the
  exact reviewed answer object and then seal it; every subsequent answer change, résumé
  or URL change, or newly observed schema shall create a new revision.
- **FORM-03:** Approval shall name an exact owned revision and its expected hashes. A
  stale or mismatched revision/hash shall fail closed without queueing browser work.
- **FORM-04:** Before approval, users may request Groq answer suggestions using their
  transient browser-held key. Suggestions shall use only the owned profile, linked
  résumé, job, and captured non-sensitive questions and shall never persist or approve
  themselves.
- **FORM-04:** Approval shall not itself fill or submit a form.
- **FORM-05:** `application_prefill` shall fill only values and résumé bytes bound to the
  approved revision, then stop without activating a provider submit control.
- **FORM-06:** `application_submit` shall be a separate explicit user request and shall
  revalidate ownership, approval, revision/hash equality, URL/provider, and current
  form schema before interacting with submit.
- **FORM-07:** A required unanswered/sensitive/unknown field, changed schema, expired
  login, CAPTCHA, MFA, security checkpoint, or ambiguous submission result shall stop
  as `needs_attention`; the worker shall never guess, bypass, or report assumed success.
- **FORM-08:** A successful submit shall require provider-page confirmation evidence and
  an idempotent terminal transition. A timeout after submit shall remain
  `needs_attention` until reconciled and shall not be blindly retried.
- **FORM-09:** Repeating the identical approval shall be idempotent; attempting to alter
  answers or hashes after approval shall fail and require a new revision.

### RUN — durable execution

- **RUN-01:** Long work shall create a durable job and return HTTP 202 with a UUID.
- **RUN-02:** Job states are `queued`, `running`, `succeeded`, `failed`, `cancelled`, or
  `needs_attention`.
- **RUN-03:** Claims shall be atomic and use a lease/heartbeat.
- **RUN-04:** At-least-once deliveries shall be safe through idempotency constraints.
- **RUN-05:** Queue payloads shall contain identifiers and redacted parameters only.
- **RUN-06:** The user shall see progress, result, error summary, attempts, and timestamps.
- **RUN-07:** Cancellation is cooperative and durable, not a process-global flag.
- **RUN-08:** Worker crashes shall leave jobs recoverable after lease expiry.
- **RUN-09:** Accepted job kinds shall include `discover_public_feeds`,
  `discover_linkedin_guest`, `discover_public_ats`, `application_scan`,
  `application_prefill`, and `application_submit` in addition to existing non-browser
  control jobs.
- **RUN-10:** Application-form jobs shall reference an owned immutable form revision;
  queue payloads shall not be accepted as an alternate source of answers or approval.

### OPS — public operation

- **OPS-01:** `/api/v1/health` shall not contact tenant providers or expose secrets.
- **OPS-02:** Errors shall use correct 4xx/5xx codes and a stable `{error:{code,message}}`
  shape with a request ID.
- **OPS-03:** Logs shall exclude authorization headers, API keys, OAuth tokens, résumé
  text, message bodies, and browser context secrets.
- **OPS-04:** A clean deployment ignore list shall exclude local credentials, databases,
  PDFs, profiles, logs, screenshots, browser profiles, backups, and generated output.
- **OPS-05:** Security headers shall include CSP, frame restrictions, MIME sniffing
  protection, referrer policy, and permissions policy.
- **OPS-06:** Permanent account deletion shall require an Auth-verified sign-in no more
  than ten minutes old, in addition to the exact deletion confirmation.

## 4. Non-functional requirements

- **NFR-01 Tenant isolation:** direct API and Supabase Data API access must both enforce
  owner isolation.
- **NFR-02 Availability:** no request depends on one warm Vercel instance or local disk.
- **NFR-03 Compatibility:** hosted code supports Python 3.12.
- **NFR-04 Idempotency:** retries never repeat an externally meaningful send/apply action.
- **NFR-05 Performance:** normal CRUD p95 target under 800 ms excluding providers; AI
  and OAuth provider latency are reported separately.
- **NFR-06 Upload safety:** direct uploads are private, size/type limited, and parsed as
  untrusted input.
- **NFR-07 Accessibility:** essential auth/onboarding/drafting/approval functions are
  keyboard usable and have programmatic labels/status messages.
- **NFR-08 Data minimization:** only data required for visible user features is retained.
- **NFR-09 Deletion:** disconnect/account deletion has documented provider and storage
  cleanup behavior.
- **NFR-10 Rate control:** auth, AI validation/generation, OAuth start, and send endpoints
  have abuse limits at the platform and/or database layer.
- **NFR-11 Launch assurance:** public promotion requires a real staging migration and
  cross-tenant RLS, Storage, send, OAuth lifecycle, deletion, and concurrency tests;
  local schema parsing and mocked tests are not substitutes.
- **NFR-12 Operator identity:** privacy/terms/support pages are not launch-ready until
  the operator supplies and reviews its real legal identity, contact, address,
  governing terms, applicable notices, and effective dates.
- **NFR-13 Outbound request safety:** discovery and browser adapters revalidate every
  navigation/redirect against provider-specific HTTPS host allowlists and never fetch
  loopback, private, link-local, metadata-service, or credential-bearing URLs.
- **NFR-14 Browser execution:** browser work requires a continuously running worker;
  Vercel request lifetime or local disk shall never be used as the job scheduler.
- **NFR-15 Human checkpoints:** CAPTCHA and MFA are user/provider controls, not errors to
  evade. The product shall pause visibly and provide a Live View/manual continuation.

## 5. Authorization matrix

| Resource | Anonymous | Owner | Worker secret |
|---|---:|---:|---:|
| Public config/health | Read | Read | Read |
| Profile/settings | None | CRUD own | Explicit user-bound access |
| Résumé metadata/object | None | CRUD own prefix | Explicit job-user access |
| Jobs/applications/drafts | None | CRUD own | Explicit job-user access |
| Discovery preferences/results | None | CRUD/read own | Explicit claimed-job user access |
| Form revisions | None | Create/read/approve exact own revision | Scan/update only through service RPCs bound to claimed job |
| Visible connection metadata | None | Read/delete own | Explicit user-bound access |
| OAuth tokens/browser secrets | None | No direct secret read | Required provider operations |
| User Google OAuth client | None | Configure/status/delete through authenticated API; no direct secret/ciphertext read | Required Gmail OAuth/refresh operations |
| Provider send-abuse hashes | None | None | Reserve/finalize/prune only |
| Automation jobs | None | Create/read/cancel own | Claim/update leased job |
| Audit events | None | Read own | Append redacted events |

## 6. Acceptance test minimum

- Anonymous calls to every private endpoint return 401.
- User A cannot select, mutate, delete, or sign URLs for User B's IDs/objects.
- RLS tests repeat the same attacks through the Supabase REST API.
- A Groq test key never appears in database writes, logs, errors, or API responses.
- OAuth state replay and cross-user callback attempts fail.
- A newer OAuth start or disconnect makes an older Google callback stale; concurrent
  callback/disconnect execution cannot resurrect a disconnected connection.
- Platform-managed OAuth is selected by default when configured; the explicitly chosen
  user-managed client also connects through the same fixed callback, while an API key
  or request-supplied redirect/scope/endpoint is rejected.
- A saved user OAuth client secret is encrypted at rest and absent from API status,
  browser storage, logs, and authenticated-role table access.
- User-client replacement/deletion fails until Gmail is disconnected. Saving or
  deleting advances its generation and invalidates an older in-flight callback; corrupt
  or unavailable bound credentials fail closed.
- Preview verification confirms that a named Google test user can connect and that the
  UI/runbook disclose the seven-day `gmail.send` Testing expiry; public promotion
  verifies the operator-managed project's exact domain, branding, and sensitive scope.
- Duplicate Gmail-send retries return the original result without sending again.
- Provider-level caps and duplicate detection survive account deletion/recreation, and
  pseudonymous ledger rows cannot influence enforcement after their maximum 90-day
  logical expiry; scheduled/hot-path cleanup and cron-failure monitoring are verified.
- Five valid résumé slots are accepted; invalid/sixth/nested slots fail, and concurrent
  registration leaves exactly one active résumé.
- Sign-out retains the signed-out user's namespaced local Groq key without exposing it
  to another signed-in user; explicit removal and account deletion remove it.
- Worker claim concurrency gives a job to only one worker.
- Telegram/RSS redirect and SSRF tests reject private/loopback/link-local/metadata hosts,
  oversized responses, and an item count above the configured bound.
- CSV/XLSX and pasted-referral tests cover flexible headings, malformed/oversized input,
  formula-like cells, stored-XSS text, normalization, and per-tenant deduplication.
- LinkedIn guest discovery is page/result bounded, backs off on throttling, persists no
  account cookie, and can create only reviewable jobs; Easy Apply remains unavailable.
- Provider URL detection covers Google Forms, Greenhouse, Lever, Ashby, YC, Wellfound,
  Cutshort, and Instahyre, and ZipRecruiter is absent from the hosted registry.
- A changed answer, résumé, target URL, or scanned schema cannot reuse approval; stale
  revision/hash approval and queue attempts fail closed.
- Scan never fills, prefill never submits, and submit requires a distinct explicit
  request for the exact approved revision.
- Fake-provider tests cover registry, host, login-context, and fail-closed behavior for
  all eight managed-browser providers. End-to-end scan/prefill/submit tests apply only
  to implemented application handlers; live launch validation additionally requires
  Browserbase credentials and controlled provider accounts/jobs.
- CAPTCHA/MFA/challenge and ambiguous submit outcomes stop as `needs_attention` and are
  never bypassed or marked successful.
- Unsupported LinkedIn Easy Apply returns a capability response, not a browser job.
- Vercel import/startup does not initialize SQLite, APScheduler, or Playwright.
