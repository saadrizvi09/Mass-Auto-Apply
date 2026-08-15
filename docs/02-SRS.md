# Software Requirements Specification

## AutoApply Cloud 2.0

**Status:** Implementation baseline
**Date:** 2026-08-15

## 1. System boundary

AutoApply Cloud consists of:

1. a static browser application served by Vercel;
2. a stateless FastAPI API deployed as a Vercel Function;
3. Supabase Auth, Postgres, and private Storage;
4. Google OAuth and Gmail API integration;
5. encrypted account-scoped Groq, Hunter, and Browserbase BYOK integrations;
6. credential-free discovery/import adapters;
7. a durable job table and separately deployable Python worker; and
8. optional managed-browser contexts for explicitly permitted providers.

The legacy local application remains a development/reference implementation and is not
loaded by the Vercel entrypoint.

## 2. Actors

- **Anonymous visitor:** may view the landing page, product configuration, health, terms,
  and privacy information.
- **Authenticated user:** owns a profile, résumés, connections, jobs, applications,
  drafts, answer bank, audit history, queued work, and encrypted Groq, Hunter, and
  Browserbase credentials managed through authenticated APIs.
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
- **PROF-05:** The profile shall store an optional user-provided public HTTPS
  `resume_url` separately from private résumé Storage metadata. It shall never be
  populated from a private object path, an expiring signed URL, or an inferred PDF link.
- **PROF-06:** Recognized passout/graduation-year application questions shall map
  deterministically to the reviewed `graduation_year` fact. Recognized résumé/CV URL
  questions shall map deterministically only to `resume_url`; absent facts shall remain
  unanswered for review.

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

### AI — encrypted account-scoped Groq key and drafting

- **AI-01:** `PUT /api/v1/provider-credentials/groq` shall validate the submitted Groq
  key, encrypt a versioned JSON payload with `TOKEN_ENCRYPTION_KEY`, and persist only
  ciphertext in the service-role-only `public.user_provider_credentials` table.
- **AI-02:** The key shall be masked after entry and removable through
  `DELETE /api/v1/provider-credentials/groq`; no read/status response shall return
  plaintext or ciphertext.
- **AI-03:** Authenticated AI requests shall resolve the owned encrypted credential on
  the trusted server and decrypt it only for the requested provider operation.
- **AI-04:** The backend shall not log or echo the key and shall reject missing,
  malformed, corrupt, or undecryptable credentials without provider fallback.
- **AI-05:** Key validation shall call a minimal Groq endpoint before persistence and
  return only validity, safe status, model, timestamps, and a non-secret masked hint.
- **AI-06:** Draft generation shall use the configured production model, the user's
  profile/résumé, and the selected JD; it shall not invent unsupported experience.
- **AI-07:** A generated draft shall remain editable and unsent until explicitly
  approved.
- **AI-08:** Background-job payloads shall never contain the Groq key; trusted code may
  resolve it by the claimed job's persisted `user_id` only when that job requires it.
- **AI-09:** Groq credentials shall be unique by authenticated user and provider.
  Sign-out shall not delete the account credential; explicit removal and successful
  account deletion shall delete its server-side row.
- **AI-10:** `POST /api/v1/discovery/resume-guided` shall use the owned Groq credential
  and the owned active parsed résumé/profile to derive bounded roles and search terms,
  return those terms for inspection, and enqueue public discovery without copying the
  key into either automation-job payload.
- **AI-11:** Batch draft generation shall remain a browser-orchestrated sequence of
  existing owned-job draft requests. Every generated application shall remain editable
  and require its own exact-content approval.

### HUNT — encrypted account-scoped Hunter contact lookup

- **HUNT-01:** `PUT /api/v1/provider-credentials/hunter` shall validate an optional
  Hunter key and persist only its encrypted versioned payload in
  `public.user_provider_credentials`; the frontend shall retain no provider key in
  local storage.
- **HUNT-02:** Trusted Hunter requests shall decrypt the owned credential just in time;
  the key shall not enter a URL, browser-readable row, analytics event, application log,
  automation payload, provider error, or API response.
- **HUNT-03:** Hunter credential validation shall make one bounded account check and
  return only validity, safe status, timestamps, a masked hint, and an allowlisted
  current quota summary.
- **HUNT-04:** `POST /api/v1/jobs/{job_id}/contacts/hunter` shall enforce job ownership,
  require the owned job's company name, accept a contact limit from 1 through 10, and
  request HR-department contacts only.
