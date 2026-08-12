-- AutoApply Cloud 2.0: multi-tenant schema, RLS, private Storage, and durable RPCs.
-- Apply with the Supabase CLI as a privileged migration role.

begin;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;
-- Supabase Cron provides a database-local, observable retention job.  Treat an
-- unavailable pg_cron extension as a deployment failure rather than silently
-- making a retention promise the database cannot keep.
create extension if not exists pg_cron;

create table public.profiles (
    user_id uuid primary key references auth.users(id) on delete cascade,
    full_name text check (full_name is null or char_length(full_name) <= 160),
    email text check (email is null or char_length(email) <= 320),
    phone text check (phone is null or char_length(phone) <= 60),
    location text check (location is null or char_length(location) <= 200),
    headline text check (headline is null or char_length(headline) <= 240),
    summary text check (summary is null or char_length(summary) <= 5000),
    years_experience numeric check (years_experience is null or years_experience between 0 and 80),
    work_authorization text check (work_authorization is null or char_length(work_authorization) <= 500),
    notice_period text check (notice_period is null or char_length(notice_period) <= 200),
    linkedin_url text check (linkedin_url is null or char_length(linkedin_url) <= 2048),
    github_url text check (github_url is null or char_length(github_url) <= 2048),
    portfolio_url text check (portfolio_url is null or char_length(portfolio_url) <= 2048),
    education jsonb not null default '[]'::jsonb check (
        jsonb_typeof(education) = 'array' and octet_length(education::text) <= 65536
    ),
    skills jsonb not null default '[]'::jsonb check (
        jsonb_typeof(skills) = 'array' and octet_length(skills::text) <= 32768
    ),
    preferences jsonb not null default '{}'::jsonb check (
        jsonb_typeof(preferences) = 'object' and octet_length(preferences::text) <= 32768
    ),
    onboarding_completed boolean not null default false,
    account_status text not null default 'active'
        check (account_status in ('active', 'deleting')),
    deletion_started_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.user_settings (
    user_id uuid primary key references auth.users(id) on delete cascade,
    daily_send_cap integer not null default 10 check (daily_send_cap between 0 and 25),
    duplicate_window_days integer not null default 7 check (duplicate_window_days between 1 and 90),
    require_review boolean not null default true check (require_review),
    timezone text not null default 'UTC' check (char_length(timezone) between 1 and 80),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table public.resumes (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    storage_path text not null check (
        split_part(storage_path, '/', 1) = user_id::text
        and array_length(string_to_array(storage_path, '/'), 1) = 2
        and lower(storage_path) like '%.pdf'
        and char_length(storage_path) <= 1024
    ),
    original_name text not null check (char_length(original_name) between 1 and 255),
    mime_type text not null check (mime_type = 'application/pdf'),
    size_bytes bigint not null check (size_bytes between 1 and 6291456),
    sha256 text check (sha256 is null or sha256 ~ '^[0-9a-fA-F]{64}$'),
    parsed_text text check (parsed_text is null or char_length(parsed_text) <= 250000),
    parse_status text not null default 'uploaded'
        check (parse_status in ('uploaded', 'parsing', 'parsed', 'failed')),
    parse_error text check (parse_error is null or char_length(parse_error) <= 500),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, storage_path)
);

create unique index resumes_one_active_per_user_idx
    on public.resumes (user_id) where is_active;
create index resumes_user_created_idx on public.resumes (user_id, created_at desc);

create table public.jobs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    source text not null default 'manual' check (char_length(source) between 1 and 60),
    external_id text check (external_id is null or char_length(external_id) <= 255),
    normalized_url text check (normalized_url is null or char_length(normalized_url) <= 2048),
    apply_url text check (apply_url is null or char_length(apply_url) <= 2048),
    title text not null check (char_length(title) between 1 and 240),
    company text not null check (char_length(company) between 1 and 240),
    location text check (location is null or char_length(location) <= 240),
    description text not null check (char_length(description) between 20 and 25000),
    contact_email text check (
        contact_email is null or (
            char_length(contact_email) <= 320
            and contact_email ~* '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
        )
    ),
    status text not null default 'saved'
        check (status in ('saved', 'drafting', 'ready', 'applied', 'rejected', 'interview', 'archived')),
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 32768
    ),
    archived_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index jobs_user_normalized_url_uidx
    on public.jobs (user_id, normalized_url) where normalized_url is not null;
create index jobs_user_created_idx on public.jobs (user_id, created_at desc);
create index jobs_user_status_idx on public.jobs (user_id, status);

create table public.applications (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    job_id uuid references public.jobs(id) on delete set null,
    channel text not null default 'email' check (channel in ('email', 'manual', 'ats')),
    status text not null default 'draft_pending' check (
        status in (
            'draft_pending', 'drafted', 'approved', 'queued', 'sent', 'manual',
            'applied', 'rejected', 'interview', 'failed', 'archived'
        )
    ),
    recipient text check (
        recipient is null or (
            char_length(recipient) <= 320
            and recipient ~* '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
        )
    ),
    subject text check (subject is null or char_length(subject) <= 500),
    body text check (body is null or char_length(body) <= 20000),
    content_revision bigint not null default 1 check (content_revision >= 1),
    approved_revision bigint check (
        approved_revision is null or approved_revision between 1 and content_revision
    ),
    approved_at timestamptz,
    sent_at timestamptz,
    provider_message_id text check (provider_message_id is null or char_length(provider_message_id) <= 1024),
    provider_thread_id text check (provider_thread_id is null or char_length(provider_thread_id) <= 1024),
    send_idempotency_key text check (send_idempotency_key is null or char_length(send_idempotency_key) between 8 and 200),
    last_error text check (last_error is null or char_length(last_error) <= 500),
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 32768
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, send_idempotency_key)
);

create index applications_user_created_idx on public.applications (user_id, created_at desc);
create index applications_user_status_idx on public.applications (user_id, status);
create index applications_job_idx on public.applications (job_id);

create table public.connections (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null check (provider ~ '^[a-z][a-z0-9_-]{0,79}$'),
    mode text not null check (
        mode in ('oauth', 'public_ats', 'managed_browser', 'manual_only', 'partner_required')
    ),
    status text not null check (
        status in ('pending', 'connected', 'disconnected', 'needs_attention', 'error')
    ),
    external_account_id text check (external_account_id is null or char_length(external_account_id) <= 512),
    display_name text check (display_name is null or char_length(display_name) <= 320),
    scopes text[] not null default '{}'::text[] check (
        cardinality(scopes) <= 16 and octet_length(array_to_string(scopes, ',')) <= 4096
    ),
    expires_at timestamptz,
    last_verified_at timestamptz,
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 32768
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, provider)
);

create index connections_user_status_idx on public.connections (user_id, status);
create unique index connections_one_google_account_uidx
    on public.connections (external_account_id)
    where provider = 'gmail' and external_account_id is not null;

create table public.connection_secrets (
    connection_id uuid primary key references public.connections(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    access_token_ciphertext text check (access_token_ciphertext is null or char_length(access_token_ciphertext) <= 16384),
    refresh_token_ciphertext text check (refresh_token_ciphertext is null or char_length(refresh_token_ciphertext) <= 16384),
    browser_context_id_ciphertext text check (browser_context_id_ciphertext is null or char_length(browser_context_id_ciphertext) <= 4096),
    browser_session_id_ciphertext text check (browser_session_id_ciphertext is null or char_length(browser_session_id_ciphertext) <= 4096),
    browser_lifecycle_generation bigint check (
        browser_lifecycle_generation is null or browser_lifecycle_generation > 0
    ),
    token_type text check (token_type is null or char_length(token_type) <= 80),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index connection_secrets_user_idx on public.connection_secrets (user_id);

-- Serializes provider connects/reconnects/disconnects. Gmail stores the generation
-- in each OAuth state; managed-browser operations store it beside their encrypted
-- remote resource IDs. A newer operation makes all older writes stale.
create table public.connection_lifecycles (
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null check (provider ~ '^[a-z][a-z0-9_-]{0,79}$'),
    generation bigint not null default 0 check (generation >= 0),
    status text not null check (
        status in ('connecting', 'connected', 'disconnecting', 'disconnected')
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, provider)
);

create table public.oauth_states (
    id uuid primary key default gen_random_uuid(),
    state_hash text not null unique check (char_length(state_hash) between 32 and 128),
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null check (provider ~ '^[a-z][a-z0-9_-]{0,79}$'),
    generation bigint not null check (generation > 0),
    return_path text not null default '/' check (
        return_path like '/%' and return_path not like '//%' and char_length(return_path) <= 1024
    ),
    pkce_verifier_ciphertext text check (
        pkce_verifier_ciphertext is null or char_length(pkce_verifier_ciphertext) <= 4096
    ),
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index oauth_states_expiry_idx on public.oauth_states (expires_at);
create index oauth_states_user_provider_idx on public.oauth_states (user_id, provider);

create table public.automation_jobs (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    application_id uuid references public.applications(id) on delete set null,
    kind text not null check (kind ~ '^[a-z][a-z0-9_]{0,79}$'),
    provider text check (provider is null or provider ~ '^[a-z][a-z0-9_-]{0,79}$'),
    status text not null default 'queued' check (
        status in ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'needs_attention')
    ),
    payload jsonb not null default '{}'::jsonb check (
        jsonb_typeof(payload) = 'object' and octet_length(payload::text) <= 32768
    ),
    progress jsonb not null default '{}'::jsonb check (
        jsonb_typeof(progress) = 'object' and octet_length(progress::text) <= 16384
    ),
    result jsonb check (
        result is null or (jsonb_typeof(result) = 'object' and octet_length(result::text) <= 32768)
    ),
    error_code text check (error_code is null or error_code ~ '^[a-z][a-z0-9_]{1,63}$'),
    error_message text check (error_message is null or char_length(error_message) <= 500),
    idempotency_key text not null check (char_length(idempotency_key) between 8 and 200),
    attempts integer not null default 0 check (attempts >= 0),
    max_attempts integer not null default 3 check (max_attempts between 1 and 20),
    run_after timestamptz not null default now(),
    locked_by text,
    locked_at timestamptz,
    lease_expires_at timestamptz,
    cancel_requested_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, idempotency_key)
);

create index automation_jobs_claim_idx
    on public.automation_jobs (status, run_after, created_at)
    where status = 'queued';
create index automation_jobs_expired_lease_idx
    on public.automation_jobs (lease_expires_at)
    where status = 'running';
create index automation_jobs_user_status_idx
    on public.automation_jobs (user_id, status, created_at desc);
create index automation_jobs_application_idx on public.automation_jobs (application_id);
create unique index automation_jobs_one_running_provider_idx
    on public.automation_jobs (user_id, provider)
    where status = 'running' and provider is not null;

create table public.send_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    application_id uuid references public.applications(id) on delete set null,
    provider text not null default 'gmail',
    outcome text not null check (
        outcome in ('pending_provider', 'sent', 'failed', 'needs_attention')
    ),
    recipient_hash text not null check (recipient_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key text not null check (char_length(idempotency_key) between 8 and 200),
    provider_message_id text,
    provider_thread_id text,
    error_code text check (error_code is null or error_code ~ '^[a-z][a-z0-9_]{1,63}$'),
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 16384
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, idempotency_key)
);

