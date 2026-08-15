-- Persist tenant-supplied Groq, Hunter, and Browserbase credentials as one
-- encrypted JSON envelope. The table and worker resolver are service-role-only;
-- plaintext credentials never enter PostgREST browser sessions or queue payloads.

begin;

-- A persistent epoch fences Browserbase account/project changes even when the
-- credential row itself is deleted.  Epoch zero represents the platform/default
-- account before a tenant credential has ever been saved.
create table public.browserbase_credential_states (
    user_id uuid primary key references auth.users(id) on delete cascade,
    epoch bigint not null default 0 check (epoch >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create trigger browserbase_credential_states_set_updated_at
    before update on public.browserbase_credential_states
    for each row execute function public.set_updated_at();

create table public.user_provider_credentials (
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null check (provider in ('groq', 'hunter', 'browserbase')),
    credential_ciphertext text not null check (
        char_length(credential_ciphertext) between 16 and 16384
    ),
    verification_status text not null default 'unverified' check (
        verification_status in ('verified', 'unverified', 'invalid')
    ),
    verification_code text check (
        verification_code is null
        or verification_code ~ '^[a-z][a-z0-9_]{1,63}$'
    ),
    verified_at timestamptz,
    generation bigint not null check (generation > 0),
    binding_fingerprint text check (
        binding_fingerprint is null or binding_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, provider),
    check (
        (verification_status = 'verified' and verified_at is not null)
        or (verification_status <> 'verified' and verified_at is null)
    ),
    check (
        (provider = 'browserbase' and binding_fingerprint is not null)
        or (provider <> 'browserbase' and binding_fingerprint is null)
    )
);

create trigger user_provider_credentials_set_updated_at
    before update on public.user_provider_credentials
    for each row execute function public.set_updated_at();

alter table public.user_provider_credentials enable row level security;
alter table public.browserbase_credential_states enable row level security;

-- Deliberately no browser policy or grant. Only the authenticated API and worker
-- can use this table, through a server-secret client.
revoke all on public.user_provider_credentials from public, anon, authenticated;
revoke all on public.browserbase_credential_states from public, anon, authenticated;
grant all on public.user_provider_credentials to service_role;
grant all on public.browserbase_credential_states to service_role;

alter table public.connection_secrets
    add column browser_credential_source text check (
        browser_credential_source is null
        or browser_credential_source in ('platform', 'user')
    ),
    add column browser_credential_generation bigint check (
        browser_credential_generation is null or browser_credential_generation > 0
    ),
    add column browser_credential_epoch bigint check (
        browser_credential_epoch is null or browser_credential_epoch >= 0
    ),
    add column browser_project_fingerprint text check (
        browser_project_fingerprint is null
        or browser_project_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    add constraint connection_secrets_browser_credential_binding check (
        (browser_credential_source is null
         and browser_credential_generation is null
         and browser_credential_epoch is null
         and browser_project_fingerprint is null)
        or
        (browser_credential_source = 'platform'
         and browser_credential_generation is null
         and browser_credential_epoch is not null
         and browser_project_fingerprint is not null)
        or
        (browser_credential_source = 'user'
         and browser_credential_generation is not null
         and browser_credential_epoch = browser_credential_generation
         and browser_project_fingerprint is not null)
    );

-- Every managed application job is permanently bound to the Browserbase
-- credential epoch visible when it is inserted. A BEFORE INSERT trigger is an
-- actual transaction fence (including for future service-role insertion paths),
-- while keeping the public idempotency payload unchanged.
alter table public.automation_jobs
    add column browserbase_credential_epoch bigint check (
        browserbase_credential_epoch is null or browserbase_credential_epoch >= 0
    );

-- This is the first credential-vault migration, so every historical managed
-- application job was created against the platform/default epoch (zero).
-- Preserve queued/running retries instead of making them fail merely because
-- the new fence column did not exist when they were enqueued.
update public.automation_jobs job
   set browserbase_credential_epoch = 0
 where job.kind in ('application_scan', 'application_prefill', 'application_submit')
   and job.browserbase_credential_epoch is null;

create or replace function public.bind_automation_job_browserbase_epoch()
returns trigger
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_epoch bigint;
    credential public.user_provider_credentials%rowtype;
begin
    if new.kind not in (
        'application_scan', 'application_prefill', 'application_submit'
    ) then
        new.browserbase_credential_epoch := null;
        return new;
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || new.user_id::text, 0
    ));
    if exists (
        select 1 from public.connection_lifecycles lifecycle
         where lifecycle.user_id = new.user_id
           and lifecycle.provider in (
               'google_forms', 'greenhouse', 'lever', 'ashby',
               'yc', 'wellfound', 'cutshort', 'instahyre'
           )
           and lifecycle.status in ('connecting', 'disconnecting')
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_connection_operation_in_progress';
    end if;
    insert into public.browserbase_credential_states (user_id)
    values (new.user_id)
    on conflict (user_id) do nothing;
    select state.epoch into current_epoch
      from public.browserbase_credential_states state
     where state.user_id = new.user_id
     for share;
    select stored.* into credential
      from public.user_provider_credentials stored
     where stored.user_id = new.user_id
       and stored.provider = 'browserbase'
     for share;
    if found and (
        credential.verification_status <> 'verified'
        or credential.generation <> current_epoch
        or credential.binding_fingerprint is null
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;
    new.browserbase_credential_epoch := current_epoch;
    return new;
end;
$$;

create trigger automation_jobs_bind_browserbase_epoch
    before insert on public.automation_jobs
    for each row execute function public.bind_automation_job_browserbase_epoch();

create or replace function public.get_browserbase_credential_state(
    user_id_input uuid
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_epoch bigint;
begin
    if user_id_input is null then
        raise exception using errcode = '22023', message = 'provider_credential_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    insert into public.browserbase_credential_states (user_id)
    values (user_id_input)
    on conflict (user_id) do nothing;
    select state.epoch into current_epoch
      from public.browserbase_credential_states state
     where state.user_id = user_id_input
     for share;
    return jsonb_build_object('epoch', current_epoch);
end;
$$;

create or replace function public.save_user_provider_credential(
    user_id_input uuid,
    provider_input text,
    credential_ciphertext_input text,
    verification_status_input text,
    verification_code_input text default null,
    verified_at_input timestamptz default null,
    binding_fingerprint_input text default null,
    expected_browserbase_epoch_input bigint default null
)
returns setof public.user_provider_credentials
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    prior_generation bigint;
    next_generation bigint;
begin
    if user_id_input is null
       or provider_input not in ('groq', 'hunter', 'browserbase')
       or char_length(coalesce(credential_ciphertext_input, '')) not between 16 and 16384
       or verification_status_input not in ('verified', 'unverified', 'invalid')
       or (verification_code_input is not null
           and verification_code_input !~ '^[a-z][a-z0-9_]{1,63}$')
       or (verification_status_input = 'verified' and verified_at_input is null)
       or (verification_status_input <> 'verified' and verified_at_input is not null)
       or (provider_input = 'browserbase' and (
           verification_status_input <> 'verified'
           or binding_fingerprint_input is null
           or binding_fingerprint_input !~ '^[0-9a-f]{64}$'
           or expected_browserbase_epoch_input is null
           or expected_browserbase_epoch_input < 0
       ))
       or (provider_input <> 'browserbase' and (
           binding_fingerprint_input is not null
           or expected_browserbase_epoch_input is not null
       )) then
        raise exception using errcode = '22023', message = 'provider_credential_invalid';
    end if;

    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;

    -- Browserbase contexts belong to the account/project that created them. The
    -- API removes remote contexts before replacing a key; these checks close the
    -- race with a queued worker or an unfinished connection lifecycle.
    if provider_input = 'browserbase' then
        perform pg_advisory_xact_lock(hashtextextended(
            'browserbase-account:' || user_id_input::text, 0
        ));
        insert into public.browserbase_credential_states (user_id)
        values (user_id_input)
        on conflict (user_id) do nothing;
        select state.epoch into prior_generation
          from public.browserbase_credential_states state
         where state.user_id = user_id_input
         for update;
        if prior_generation <> expected_browserbase_epoch_input then
            raise exception using
                errcode = 'P0001', message = 'browserbase_credential_binding_stale';
        end if;
        if exists (
            select 1 from public.connection_lifecycles lifecycle
             where lifecycle.user_id = user_id_input
               and lifecycle.provider in (
                   'google_forms', 'greenhouse', 'lever', 'ashby',
                   'yc', 'wellfound', 'cutshort', 'instahyre'
               )
               and lifecycle.status in ('connecting', 'disconnecting')
        ) then
            raise exception using
                errcode = 'P0001', message = 'browserbase_connection_operation_in_progress';
        end if;
        if exists (
            select 1 from public.automation_jobs job
             where job.user_id = user_id_input
               and job.kind in ('application_scan', 'application_prefill', 'application_submit')
               and job.status in ('queued', 'running')
        ) then
            raise exception using errcode = 'P0001', message = 'browserbase_jobs_active';
        end if;
        if exists (
            select 1
              from public.connection_secrets secret
              join public.connections connection on connection.id = secret.connection_id
             where secret.user_id = user_id_input
               and connection.user_id = user_id_input
               and connection.mode = 'managed_browser'
               and (secret.browser_context_id_ciphertext is not null
                    or secret.browser_session_id_ciphertext is not null)
        ) then
            raise exception using errcode = 'P0001', message = 'browserbase_disconnect_required';
        end if;
        next_generation := prior_generation + 1;
        update public.browserbase_credential_states state
           set epoch = next_generation
         where state.user_id = user_id_input
           and state.epoch = prior_generation;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'browserbase_credential_binding_stale';
        end if;
    else
        perform pg_advisory_xact_lock(hashtextextended(
            'provider-credential:' || user_id_input::text || ':' || provider_input, 0
        ));
        select credential.generation into prior_generation
          from public.user_provider_credentials credential
         where credential.user_id = user_id_input
           and credential.provider = provider_input
         for update;
        next_generation := coalesce(prior_generation, 0) + 1;
    end if;

    return query
    insert into public.user_provider_credentials as credential (
        user_id, provider, credential_ciphertext, verification_status,
        verification_code, verified_at, generation, binding_fingerprint
    ) values (
        user_id_input, provider_input, credential_ciphertext_input,
        verification_status_input, verification_code_input,
        verified_at_input, next_generation, binding_fingerprint_input
    )
    on conflict (user_id, provider) do update
       set credential_ciphertext = excluded.credential_ciphertext,
           verification_status = excluded.verification_status,
           verification_code = excluded.verification_code,
           verified_at = excluded.verified_at,
           generation = excluded.generation,
           binding_fingerprint = excluded.binding_fingerprint
    returning credential.*;

    insert into public.audit_events (
        user_id, event_type, resource_type, metadata
    ) values (
        user_id_input, 'provider_credential.saved', 'provider_credential',
        jsonb_build_object(
            'provider', provider_input,
            'generation', next_generation,
            'verification_status', verification_status_input
        )
    );
end;
$$;

create or replace function public.delete_user_provider_credential(
    user_id_input uuid,
    provider_input text,
    expected_browserbase_epoch_input bigint default null
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
begin
    if user_id_input is null
       or provider_input not in ('groq', 'hunter', 'browserbase')
       or (provider_input = 'browserbase' and (
           expected_browserbase_epoch_input is null
           or expected_browserbase_epoch_input < 0
       ))
       or (provider_input <> 'browserbase'
           and expected_browserbase_epoch_input is not null) then
        raise exception using errcode = '22023', message = 'provider_credential_invalid';
    end if;

    if provider_input = 'browserbase' then
        perform pg_advisory_xact_lock(hashtextextended(
            'browserbase-account:' || user_id_input::text, 0
        ));
        insert into public.browserbase_credential_states (user_id)
        values (user_id_input)
        on conflict (user_id) do nothing;
        perform 1 from public.browserbase_credential_states state
         where state.user_id = user_id_input
           and state.epoch = expected_browserbase_epoch_input
         for update;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'browserbase_credential_binding_stale';
        end if;
        perform 1 from public.user_provider_credentials credential
         where credential.user_id = user_id_input
           and credential.provider = 'browserbase'
         for update;
        if not found then
            return true;
        end if;
        if exists (
            select 1 from public.connection_lifecycles lifecycle
             where lifecycle.user_id = user_id_input
               and lifecycle.provider in (
                   'google_forms', 'greenhouse', 'lever', 'ashby',
                   'yc', 'wellfound', 'cutshort', 'instahyre'
               )
               and lifecycle.status in ('connecting', 'disconnecting')
        ) then
            raise exception using
                errcode = 'P0001', message = 'browserbase_connection_operation_in_progress';
        end if;
        if exists (
            select 1 from public.automation_jobs job
             where job.user_id = user_id_input
               and job.kind in ('application_scan', 'application_prefill', 'application_submit')
               and job.status in ('queued', 'running')
        ) then
            raise exception using errcode = 'P0001', message = 'browserbase_jobs_active';
        end if;
        if exists (
            select 1
              from public.connection_secrets secret
              join public.connections connection on connection.id = secret.connection_id
             where secret.user_id = user_id_input
               and connection.user_id = user_id_input
               and connection.mode = 'managed_browser'
               and (secret.browser_context_id_ciphertext is not null
                    or secret.browser_session_id_ciphertext is not null)
        ) then
            raise exception using errcode = 'P0001', message = 'browserbase_disconnect_required';
        end if;
        update public.browserbase_credential_states state
           set epoch = state.epoch + 1
         where state.user_id = user_id_input
           and state.epoch = expected_browserbase_epoch_input;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'browserbase_credential_binding_stale';
        end if;
    else
        perform pg_advisory_xact_lock(hashtextextended(
            'provider-credential:' || user_id_input::text || ':' || provider_input, 0
        ));
    end if;

    delete from public.user_provider_credentials credential
     where credential.user_id = user_id_input
       and credential.provider = provider_input;
    if not found then
        return true;
    end if;

    insert into public.audit_events (
        user_id, event_type, resource_type, metadata
    ) values (
        user_id_input, 'provider_credential.deleted', 'provider_credential',
        jsonb_build_object('provider', provider_input)
    );
    return true;
end;
$$;

-- The worker receives Browserbase ciphertext only for the tenant/job whose
-- active lease it already holds. It decrypts immediately in worker memory and
-- never copies credentials into automation_jobs payload/progress/result.
create or replace function public.get_application_job_browserbase_credential(
    job_id uuid,
    worker_id text
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    queue_job public.automation_jobs%rowtype;
    credential public.user_provider_credentials%rowtype;
    current_epoch bigint;
    user_id_input uuid;
begin
    if job_id is null or nullif(btrim(worker_id), '') is null
       or char_length(worker_id) > 128 then
        raise exception using errcode = '22023', message = 'application_job_invalid';
    end if;
    -- Discover the tenant without taking a row lock, then establish the same
    -- account-lock ordering used by deletion and credential rotation. The exact
    -- lease is re-read and locked only after the account lock is held.
    select job.user_id into user_id_input
      from public.automation_jobs job
     where job.id = job_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    select job.* into queue_job
      from public.automation_jobs job
     where job.id = job_id
       and job.user_id = user_id_input
       and job.kind in ('application_scan', 'application_prefill', 'application_submit')
       and job.status = 'running'
       and job.locked_by = worker_id
       and job.lease_expires_at >= clock_timestamp()
       and job.cancel_requested_at is null
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;
    insert into public.browserbase_credential_states (user_id)
    values (queue_job.user_id)
    on conflict (user_id) do nothing;
    select state.epoch into current_epoch
      from public.browserbase_credential_states state
     where state.user_id = queue_job.user_id
     for share;
    if queue_job.browserbase_credential_epoch is null
       or queue_job.browserbase_credential_epoch <> current_epoch then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;

    select stored.* into credential
      from public.user_provider_credentials stored
     where stored.user_id = queue_job.user_id
       and stored.provider = 'browserbase';
    if not found then
        return null;
    end if;
    if credential.verification_status <> 'verified'
       or credential.generation <> current_epoch
       or credential.binding_fingerprint is null then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;
    return jsonb_build_object(
        'user_id', credential.user_id,
        'credential_source', 'user',
        'credential_ciphertext', credential.credential_ciphertext,
        'verification_status', credential.verification_status,
        'verification_code', credential.verification_code,
        'generation', credential.generation,
        'binding_fingerprint', credential.binding_fingerprint,
        'epoch', current_epoch
    );
end;
$$;

-- Return the exact persisted context binding only to the worker that currently
-- owns the application lease. The worker compares this fingerprint to the
-- decrypted/current project before reusing a Browserbase context.
create or replace function public.get_application_job_browser_context_binding(
    job_id uuid,
    worker_id text
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    queue_job public.automation_jobs%rowtype;
    target_secret public.connection_secrets%rowtype;
    current_epoch bigint;
    user_id_input uuid;
begin
    if job_id is null or nullif(btrim(worker_id), '') is null
       or char_length(worker_id) > 128 then
        raise exception using errcode = '22023', message = 'application_job_invalid';
    end if;
    -- Match the worker-credential RPC's account-first lock order. Account
    -- deletion takes this advisory lock before updating queue rows, so holding a
    -- job row lock while waiting for it would create a lock-order deadlock.
    select job.user_id into user_id_input
      from public.automation_jobs job
     where job.id = job_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    select job.* into queue_job
      from public.automation_jobs job
     where job.id = job_id
       and job.user_id = user_id_input
       and job.kind in ('application_scan', 'application_prefill', 'application_submit')
       and job.status = 'running'
       and job.locked_by = worker_id
       and job.lease_expires_at >= clock_timestamp()
       and job.cancel_requested_at is null
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;
    insert into public.browserbase_credential_states (user_id)
    values (queue_job.user_id)
    on conflict (user_id) do nothing;
    select state.epoch into current_epoch
      from public.browserbase_credential_states state
     where state.user_id = queue_job.user_id
     for share;
    if queue_job.browserbase_credential_epoch is null
       or queue_job.browserbase_credential_epoch <> current_epoch then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;

    select secret.* into target_secret
      from public.connections connection
      join public.connection_secrets secret
        on secret.connection_id = connection.id
       and secret.user_id = connection.user_id
     where connection.user_id = queue_job.user_id
       and connection.provider = queue_job.provider
       and connection.mode = 'managed_browser'
     for share of connection, secret;
    if not found or target_secret.browser_context_id_ciphertext is null then
        return jsonb_build_object(
            'browser_context_id_ciphertext', null,
            'credential_source', null,
            'credential_generation', null,
            'credential_epoch', null,
            'project_fingerprint', null
        );
    end if;
    if target_secret.browser_credential_epoch <> queue_job.browserbase_credential_epoch
       or target_secret.browser_credential_source not in ('platform', 'user')
       or target_secret.browser_project_fingerprint is null
       or (
           target_secret.browser_credential_source = 'platform'
           and target_secret.browser_credential_generation is not null
       )
       or (
           target_secret.browser_credential_source = 'user'
           and (
               target_secret.browser_credential_generation is null
               or target_secret.browser_credential_generation <> target_secret.browser_credential_epoch
           )
       ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;
    if target_secret.browser_credential_source = 'user' then
        perform 1 from public.user_provider_credentials credential
         where credential.user_id = queue_job.user_id
           and credential.provider = 'browserbase'
           and credential.verification_status = 'verified'
           and credential.generation = target_secret.browser_credential_generation
           and credential.binding_fingerprint = target_secret.browser_project_fingerprint
         for share;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'browserbase_credential_binding_stale';
        end if;
    elsif exists (
        select 1 from public.user_provider_credentials credential
         where credential.user_id = queue_job.user_id
           and credential.provider = 'browserbase'
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;

    return jsonb_build_object(
        'browser_context_id_ciphertext', target_secret.browser_context_id_ciphertext,
        'credential_source', target_secret.browser_credential_source,
        'credential_generation', target_secret.browser_credential_generation,
        'credential_epoch', target_secret.browser_credential_epoch,
        'project_fingerprint', target_secret.browser_project_fingerprint
    );
end;
$$;

-- Browser starts and Browserbase credential changes share one tenant-wide lock.
-- The start transaction wins by moving its provider lifecycle to `connecting`;
-- a concurrent save/delete then fails before changing the account epoch.
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
    current_epoch bigint;
    current_credential public.user_provider_credentials%rowtype;
    credential_exists boolean := false;
begin
    if user_id_input is null or provider_input is null
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       ) then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    insert into public.browserbase_credential_states (user_id)
    values (user_id_input)
    on conflict (user_id) do nothing;
    select state.epoch into current_epoch
      from public.browserbase_credential_states state
     where state.user_id = user_id_input
     for share;
    select credential.* into current_credential
      from public.user_provider_credentials credential
     where credential.user_id = user_id_input
       and credential.provider = 'browserbase'
     for share;
    credential_exists := found;
    if credential_exists and (
        current_credential.verification_status <> 'verified'
        or current_credential.generation <> current_epoch
        or current_credential.binding_fingerprint is null
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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
    if lifecycle_exists and prior_status in ('connecting', 'disconnecting') then
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
        jsonb_build_object(
            'provider', provider_input,
            'generation', next_generation,
            'browserbase_epoch', current_epoch
        )
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
        -- Contexts created before credential/project binding existed cannot be
        -- proven to belong to the active Browserbase project. Never silently
        -- reuse one: the control plane must remove/recreate it (or require the
        -- explicit abandon flow if remote cleanup is no longer possible).
        if current_secret.browser_context_id_ciphertext is not null
           and current_secret.browser_credential_source is null
           and current_secret.browser_credential_generation is null
           and current_secret.browser_credential_epoch is null
           and current_secret.browser_project_fingerprint is null then
            reuse_context := false;
        end if;
    end if;

    return jsonb_build_object(
        'generation', next_generation,
        'connection_id', current_connection.id,
        'context_ciphertext', current_secret.browser_context_id_ciphertext,
        'session_ciphertext', current_secret.browser_session_id_ciphertext,
        'reuse_context', reuse_context,
        'credential_epoch', current_epoch,
        'active_credential_source', case
            when credential_exists then 'user' else 'platform'
        end,
        'active_credential_generation', case
            when credential_exists then current_credential.generation else null
        end,
        'active_project_fingerprint', case
            when credential_exists then current_credential.binding_fingerprint else null
        end,
        'context_credential_source', current_secret.browser_credential_source,
        'context_credential_generation', current_secret.browser_credential_generation,
        'context_credential_epoch', current_secret.browser_credential_epoch,
        'context_project_fingerprint', current_secret.browser_project_fingerprint
    );
end;
$$;

create or replace function public.save_browser_connection_context_bound(
    user_id_input uuid,
    provider_input text,
    expected_generation_input bigint,
    display_name_input text,
    context_ciphertext_input text,
    credential_source_input text,
    credential_generation_input bigint,
    credential_epoch_input bigint,
    project_fingerprint_input text
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
       or expected_generation_input is null or expected_generation_input < 1
       or nullif(btrim(display_name_input), '') is null
       or nullif(btrim(context_ciphertext_input), '') is null
       or credential_source_input not in ('platform', 'user')
       or credential_epoch_input is null or credential_epoch_input < 0
       or project_fingerprint_input !~ '^[0-9a-f]{64}$'
       or (credential_source_input = 'platform' and credential_generation_input is not null)
       or (credential_source_input = 'user' and (
           credential_generation_input is null
           or credential_generation_input < 1
           or credential_generation_input <> credential_epoch_input
       )) then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    perform 1 from public.browserbase_credential_states state
     where state.user_id = user_id_input
       and state.epoch = credential_epoch_input
     for share;
    if not found then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;
    if credential_source_input = 'user' then
        perform 1 from public.user_provider_credentials credential
         where credential.user_id = user_id_input
           and credential.provider = 'browserbase'
           and credential.verification_status = 'verified'
           and credential.generation = credential_generation_input
           and credential.binding_fingerprint = project_fingerprint_input
         for share;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'browserbase_credential_binding_stale';
        end if;
    elsif exists (
        select 1 from public.user_provider_credentials credential
         where credential.user_id = user_id_input
           and credential.provider = 'browserbase'
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_credential_binding_stale';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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
        browser_session_id_ciphertext, browser_lifecycle_generation,
        browser_credential_source, browser_credential_generation,
        browser_credential_epoch, browser_project_fingerprint
    ) values (
        saved_connection.id, user_id_input, context_ciphertext_input, null,
        expected_generation_input, credential_source_input,
        credential_generation_input, credential_epoch_input,
        project_fingerprint_input
    )
    on conflict (connection_id) do update
       set user_id = excluded.user_id,
           browser_context_id_ciphertext = excluded.browser_context_id_ciphertext,
           browser_session_id_ciphertext = null,
           browser_lifecycle_generation = excluded.browser_lifecycle_generation,
           browser_credential_source = excluded.browser_credential_source,
           browser_credential_generation = excluded.browser_credential_generation,
           browser_credential_epoch = excluded.browser_credential_epoch,
           browser_project_fingerprint = excluded.browser_project_fingerprint;
    return next saved_connection;
end;
$$;

-- Replace the remaining lifecycle functions so every provider exposed by the
-- control plane uses the same full managed-provider set. These definitions are
-- otherwise equivalent to the original lifecycle operations.
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
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
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
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
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
       or expected_generation_input is null or expected_generation_input < 1
       or drop_connection_input is null then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
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
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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

-- Disconnect obtains the same Browserbase-account lock as credential rotation
-- before changing lifecycle state and returns the exact persisted binding.
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       ) then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = user_id_input
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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
        'session_ciphertext', current_secret.browser_session_id_ciphertext,
        'context_credential_source', current_secret.browser_credential_source,
        'context_credential_generation', current_secret.browser_credential_generation,
        'context_credential_epoch', current_secret.browser_credential_epoch,
        'context_project_fingerprint', current_secret.browser_project_fingerprint
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
       or provider_input not in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
       or expected_generation_input is null or expected_generation_input < 1 then
        raise exception using errcode = '22023', message = 'browser_connection_invalid';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browser-lifecycle:' || user_id_input::text || ':' || provider_input, 0
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

-- Account deletion first blocks all new authenticated work, then drains
-- worker-held plaintext. Returning false commits the deletion-pending state and
-- cancellation requests; retrying DELETE remains possible through the API's
-- authenticated (rather than active-account) dependency.
create or replace function public.begin_account_deletion(confirmation_input text)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := auth.uid();
    timestamp_now timestamptz := clock_timestamp();
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

    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || current_user_id::text, 0
    ));
    if exists (
        select 1 from public.connection_lifecycles lifecycle
         where lifecycle.user_id = current_user_id
           and lifecycle.provider in (
               'google_forms', 'greenhouse', 'lever', 'ashby',
               'yc', 'wellfound', 'cutshort', 'instahyre'
           )
           and lifecycle.status in ('connecting', 'disconnecting')
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_connection_operation_in_progress';
    end if;
    update public.profiles profile
       set account_status = 'deleting',
           deletion_started_at = coalesce(profile.deletion_started_at, timestamp_now)
     where profile.user_id = current_user_id;
    update public.automation_jobs job
       set status = 'cancelled',
           cancel_requested_at = coalesce(job.cancel_requested_at, timestamp_now),
           error_code = 'account_deletion_requested',
           error_message = 'Cancelled because account deletion was requested.',
           locked_by = null, locked_at = null, lease_expires_at = null
     where job.user_id = current_user_id
       and job.status = 'queued';
    update public.automation_jobs job
       set status = 'cancelled',
           cancel_requested_at = coalesce(job.cancel_requested_at, timestamp_now),
           error_code = 'account_deletion_requested',
           error_message = 'Cancelled after its worker lease expired during account deletion.',
           locked_by = null, locked_at = null, lease_expires_at = null
     where job.user_id = current_user_id
       and job.status = 'running'
       and (job.lease_expires_at is null or job.lease_expires_at < timestamp_now);
    update public.automation_jobs job
       set cancel_requested_at = coalesce(job.cancel_requested_at, timestamp_now)
     where job.user_id = current_user_id
       and job.status = 'running';
    if exists (
        select 1 from public.automation_jobs job
         where job.user_id = current_user_id
           and job.status = 'running'
    ) then
        insert into public.audit_events (
            user_id, event_type, resource_type, outcome, metadata
        ) values (
            current_user_id, 'account.deletion_drain', 'account', 'needs_attention',
            jsonb_build_object('running_jobs', (
                select count(*) from public.automation_jobs job
                 where job.user_id = current_user_id and job.status = 'running'
            ))
        );
        return false;
    end if;
    return true;
end;
$$;

-- Explicit recovery for a revoked/corrupt Browserbase key.  No remote deletion
-- is claimed: local handles and the credential are abandoned only after the
-- caller types the exact confirmation, and the event is marked needs-attention.
create or replace function public.abandon_browserbase_resources(
    user_id_input uuid,
    confirmation_input text
)
returns boolean
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    removed_connections bigint;
    profile_status text;
begin
    if user_id_input is null
       or confirmation_input <> 'ABANDON REMOTE BROWSER DATA' then
        raise exception using
            errcode = '22023', message = 'browserbase_abandon_confirmation_invalid';
    end if;
    select profile.account_status into profile_status
      from public.profiles profile
     where profile.user_id = user_id_input
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'browserbase-account:' || user_id_input::text, 0
    ));
    if exists (
        select 1 from public.automation_jobs job
         where job.user_id = user_id_input
           and job.status in ('queued', 'running')
    ) then
        raise exception using errcode = 'P0001', message = 'browserbase_jobs_active';
    end if;
    if profile_status <> 'deleting' and exists (
        select 1 from public.connection_lifecycles lifecycle
         where lifecycle.user_id = user_id_input
           and lifecycle.provider in (
               'google_forms', 'greenhouse', 'lever', 'ashby',
               'yc', 'wellfound', 'cutshort', 'instahyre'
           )
           and lifecycle.status in ('connecting', 'disconnecting')
    ) then
        raise exception using
            errcode = 'P0001', message = 'browserbase_connection_operation_in_progress';
    end if;

    delete from public.connections connection
     where connection.user_id = user_id_input
       and connection.mode = 'managed_browser';
    get diagnostics removed_connections = row_count;
    update public.connection_lifecycles lifecycle
       set generation = lifecycle.generation + 1, status = 'disconnected'
     where lifecycle.user_id = user_id_input
       and lifecycle.provider in (
           'google_forms', 'greenhouse', 'lever', 'ashby',
           'yc', 'wellfound', 'cutshort', 'instahyre'
       )
       and lifecycle.status in ('connecting', 'connected', 'disconnecting');
    insert into public.browserbase_credential_states (user_id, epoch)
    values (user_id_input, 1)
    on conflict (user_id) do update
       set epoch = browserbase_credential_states.epoch + 1;
    delete from public.user_provider_credentials credential
     where credential.user_id = user_id_input
       and credential.provider = 'browserbase';
    insert into public.audit_events (
        user_id, event_type, resource_type, outcome, metadata
    ) values (
        user_id_input, 'browserbase.resources_abandoned', 'provider_credential',
        'needs_attention', jsonb_build_object(
            'remote_cleanup_confirmed', false,
            'removed_local_connections', removed_connections
        )
    );
    return true;
end;
$$;

-- Every ciphertext/binding/lifecycle function is service-role-only. PostgreSQL
-- grants EXECUTE to PUBLIC on new functions by default, so every exact overload
-- is revoked explicitly before the narrow grants below.
revoke all on function public.bind_automation_job_browserbase_epoch()
    from public, anon, authenticated, service_role;
revoke all on function public.get_browserbase_credential_state(uuid)
    from public, anon, authenticated;
revoke all on function public.save_user_provider_credential(uuid, text, text, text, text, timestamptz, text, bigint)
    from public, anon, authenticated;
revoke all on function public.delete_user_provider_credential(uuid, text, bigint)
    from public, anon, authenticated;
revoke all on function public.get_application_job_browserbase_credential(uuid, text)
    from public, anon, authenticated;
revoke all on function public.get_application_job_browser_context_binding(uuid, text)
    from public, anon, authenticated;
revoke all on function public.begin_browser_start(uuid, text)
    from public, anon, authenticated;
revoke all on function public.save_browser_connection_context_bound(uuid, text, bigint, text, text, text, bigint, bigint, text)
    from public, anon, authenticated;
revoke all on function public.save_browser_connection_session(
    uuid, text, bigint, uuid, text, text
) from public, anon, authenticated;
revoke all on function public.confirm_browser_start(
    uuid, text, bigint, uuid, text, text
) from public, anon, authenticated;
revoke all on function public.abort_browser_start(
    uuid, text, bigint, uuid, text, boolean
) from public, anon, authenticated;
revoke all on function public.finish_browser_start(uuid, text, bigint, uuid, text)
    from public, anon, authenticated;
revoke all on function public.begin_browser_disconnect(uuid, text)
    from public, anon, authenticated;
revoke all on function public.finish_browser_disconnect(uuid, text, bigint, uuid)
    from public, anon, authenticated;
revoke all on function public.abandon_browserbase_resources(uuid, text)
    from public, anon, authenticated;

grant execute on function public.get_browserbase_credential_state(uuid)
    to service_role;
grant execute on function public.save_user_provider_credential(uuid, text, text, text, text, timestamptz, text, bigint)
    to service_role;
grant execute on function public.delete_user_provider_credential(uuid, text, bigint)
    to service_role;
grant execute on function public.get_application_job_browserbase_credential(uuid, text)
    to service_role;
grant execute on function public.get_application_job_browser_context_binding(uuid, text)
    to service_role;
grant execute on function public.begin_browser_start(uuid, text) to service_role;
grant execute on function public.save_browser_connection_context_bound(uuid, text, bigint, text, text, text, bigint, bigint, text)
    to service_role;
grant execute on function public.save_browser_connection_session(
    uuid, text, bigint, uuid, text, text
) to service_role;
grant execute on function public.confirm_browser_start(
    uuid, text, bigint, uuid, text, text
) to service_role;
grant execute on function public.abort_browser_start(
    uuid, text, bigint, uuid, text, boolean
) to service_role;
grant execute on function public.finish_browser_start(uuid, text, bigint, uuid, text)
    to service_role;
grant execute on function public.begin_browser_disconnect(uuid, text) to service_role;
grant execute on function public.finish_browser_disconnect(uuid, text, bigint, uuid)
    to service_role;
grant execute on function public.abandon_browserbase_resources(uuid, text)
    to service_role;

-- The authenticated account owner still initiates the deletion-drain RPC.
revoke all on function public.begin_account_deletion(text) from public, anon;
grant execute on function public.begin_account_deletion(text) to authenticated;

-- The former unbound context writer could bypass project/epoch validation if
-- service_role retained its historical grant. Keep the function only for
-- migration compatibility, but make it uncallable by application roles.
revoke all on function public.save_browser_connection_context(
    uuid, text, bigint, text, text
) from public, anon, authenticated;
revoke all on function public.save_browser_connection_context(uuid, text, bigint, text, text)
    from service_role;

do $assert_user_provider_credentials_server_only$
declare
    function_signature text;
    server_only_signatures text[] := array[
        'public.get_browserbase_credential_state(uuid)',
        'public.save_user_provider_credential(uuid,text,text,text,text,timestamptz,text,bigint)',
        'public.delete_user_provider_credential(uuid,text,bigint)',
        'public.get_application_job_browserbase_credential(uuid,text)',
        'public.get_application_job_browser_context_binding(uuid,text)',
        'public.begin_browser_start(uuid,text)',
        'public.save_browser_connection_context_bound(uuid,text,bigint,text,text,text,bigint,bigint,text)',
        'public.save_browser_connection_session(uuid,text,bigint,uuid,text,text)',
        'public.confirm_browser_start(uuid,text,bigint,uuid,text,text)',
        'public.abort_browser_start(uuid,text,bigint,uuid,text,boolean)',
        'public.finish_browser_start(uuid,text,bigint,uuid,text)',
        'public.begin_browser_disconnect(uuid,text)',
        'public.finish_browser_disconnect(uuid,text,bigint,uuid)',
        'public.abandon_browserbase_resources(uuid,text)'
    ];
begin
    if has_table_privilege('anon', 'public.user_provider_credentials', 'SELECT')
       or has_table_privilege('anon', 'public.user_provider_credentials', 'INSERT')
       or has_table_privilege('anon', 'public.user_provider_credentials', 'UPDATE')
       or has_table_privilege('anon', 'public.user_provider_credentials', 'DELETE')
       or has_table_privilege('authenticated', 'public.user_provider_credentials', 'SELECT')
       or has_table_privilege('authenticated', 'public.user_provider_credentials', 'INSERT')
       or has_table_privilege('authenticated', 'public.user_provider_credentials', 'UPDATE')
       or has_table_privilege('authenticated', 'public.user_provider_credentials', 'DELETE') then
        raise exception 'tenant provider credentials must remain service-role-only';
    end if;
    foreach function_signature in array server_only_signatures loop
        if has_function_privilege('anon', function_signature, 'EXECUTE')
           or has_function_privilege('authenticated', function_signature, 'EXECUTE')
           or not has_function_privilege('service_role', function_signature, 'EXECUTE')
           or exists (
               select 1
                 from pg_proc procedure
                 cross join lateral aclexplode(
                     coalesce(procedure.proacl, acldefault('f', procedure.proowner))
                 ) privilege
                where procedure.oid = to_regprocedure(function_signature)
                  and privilege.grantee = 0
                  and privilege.privilege_type = 'EXECUTE'
           ) then
            raise exception 'server-only function privilege regression: %', function_signature;
        end if;
    end loop;
    if has_function_privilege(
           'anon', 'public.bind_automation_job_browserbase_epoch()', 'EXECUTE'
       )
       or has_function_privilege(
           'authenticated', 'public.bind_automation_job_browserbase_epoch()', 'EXECUTE'
       )
       or has_function_privilege(
           'service_role', 'public.bind_automation_job_browserbase_epoch()', 'EXECUTE'
       )
       or has_function_privilege(
           'service_role',
           'public.save_browser_connection_context(uuid,text,bigint,text,text)',
           'EXECUTE'
       )
       or not has_function_privilege(
           'authenticated', 'public.begin_account_deletion(text)', 'EXECUTE'
       )
       or has_function_privilege(
           'anon', 'public.begin_account_deletion(text)', 'EXECUTE'
       ) then
        raise exception 'Browserbase trigger/legacy/account privilege regression';
    end if;
end;
$assert_user_provider_credentials_server_only$;

commit;