- **HUNT-05:** Contact results shall expose only bounded email, name, position,
  confidence, verification status, and domain fields. The user shall choose a contact;
  lookup shall not approve a draft or send a message.
- **HUNT-06:** Hunter calls shall be explicit foreground requests. No operator Hunter
  key, durable Hunter job, or unattended contact crawl shall exist. Account-scoped
  server persistence is permitted only through the encrypted credential store.

### CRED — account-scoped provider credentials

- **CRED-01:** `GET /api/v1/provider-credentials` shall expose only safe status,
  provider name, validation timestamps, and masked hints for `groq`, `hunter`, and
  `browserbase`.
- **CRED-02:** `PUT` and `DELETE /api/v1/provider-credentials/{provider}` shall accept
  only those three provider IDs, derive ownership from the bearer token, and replace or
  delete only that user's credential.
- **CRED-03:** `public.user_provider_credentials` shall have no browser-role grants or
  policies. All reads/writes require the service role and explicit `user_id` scoping.
- **CRED-04:** Every payload shall be encrypted with the deployment's Fernet
  `TOKEN_ENCRYPTION_KEY`; plaintext and ciphertext shall be absent from responses,
  logs, analytics, audit detail, and automation payloads.
- **CRED-05:** A missing, corrupt, or undecryptable credential shall fail closed with a
  stable redacted error. The system shall not try another user's or stale credential.
- **CRED-06:** On first authenticated use after upgrade, the frontend may import only
  that user's namespaced legacy Groq/Hunter browser values through the normal validated
  PUT endpoints. It shall delete a browser copy only after successful encrypted save;
  failure shall retain it for an explicit retry and shall not use it as a fallback key
  header. New credential saves shall never write provider secrets to browser storage.

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
- **DISC-05B:** YC application automation shall accept only an exact, current public YC
  job-detail URL explicitly saved by the user. Saved YC query, remote, and limit
  preferences are organization and matching hints only; they shall never fetch, crawl,
  scrape, discover, or enqueue provider work, and the product shall expose no YC bulk
  discovery or bulk-apply path.
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
- **DISC-10:** `POST /api/v1/discovery/resume-guided` shall require an active parsed
  owned résumé and an account-scoped Groq credential, accept bounded location/remote/result options
  plus an idempotency key, and return HTTP 202 with an inspectable plan and two redacted
  automation jobs.
- **DISC-11:** The guided plan shall prefer saved target roles, then Groq-analyzed roles,
  then deterministic résumé recommendations; combine bounded analyzed and saved skills;
  and choose the request, discovery preference, profile, or `India` location in that
  order.
- **DISC-12:** Guided discovery shall enqueue one `discover_linkedin_guest` job using
  the first role and one `discover_public_feeds` job for `telegram` and `rss`. Child
  idempotency keys shall be deterministically derived from the supplied key.
- **DISC-13:** The public-feed worker shall validate the bounded search-term list and
  retain only Telegram/RSS candidates whose searchable text matches at least one term
  before source interleaving, deduplication, and the global cap. An unfiltered manual
  public-feed run shall retain its existing behavior.
- **DISC-14:** `GET /api/v1/discovery/google-forms` shall return a bounded, paginated,
  tenant-scoped queue of direct saved Google Forms and form URLs discovered in job
  metadata, deduplicate by normalized URL, use stable item IDs, and link the latest
  existing application for a saved job. Queue reads shall not enqueue scans.
- **DISC-15:** Form Pilot Stage 01 shall accept either one Google Form URL or a complete
  referral message. Referral parsing shall split numbered postings and extract labeled
  Company, Role, Batch, CTC/Stipend/compensation, Location, application URL, application
  email, subject, and CC fields when present.
- **DISC-16:** Referral parsing shall exclude recognized WhatsApp/channel, Telegram,
  Topmate, paid-group, and premium-referral promotion links/tails from actionable
  application routes. Google Form routes shall appear in Form Pilot; email-address
  routes shall become saved jobs available in Mass Cold Email.