create index send_events_user_created_idx on public.send_events (user_id, created_at desc);
create index send_events_duplicate_idx
    on public.send_events (user_id, recipient_hash, created_at desc)
    where outcome in ('pending_provider', 'sent', 'needs_attention');
create index send_events_application_idx on public.send_events (application_id);

-- Provider-scoped abuse controls must survive deletion of a tenant account.  This
-- ledger deliberately contains no user/application foreign key or provider/recipient
-- plaintext: only domain-separated SHA-256 digests and the random send-event ID are
-- retained.  Its expiry constraint caps active retention at the product's 90-day
-- duplicate window, while reservations remain active for at least 24 hours for the
-- provider-level cap. Expired rows are physically pruned on the next reservation.
create table public.provider_send_events (
    send_event_id uuid primary key,
    provider text not null default 'gmail' check (provider = 'gmail'),
    provider_account_hash text not null check (provider_account_hash ~ '^[0-9a-f]{64}$'),
    recipient_hash text not null check (recipient_hash ~ '^[0-9a-f]{64}$'),
    outcome text not null check (
        outcome in ('pending_provider', 'sent', 'failed', 'needs_attention')
    ),
    error_code text check (error_code is null or error_code ~ '^[a-z][a-z0-9_]{1,63}$'),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null,
    check (expires_at > created_at and expires_at <= created_at + interval '90 days')
);

create index provider_send_events_account_created_idx
    on public.provider_send_events (provider_account_hash, created_at desc)
    where outcome in ('pending_provider', 'sent', 'needs_attention');
create index provider_send_events_duplicate_idx
    on public.provider_send_events (provider_account_hash, recipient_hash, expires_at desc)
    where outcome in ('pending_provider', 'sent', 'needs_attention');
create index provider_send_events_expiry_idx on public.provider_send_events (expires_at);

create or replace function public.prune_provider_send_events()
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
    removed_count bigint;
begin
    delete from public.provider_send_events event
     where event.expires_at <= clock_timestamp();
    get diagnostics removed_count = row_count;
    return removed_count;
end;
$$;

revoke all on function public.prune_provider_send_events()
    from public, anon, authenticated;
grant execute on function public.prune_provider_send_events() to service_role;

-- A named job is idempotently replaced by pg_cron when this migration is rebuilt.
-- The reservation path also prunes, while this hourly job guarantees cleanup for
-- provider accounts that never send again.
select cron.schedule(
    'autoapply-prune-provider-send-events',
    '17 * * * *',
    $cron_command$select public.prune_provider_send_events()$cron_command$
);

create table public.answer_bank (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    normalized_question text not null check (char_length(normalized_question) between 1 and 1000),
    question text not null check (char_length(question) between 1 and 2000),
    answer text not null check (char_length(answer) between 1 and 10000),
    source text not null default 'user' check (source in ('user', 'profile', 'generated')),
    last_used_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, normalized_question)
);

create index answer_bank_user_updated_idx on public.answer_bank (user_id, updated_at desc);

create table public.audit_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    event_type text not null check (event_type ~ '^[a-z][a-z0-9_.-]{0,99}$'),
    resource_type text check (resource_type is null or resource_type ~ '^[a-z][a-z0-9_-]{0,79}$'),
    resource_id uuid,
    outcome text not null default 'success' check (
        outcome in ('success', 'failure', 'denied', 'cancelled', 'needs_attention')
    ),
    request_id uuid,
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 16384
    ),
    created_at timestamptz not null default now()
);

create index audit_events_user_created_idx on public.audit_events (user_id, created_at desc);

-- Keep update timestamps consistent across API, worker, and direct RLS writes.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger profiles_set_updated_at before update on public.profiles
    for each row execute function public.set_updated_at();
create trigger user_settings_set_updated_at before update on public.user_settings
    for each row execute function public.set_updated_at();
create trigger resumes_set_updated_at before update on public.resumes
    for each row execute function public.set_updated_at();
create trigger jobs_set_updated_at before update on public.jobs
    for each row execute function public.set_updated_at();
create trigger applications_set_updated_at before update on public.applications
    for each row execute function public.set_updated_at();
create trigger connections_set_updated_at before update on public.connections
    for each row execute function public.set_updated_at();
create trigger connection_secrets_set_updated_at before update on public.connection_secrets
    for each row execute function public.set_updated_at();
create trigger connection_lifecycles_set_updated_at before update on public.connection_lifecycles
    for each row execute function public.set_updated_at();
create trigger automation_jobs_set_updated_at before update on public.automation_jobs
    for each row execute function public.set_updated_at();
create trigger send_events_set_updated_at before update on public.send_events
    for each row execute function public.set_updated_at();
create trigger provider_send_events_set_updated_at before update on public.provider_send_events
    for each row execute function public.set_updated_at();
create trigger answer_bank_set_updated_at before update on public.answer_bank
    for each row execute function public.set_updated_at();

-- Content edits always invalidate approval against the row's current state.  This
-- trigger is the last line of defence against concurrent PATCH/approve/send races,
-- including server-side writes that do not originate in the web API.
create or replace function public.guard_application_content_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.recipient is distinct from old.recipient
       or new.subject is distinct from old.subject
       or new.body is distinct from old.body then
        if old.status in ('queued', 'sent') then
            raise exception using errcode = 'P0001', message = 'application_content_locked';
        end if;
        new.content_revision = old.content_revision + 1;
        new.approved_revision = null;
        new.approved_at = null;
        new.status = case
            when nullif(btrim(new.recipient), '') is not null
             and nullif(btrim(new.subject), '') is not null
             and nullif(btrim(new.body), '') is not null then 'drafted'
            else 'draft_pending'
        end;
    end if;
    return new;
end;
$$;

create trigger applications_guard_content_revision
    before update of recipient, subject, body on public.applications
    for each row execute function public.guard_application_content_revision();

-- Bound tenant-created rows even when a signed-in browser writes through the
-- Supabase data API directly.  The settings row acts as a per-user transaction lock.
create or replace function public.enforce_tenant_row_quota()
returns trigger
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    existing_count bigint;
    row_limit integer;
begin
    if auth.uid() is not null and auth.uid() <> new.user_id then
        raise exception using errcode = '42501', message = 'tenant_owner_mismatch';
    end if;
    insert into public.user_settings (user_id) values (new.user_id)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings settings
     where settings.user_id = new.user_id for update;

    if tg_table_name = 'resumes' then
        row_limit := 5;
        select count(*) into existing_count from public.resumes where user_id = new.user_id;
    elsif tg_table_name = 'jobs' then
        row_limit := 2000;
        select count(*) into existing_count from public.jobs where user_id = new.user_id;
    elsif tg_table_name = 'applications' then
        row_limit := 2000;
        select count(*) into existing_count from public.applications where user_id = new.user_id;
    elsif tg_table_name = 'answer_bank' then
        row_limit := 1000;
        select count(*) into existing_count from public.answer_bank where user_id = new.user_id;
    else
        raise exception using errcode = '22023', message = 'tenant_quota_table_invalid';
    end if;
    if existing_count >= row_limit then
        raise exception using errcode = 'P0001', message = 'tenant_row_quota_reached';
    end if;
    return new;
end;
$$;

create trigger resumes_enforce_tenant_quota before insert on public.resumes
    for each row execute function public.enforce_tenant_row_quota();
create trigger jobs_enforce_tenant_quota before insert on public.jobs
    for each row execute function public.enforce_tenant_row_quota();
create trigger applications_enforce_tenant_quota before insert on public.applications
    for each row execute function public.enforce_tenant_row_quota();
create trigger answer_bank_enforce_tenant_quota before insert on public.answer_bank
    for each row execute function public.enforce_tenant_row_quota();

create or replace function public.enforce_audit_retention()
returns trigger
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    existing_count bigint;
begin
    delete from public.audit_events event
     where event.user_id = new.user_id
       and event.created_at < clock_timestamp() - interval '90 days';
    select count(*) into existing_count
      from public.audit_events event where event.user_id = new.user_id;
    if existing_count >= 50000 then
        delete from public.audit_events event
         where event.id in (
             select oldest.id from public.audit_events oldest
              where oldest.user_id = new.user_id
              order by oldest.created_at, oldest.id
              limit (existing_count - 49000)
         );
    end if;
    return new;
end;
$$;

create trigger audit_events_enforce_retention before insert on public.audit_events
    for each row execute function public.enforce_audit_retention();

create or replace function public.prune_expired_oauth_states()
returns trigger
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    delete from public.oauth_states state where state.expires_at <= clock_timestamp();
    return new;
end;
$$;

create trigger oauth_states_prune_expired before insert on public.oauth_states
    for each row execute function public.prune_expired_oauth_states();

-- Foreign keys alone do not ensure two tenant-owned rows have the same owner.
create or replace function public.enforce_owned_reference()
returns trigger
language plpgsql
set search_path = 'public'
as $$
begin
    if tg_table_name = 'applications' and new.job_id is not null and not exists (
        select 1 from public.jobs where id = new.job_id and user_id = new.user_id
    ) then
        raise exception using errcode = '23503', message = 'owned_job_not_found';
    elsif tg_table_name = 'automation_jobs' and new.application_id is not null and not exists (
        select 1 from public.applications where id = new.application_id and user_id = new.user_id
    ) then
        raise exception using errcode = '23503', message = 'owned_application_not_found';
    elsif tg_table_name = 'connection_secrets' and not exists (
        select 1 from public.connections where id = new.connection_id and user_id = new.user_id
    ) then
        raise exception using errcode = '23503', message = 'owned_connection_not_found';
    elsif tg_table_name = 'send_events' and new.application_id is not null and not exists (
        select 1 from public.applications where id = new.application_id and user_id = new.user_id
    ) then
        raise exception using errcode = '23503', message = 'owned_application_not_found';
    end if;
    return new;
end;
$$;

create trigger applications_owned_job before insert or update of job_id, user_id
    on public.applications for each row execute function public.enforce_owned_reference();
create trigger automation_jobs_owned_application before insert or update of application_id, user_id
    on public.automation_jobs for each row execute function public.enforce_owned_reference();
create trigger connection_secrets_owned_connection before insert or update of connection_id, user_id
    on public.connection_secrets for each row execute function public.enforce_owned_reference();
create trigger send_events_owned_application before insert or update of application_id, user_id
    on public.send_events for each row execute function public.enforce_owned_reference();