- **DISC-17:** Referral parsing shall return bounded routing counts and shall not scan,
  prepare, approve, prefill, submit, draft, or send as a side effect.

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
- **MAIL-25:** The **Mass Cold Email** sidebar destination shall contain **Build
  campaign** and **Review & send** subtabs rather than exposing review as a separate
  primary-navigation item. Both subtabs shall include email-channel applications only;
  ATS/form revisions and form answers belong to Form Pilot. Mass Cold Email may collect
  at most 10 selected jobs, but the backend shall continue to expose only the existing
  one-application send operation. The browser shall invoke it sequentially for
  individually approved email applications after an explicit final confirmation.
- **MAIL-26:** Every send in that sequence shall independently revalidate ownership,
  exact approval, recipient/content, Gmail connection, active résumé, user daily cap,
  provider-account cap, duplicate-recipient window, and send idempotency reservation.
  A UI batch shall not weaken, pre-reserve around, or bypass any gate.
- **MAIL-27:** The product shall not generate approval from batch selection and shall
  expose no autonomous, delayed, or unreviewed bulk cold-email endpoint.

### CONN — provider connection center

- **CONN-01:** The API shall return a catalog with mode, status, availability, required
  configuration, and plain-language limitations for each provider.
- **CONN-02:** `linkedin` shall report `partner_required` for hosted apply automation.
- **CONN-03:** LinkedIn profile/manual handoff may be used without storing LinkedIn
  credentials or cookies.
- **CONN-04:** A managed-browser login may start only for an operator-allowlisted
  provider and when the worker can resolve either the account owner's valid Browserbase
  BYOK credential or the optional platform fallback.
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
  available from the user's BYOK credential or platform fallback, the operator
  allowlists it, and its current flow is validated with a controlled test account/job.
- **CONN-11:** Connection availability shall not imply application capability. The
  catalog shall report Greenhouse as an aligned managed-browser handler and one-page
  Google Forms as scan plus explicit exact-approved background submit with verified
  confirmation and needs-attention-only Live View; Lever and Ashby as read-only-scan
  validated but pending controlled submit canaries; Wellfound as pending a signed-in
  canary; YC as a finished exact saved-job scan/review/sealed-submit state machine that
  remains operator-allowlist gated until its signed-in canary passes; and Cutshort and
  Instahyre as connection-only until tenant-aware multi-step state machines exist. The
  generic exact-host company-form adapter shall remain internal/gated and absent from
  the public catalog. Multi-page or branching Google Forms shall remain unavailable.
- **CONN-12:** Browserbase BYOK shall require both API key and Project ID. Validation
  shall call Browserbase `GET /v1/projects/{project_id}` with `X-BB-API-Key`, require a
  successful response whose ID matches, and shall not create a browser session.
- **CONN-13:** Browser execution shall prefer the claimed job owner's encrypted
  Browserbase credential. `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID` may be used
  only as a trusted platform fallback when that account has no BYOK credential.
- **CONN-14:** Managed Browserbase sessions shall close immediately when work finishes
  and use a 90-second stall cap. Product copy shall disclose Browserbase's one-minute
  minimum billing period for every created session; reducing the cap shall not be
  described as reducing the duration of successful runs.

### FORM — immutable application-form review

- **FORM-01:** `application_scan` shall inspect one owned job/application URL and persist
  a new form revision containing the detected provider, target URL, exact field schema,
  provider-observed values, selected résumé ID, and deterministic content/schema hashes.
  Groq suggestions shall not be persisted by the scan worker.
- **FORM-02:** Form ownership, provider/URL, résumé, detected schema, and revision number
  are immutable. The first approval may atomically replace proposed answers with the
  exact reviewed answer object and then seal it; every subsequent answer change, résumé
  or URL change, or newly observed schema shall create a new revision.
- **FORM-03:** Approval shall name an exact owned revision and its expected hashes. A
  stale or mismatched revision/hash shall fail closed without queueing browser work.
- **FORM-04:** When a newly scanned, unapproved revision is loaded and the authenticated
  account has a usable Groq credential, the browser shall automatically request one
  set of grounded answer suggestions. The request shall be deduplicated per revision,
  use only the owned profile, linked résumé, job, and captured non-sensitive questions,
  and keep the key and suggested answers out of the worker payload. A missing/failed
  Groq request shall leave the fields editable and expose a retry rather than blocking
  manual review. Suggestions shall never approve themselves.
- **FORM-05:** Calling the approval RPC alone shall not fill or submit a form. The normal
  Form Pilot UI may intentionally compose exact approval and submit enqueue behind one
  explicit **Approve & submit in background** action.
- **FORM-06:** `application_prefill` shall remain an observable diagnostic/canary path:
  it fills only values and résumé bytes bound to an approved revision and never
  activates the provider submit control. It is not the normal Google Forms completion
  path.
- **FORM-07:** The normal Google Forms action shall atomically seal the latest exact
  revision, reject any incomplete required-answer preflight, and enqueue an idempotent
  `application_submit` bound to that revision. A queued or running job shall never be
  described as submitted.
- **FORM-08:** A required unanswered/sensitive/unknown field, changed schema, expired
  login, CAPTCHA, MFA, security checkpoint, or ambiguous submission result shall stop
  as `needs_attention`; the worker shall never guess, bypass, or report assumed success.
- **FORM-09:** For Google Forms and every separately enabled provider that permits a
  worker submit phase, success shall require freshly observed provider-page confirmation
  evidence and an idempotent terminal transition. The verified result contract is
  `code=application_submitted` and `submission_state=confirmed`. A timeout or ambiguous
  state after submit shall remain `needs_attention` and shall not be blindly retried.
- **FORM-10:** Repeating the identical approval shall be idempotent; attempting to alter
  answers or hashes after approval shall fail and require a new revision.
- **FORM-11:** Form Pilot shall own scanning, suggestion review, exact approval-bound
  submission progress, verified confirmation, and any needs-attention Browserbase Live
  View fallback for form-channel applications. It
  shall not move Google Form revisions into **Mass Cold Email**, whose **Review & send**
  subtab is reserved for email drafts.
- **FORM-12:** A YC application shall remain bound to one exact current public job URL
  and the owning tenant's isolated persistent Browserbase BYOK context. Playwright in a
  separate continuously running worker shall scan only visible job-bound fields, create
  an immutable résumé/Groq-grounded revision for review, and activate one unique submit
  control only for that sealed revision. Success requires fresh YC confirmation; login,
  MFA/CAPTCHA, a changed page/schema, an unknown required field, an ambiguous control,
  or uncertain confirmation shall fail closed without a blind retry. Vercel shall not
  launch Chromium, control Browserbase, or execute the worker.

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
- **RUN-11:** Durable discovery payloads may contain bounded résumé-derived search
  terms, but shall contain no résumé text, Groq key, Hunter key, Browserbase key/project
  pair, contact candidates, or draft message content.

### OPS — public operation

- **OPS-01:** `/api/v1/health` shall not contact tenant providers or expose secrets.
- **OPS-02:** Errors shall use correct 4xx/5xx codes and a stable `{error:{code,message}}`
  shape with a request ID.
- **OPS-03:** Logs shall exclude authorization headers, API keys, encrypted credential
  payloads, OAuth tokens, résumé text, message bodies, and browser context secrets.
- **OPS-04:** A clean deployment ignore list shall exclude local credentials, databases,
  PDFs, profiles, logs, screenshots, browser profiles, backups, and generated output.
- **OPS-05:** Security headers shall include CSP, frame restrictions, MIME sniffing
  protection, referrer policy, and permissions policy.
- **OPS-06:** Permanent account deletion shall require an Auth-verified sign-in no more
  than ten minutes old, in addition to the exact deletion confirmation.
- **OPS-07:** `TOKEN_ENCRYPTION_KEY` shall be present and identical in Vercel and every
  worker before provider credentials are accepted. Rotation requires an explicit
  decrypt/re-encrypt migration; silently changing or losing it is prohibited.

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
- **NFR-10 Rate control:** auth, AI validation/generation, Hunter validation/contact
  lookup, OAuth start, and send endpoints have bounds or abuse limits at the provider,
  platform, and/or database layer.
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
  it shall be deployed on a persistent process host outside Vercel. Vercel request
  lifetime or local disk shall never be used as the job scheduler.
- **NFR-15 Human checkpoints:** CAPTCHA and MFA are user/provider controls, not errors to
  evade. The product shall pause visibly and may provide a Live View/manual continuation
  only as a `needs_attention` fallback.

## 5. Authorization matrix

| Resource | Anonymous | Owner | Worker secret |
|---|---:|---:|---:|
| Public config/health | Read | Read | Read |
| Profile/settings | None | CRUD own | Explicit user-bound access |
| Résumé metadata/object | None | CRUD own prefix | Explicit job-user access |
| Groq/Hunter/Browserbase provider credentials | None | Configure/status/delete through authenticated API; no direct secret/ciphertext read | Resolve only for explicit owned provider work |
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
- Groq, Hunter, and Browserbase test secrets appear in the database only as ciphertext
  in `user_provider_credentials`, and never appear in URLs, browser-readable queries,
  logs, errors, automation payloads, analytics, or API responses.