-- Provision tenant defaults for every new Supabase Auth user.
create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    insert into public.profiles (user_id, full_name, email)
    values (
        new.id,
        nullif(btrim(coalesce(new.raw_user_meta_data ->> 'full_name', '')), ''),
        new.email
    )
    on conflict (user_id) do nothing;

    insert into public.user_settings (user_id)
    values (new.id)
    on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_auth_user();

create or replace function public.account_is_active()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
    select exists (
        select 1 from public.profiles profile
         where profile.user_id = auth.uid() and profile.account_status = 'active'
    );
$$;

create or replace function public.assert_active_user()
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
    current_user_id uuid := auth.uid();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = current_user_id and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    return current_user_id;
end;
$$;

create or replace function public.begin_account_deletion(confirmation_input text)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := auth.uid();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if confirmation_input <> 'DELETE' then
        raise exception using errcode = '22023', message = 'account_deletion_confirmation_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;
    if exists (
        select 1 from public.send_events event
         where event.user_id = current_user_id
           and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'account_operation_in_progress';
    end if;
    update public.profiles profile
       set account_status = 'deleting',
           deletion_started_at = coalesce(profile.deletion_started_at, clock_timestamp())
     where profile.user_id = current_user_id;
    return true;
end;
$$;

revoke all on function public.account_is_active() from public, anon;
revoke all on function public.assert_active_user() from public, anon, authenticated;
revoke all on function public.begin_account_deletion(text) from public, anon;
grant execute on function public.account_is_active() to authenticated;
grant execute on function public.begin_account_deletion(text) to authenticated;

-- RLS is enabled on every tenant or server-only table.
alter table public.profiles enable row level security;
alter table public.user_settings enable row level security;
alter table public.resumes enable row level security;
alter table public.jobs enable row level security;
alter table public.applications enable row level security;
alter table public.connections enable row level security;
alter table public.connection_secrets enable row level security;
alter table public.connection_lifecycles enable row level security;
alter table public.oauth_states enable row level security;
alter table public.automation_jobs enable row level security;
alter table public.send_events enable row level security;
alter table public.provider_send_events enable row level security;
alter table public.answer_bank enable row level security;
alter table public.audit_events enable row level security;

create policy profiles_select_own on public.profiles for select to authenticated
    using ((select auth.uid()) = user_id);
create policy profiles_insert_own on public.profiles for insert to authenticated
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy profiles_update_own on public.profiles for update to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active())
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy profiles_delete_own on public.profiles for delete to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active());

create policy user_settings_select_own on public.user_settings for select to authenticated
    using ((select auth.uid()) = user_id);
create policy user_settings_insert_own on public.user_settings for insert to authenticated
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy user_settings_update_own on public.user_settings for update to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active())
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy user_settings_delete_own on public.user_settings for delete to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active());

create policy resumes_select_own on public.resumes for select to authenticated
    using ((select auth.uid()) = user_id);

create policy jobs_select_own on public.jobs for select to authenticated
    using ((select auth.uid()) = user_id);
create policy jobs_insert_own on public.jobs for insert to authenticated
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy jobs_update_own on public.jobs for update to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active())
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy jobs_delete_own on public.jobs for delete to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active());

create policy applications_select_own on public.applications for select to authenticated
    using ((select auth.uid()) = user_id);
create policy applications_insert_own on public.applications for insert to authenticated
    with check ((select auth.uid()) = user_id);
create policy applications_update_own on public.applications for update to authenticated
    using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy applications_delete_own on public.applications for delete to authenticated
    using ((select auth.uid()) = user_id);

create policy connections_select_own on public.connections for select to authenticated
    using ((select auth.uid()) = user_id);
create policy connections_delete_own on public.connections for delete to authenticated
    using ((select auth.uid()) = user_id);

create policy automation_jobs_select_own on public.automation_jobs for select to authenticated
    using ((select auth.uid()) = user_id);

create policy send_events_select_own on public.send_events for select to authenticated
    using ((select auth.uid()) = user_id);

create policy answer_bank_select_own on public.answer_bank for select to authenticated
    using ((select auth.uid()) = user_id);
create policy answer_bank_insert_own on public.answer_bank for insert to authenticated
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy answer_bank_update_own on public.answer_bank for update to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active())
    with check ((select auth.uid()) = user_id and public.account_is_active());
create policy answer_bank_delete_own on public.answer_bank for delete to authenticated
    using ((select auth.uid()) = user_id and public.account_is_active());

create policy audit_events_select_own on public.audit_events for select to authenticated
    using ((select auth.uid()) = user_id);

-- Explicit browser grants complement RLS. Server-only tables receive no browser grant.
grant usage on schema public to authenticated, service_role;
revoke all on public.connections, public.automation_jobs, public.send_events,
    public.provider_send_events, public.audit_events from authenticated;
revoke all on public.resumes from authenticated;
grant select on public.profiles to authenticated;
grant update (
    full_name, email, phone, location, headline, summary, years_experience,
    work_authorization, notice_period, linkedin_url, github_url, portfolio_url,
    education, skills, preferences, onboarding_completed
) on public.profiles to authenticated;
grant select, insert, update, delete on public.user_settings to authenticated;
grant select on public.resumes to authenticated;
grant select, insert, update, delete on public.jobs to authenticated;
-- Application state transitions and provider cleanup are server-managed. Browsers can
-- read their own rows through RLS but cannot bypass review or cleanup side effects.
grant select on public.applications to authenticated;
grant select on public.connections to authenticated;
grant select on public.automation_jobs to authenticated;
grant select on public.send_events to authenticated;
grant select, insert, update, delete on public.answer_bank to authenticated;
grant select on public.audit_events to authenticated;

grant all on public.profiles, public.user_settings, public.resumes, public.jobs,
    public.applications, public.connections, public.connection_secrets,
    public.connection_lifecycles, public.oauth_states,
    public.automation_jobs, public.send_events, public.provider_send_events,
    public.answer_bank, public.audit_events
    to service_role;

revoke all on public.connection_secrets from anon, authenticated;
revoke all on public.connection_lifecycles from anon, authenticated;
revoke all on public.oauth_states from anon, authenticated;
revoke all on public.provider_send_events from public, anon, authenticated;
revoke all on public.profiles, public.user_settings, public.resumes, public.jobs,
    public.applications, public.connections, public.automation_jobs, public.send_events,
    public.answer_bank, public.audit_events from anon;

do $migration_assert_server_managed_state$
begin
    if has_table_privilege('authenticated', 'public.applications', 'INSERT')
       or has_table_privilege('authenticated', 'public.applications', 'UPDATE')
       or has_table_privilege('authenticated', 'public.applications', 'DELETE') then
        raise exception 'application mutations must remain server-managed';
    end if;
    if has_table_privilege('authenticated', 'public.connections', 'DELETE') then
        raise exception 'connection cleanup must remain server-managed';
    end if;
    if has_table_privilege('authenticated', 'public.automation_jobs', 'INSERT') then
        raise exception 'automation job insertion must remain RPC-managed';
    end if;
    if has_table_privilege('authenticated', 'public.resumes', 'INSERT')
       or has_table_privilege('authenticated', 'public.resumes', 'UPDATE')
       or has_table_privilege('authenticated', 'public.resumes', 'DELETE') then
        raise exception 'resume mutations must remain API/RPC-managed';
    end if;
    if has_table_privilege('authenticated', 'public.provider_send_events', 'SELECT')
       or has_table_privilege('authenticated', 'public.provider_send_events', 'INSERT')
       or has_table_privilege('authenticated', 'public.provider_send_events', 'UPDATE')
       or has_table_privilege('authenticated', 'public.provider_send_events', 'DELETE') then
        raise exception 'provider send abuse ledger must remain service-role-only';
    end if;
end;
$migration_assert_server_managed_state$;