- Cross-tenant credential status, replacement, deletion, contact lookup, and browser
  execution fail before a provider is called. Hunter requested/results limits remain
  between 1 and 10.
- Browserbase validation uses `GET /v1/projects/{project_id}` and creates no session;
  invalid or mismatched key/project pairs are not saved. Browser jobs select owner BYOK
  before platform fallback, stop after the 90-second stall cap, and close immediately
  on normal completion.
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
- Deterministic form mapping uses the reviewed graduation year for recognized passout/
  graduation questions and only the explicit public HTTPS résumé URL for recognized
  résumé-link questions; a private Storage path or signed URL is never substituted.
- Sign-out retains account-scoped provider credentials without exposing them in browser
  storage or to another signed-in user; explicit removal and account deletion remove
  the matching service-only rows.
- Worker claim concurrency gives a job to only one worker.
- Telegram/RSS redirect and SSRF tests reject private/loopback/link-local/metadata hosts,
  oversized responses, and an item count above the configured bound.
- Résumé-guided discovery requires an owned parsed active résumé, returns only bounded
  roles/keywords/search terms, derives two tenant-owned idempotent jobs, and passes only
  those terms—not résumé text or the Groq key—to the public-feed worker. Nonmatching
  Telegram/RSS candidates are omitted before the global result cap.
- CSV/XLSX and pasted-referral tests cover flexible headings, malformed/oversized input,
  formula-like cells, stored-XSS text, normalization, and per-tenant deduplication.
- LinkedIn guest discovery is page/result bounded, backs off on throttling, persists no
  account cookie, and can create only reviewable jobs; Easy Apply remains unavailable.
- Google Forms queue tests cover tenant isolation, direct and metadata-discovered URLs,
  normalized-URL deduplication, stable IDs, latest-application linkage, pagination, and
  the absence of scan side effects.
- Provider URL detection covers Google Forms, Greenhouse, Lever, Ashby, YC, Wellfound,
  Cutshort, and Instahyre, and ZipRecruiter is absent from the hosted registry.
- A changed answer, résumé, target URL, or scanned schema cannot reuse approval; stale
  revision/hash approval and queue attempts fail closed.
- Scan never fills or submits. The normal Google Forms path requires one explicit
  approval-bound submit action for the exact latest revision and complete required
  answers. The worker submits once and succeeds only with fresh confirmation; an
  uncertain result stops as `needs_attention` without blind retry.
- Fake-provider tests cover registry, host, login-context, and fail-closed behavior for
  all eight managed-browser providers. End-to-end tests exercise only the stages each
  handler exposes: Google Forms exercises exact-approved submit and verified
  confirmation, while every other provider-specific worker submit is tested only when
  separately enabled. Live launch
  validation additionally requires Browserbase credentials and controlled provider
  accounts/jobs.
- CAPTCHA/MFA/challenge and ambiguous submit outcomes stop as `needs_attention` and are
  never bypassed or marked successful.
- Unsupported LinkedIn Easy Apply returns a capability response, not a browser job.
- **Mass Cold Email** UI tests or controlled staging checks show that the sidebar has no
  separate review destination; **Build campaign** and **Review & send** are subtabs,
  selection stops at 10, projected Hunter credit use appears inline before an explicit
  search, contact choice is required, every Groq draft requires exact individual
  approval, final send requires confirmation, and Gmail calls execute sequentially
  under the unchanged daily/duplicate/idempotency gates. Both subtabs exclude ATS/form
  applications; Form Pilot retains ownership of those revisions, submissions, and
  needs-attention fallbacks.
- Form Pilot browser tests prove a newly loaded eligible revision automatically makes
  at most one Groq suggestion request, preserves editable/manual review on a missing
  credential or provider failure, never returns the key, and never grants approval.
- A Google Forms canary proves exact latest-revision approval, complete required-answer
  preflight, one idempotent `application_submit`, and success only when the provider
  freshly confirms submission. Login/challenge/schema/confirmation uncertainty proves
  the separate `needs_attention` Live View fallback and absence of blind retries.
- Vercel import/startup does not initialize SQLite, APScheduler, or Playwright.