-- Create an OAuth state and advance its per-user generation atomically.  Only the
-- newest flow can save a token, and starts are paused while a disconnect retry is
-- pending so an old callback cannot recreate the connection.
create or replace function public.create_google_oauth_state(
    user_id_input uuid,
    state_hash_input text,
    return_path_input text,
    pkce_verifier_ciphertext_input text,
    expires_at_input timestamptz
)
returns bigint
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    next_generation bigint;
begin
    if user_id_input is null
       or state_hash_input !~ '^[0-9a-f]{64}$'
       or return_path_input not like '/%'
       or return_path_input like '//%'
       or char_length(return_path_input) > 1024
       or nullif(pkce_verifier_ciphertext_input, '') is null
       or expires_at_input <= clock_timestamp()
       or expires_at_input > clock_timestamp() + interval '30 minutes' then
        raise exception using errcode = '22023', message = 'google_oauth_state_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    insert into public.connection_lifecycles as lifecycle (
        user_id, provider, generation, status
    ) values (user_id_input, 'gmail', 1, 'connecting')
    on conflict (user_id, provider) do update
       set generation = lifecycle.generation + 1,
           status = 'connecting'
     where lifecycle.status <> 'disconnecting'
    returning generation into next_generation;
    if not found then
        raise exception using errcode = 'P0001', message = 'connection_operation_in_progress';
    end if;
    if exists (
        select 1 from public.send_events event
         where event.user_id = user_id_input and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'gmail_send_in_progress';
    end if;

    delete from public.oauth_states state
     where state.user_id = user_id_input and state.provider = 'google';
    insert into public.oauth_states (
        state_hash, user_id, provider, generation, return_path,
        pkce_verifier_ciphertext, expires_at
    ) values (
        state_hash_input, user_id_input, 'google', next_generation,
        return_path_input, pkce_verifier_ciphertext_input, expires_at_input
    );
    return next_generation;
end;
$$;

-- Mark the lifecycle before any external revocation.  Repeated calls return the
-- same generation, making a failed/network-interrupted disconnect safely retryable.
create or replace function public.begin_google_disconnect(user_id_input uuid)
returns bigint
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    lifecycle_generation bigint;
    lifecycle_status text;
    lifecycle_exists boolean;
begin
    if user_id_input is null then
        raise exception using errcode = '22023', message = 'google_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    select lifecycle.generation, lifecycle.status
      into lifecycle_generation, lifecycle_status
     from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail'
     for update;
    lifecycle_exists := found;
    if exists (
        select 1 from public.send_events event
         where event.user_id = user_id_input and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'gmail_send_in_progress';
    end if;
    if not lifecycle_exists then
        lifecycle_generation := 1;
        insert into public.connection_lifecycles (
            user_id, provider, generation, status
        ) values (user_id_input, 'gmail', lifecycle_generation, 'disconnecting');
    elsif lifecycle_status <> 'disconnecting' then
        lifecycle_generation := lifecycle_generation + 1;
        update public.connection_lifecycles lifecycle
           set generation = lifecycle_generation, status = 'disconnecting'
         where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail';
    end if;
    delete from public.oauth_states state
     where state.user_id = user_id_input and state.provider = 'google';
    return lifecycle_generation;
end;
$$;

create or replace function public.finish_google_disconnect(
    user_id_input uuid,
    expected_generation_input bigint
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    if user_id_input is null or expected_generation_input is null
       or expected_generation_input < 1 then
        raise exception using errcode = '22023', message = 'google_connection_invalid';
    end if;
    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = 'gmail'
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'disconnecting'
     for update;
    if not found then
        raise exception using errcode = 'P0001', message = 'connection_operation_stale';
    end if;

    delete from public.connections connection
     where connection.user_id = user_id_input and connection.provider = 'gmail';
    update public.connection_lifecycles lifecycle
       set status = 'disconnected'
     where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail';
    return true;
end;
$$;

revoke all on function public.create_google_oauth_state(uuid, text, text, text, timestamptz)
    from public, anon, authenticated;
revoke all on function public.begin_google_disconnect(uuid)
    from public, anon, authenticated;
revoke all on function public.finish_google_disconnect(uuid, bigint)
    from public, anon, authenticated;
grant execute on function public.create_google_oauth_state(uuid, text, text, text, timestamptz)
    to service_role;
grant execute on function public.begin_google_disconnect(uuid) to service_role;
grant execute on function public.finish_google_disconnect(uuid, bigint) to service_role;

-- A single-use OAuth state is deleted even when expired; only a valid row is returned.
create or replace function public.consume_oauth_state(
    state_hash_input text,
    provider_input text
)
returns setof public.oauth_states
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    return query
    with consumed as (
        delete from public.oauth_states state
        where state.state_hash = state_hash_input
          and state.provider = provider_input
        returning state.*
    )
    select consumed.* from consumed where consumed.expires_at > clock_timestamp();
end;
$$;

revoke all on function public.consume_oauth_state(text, text) from public, anon, authenticated;
grant execute on function public.consume_oauth_state(text, text) to service_role;

-- Register and activate a résumé in one transaction.  The per-user lock prevents
-- concurrent tabs from leaving every résumé inactive or activating two records.
create or replace function public.register_resume(
    storage_path_input text,
    original_name_input text,
    mime_type_input text,
    size_bytes_input bigint,
    sha256_input text default null
)
returns setof public.resumes
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    existing_resume public.resumes%rowtype;
    saved_resume public.resumes%rowtype;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if split_part(storage_path_input, '/', 1) <> current_user_id::text
       or array_length(string_to_array(storage_path_input, '/'), 1) <> 2
       or lower(split_part(storage_path_input, '/', 2)) !~ '^resume-[1-5]\.pdf$'
       or nullif(btrim(original_name_input), '') is null
       or original_name_input ~ '[/\\]'
       or lower(original_name_input) not like '%.pdf'
       or mime_type_input <> 'application/pdf'
       or size_bytes_input is null or size_bytes_input < 1 or size_bytes_input > 6291456
       or (sha256_input is not null and lower(sha256_input) !~ '^[0-9a-f]{64}$') then
        raise exception using errcode = '22023', message = 'resume_registration_invalid';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('resume-register:' || current_user_id::text, 0)
    );
    perform 1 from storage.objects object
     where object.bucket_id = 'resumes' and object.name = storage_path_input
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'resume_object_not_found';
    end if;

    select resume.* into existing_resume
      from public.resumes resume
     where resume.user_id = current_user_id
       and resume.storage_path = storage_path_input
     for update;
    update public.resumes resume
       set is_active = false
     where resume.user_id = current_user_id and resume.is_active
       and (existing_resume.id is null or resume.id <> existing_resume.id);

    if existing_resume.id is null then
        insert into public.resumes (
            user_id, storage_path, original_name, mime_type, size_bytes, sha256,
            parse_status, parse_error, parsed_text, is_active
        ) values (
            current_user_id, storage_path_input, original_name_input,
            mime_type_input, size_bytes_input, lower(sha256_input),
            'uploaded', null, null, true
        ) returning * into saved_resume;
    else
        update public.resumes resume
           set original_name = original_name_input,
               mime_type = mime_type_input,
               size_bytes = size_bytes_input,
               sha256 = lower(sha256_input),
               parse_status = 'uploaded',
               parse_error = null,
               parsed_text = null,
               is_active = true
         where resume.id = existing_resume.id and resume.user_id = current_user_id
         returning * into saved_resume;
    end if;
    return next saved_resume;
end;
$$;

revoke all on function public.register_resume(text, text, text, bigint, text)
    from public, anon;
grant execute on function public.register_resume(text, text, text, bigint, text)
    to authenticated;

-- Approve exactly the immutable content revision shown to the user.  Content edits
-- are guarded by the trigger above and reservations compare both revisions.
create or replace function public.approve_application_revision(
    application_id_input uuid,
    expected_revision_input bigint
)
returns setof public.applications
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    target_application public.applications%rowtype;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if expected_revision_input is null or expected_revision_input < 1 then
        raise exception using errcode = '22023', message = 'application_revision_invalid';
    end if;

    select application.* into target_application
      from public.applications application
     where application.id = application_id_input
       and application.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;
    if target_application.content_revision <> expected_revision_input then
        raise exception using errcode = 'P0001', message = 'application_revision_conflict';
    end if;
    if target_application.channel <> 'email' then
        raise exception using errcode = 'P0001', message = 'application_channel_invalid';
    end if;
    if target_application.status not in ('draft_pending', 'drafted', 'failed', 'approved') then
        raise exception using errcode = 'P0001', message = 'application_status_conflict';
    end if;
    if (
        target_application.recipient is null
        or target_application.recipient !~* '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
        or nullif(btrim(target_application.subject), '') is null
        or nullif(btrim(target_application.body), '') is null
    ) then
        raise exception using errcode = 'P0001', message = 'application_not_sendable';
    end if;

    return query
    update public.applications application
       set status = 'approved', approved_at = clock_timestamp(),
           approved_revision = application.content_revision, last_error = null
     where application.id = target_application.id
       and application.user_id = current_user_id
       and application.content_revision = expected_revision_input
     returning application.*;
end;
$$;

revoke all on function public.approve_application_revision(uuid, bigint)
    from public, anon;
grant execute on function public.approve_application_revision(uuid, bigint)
    to authenticated;

-- Save the public Gmail connection and its encrypted token material in one database
-- transaction.  A reconnect can never display account B while retaining account A's
-- refresh token.
create or replace function public.save_google_connection(
    user_id_input uuid,
    expected_generation_input bigint,
    external_account_id_input text,
    display_name_input text,
    scopes_input text[],
    expires_at_input timestamptz,
    metadata_input jsonb,
    access_token_ciphertext_input text,
    refresh_token_ciphertext_input text,
    token_type_input text
)
returns setof public.connections
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    lifecycle public.connection_lifecycles%rowtype;
    prior_connection public.connections%rowtype;
    prior_refresh_ciphertext text;
    saved_connection public.connections%rowtype;
begin
    if user_id_input is null
       or expected_generation_input is null
       or expected_generation_input < 1
       or nullif(btrim(external_account_id_input), '') is null
       or nullif(btrim(display_name_input), '') is null
       or nullif(btrim(access_token_ciphertext_input), '') is null then
        raise exception using errcode = '22023', message = 'google_connection_invalid';
    end if;
    if scopes_input is null
       or not ('https://www.googleapis.com/auth/gmail.send' = any(scopes_input)) then
        raise exception using errcode = '22023', message = 'google_scope_missing';
    end if;
    if metadata_input is null or jsonb_typeof(metadata_input) <> 'object' then
        raise exception using errcode = '22023', message = 'google_connection_invalid';
    end if;

    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    select row.* into lifecycle
      from public.connection_lifecycles row
     where row.user_id = user_id_input and row.provider = 'gmail'
     for update;
    if not found
       or lifecycle.generation <> expected_generation_input
       or lifecycle.status <> 'connecting' then
        raise exception using errcode = 'P0001', message = 'google_oauth_flow_stale';
    end if;
    if exists (
        select 1 from public.send_events event
         where event.user_id = user_id_input and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'gmail_send_in_progress';
    end if;
    perform pg_advisory_xact_lock(
        hashtextextended('gmail:' || external_account_id_input, 0)
    );
    if exists (
        select 1 from public.connections connection
         where connection.provider = 'gmail'
           and connection.external_account_id = external_account_id_input
           and connection.user_id <> user_id_input
    ) then
        raise exception using errcode = 'P0001', message = 'google_account_already_connected';
    end if;

    select connection.* into prior_connection
      from public.connections connection
     where connection.user_id = user_id_input and connection.provider = 'gmail'
     for update;
    if found then
        select secret.refresh_token_ciphertext into prior_refresh_ciphertext
          from public.connection_secrets secret
         where secret.connection_id = prior_connection.id
           and secret.user_id = user_id_input;
    end if;

    if refresh_token_ciphertext_input is null and (
        prior_connection.id is null
        or prior_connection.external_account_id is distinct from external_account_id_input
        or prior_refresh_ciphertext is null
    ) then
        raise exception using errcode = 'P0001', message = 'google_refresh_token_required';
    end if;

    insert into public.connections (
        user_id, provider, mode, status, external_account_id, display_name,
        scopes, expires_at, last_verified_at, metadata
    ) values (
        user_id_input, 'gmail', 'oauth', 'connected', external_account_id_input,
        display_name_input, scopes_input, expires_at_input, clock_timestamp(), metadata_input
    )
    on conflict (user_id, provider) do update
       set mode = excluded.mode, status = excluded.status,
           external_account_id = excluded.external_account_id,
           display_name = excluded.display_name, scopes = excluded.scopes,
           expires_at = excluded.expires_at,
           last_verified_at = excluded.last_verified_at,
           metadata = excluded.metadata
    returning * into saved_connection;

    insert into public.connection_secrets (
        connection_id, user_id, access_token_ciphertext,
        refresh_token_ciphertext, token_type
    ) values (
        saved_connection.id, user_id_input, access_token_ciphertext_input,
        refresh_token_ciphertext_input, token_type_input
    )
    on conflict (connection_id) do update
       set user_id = excluded.user_id,
           access_token_ciphertext = excluded.access_token_ciphertext,
           refresh_token_ciphertext = coalesce(
               excluded.refresh_token_ciphertext,
               connection_secrets.refresh_token_ciphertext
           ),
           token_type = excluded.token_type;

    update public.connection_lifecycles row
       set status = 'connected'
     where row.user_id = user_id_input and row.provider = 'gmail'
       and row.generation = expected_generation_input;

    return next saved_connection;
end;
$$;

revoke all on function public.save_google_connection(
    uuid, bigint, text, text, text[], timestamptz, jsonb, text, text, text
) from public, anon, authenticated;
grant execute on function public.save_google_connection(
    uuid, bigint, text, text, text[], timestamptz, jsonb, text, text, text
) to service_role;

-- Managed-browser lifecycle operations use the same generation table as Gmail but
-- have provider-specific transactions. External Browserbase calls happen between
-- these transactions; generation checks ensure that a late response can never
-- overwrite a newer start or recreate a connection after disconnect begins.
create or replace function public.begin_browser_start(
    user_id_input uuid,
    provider_input text
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    prior_status text;
    next_generation bigint;
    lifecycle_exists boolean;
    reuse_context boolean := false;
    current_connection public.connections%rowtype;
    current_secret public.connection_secrets%rowtype;
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby') then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    insert into public.user_settings (user_id) values (user_id_input)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings settings
     where settings.user_id = user_id_input
     for update;
    if exists (
        select 1 from public.audit_events event
         where event.user_id = user_id_input
           and event.event_type = 'browser.start'
           and event.created_at >= clock_timestamp() - interval '1 minute'
    ) or (
        select count(*) from public.audit_events event
         where event.user_id = user_id_input
           and event.event_type = 'browser.start'
           and event.created_at >= clock_timestamp() - interval '1 hour'
    ) >= 5 then
        raise exception using errcode = 'P0001', message = 'browser_start_rate_limited';
    end if;
    select lifecycle.status, lifecycle.generation
      into prior_status, next_generation
      from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
     for update;
    lifecycle_exists := found;
    if lifecycle_exists and prior_status = 'disconnecting' then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_in_progress';
    elsif lifecycle_exists then
        next_generation := next_generation + 1;
        reuse_context := prior_status = 'connected';
        update public.connection_lifecycles lifecycle
           set generation = next_generation, status = 'connecting'
         where lifecycle.user_id = user_id_input
           and lifecycle.provider = provider_input;
    else
        next_generation := 1;
        insert into public.connection_lifecycles (
            user_id, provider, generation, status
        ) values (user_id_input, provider_input, next_generation, 'connecting');
    end if;
    insert into public.audit_events (user_id, event_type, resource_type, metadata)
    values (
        user_id_input, 'browser.start', 'connection',
        jsonb_build_object('provider', provider_input, 'generation', next_generation)
    );

    select connection.* into current_connection
      from public.connections connection
     where connection.user_id = user_id_input
       and connection.provider = provider_input
     for update;
    if found then
        if not lifecycle_exists then
            reuse_context := current_connection.status in ('connected', 'needs_attention');
        end if;
        select secret.* into current_secret
          from public.connection_secrets secret
         where secret.connection_id = current_connection.id
           and secret.user_id = user_id_input
         for update;
    end if;

    return jsonb_build_object(
        'generation', next_generation,
        'connection_id', current_connection.id,
        'context_ciphertext', current_secret.browser_context_id_ciphertext,
        'session_ciphertext', current_secret.browser_session_id_ciphertext,
        'reuse_context', reuse_context
    );
end;
$$;

create or replace function public.save_browser_connection_context(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    display_name_input text,
    context_ciphertext_input text
)
returns setof public.connections
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    saved_connection public.connections%rowtype;
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby')
       or expected_generation_input is null or expected_generation_input < 1
       or nullif(btrim(display_name_input), '') is null
       or nullif(btrim(context_ciphertext_input), '') is null then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'connecting'
     for update;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;

    insert into public.connections (
        user_id, provider, mode, status, display_name, last_verified_at, metadata
    ) values (
        user_id_input, provider_input, 'managed_browser', 'pending',
        display_name_input, null,
        '{"provider_login_verified":false,"login_confirmation":"pending"}'::jsonb
    )
    on conflict (user_id, provider) do update
       set mode = excluded.mode, status = excluded.status,
           display_name = excluded.display_name, last_verified_at = null,
           metadata = excluded.metadata
    returning * into saved_connection;

    insert into public.connection_secrets (
        connection_id, user_id, browser_context_id_ciphertext,
        browser_session_id_ciphertext, browser_lifecycle_generation
    ) values (
        saved_connection.id, user_id_input, context_ciphertext_input, null,
        expected_generation_input
    )
    on conflict (connection_id) do update
       set user_id = excluded.user_id,
           browser_context_id_ciphertext = excluded.browser_context_id_ciphertext,
           browser_session_id_ciphertext = null,
           browser_lifecycle_generation = excluded.browser_lifecycle_generation;
    return next saved_connection;
end;
$$;

create or replace function public.save_browser_connection_session(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    expected_connection_id_input uuid,
    expected_context_ciphertext_input text,
    session_ciphertext_input text
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby')
       or expected_generation_input is null or expected_generation_input < 1
       or expected_connection_id_input is null
       or nullif(btrim(expected_context_ciphertext_input), '') is null
       or nullif(btrim(session_ciphertext_input), '') is null then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'connecting'
     for update;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;

    update public.connection_secrets secret
       set browser_session_id_ciphertext = session_ciphertext_input
     where secret.connection_id = expected_connection_id_input
       and secret.user_id = user_id_input
       and secret.browser_lifecycle_generation = expected_generation_input
       and secret.browser_context_id_ciphertext = expected_context_ciphertext_input
       and exists (
           select 1 from public.connections connection
            where connection.id = secret.connection_id
              and connection.user_id = user_id_input
              and connection.provider = provider_input
              and connection.mode = 'managed_browser'
       );
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;
    return true;
end;
$$;

-- Re-check ownership after Browserbase returns the Live View URL. Without this
-- final fence, a request superseded between session persistence and the external
-- debug lookup could return a Live View that a newer start is already deleting.
create or replace function public.confirm_browser_start(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    expected_connection_id_input uuid,
    expected_context_ciphertext_input text,
    expected_session_ciphertext_input text
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby')
       or expected_generation_input is null or expected_generation_input < 1
       or expected_connection_id_input is null
       or nullif(btrim(expected_context_ciphertext_input), '') is null
       or nullif(btrim(expected_session_ciphertext_input), '') is null then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    perform 1
      from public.connection_lifecycles lifecycle
      join public.connections connection
        on connection.user_id = lifecycle.user_id
       and connection.provider = lifecycle.provider
      join public.connection_secrets secret
        on secret.connection_id = connection.id
       and secret.user_id = lifecycle.user_id
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'connecting'
       and connection.id = expected_connection_id_input
       and connection.mode = 'managed_browser'
       and secret.browser_lifecycle_generation = expected_generation_input
       and secret.browser_context_id_ciphertext = expected_context_ciphertext_input
       and secret.browser_session_id_ciphertext = expected_session_ciphertext_input
     for share of lifecycle, connection, secret;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;
    return true;
end;
$$;

-- A failed start may clear only the resources owned by its still-current
-- generation. Returning false for a stale generation lets the API preserve the
-- newer operation while still cleaning its own remote Browserbase resources.
create or replace function public.abort_browser_start(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    expected_connection_id_input uuid,
    expected_session_ciphertext_input text,
    drop_connection_input boolean
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby')
       or expected_generation_input is null or expected_generation_input < 1
       or drop_connection_input is null then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'connecting'
     for update;
    if not found then
        return false;
    end if;

    if drop_connection_input and expected_connection_id_input is not null then
        delete from public.connections connection
         where connection.id = expected_connection_id_input
           and connection.user_id = user_id_input
           and connection.provider = provider_input;
    elsif expected_connection_id_input is not null then
        update public.connection_secrets secret
           set browser_session_id_ciphertext = null
         where secret.connection_id = expected_connection_id_input
           and secret.user_id = user_id_input
           and secret.browser_lifecycle_generation = expected_generation_input
           and (
               expected_session_ciphertext_input is null
               or secret.browser_session_id_ciphertext = expected_session_ciphertext_input
           );
        update public.connections connection
           set status = 'needs_attention', last_verified_at = null,
               metadata = '{"provider_login_verified":false,"login_confirmation":"start_failed"}'::jsonb
         where connection.id = expected_connection_id_input
           and connection.user_id = user_id_input
           and connection.provider = provider_input
           and connection.mode = 'managed_browser';
    end if;

    update public.connection_lifecycles lifecycle
       set status = case when exists (
               select 1 from public.connections connection
                where connection.user_id = user_id_input
                  and connection.provider = provider_input
           ) then 'connected' else 'disconnected' end
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input;
    return true;
end;
$$;

create or replace function public.finish_browser_start(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    expected_connection_id_input uuid,
    expected_session_ciphertext_input text
)
returns setof public.connections
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    saved_connection public.connections%rowtype;
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby')
       or expected_generation_input is null or expected_generation_input < 1
       or expected_connection_id_input is null
       or nullif(btrim(expected_session_ciphertext_input), '') is null then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'connecting'
     for update;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;

    update public.connection_secrets secret
       set browser_session_id_ciphertext = null
     where secret.connection_id = expected_connection_id_input
       and secret.user_id = user_id_input
       and secret.browser_lifecycle_generation = expected_generation_input
       and secret.browser_session_id_ciphertext = expected_session_ciphertext_input;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;
    update public.connections connection
       set status = 'needs_attention', last_verified_at = null,
           metadata = '{"login_confirmed_by_user":true,"provider_login_verified":false,"login_confirmation":"session_saved_unverified"}'::jsonb
     where connection.id = expected_connection_id_input
       and connection.user_id = user_id_input
       and connection.provider = provider_input
       and connection.mode = 'managed_browser'
     returning connection.* into saved_connection;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;
    update public.connection_lifecycles lifecycle
       set status = 'connected'
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input;
    return next saved_connection;
end;
$$;

-- Disconnect begins before any remote cleanup and is idempotent while cleanup is
-- incomplete. Its generation invalidates every in-flight start. The finish step
-- deletes only the exact connection row captured by begin.
create or replace function public.begin_browser_disconnect(
    user_id_input uuid,
    provider_input text
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    lifecycle_generation bigint;
    lifecycle_status text;
    current_connection public.connections%rowtype;
    current_secret public.connection_secrets%rowtype;
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby') then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    select lifecycle.generation, lifecycle.status
      into lifecycle_generation, lifecycle_status
      from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
     for update;
    if not found then
        lifecycle_generation := 1;
        insert into public.connection_lifecycles (
            user_id, provider, generation, status
        ) values (user_id_input, provider_input, lifecycle_generation, 'disconnecting');
    elsif lifecycle_status <> 'disconnecting' then
        lifecycle_generation := lifecycle_generation + 1;
        update public.connection_lifecycles lifecycle
           set generation = lifecycle_generation, status = 'disconnecting'
         where lifecycle.user_id = user_id_input
           and lifecycle.provider = provider_input;
    end if;

    select connection.* into current_connection
      from public.connections connection
     where connection.user_id = user_id_input
       and connection.provider = provider_input
     for update;
    if found then
        select secret.* into current_secret
          from public.connection_secrets secret
         where secret.connection_id = current_connection.id
           and secret.user_id = user_id_input
         for update;
    end if;
    return jsonb_build_object(
        'generation', lifecycle_generation,
        'connection_id', current_connection.id,
        'context_ciphertext', current_secret.browser_context_id_ciphertext,
        'session_ciphertext', current_secret.browser_session_id_ciphertext
    );
end;
$$;

create or replace function public.finish_browser_disconnect(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    expected_connection_id_input uuid
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_connection_id uuid;
begin
    if user_id_input is null or provider_input is null
       or provider_input not in ('greenhouse', 'lever', 'ashby')
       or expected_generation_input is null or expected_generation_input < 1 then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input,
        0
    ));
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input
       and lifecycle.status = 'disconnecting'
     for update;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;

    select connection.id into current_connection_id
      from public.connections connection
     where connection.user_id = user_id_input
       and connection.provider = provider_input
     for update;
    if current_connection_id is distinct from expected_connection_id_input then
        raise exception using
            errcode = 'P0001', message = 'browser_connection_operation_stale';
    end if;
    if expected_connection_id_input is not null then
        delete from public.connections connection
         where connection.id = expected_connection_id_input
           and connection.user_id = user_id_input
           and connection.provider = provider_input;
    end if;
    update public.connection_lifecycles lifecycle
       set status = 'disconnected'
     where lifecycle.user_id = user_id_input
       and lifecycle.provider = provider_input
       and lifecycle.generation = expected_generation_input;
    return true;
end;
$$;

revoke all on function public.begin_browser_start(uuid, text)
    from public, anon, authenticated;
revoke all on function public.save_browser_connection_context(uuid, text, bigint, text, text)
    from public, anon, authenticated;
revoke all on function public.save_browser_connection_session(uuid, text, bigint, uuid, text, text)
    from public, anon, authenticated;
revoke all on function public.confirm_browser_start(uuid, text, bigint, uuid, text, text)
    from public, anon, authenticated;
revoke all on function public.abort_browser_start(uuid, text, bigint, uuid, text, boolean)
    from public, anon, authenticated;
revoke all on function public.finish_browser_start(uuid, text, bigint, uuid, text)
    from public, anon, authenticated;
revoke all on function public.begin_browser_disconnect(uuid, text)
    from public, anon, authenticated;
revoke all on function public.finish_browser_disconnect(uuid, text, bigint, uuid)
    from public, anon, authenticated;
grant execute on function public.begin_browser_start(uuid, text) to service_role;
grant execute on function public.save_browser_connection_context(uuid, text, bigint, text, text)
    to service_role;
grant execute on function public.save_browser_connection_session(uuid, text, bigint, uuid, text, text)
    to service_role;
grant execute on function public.confirm_browser_start(uuid, text, bigint, uuid, text, text)
    to service_role;
grant execute on function public.abort_browser_start(uuid, text, bigint, uuid, text, boolean)
    to service_role;
grant execute on function public.finish_browser_start(uuid, text, bigint, uuid, text)
    to service_role;
grant execute on function public.begin_browser_disconnect(uuid, text) to service_role;
grant execute on function public.finish_browser_disconnect(uuid, text, bigint, uuid)
    to service_role;

-- Queue insertion is an authenticated RPC so users cannot bypass operation and
-- capacity checks through the Supabase REST endpoint.
create or replace function public.enqueue_automation_job(
    kind_input text,
    provider_input text,
    application_id_input uuid,
    payload_input jsonb,
    idempotency_key_input text
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    existing_job public.automation_jobs%rowtype;
    active_count integer;
    daily_count integer;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if kind_input not in ('manual_handoff', 'ats_prepare', 'connection_check') then
        raise exception using errcode = '22023', message = 'automation_kind_invalid';
    end if;
    if provider_input is null or provider_input not in (
        'gmail', 'linkedin', 'yc', 'cutshort', 'ziprecruiter', 'indeed',
        'external_job_board', 'greenhouse', 'lever', 'ashby'
    ) then
        raise exception using errcode = '22023', message = 'automation_provider_invalid';
    end if;
    if kind_input = 'ats_prepare' and provider_input not in ('greenhouse', 'lever', 'ashby') then
        raise exception using errcode = 'P0001', message = 'provider_automation_unavailable';
    end if;
    if kind_input in ('manual_handoff', 'ats_prepare') and application_id_input is null then
        raise exception using errcode = '22023', message = 'application_required';
    end if;
    if application_id_input is not null and not exists (
        select 1 from public.applications application
         where application.id = application_id_input
           and application.user_id = current_user_id
    ) then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;
    if payload_input is null or jsonb_typeof(payload_input) <> 'object'
       or octet_length(payload_input::text) > 32768 then
        raise exception using errcode = '22023', message = 'automation_payload_invalid';
    end if;
    if idempotency_key_input is null
       or idempotency_key_input !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$' then
        raise exception using errcode = '22023', message = 'idempotency_key_invalid';
    end if;

    select job.* into existing_job
      from public.automation_jobs job
     where job.user_id = current_user_id
       and job.idempotency_key = idempotency_key_input;
    if found then
        if existing_job.kind is distinct from kind_input
           or existing_job.provider is distinct from provider_input
           or existing_job.application_id is distinct from application_id_input
           or existing_job.payload is distinct from payload_input then
            raise exception using errcode = '23505', message = 'idempotency_key_conflict';
        end if;
        return next existing_job;
        return;
    end if;

    insert into public.user_settings (user_id) values (current_user_id)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings settings
     where settings.user_id = current_user_id for update;

    delete from public.automation_jobs job
     where job.user_id = current_user_id
       and job.status in ('succeeded', 'failed', 'cancelled', 'needs_attention')
       and job.updated_at < clock_timestamp() - interval '90 days';

    select count(*) into active_count
      from public.automation_jobs job
     where job.user_id = current_user_id and job.status in ('queued', 'running');
    if active_count >= 20 then
        raise exception using errcode = 'P0001', message = 'automation_queue_full';
    end if;
    select count(*) into daily_count
      from public.automation_jobs job
     where job.user_id = current_user_id
       and job.created_at >= clock_timestamp() - interval '24 hours';
    if daily_count >= 100 then
        raise exception using errcode = 'P0001', message = 'automation_daily_limit_reached';
    end if;

    return query
    insert into public.automation_jobs (
        user_id, application_id, kind, provider, payload, idempotency_key
    ) values (
        current_user_id, application_id_input, kind_input, provider_input,
        payload_input, idempotency_key_input
    )
    returning automation_jobs.*;
end;
$$;

revoke all on function public.enqueue_automation_job(text, text, uuid, jsonb, text)
    from public, anon;
grant execute on function public.enqueue_automation_job(text, text, uuid, jsonb, text)
    to authenticated;

-- Fixed-action rate reservations prevent a caller from choosing its own limits.
create or replace function public.reserve_google_oauth_start()
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    insert into public.user_settings (user_id) values (current_user_id)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings where user_id = current_user_id for update;
    if exists (
        select 1 from public.audit_events event
         where event.user_id = current_user_id and event.event_type = 'oauth.google.start'
           and event.created_at >= clock_timestamp() - interval '30 seconds'
    ) or (
        select count(*) from public.audit_events event
         where event.user_id = current_user_id and event.event_type = 'oauth.google.start'
           and event.created_at >= clock_timestamp() - interval '1 hour'
    ) >= 10 then
        raise exception using errcode = 'P0001', message = 'oauth_start_rate_limited';
    end if;
    insert into public.audit_events (user_id, event_type)
    values (current_user_id, 'oauth.google.start');
    return true;
end;
$$;

create or replace function public.reserve_resume_parse(resume_id_input uuid)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if not exists (
        select 1 from public.resumes resume
         where resume.id = resume_id_input and resume.user_id = current_user_id
    ) then
        raise exception using errcode = 'P0002', message = 'resume_not_found';
    end if;
    insert into public.user_settings (user_id) values (current_user_id)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings where user_id = current_user_id for update;
    if exists (
        select 1 from public.audit_events event
         where event.user_id = current_user_id and event.event_type = 'resume.parse'
           and event.created_at >= clock_timestamp() - interval '15 seconds'
    ) or (
        select count(*) from public.audit_events event
         where event.user_id = current_user_id and event.event_type = 'resume.parse'
           and event.created_at >= clock_timestamp() - interval '1 hour'
    ) >= 20 then
        raise exception using errcode = 'P0001', message = 'resume_parse_rate_limited';
    end if;
    insert into public.audit_events (user_id, event_type, resource_type, resource_id)
    values (current_user_id, 'resume.parse', 'resume', resume_id_input);
    return true;
end;
$$;

create or replace function public.reserve_groq_request(operation_input text)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    action_name text;
    minimum_interval interval;
    hourly_limit integer;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if operation_input = 'validate' then
        action_name := 'groq.validate';
        minimum_interval := interval '5 seconds';
        hourly_limit := 30;
    elsif operation_input = 'generate' then
        action_name := 'groq.generate';
        minimum_interval := interval '2 seconds';
        hourly_limit := 60;
    else
        raise exception using errcode = '22023', message = 'groq_operation_invalid';
    end if;
    insert into public.user_settings (user_id) values (current_user_id)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings where user_id = current_user_id for update;
    if exists (
        select 1 from public.audit_events event
         where event.user_id = current_user_id and event.event_type = action_name
           and event.created_at >= clock_timestamp() - minimum_interval
    ) or (
        select count(*) from public.audit_events event
         where event.user_id = current_user_id and event.event_type = action_name
           and event.created_at >= clock_timestamp() - interval '1 hour'
    ) >= hourly_limit then
        raise exception using errcode = 'P0001', message = 'groq_request_rate_limited';
    end if;
    insert into public.audit_events (user_id, event_type)
    values (current_user_id, action_name);
    return true;
end;
$$;

revoke all on function public.reserve_google_oauth_start() from public, anon;
revoke all on function public.reserve_resume_parse(uuid) from public, anon;
revoke all on function public.reserve_groq_request(text) from public, anon;
grant execute on function public.reserve_google_oauth_start() to authenticated;
grant execute on function public.reserve_resume_parse(uuid) to authenticated;
grant execute on function public.reserve_groq_request(text) to authenticated;

-- Claim one due job atomically, recovering expired leases first.
create or replace function public.claim_automation_job(
    worker_id text,
    lease_seconds integer default 120,
    kinds text[] default null
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_worker_id alias for $1;
    p_lease_seconds alias for $2;
    p_kinds alias for $3;
    claimed_id uuid;
    timestamp_now timestamptz := clock_timestamp();
begin
    if nullif(btrim(p_worker_id), '') is null or char_length(p_worker_id) > 128 then
        raise exception using errcode = '22023', message = 'worker_id_invalid';
    end if;
    if p_lease_seconds is null or p_lease_seconds < 15 or p_lease_seconds > 3600 then
        raise exception using errcode = '22023', message = 'lease_seconds_invalid';
    end if;

    update public.automation_jobs job
       set status = 'cancelled', locked_by = null, locked_at = null,
           lease_expires_at = null, updated_at = timestamp_now
     where job.status = 'queued' and job.cancel_requested_at is not null;

    update public.automation_jobs job
       set status = case
               when job.cancel_requested_at is not null then 'cancelled'
               when job.attempts >= job.max_attempts then 'failed'
               else 'queued'
           end,
           error_code = case
               when job.cancel_requested_at is not null then job.error_code
               when job.attempts >= job.max_attempts then 'lease_expired'
               else job.error_code
           end,
           error_message = case
               when job.attempts >= job.max_attempts then 'The worker lease expired.'
               else job.error_message
           end,
           run_after = case
               when job.cancel_requested_at is null and job.attempts < job.max_attempts
                   then timestamp_now
               else job.run_after
           end,
           locked_by = null, locked_at = null, lease_expires_at = null,
           updated_at = timestamp_now
     where job.status = 'running' and job.lease_expires_at < timestamp_now;

    select job.id into claimed_id
      from public.automation_jobs job
     where job.status = 'queued'
       and job.cancel_requested_at is null
       and job.run_after <= timestamp_now
       and job.attempts < job.max_attempts
       and (coalesce(cardinality(p_kinds), 0) = 0 or job.kind = any (p_kinds))
       and (
           job.provider is null or not exists (
               select 1 from public.automation_jobs active
                where active.user_id = job.user_id
                  and active.provider = job.provider
                  and active.status = 'running'
           )
       )
     order by job.run_after, job.created_at, job.id
     for update skip locked
     limit 1;

    if claimed_id is null then
        return;
    end if;

    return query
    update public.automation_jobs job
       set status = 'running', attempts = job.attempts + 1,
           locked_by = p_worker_id, locked_at = timestamp_now,
           lease_expires_at = timestamp_now + make_interval(secs => p_lease_seconds),
           error_code = null, error_message = null, updated_at = timestamp_now
     where job.id = claimed_id
     returning job.*;
end;
$$;

create or replace function public.heartbeat_automation_job(
    job_id uuid,
    worker_id text,
    lease_seconds integer default 120
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_lease_seconds alias for $3;
    timestamp_now timestamptz := clock_timestamp();
begin
    if p_lease_seconds is null or p_lease_seconds < 15 or p_lease_seconds > 3600 then
        raise exception using errcode = '22023', message = 'lease_seconds_invalid';
    end if;
    return query
    update public.automation_jobs job
       set status = case when job.cancel_requested_at is null then 'running' else 'cancelled' end,
           lease_expires_at = case
               when job.cancel_requested_at is null
                   then timestamp_now + make_interval(secs => p_lease_seconds)
               else null
           end,
           locked_by = case when job.cancel_requested_at is null then job.locked_by else null end,
           locked_at = case when job.cancel_requested_at is null then job.locked_at else null end,
           updated_at = timestamp_now
     where job.id = p_job_id and job.status = 'running'
       and job.locked_by = p_worker_id and job.lease_expires_at >= timestamp_now
     returning job.*;
end;
$$;

-- Authenticated cancellation is deliberately narrower than a table UPDATE policy.
-- Queued work is cancelled synchronously; running work is cancelled cooperatively.
create or replace function public.cancel_automation_job(job_id uuid)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    current_user_id uuid := public.assert_active_user();
    target_job public.automation_jobs%rowtype;
    timestamp_now timestamptz := clock_timestamp();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;

    select job.* into target_job
      from public.automation_jobs job
     where job.id = p_job_id and job.user_id = current_user_id
     for update;
    if not found then
        return;
    end if;

    if target_job.status not in ('queued', 'running') then
        return next target_job;
        return;
    end if;

    return query
    update public.automation_jobs job
       set status = case when job.status = 'queued' then 'cancelled' else job.status end,
           cancel_requested_at = coalesce(job.cancel_requested_at, timestamp_now),
           locked_by = case when job.status = 'queued' then null else job.locked_by end,
           locked_at = case when job.status = 'queued' then null else job.locked_at end,
           lease_expires_at = case
               when job.status = 'queued' then null else job.lease_expires_at
           end,
           updated_at = timestamp_now
     where job.id = p_job_id and job.user_id = current_user_id
       and job.status in ('queued', 'running')
     returning job.*;
end;
$$;

create or replace function public.complete_automation_job(
    job_id uuid,
    worker_id text,
    result jsonb,
    terminal_status text default 'succeeded'
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_result alias for $3;
    p_terminal_status alias for $4;
    timestamp_now timestamptz := clock_timestamp();
begin
    if p_terminal_status not in ('succeeded', 'needs_attention') then
        raise exception using errcode = '22023', message = 'terminal_status_invalid';
    end if;
    if p_result is not null and jsonb_typeof(p_result) <> 'object' then
        raise exception using errcode = '22023', message = 'job_result_invalid';
    end if;
    return query
    update public.automation_jobs job
       set status = p_terminal_status, result = coalesce(p_result, '{}'::jsonb),
           progress = '{}'::jsonb, error_code = null, error_message = null,
           locked_by = null, locked_at = null, lease_expires_at = null,
           updated_at = timestamp_now
     where job.id = p_job_id and job.status = 'running'
       and job.locked_by = p_worker_id and job.lease_expires_at >= timestamp_now
       and job.cancel_requested_at is null
     returning job.*;
end;
$$;

create or replace function public.fail_automation_job(
    job_id uuid,
    worker_id text,
    error_code text,
    error_message text,
    retry_after_seconds integer default 0
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_error_code alias for $3;
    p_error_message alias for $4;
    p_retry_after_seconds alias for $5;
    timestamp_now timestamptz := clock_timestamp();
begin
    if p_error_code is null or p_error_code !~ '^[a-z][a-z0-9_]{1,63}$' then
        raise exception using errcode = '22023', message = 'job_error_code_invalid';
    end if;
    if p_error_message is null or char_length(p_error_message) > 500 then
        raise exception using errcode = '22023', message = 'job_error_message_invalid';
    end if;
    if p_retry_after_seconds is null or p_retry_after_seconds < 0 or p_retry_after_seconds > 86400 then
        raise exception using errcode = '22023', message = 'retry_delay_invalid';
    end if;
    return query
    update public.automation_jobs job
       set status = case
               when job.cancel_requested_at is not null then 'cancelled'
               when p_retry_after_seconds > 0 and job.attempts < job.max_attempts then 'queued'
               else 'failed'
           end,
           run_after = case
               when p_retry_after_seconds > 0 and job.attempts < job.max_attempts
                   then timestamp_now + make_interval(secs => p_retry_after_seconds)
               else job.run_after
           end,
           error_code = p_error_code, error_message = p_error_message,
           locked_by = null, locked_at = null, lease_expires_at = null,
           updated_at = timestamp_now
     where job.id = p_job_id and job.status = 'running'
       and job.locked_by = p_worker_id and job.lease_expires_at >= timestamp_now
     returning job.*;
end;
$$;

revoke all on function public.claim_automation_job(text, integer, text[]) from public, anon, authenticated;
revoke all on function public.heartbeat_automation_job(uuid, text, integer) from public, anon, authenticated;
revoke all on function public.cancel_automation_job(uuid) from public, anon, authenticated;
revoke all on function public.complete_automation_job(uuid, text, jsonb, text) from public, anon, authenticated;
revoke all on function public.fail_automation_job(uuid, text, text, text, integer) from public, anon, authenticated;
grant execute on function public.claim_automation_job(text, integer, text[]) to service_role;
grant execute on function public.heartbeat_automation_job(uuid, text, integer) to service_role;
grant execute on function public.cancel_automation_job(uuid) to authenticated;
grant execute on function public.complete_automation_job(uuid, text, jsonb, text) to service_role;
grant execute on function public.fail_automation_job(uuid, text, text, text, integer) to service_role;

-- Fail deployment if future default privileges accidentally broaden cancellation.
do $migration_assert_cancel_permissions$
begin
    if has_table_privilege('authenticated', 'public.automation_jobs', 'UPDATE') then
        raise exception 'authenticated must not have automation_jobs UPDATE privilege';
    end if;
    if not has_function_privilege(
        'authenticated', 'public.cancel_automation_job(uuid)', 'EXECUTE'
    ) then
        raise exception 'authenticated must be able to execute cancel_automation_job';
    end if;
end;
$migration_assert_cancel_permissions$;

-- Atomically enforce approval, idempotency, tenant limits, and provider-account
-- limits.  The provider ledger and advisory lock prevent deleting/recreating an
-- AutoApply account from resetting Gmail's rolling cap or duplicate window.
create or replace function public.reserve_application_send(
    application_id uuid,
    idempotency_key text
)
returns setof public.send_events
language plpgsql
security definer
set search_path = 'public', 'extensions'
as $$
declare
    p_application_id alias for $1;
    p_idempotency_key alias for $2;
    current_user_id uuid := public.assert_active_user();
    target_application public.applications%rowtype;
    tenant_settings public.user_settings%rowtype;
    gmail_connection public.connections%rowtype;
    existing_event public.send_events%rowtype;
    new_event public.send_events%rowtype;
    address_hash text;
    account_hash text;
    sent_count integer;
    provider_sent_count integer;
    day_start timestamptz;
    reservation_time timestamptz := clock_timestamp();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if p_idempotency_key is null or char_length(p_idempotency_key) not between 8 and 200 then
        raise exception using errcode = '22023', message = 'idempotency_key_invalid';
    end if;

    select application.* into target_application
      from public.applications application
     where application.id = p_application_id and application.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;

    select event.* into existing_event
      from public.send_events event
     where event.user_id = current_user_id and event.idempotency_key = p_idempotency_key;
    if found then
        if existing_event.application_id is distinct from target_application.id then
            raise exception using errcode = '23505', message = 'idempotency_key_conflict';
        end if;
        if existing_event.outcome = 'pending_provider' then
            raise exception using errcode = 'P0001', message = 'send_in_progress';
        end if;
        return next existing_event;
        return;
    end if;

    if target_application.channel <> 'email'
       or target_application.status <> 'approved'
       or target_application.approved_at is null
       or target_application.approved_revision is distinct from target_application.content_revision
       or nullif(btrim(target_application.recipient), '') is null
       or target_application.recipient !~* '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
       or nullif(btrim(target_application.subject), '') is null
       or nullif(btrim(target_application.body), '') is null then
        raise exception using errcode = 'P0001', message = 'application_not_sendable';
    end if;

    -- Hold the lifecycle row through reservation commit. OAuth save/disconnect
    -- transactions take an update lock and reject once the pending event exists, so
    -- the sender account and encrypted token set cannot change before finalization.
    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = current_user_id
       and lifecycle.provider = 'gmail'
       and lifecycle.status = 'connected'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'gmail_not_connected';
    end if;

    select connection.* into gmail_connection
      from public.connections connection
     where connection.user_id = current_user_id
       and connection.provider = 'gmail'
       and connection.status = 'connected'
       and nullif(btrim(connection.external_account_id), '') is not null
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'gmail_not_connected';
    end if;

    select settings.* into tenant_settings
      from public.user_settings settings
     where settings.user_id = current_user_id
     for update;
    if not found then
        insert into public.user_settings (user_id) values (current_user_id)
        returning * into tenant_settings;
    end if;

    begin
        day_start := date_trunc(
            'day', reservation_time at time zone tenant_settings.timezone
        ) at time zone tenant_settings.timezone;
    exception when invalid_parameter_value then
        day_start := date_trunc('day', reservation_time at time zone 'UTC') at time zone 'UTC';
    end;

    select count(*) into sent_count
      from public.send_events event
     where event.user_id = current_user_id
       and event.outcome in ('pending_provider', 'sent', 'needs_attention')
       and event.created_at >= day_start;
    if sent_count >= tenant_settings.daily_send_cap then
        raise exception using errcode = 'P0001', message = 'daily_send_cap_reached';
    end if;

    address_hash := encode(
        extensions.digest(
            'gmail-recipient:' || lower(btrim(target_application.recipient)), 'sha256'
        ),
        'hex'
    );
    account_hash := encode(
        extensions.digest('gmail:' || btrim(gmail_connection.external_account_id), 'sha256'),
        'hex'
    );

    -- Serialize all reservations for this Google subject, even if it is later linked
    -- to a freshly-created AutoApply user. Expired pseudonymous rows are ignored by
    -- every limit query and removed globally on this reservation hot path.
    perform pg_advisory_xact_lock(hashtextextended(account_hash, 0));
    delete from public.provider_send_events provider_event
     where provider_event.expires_at <= reservation_time;

    select count(*) into provider_sent_count
      from public.provider_send_events provider_event
     where provider_event.provider = 'gmail'
       and provider_event.provider_account_hash = account_hash
       and provider_event.outcome in ('pending_provider', 'sent', 'needs_attention')
       and provider_event.created_at >= reservation_time - interval '24 hours'
       and provider_event.expires_at > reservation_time;
    if provider_sent_count >= 25 then
        raise exception using errcode = 'P0001', message = 'provider_daily_send_cap_reached';
    end if;

    if exists (
        select 1 from public.send_events event
         where event.user_id = current_user_id
           and event.recipient_hash = address_hash
           and event.outcome in ('pending_provider', 'sent', 'needs_attention')
           and event.created_at >= reservation_time
               - make_interval(days => tenant_settings.duplicate_window_days)
    ) then
        raise exception using errcode = 'P0001', message = 'duplicate_recipient_window';
    end if;
    if exists (
        select 1 from public.provider_send_events provider_event
         where provider_event.provider = 'gmail'
           and provider_event.provider_account_hash = account_hash
           and provider_event.recipient_hash = address_hash
           and provider_event.outcome in ('pending_provider', 'sent', 'needs_attention')
           and provider_event.expires_at > reservation_time
    ) then
        raise exception using errcode = 'P0001', message = 'duplicate_recipient_window';
    end if;

    update public.applications application
       set status = 'queued', send_idempotency_key = p_idempotency_key,
           last_error = null, updated_at = reservation_time
     where application.id = target_application.id;

    insert into public.send_events (
        user_id, application_id, provider, outcome, recipient_hash, idempotency_key,
        created_at, updated_at
    ) values (
        current_user_id, target_application.id, 'gmail', 'pending_provider',
        address_hash, p_idempotency_key, reservation_time, reservation_time
    )
    returning * into new_event;

    insert into public.provider_send_events (
        send_event_id, provider, provider_account_hash, recipient_hash, outcome,
        created_at, updated_at, expires_at
    ) values (
        new_event.id, 'gmail', account_hash, address_hash, 'pending_provider',
        reservation_time, reservation_time,
        reservation_time + make_interval(days => tenant_settings.duplicate_window_days)
    );

    return next new_event;
    return;
end;
$$;

-- A stale reservation is never made retryable automatically.  It moves to a
-- needs-attention state so the user can inspect Gmail before choosing what to do.
create or replace function public.reconcile_stale_application_send(
    application_id_input uuid
)
returns setof public.send_events
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    target_application public.applications%rowtype;
    target_event public.send_events%rowtype;
    timestamp_now timestamptz := clock_timestamp();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    select application.* into target_application
      from public.applications application
     where application.id = application_id_input
       and application.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;
    select event.* into target_event
      from public.send_events event
     where event.application_id = target_application.id
       and event.user_id = current_user_id
     order by event.created_at desc
     limit 1
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'send_reservation_not_found';
    end if;
    if target_event.outcome <> 'pending_provider' then
        return next target_event;
        return;
    end if;
    if target_event.updated_at > timestamp_now - interval '15 minutes' then
        raise exception using errcode = 'P0001', message = 'send_reconciliation_too_early';
    end if;

    update public.applications application
       set status = 'failed', last_error = 'gmail_send_unconfirmed',
           updated_at = timestamp_now
     where application.id = target_application.id
       and application.user_id = current_user_id;
    update public.provider_send_events provider_event
       set outcome = 'needs_attention', error_code = 'gmail_send_unconfirmed',
           updated_at = timestamp_now
     where provider_event.send_event_id = target_event.id;
    return query
    update public.send_events event
       set outcome = 'needs_attention', error_code = 'gmail_send_unconfirmed',
           updated_at = timestamp_now
     where event.id = target_event.id and event.user_id = current_user_id
     returning event.*;
end;
$$;

create or replace function public.finalize_application_send(
    application_id uuid,
    idempotency_key text,
    outcome text,
    provider_message_id text default null,
    provider_thread_id text default null,
    error_code text default null
)
returns setof public.send_events
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_application_id alias for $1;
    p_idempotency_key alias for $2;
    p_outcome alias for $3;
    p_provider_message_id alias for $4;
    p_provider_thread_id alias for $5;
    p_error_code alias for $6;
    existing_event public.send_events%rowtype;
    event_user_id uuid;
    timestamp_now timestamptz := clock_timestamp();
begin
    if p_outcome not in ('sent', 'failed', 'needs_attention') then
        raise exception using errcode = '22023', message = 'send_outcome_invalid';
    end if;
    if p_error_code is not null and p_error_code !~ '^[a-z][a-z0-9_]{1,63}$' then
        raise exception using errcode = '22023', message = 'send_error_code_invalid';
    end if;

    select event.* into existing_event
      from public.send_events event
     where event.application_id = p_application_id
       and event.idempotency_key = p_idempotency_key
     for update;
    if not found then
        return;
    end if;
    if existing_event.outcome <> 'pending_provider' then
        return next existing_event;
        return;
    end if;
    event_user_id := existing_event.user_id;

    update public.applications application
       set status = case when p_outcome = 'sent' then 'sent' else 'failed' end,
           sent_at = case when p_outcome = 'sent' then timestamp_now else application.sent_at end,
           provider_message_id = case
               when p_outcome = 'sent' then p_provider_message_id else application.provider_message_id
           end,
           provider_thread_id = case
               when p_outcome = 'sent' then p_provider_thread_id else application.provider_thread_id
           end,
           last_error = case when p_outcome = 'sent' then null else p_error_code end,
           updated_at = timestamp_now
     where application.id = p_application_id and application.user_id = event_user_id;

    update public.provider_send_events provider_event
       set outcome = p_outcome,
           error_code = p_error_code,
           updated_at = timestamp_now
     where provider_event.send_event_id = existing_event.id;

    return query
    update public.send_events event
       set outcome = p_outcome,
           provider_message_id = p_provider_message_id,
           provider_thread_id = p_provider_thread_id,
           error_code = p_error_code,
           updated_at = timestamp_now
     where event.application_id = p_application_id
       and event.user_id = event_user_id
       and event.idempotency_key = p_idempotency_key
     returning event.*;
end;
$$;

revoke all on function public.reserve_application_send(uuid, text) from public, anon;
grant execute on function public.reserve_application_send(uuid, text) to authenticated;
revoke all on function public.reconcile_stale_application_send(uuid)
    from public, anon;
grant execute on function public.reconcile_stale_application_send(uuid)
    to authenticated;
revoke all on function public.finalize_application_send(uuid, text, text, text, text, text)
    from public, anon, authenticated;
grant execute on function public.finalize_application_send(uuid, text, text, text, text, text)
    to service_role;

-- Keep the at-most-once send guard executable and migration-enforced.
do $migration_assert_send_in_progress$
begin
    if position(
        'existing_event.outcome = ''pending_provider'''
        in pg_get_functiondef('public.reserve_application_send(uuid, text)'::regprocedure)
    ) = 0 or position(
        'message = ''send_in_progress'''
        in pg_get_functiondef('public.reserve_application_send(uuid, text)'::regprocedure)
    ) = 0 then
        raise exception 'reserve_application_send must reject an existing pending reservation';
    end if;
end;
$migration_assert_send_in_progress$;

-- Private résumé object storage. The first path segment is always the JWT subject.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('resumes', 'resumes', false, 6291456, array['application/pdf']::text[])
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create or replace function public.resume_object_quota_available()
returns boolean
language plpgsql
volatile
security definer
set search_path = ''
as $$
declare
    current_user_id uuid := auth.uid();
begin
    if current_user_id is null then
        return false;
    end if;
    -- Match account deletion's lock order.  The profile share lock prevents a
    -- Storage insert that is already in flight from committing after deletion has
    -- switched the account to `deleting` and enumerated its private objects.
    perform 1 from public.profiles profile
     where profile.user_id = current_user_id and profile.account_status = 'active'
     for share;
    if not found then
        return false;
    end if;
    perform pg_advisory_xact_lock(
        hashtextextended('resume-storage:' || current_user_id::text, 0)
    );
    return (
        select count(*)
          from storage.objects object
         where object.bucket_id = 'resumes'
           and (storage.foldername(object.name))[1] = current_user_id::text
    ) < 5;
end;
$$;

revoke all on function public.resume_object_quota_available()
    from public, anon;
grant execute on function public.resume_object_quota_available()
    to authenticated;

create policy resumes_objects_select_own
on storage.objects for select to authenticated
using (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = (select auth.uid())::text
);

create policy resumes_objects_insert_own
on storage.objects for insert to authenticated
with check (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = (select auth.uid())::text
    and public.account_is_active()
    and array_length(storage.foldername(name), 1) = 1
    and lower(storage.filename(name)) ~ '^resume-[1-5]\.pdf$'
    and public.resume_object_quota_available()
);

create policy resumes_objects_update_own
on storage.objects for update to authenticated
using (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = (select auth.uid())::text
    and public.account_is_active()
)
with check (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = (select auth.uid())::text
    and public.account_is_active()
    and array_length(storage.foldername(name), 1) = 1
    and lower(storage.filename(name)) ~ '^resume-[1-5]\.pdf$'
);

create policy resumes_objects_delete_own
on storage.objects for delete to authenticated
using (
    bucket_id = 'resumes'
    and (storage.foldername(name))[1] = (select auth.uid())::text
    and public.account_is_active()
);

commit;
