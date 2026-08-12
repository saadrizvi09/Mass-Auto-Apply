-- Hosted discovery and reviewed, tenant-isolated browser applications.
--
-- This migration intentionally keeps the original five-argument queue RPC
-- contract. Application revision IDs are accepted in payload.form_revision_id
-- and copied into a real foreign-key column after ownership validation.

begin;

-- Providers for which a user may create a dedicated managed-browser context.
-- LinkedIn is deliberately absent: guest discovery does not use a signed-in
-- browser context, and LinkedIn Easy Apply is not represented as OAuth access.
create or replace function public.is_managed_application_provider(provider_input text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select provider_input = any (array[
        'google_forms', 'greenhouse', 'lever', 'ashby', 'yc', 'wellfound',
        'cutshort', 'instahyre'
    ]::text[])
$$;

revoke all on function public.is_managed_application_provider(text)
    from public, anon, authenticated;
grant execute on function public.is_managed_application_provider(text) to service_role;

-- Preserve every managed-browser lifecycle contract while replacing the old
-- three-provider gate in-place. The assertion makes migration drift fail closed.
do $extend_managed_browser_lifecycles$
declare
    routine regprocedure;
    routine_definition text;
    old_gate constant text :=
        'provider_input not in (''greenhouse'', ''lever'', ''ashby'')';
begin
    foreach routine in array array[
        'public.begin_browser_start(uuid,text)'::regprocedure,
        'public.save_browser_connection_context(uuid,text,bigint,text,text)'::regprocedure,
        'public.save_browser_connection_session(uuid,text,bigint,uuid,text,text)'::regprocedure,
        'public.confirm_browser_start(uuid,text,bigint,uuid,text,text)'::regprocedure,
        'public.abort_browser_start(uuid,text,bigint,uuid,text,boolean)'::regprocedure,
        'public.finish_browser_start(uuid,text,bigint,uuid,text)'::regprocedure,
        'public.begin_browser_disconnect(uuid,text)'::regprocedure,
        'public.finish_browser_disconnect(uuid,text,bigint,uuid)'::regprocedure
    ]
    loop
        routine_definition := pg_get_functiondef(routine);
        if strpos(routine_definition, old_gate) = 0 then
            raise exception 'managed browser provider gate missing from %', routine;
        end if;
        routine_definition := replace(
            routine_definition,
            old_gate,
            'not public.is_managed_application_provider(provider_input)'
        );
        execute routine_definition;
    end loop;
end;
$extend_managed_browser_lifecycles$;

create table public.discovery_preferences (
    user_id uuid primary key references auth.users(id) on delete cascade,
    enabled_sources text[] not null default array['telegram', 'rss', 'public_ats']::text[]
        check (
            cardinality(enabled_sources) <= 7
            and enabled_sources <@ array[
                'telegram', 'rss', 'referral_digest', 'csv', 'xlsx',
                'public_ats', 'linkedin_guest'
            ]::text[]
        ),
    keywords text[] not null default '{}'::text[] check (
        cardinality(keywords) <= 50
        and octet_length(array_to_string(keywords, ',')) <= 8192
    ),
    excluded_keywords text[] not null default '{}'::text[] check (
        cardinality(excluded_keywords) <= 50
        and octet_length(array_to_string(excluded_keywords, ',')) <= 8192
    ),
    locations text[] not null default '{}'::text[] check (
        cardinality(locations) <= 30
        and octet_length(array_to_string(locations, ',')) <= 4096
    ),
    remote_only boolean not null default false,
    schedule_enabled boolean not null default false,
    schedule_interval_minutes integer not null default 360
        check (schedule_interval_minutes between 15 and 1440),
    max_results_per_run integer not null default 100
        check (max_results_per_run between 1 and 200),
    feed_urls text[] not null default '{}'::text[] check (
        cardinality(feed_urls) <= 32
        and octet_length(array_to_string(feed_urls, ',')) <= 32768
    ),
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 32768
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create trigger discovery_preferences_set_updated_at
    before update on public.discovery_preferences
    for each row execute function public.set_updated_at();

-- A form revision is an immutable snapshot of the provider schema and proposed
-- answers. Only review/submission state may advance. Any changed question or
-- answer therefore receives a new revision and invalidates the prior approval.
create table public.application_form_revisions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    application_id uuid not null references public.applications(id) on delete cascade,
    job_id uuid not null references public.jobs(id) on delete cascade,
    resume_id uuid not null references public.resumes(id) on delete cascade,
    provider text not null check (provider in (
        'google_forms', 'greenhouse', 'lever', 'ashby', 'yc', 'wellfound',
        'cutshort', 'instahyre'
    )),
    form_url text not null check (
        char_length(form_url) between 8 and 2048
        and form_url ~* '^https?://[^[:space:]]+$'
    ),
    revision bigint not null check (revision >= 1),
    schema_hash text not null check (schema_hash ~ '^[0-9a-f]{64}$'),
    question_schema jsonb not null check (
        jsonb_typeof(question_schema) = 'array'
        and octet_length(question_schema::text) <= 262144
    ),
    answers jsonb not null default '{}'::jsonb check (
        jsonb_typeof(answers) = 'object'
        and octet_length(answers::text) <= 262144
    ),
    status text not null default 'scanned' check (status in (
        'scanned', 'prefilled', 'approved', 'submitted', 'needs_attention',
        'failed', 'superseded'
    )),
    approved_revision bigint,
    approved_schema_hash text,
    approved_at timestamptz,
    submitted_at timestamptz,
    provider_submission_id text check (
        provider_submission_id is null or char_length(provider_submission_id) <= 1024
    ),
    submission_result jsonb check (
        submission_result is null or (
            jsonb_typeof(submission_result) = 'object'
            and octet_length(submission_result::text) <= 32768
        )
    ),
    last_error text check (last_error is null or char_length(last_error) <= 500),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (application_id, revision),
    check (
        (approved_revision is null and approved_schema_hash is null and approved_at is null)
        or (
            approved_revision = revision
            and approved_schema_hash = schema_hash
            and approved_at is not null
        )
    ),
    check (
        status <> 'approved'
        or (approved_revision = revision and approved_schema_hash = schema_hash)
    ),
    check (
        status <> 'submitted'
        or (
            approved_revision = revision
            and approved_schema_hash = schema_hash
            and approved_at is not null
            and submitted_at is not null
        )
    )
);

create index application_form_revisions_user_created_idx
    on public.application_form_revisions (user_id, created_at desc);
create index application_form_revisions_application_revision_idx
    on public.application_form_revisions (application_id, revision desc);
create index application_form_revisions_job_idx
    on public.application_form_revisions (job_id);
create index application_form_revisions_resume_idx
    on public.application_form_revisions (resume_id);
create unique index application_form_revisions_one_live_idx
    on public.application_form_revisions (application_id)
    where status in ('scanned', 'prefilled', 'approved', 'needs_attention');

create trigger application_form_revisions_set_updated_at
    before update on public.application_form_revisions
    for each row execute function public.set_updated_at();

create or replace function public.guard_application_form_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.user_id is distinct from old.user_id
       or new.application_id is distinct from old.application_id
       or new.job_id is distinct from old.job_id
       or new.resume_id is distinct from old.resume_id
       or new.provider is distinct from old.provider
       or new.form_url is distinct from old.form_url
       or new.revision is distinct from old.revision
       or new.schema_hash is distinct from old.schema_hash
       or new.question_schema is distinct from old.question_schema
       or new.created_at is distinct from old.created_at then
        raise exception using errcode = 'P0001', message = 'form_revision_immutable';
    end if;

    -- The sole content transition is the authenticated approval transaction:
    -- pending suggested answers may be replaced by the user's reviewed answers
    -- while that exact revision/schema is sealed. Afterwards answers are fixed.
    if new.answers is distinct from old.answers and not (
        old.status in ('scanned', 'prefilled')
        and old.approved_at is null
        and new.status = 'approved'
        and new.approved_revision = new.revision
        and new.approved_schema_hash = new.schema_hash
        and new.approved_at is not null
    ) then
        raise exception using errcode = 'P0001', message = 'form_revision_immutable';
    end if;

    if old.approved_at is not null and (
        new.approved_revision is distinct from old.approved_revision
        or new.approved_schema_hash is distinct from old.approved_schema_hash
        or new.approved_at is distinct from old.approved_at
    ) then
        raise exception using errcode = 'P0001', message = 'form_approval_immutable';
    end if;
    if old.status in ('submitted', 'superseded') and new.status <> old.status then
        raise exception using errcode = 'P0001', message = 'form_revision_locked';
    end if;
    if new.status = 'approved' and not (
        new.approved_revision = new.revision
        and new.approved_schema_hash = new.schema_hash
        and new.approved_at is not null
    ) then
        raise exception using errcode = 'P0001', message = 'form_approval_required';
    end if;
    if new.status = 'submitted' and not (
        new.approved_revision = new.revision
        and new.approved_schema_hash = new.schema_hash
        and new.approved_at is not null
        and new.submitted_at is not null
    ) then
        raise exception using errcode = 'P0001', message = 'form_approval_required';
    end if;
    if new.status in ('scanned', 'prefilled') and new.approved_at is not null then
        raise exception using errcode = 'P0001', message = 'form_revision_state_invalid';
    end if;
    return new;
end;
$$;

create trigger application_form_revisions_guard_immutable
    before update on public.application_form_revisions
    for each row execute function public.guard_application_form_revision();

create or replace function public.enforce_application_form_revision_ownership()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if not exists (
        select 1
          from public.applications application
          join public.jobs job
            on job.id = new.job_id and job.user_id = new.user_id
          join public.resumes resume
            on resume.id = new.resume_id and resume.user_id = new.user_id
         where application.id = new.application_id
           and application.user_id = new.user_id
           and application.job_id = new.job_id
    ) then
        raise exception using errcode = '23503', message = 'owned_form_references_not_found';
    end if;
    return new;
end;
$$;

create trigger application_form_revisions_owned_references
    before insert or update of user_id, application_id, job_id, resume_id
    on public.application_form_revisions
    for each row execute function public.enforce_application_form_revision_ownership();

alter table public.automation_jobs
    add column form_revision_id uuid
        references public.application_form_revisions(id) on delete set null;
create index automation_jobs_form_revision_idx
    on public.automation_jobs (form_revision_id);

create or replace function public.enforce_automation_form_revision_ownership()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.form_revision_id is not null and not exists (
        select 1
          from public.application_form_revisions revision
         where revision.id = new.form_revision_id
           and revision.user_id = new.user_id
           and revision.application_id = new.application_id
    ) then
        raise exception using errcode = '23503', message = 'owned_form_revision_not_found';
    end if;
    return new;
end;
$$;

create trigger automation_jobs_owned_form_revision
    before insert or update of user_id, application_id, form_revision_id
    on public.automation_jobs
    for each row execute function public.enforce_automation_form_revision_ownership();

-- Existing users receive a row immediately; future users receive one through the
-- Auth provisioning trigger below.
insert into public.discovery_preferences (user_id)
select profile.user_id from public.profiles profile
on conflict (user_id) do nothing;

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

    insert into public.discovery_preferences (user_id)
    values (new.id)
    on conflict (user_id) do nothing;
    return new;
end;
$$;

revoke all on function public.handle_new_auth_user()
    from public, anon, authenticated;

alter table public.discovery_preferences enable row level security;
alter table public.application_form_revisions enable row level security;

create policy discovery_preferences_select_own
    on public.discovery_preferences for select to authenticated
    using (user_id = (select auth.uid()) and public.account_is_active());
create policy discovery_preferences_insert_own
    on public.discovery_preferences for insert to authenticated
    with check (user_id = (select auth.uid()) and public.account_is_active());
create policy discovery_preferences_update_own
    on public.discovery_preferences for update to authenticated
    using (user_id = (select auth.uid()) and public.account_is_active())
    with check (user_id = (select auth.uid()) and public.account_is_active());
create policy discovery_preferences_delete_own
    on public.discovery_preferences for delete to authenticated
    using (user_id = (select auth.uid()) and public.account_is_active());

create policy application_form_revisions_select_own
    on public.application_form_revisions for select to authenticated
    using (user_id = (select auth.uid()) and public.account_is_active());

revoke all on public.discovery_preferences, public.application_form_revisions
    from public, anon, authenticated;
grant select, insert, update, delete on public.discovery_preferences to authenticated;
grant select on public.application_form_revisions to authenticated;
grant all on public.discovery_preferences, public.application_form_revisions to service_role;

revoke all on function public.guard_application_form_revision()
    from public, anon, authenticated;
revoke all on function public.enforce_application_form_revision_ownership()
    from public, anon, authenticated;
revoke all on function public.enforce_automation_form_revision_ownership()
    from public, anon, authenticated;

alter table public.jobs add column last_discovered_at timestamptz;
create index jobs_user_last_discovered_idx
    on public.jobs (user_id, last_discovered_at desc)
    where last_discovered_at is not null;

-- Internal race-safe ingestion. Public wrappers below derive the tenant from
-- auth.uid() or from a worker's current queue lease; callers never supply it.
-- Existing user-controlled status and job contents are not overwritten when a
-- discovery source sees the same normalized URL.
create or replace function public.ingest_discovered_jobs_for_user(
    user_id_input uuid,
    jobs_input jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := user_id_input;
    item jsonb;
    saved_job public.jobs%rowtype;
    item_metadata jsonb;
    normalized_url_input text;
    apply_url_input text;
    contact_email_input text;
    source_input text;
    prior_job_id uuid;
    saved_job_id uuid;
    was_inserted boolean;
    result_items jsonb := '[]'::jsonb;
    inserted_count integer := 0;
    updated_count integer := 0;
    timestamp_now timestamptz := clock_timestamp();
begin
    if current_user_id is null then
        raise exception using errcode = 'P0002', message = 'active_profile_not_found';
    end if;
    perform 1 from public.profiles profile
     where profile.user_id = current_user_id and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'active_profile_not_found';
    end if;
    if jobs_input is null or jsonb_typeof(jobs_input) <> 'array'
       or jsonb_array_length(jobs_input) < 1
       or jsonb_array_length(jobs_input) > 200
       or octet_length(jobs_input::text) > 2097152 then
        raise exception using errcode = '22023', message = 'discovered_jobs_invalid';
    end if;

    -- Serialize one tenant's discovery upserts so action counts remain exact.
    perform pg_advisory_xact_lock(hashtextextended(
        'discovered-jobs:' || current_user_id::text, 0
    ));

    for item in select value from jsonb_array_elements(jobs_input)
    loop
        if jsonb_typeof(item) <> 'object' then
            raise exception using errcode = '22023', message = 'discovered_job_invalid';
        end if;
        source_input := lower(nullif(btrim(item ->> 'source'), ''));
        normalized_url_input := nullif(btrim(item ->> 'normalized_url'), '');
        apply_url_input := nullif(btrim(item ->> 'apply_url'), '');
        contact_email_input := nullif(btrim(item ->> 'contact_email'), '');
        item_metadata := coalesce(item -> 'metadata', '{}'::jsonb);

        if source_input is null or char_length(source_input) > 60
           or source_input = 'ziprecruiter'
           or nullif(btrim(item ->> 'title'), '') is null
           or char_length(item ->> 'title') > 240
           or nullif(btrim(item ->> 'company'), '') is null
           or char_length(item ->> 'company') > 240
           or nullif(btrim(item ->> 'description'), '') is null
           or char_length(item ->> 'description') not between 20 and 25000
           or (item ? 'status' and item ->> 'status' <> 'saved')
           or (item ? 'location' and char_length(item ->> 'location') > 240)
           or (item ? 'external_id' and char_length(item ->> 'external_id') > 255)
           or (contact_email_input is not null and (
                char_length(contact_email_input) > 320
                or contact_email_input !~* '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
           ))
           or jsonb_typeof(item_metadata) <> 'object'
           or octet_length(item_metadata::text) > 32768 then
            raise exception using errcode = '22023', message = 'discovered_job_invalid';
        end if;
        if (normalized_url_input is not null and (
                char_length(normalized_url_input) > 2048
                or normalized_url_input !~* '^https?://[^[:space:]]+$'
            ))
           or (apply_url_input is not null and (
                char_length(apply_url_input) > 2048
                or apply_url_input !~* '^https?://[^[:space:]]+$'
            )) then
            raise exception using errcode = '22023', message = 'discovered_job_url_invalid';
        end if;

        prior_job_id := null;
        if normalized_url_input is not null then
            select job.id into prior_job_id
              from public.jobs job
             where job.user_id = current_user_id
               and job.normalized_url = normalized_url_input
             for update;
        end if;

        if prior_job_id is not null then
            update public.jobs job
               set metadata = job.metadata || jsonb_build_object(
                       'discovered', true,
                       'discovery', item_metadata,
                       'last_discovered_at', timestamp_now
                   ),
                   last_discovered_at = timestamp_now
             where job.id = prior_job_id and job.user_id = current_user_id
            returning job.* into saved_job;
            was_inserted := false;
        else
            insert into public.jobs (
                user_id, source, external_id, normalized_url, apply_url, title,
                company, location, description, contact_email, status, metadata,
                last_discovered_at
            ) values (
                current_user_id,
                source_input,
                nullif(btrim(item ->> 'external_id'), ''),
                normalized_url_input,
                apply_url_input,
                btrim(item ->> 'title'),
                btrim(item ->> 'company'),
                nullif(btrim(item ->> 'location'), ''),
                btrim(item ->> 'description'),
                contact_email_input,
                'saved',
                jsonb_build_object(
                    'discovered', true,
                    'discovery', item_metadata,
                    'first_discovered_at', timestamp_now,
                    'last_discovered_at', timestamp_now
                ),
                timestamp_now
            )
            on conflict (user_id, normalized_url)
                where normalized_url is not null
            do update set
                metadata = jobs.metadata || jsonb_build_object(
                    'discovered', true,
                    'discovery', item_metadata,
                    'last_discovered_at', timestamp_now
                ),
                last_discovered_at = timestamp_now
            returning jobs.id, (xmax = 0) into saved_job_id, was_inserted;
            select job.* into saved_job
              from public.jobs job
             where job.id = saved_job_id and job.user_id = current_user_id;
        end if;

        if was_inserted then
            inserted_count := inserted_count + 1;
            result_items := result_items || jsonb_build_array(
                to_jsonb(saved_job) || jsonb_build_object('discovery_action', 'inserted')
            );
        else
            updated_count := updated_count + 1;
            result_items := result_items || jsonb_build_array(
                to_jsonb(saved_job) || jsonb_build_object('discovery_action', 'updated')
            );
        end if;
    end loop;

    return jsonb_build_object(
        'items', result_items,
        'count', inserted_count + updated_count,
        'inserted', inserted_count,
        'updated', updated_count
    );
end;
$$;

revoke all on function public.ingest_discovered_jobs_for_user(uuid, jsonb)
    from public, anon, authenticated, service_role;

create or replace function public.ingest_discovered_jobs(jobs_input jsonb)
returns jsonb
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
    return public.ingest_discovered_jobs_for_user(current_user_id, jobs_input);
end;
$$;

-- Discovery workers are bound to the exact current queue lease and derive the
-- tenant exclusively from that row.
create or replace function public.ingest_discovered_jobs(
    job_id uuid,
    worker_id text,
    jobs jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_jobs alias for $3;
    queue_job public.automation_jobs%rowtype;
begin
    if p_job_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128 then
        raise exception using errcode = '22023', message = 'discovery_job_invalid';
    end if;
    select automation.* into queue_job
      from public.automation_jobs automation
     where automation.id = p_job_id
       and automation.kind in ('discover_public_feeds', 'discover_linkedin_guest')
       and automation.status = 'running'
       and automation.locked_by = p_worker_id
       and automation.lease_expires_at >= clock_timestamp()
       and automation.cancel_requested_at is null
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'discovery_job_not_owned';
    end if;
    return public.ingest_discovered_jobs_for_user(queue_job.user_id, p_jobs);
end;
$$;

revoke all on function public.ingest_discovered_jobs(jsonb) from public, anon;
grant execute on function public.ingest_discovered_jobs(jsonb) to authenticated;
revoke all on function public.ingest_discovered_jobs(uuid, text, jsonb)
    from public, anon, authenticated;
grant execute on function public.ingest_discovered_jobs(uuid, text, jsonb)
    to service_role;

-- Exact review: the caller must present the latest immutable revision, schema
-- hash, and answers that were rendered in the browser. A stale tab cannot approve
-- a newly scanned or regenerated form.
create or replace function public.approve_application_form_revision(
    revision_id_input uuid,
    revision_input bigint,
    schema_hash_input text,
    answers_input jsonb
)
returns setof public.application_form_revisions
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    target_revision public.application_form_revisions%rowtype;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if revision_id_input is null or revision_input is null or revision_input < 1
       or schema_hash_input is null or schema_hash_input !~ '^[0-9a-f]{64}$'
       or answers_input is null or jsonb_typeof(answers_input) <> 'object'
       or octet_length(answers_input::text) > 262144 then
        raise exception using errcode = '22023', message = 'form_approval_input_invalid';
    end if;

    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = revision_id_input
       and revision.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'form_revision_not_found';
    end if;
    if target_revision.revision <> revision_input
       or target_revision.schema_hash <> schema_hash_input
       or exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = target_revision.application_id
               and newer.revision > target_revision.revision
       ) then
        raise exception using errcode = 'P0001', message = 'form_revision_stale';
    end if;

    -- An identical retry is safe; a sealed revision can never be approved with
    -- different answers.
    if target_revision.status = 'approved' then
        if target_revision.answers <> answers_input
           or target_revision.approved_revision <> target_revision.revision
           or target_revision.approved_schema_hash <> target_revision.schema_hash
           or target_revision.approved_at is null then
            raise exception using errcode = 'P0001', message = 'form_approval_sealed';
        end if;
        return next target_revision;
        return;
    elsif target_revision.status not in ('scanned', 'prefilled') then
        raise exception using errcode = 'P0001', message = 'form_revision_stale';
    end if;

    return query
    update public.application_form_revisions revision
       set answers = answers_input, status = 'approved',
           approved_revision = revision.revision,
           approved_schema_hash = revision.schema_hash, approved_at = clock_timestamp(),
           last_error = null
     where revision.id = target_revision.id and revision.user_id = current_user_id
    returning revision.*;

    insert into public.audit_events (
        user_id, event_type, resource_type, resource_id, metadata
    ) values (
        current_user_id, 'application.form.approved', 'application_form_revision',
        target_revision.id,
        jsonb_build_object(
            'application_id', target_revision.application_id,
            'provider', target_revision.provider,
            'revision', target_revision.revision,
            'schema_hash', target_revision.schema_hash
        )
    );
end;
$$;

revoke all on function public.approve_application_form_revision(uuid, bigint, text, jsonb)
    from public, anon;
grant execute on function public.approve_application_form_revision(uuid, bigint, text, jsonb)
    to authenticated;

-- Keep the legacy queue signature but enforce the expanded kind/provider matrix,
-- revision ownership, review gates, and complete removal of ZipRecruiter.
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
    target_revision public.application_form_revisions%rowtype;
    form_revision_id_input uuid;
    requested_resume_id uuid;
    active_count integer;
    daily_count integer;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if kind_input not in (
        'manual_handoff', 'ats_prepare', 'connection_check',
        'discover_public_feeds', 'discover_linkedin_guest',
        'application_scan', 'application_prefill', 'application_submit'
    ) then
        raise exception using errcode = '22023', message = 'automation_kind_invalid';
    end if;
    if provider_input is null or provider_input = 'ziprecruiter' or provider_input not in (
        'gmail', 'linkedin', 'telegram', 'rss', 'referral_digest', 'csv', 'xlsx',
        'public_feeds', 'public_ats', 'indeed', 'external_job_board',
        'google_forms', 'greenhouse',
        'lever', 'ashby', 'yc', 'wellfound', 'cutshort', 'instahyre'
    ) then
        raise exception using errcode = '22023', message = 'automation_provider_invalid';
    end if;
    if payload_input is null or jsonb_typeof(payload_input) <> 'object'
       or octet_length(payload_input::text) > 32768 then
        raise exception using errcode = '22023', message = 'automation_payload_invalid';
    end if;
    if idempotency_key_input is null
       or idempotency_key_input !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$' then
        raise exception using errcode = '22023', message = 'idempotency_key_invalid';
    end if;

    begin
        form_revision_id_input := nullif(payload_input ->> 'form_revision_id', '')::uuid;
        requested_resume_id := nullif(payload_input ->> 'resume_id', '')::uuid;
    exception when invalid_text_representation then
        raise exception using errcode = '22023', message = 'automation_reference_invalid';
    end;

    if kind_input = 'ats_prepare' and provider_input not in ('greenhouse', 'lever', 'ashby') then
        raise exception using errcode = 'P0001', message = 'provider_automation_unavailable';
    end if;
    if kind_input = 'discover_public_feeds' and provider_input not in (
        'public_feeds', 'telegram', 'rss', 'referral_digest', 'csv', 'xlsx', 'public_ats',
        'greenhouse', 'lever', 'ashby', 'yc', 'wellfound', 'cutshort', 'instahyre'
    ) then
        raise exception using errcode = 'P0001', message = 'provider_discovery_unavailable';
    end if;
    if kind_input = 'discover_linkedin_guest' and provider_input <> 'linkedin' then
        raise exception using errcode = 'P0001', message = 'provider_discovery_unavailable';
    end if;
    if provider_input = 'public_feeds' and kind_input <> 'discover_public_feeds' then
        raise exception using errcode = 'P0001', message = 'provider_automation_unavailable';
    end if;
    if kind_input in ('application_scan', 'application_prefill', 'application_submit')
       and not public.is_managed_application_provider(provider_input) then
        raise exception using errcode = 'P0001', message = 'provider_automation_unavailable';
    end if;
    if kind_input in ('application_scan', 'application_prefill', 'application_submit')
       and provider_input in ('yc', 'wellfound', 'cutshort', 'instahyre')
       and not exists (
            select 1
              from public.connections connection
              join public.connection_secrets secret
                on secret.connection_id = connection.id
               and secret.user_id = connection.user_id
              join public.connection_lifecycles lifecycle
                on lifecycle.user_id = connection.user_id
               and lifecycle.provider = connection.provider
               and lifecycle.generation = secret.browser_lifecycle_generation
             where connection.user_id = current_user_id
               and connection.provider = provider_input
               and connection.mode = 'managed_browser'
               and connection.status in ('connected', 'needs_attention')
               and lifecycle.status = 'connected'
               and nullif(btrim(secret.browser_context_id_ciphertext), '') is not null
       ) then
        raise exception using errcode = 'P0001', message = 'provider_connection_required';
    end if;

    if kind_input in (
        'manual_handoff', 'ats_prepare', 'application_scan',
        'application_prefill', 'application_submit'
    ) and application_id_input is null then
        raise exception using errcode = '22023', message = 'application_required';
    end if;
    if kind_input in (
        'connection_check', 'discover_public_feeds', 'discover_linkedin_guest'
    ) and application_id_input is not null then
        raise exception using errcode = '22023', message = 'application_not_allowed';
    end if;
    if application_id_input is not null and not exists (
        select 1 from public.applications application
         where application.id = application_id_input
           and application.user_id = current_user_id
    ) then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;

    if kind_input = 'application_scan' then
        if form_revision_id_input is not null then
            raise exception using errcode = '22023', message = 'form_revision_not_allowed';
        end if;
        if requested_resume_id is not null and not exists (
            select 1 from public.resumes resume
             where resume.id = requested_resume_id
               and resume.user_id = current_user_id and resume.is_active
        ) then
            raise exception using errcode = 'P0002', message = 'resume_not_found';
        elsif requested_resume_id is null and not exists (
            select 1 from public.resumes resume
             where resume.user_id = current_user_id and resume.is_active
        ) then
            raise exception using errcode = 'P0002', message = 'active_resume_not_found';
        end if;
    elsif kind_input in ('application_prefill', 'application_submit') then
        if form_revision_id_input is null then
            raise exception using errcode = '22023', message = 'form_revision_required';
        end if;
        select revision.* into target_revision
          from public.application_form_revisions revision
         where revision.id = form_revision_id_input
           and revision.user_id = current_user_id
           and revision.application_id = application_id_input
           and revision.provider = provider_input
         for share;
        if not found then
            raise exception using errcode = 'P0002', message = 'form_revision_not_found';
        end if;
        if exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = target_revision.application_id
               and newer.revision > target_revision.revision
        ) then
            raise exception using errcode = 'P0001', message = 'form_revision_stale';
        end if;
        if kind_input in ('application_prefill', 'application_submit') and not (
            target_revision.status = 'approved'
            and target_revision.approved_revision = target_revision.revision
            and target_revision.approved_schema_hash = target_revision.schema_hash
            and target_revision.approved_at is not null
        ) then
            raise exception using errcode = 'P0001', message = 'form_approval_required';
        end if;
    elsif form_revision_id_input is not null then
        raise exception using errcode = '22023', message = 'form_revision_not_allowed';
    end if;

    select job.* into existing_job
      from public.automation_jobs job
     where job.user_id = current_user_id
       and job.idempotency_key = idempotency_key_input;
    if found then
        if existing_job.kind is distinct from kind_input
           or existing_job.provider is distinct from provider_input
           or existing_job.application_id is distinct from application_id_input
           or existing_job.form_revision_id is distinct from form_revision_id_input
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
        user_id, application_id, form_revision_id, kind, provider, payload,
        idempotency_key
    ) values (
        current_user_id, application_id_input, form_revision_id_input, kind_input,
        provider_input, payload_input, idempotency_key_input
    )
    returning automation_jobs.*;
end;
$$;

revoke all on function public.enqueue_automation_job(text, text, uuid, jsonb, text)
    from public, anon;
grant execute on function public.enqueue_automation_job(text, text, uuid, jsonb, text)
    to authenticated;

-- Persist a worker-observed form snapshot. New content always receives the next
-- revision number under an application-scoped advisory lock and supersedes every
-- prior unsubmitted revision, including an approval from an older scan.
create or replace function public.store_application_form_scan(
    job_id uuid,
    worker_id text,
    provider text,
    form_url text,
    schema_hash text,
    question_schema jsonb,
    answers jsonb
)
returns setof public.application_form_revisions
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_provider alias for $3;
    p_form_url alias for $4;
    p_schema_hash alias for $5;
    p_question_schema alias for $6;
    p_answers alias for $7;
    queue_job public.automation_jobs%rowtype;
    target_application public.applications%rowtype;
    selected_resume public.resumes%rowtype;
    saved_revision public.application_form_revisions%rowtype;
    selected_resume_id uuid;
    next_revision bigint;
begin
    if p_job_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128
       or not public.is_managed_application_provider(p_provider)
       or p_form_url is null or char_length(p_form_url) > 2048
       or p_form_url !~* '^https?://[^[:space:]]+$'
       or p_schema_hash is null or p_schema_hash !~ '^[0-9a-f]{64}$'
       or p_question_schema is null or jsonb_typeof(p_question_schema) <> 'array'
       or octet_length(p_question_schema::text) > 262144
       or p_answers is null or jsonb_typeof(p_answers) <> 'object'
       or octet_length(p_answers::text) > 262144 then
        raise exception using errcode = '22023', message = 'form_scan_invalid';
    end if;

    select automation.* into queue_job
      from public.automation_jobs automation
     where automation.id = p_job_id
       and automation.kind = 'application_scan'
       and automation.provider = p_provider
       and automation.status = 'running'
       and automation.locked_by = p_worker_id
       and automation.lease_expires_at >= clock_timestamp()
       and automation.cancel_requested_at is null
     for update;
    if not found or queue_job.application_id is null then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;

    select application.* into target_application
      from public.applications application
     where application.id = queue_job.application_id
       and application.user_id = queue_job.user_id
       and application.job_id is not null
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;

    if queue_job.form_revision_id is not null then
        select resume.* into selected_resume
          from public.application_form_revisions revision
          join public.resumes resume
            on resume.id = revision.resume_id and resume.user_id = queue_job.user_id
         where revision.id = queue_job.form_revision_id
           and revision.application_id = queue_job.application_id
           and revision.user_id = queue_job.user_id
         for share of resume;
    else
        begin
            selected_resume_id := nullif(queue_job.payload ->> 'resume_id', '')::uuid;
        exception when invalid_text_representation then
            raise exception using errcode = '22023', message = 'resume_reference_invalid';
        end;
        select resume.* into selected_resume
          from public.resumes resume
         where resume.user_id = queue_job.user_id
           and (
               (selected_resume_id is not null and resume.id = selected_resume_id)
               or (selected_resume_id is null and resume.is_active)
           )
         order by resume.created_at desc
         for share
         limit 1;
    end if;
    if selected_resume.id is null then
        raise exception using errcode = 'P0002', message = 'active_resume_not_found';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'application-form:' || queue_job.application_id::text, 0
    ));
    if exists (
        select 1 from public.application_form_revisions revision
         where revision.application_id = queue_job.application_id
           and revision.status = 'submitted'
    ) then
        raise exception using errcode = 'P0001', message = 'application_already_submitted';
    end if;
    if exists (
        select 1 from public.automation_jobs active
         where active.user_id = queue_job.user_id
           and active.application_id = queue_job.application_id
           and active.kind in ('application_prefill', 'application_submit')
           and active.status = 'running'
    ) then
        raise exception using errcode = 'P0001', message = 'application_operation_in_progress';
    end if;
    select coalesce(max(revision.revision), 0) + 1 into next_revision
      from public.application_form_revisions revision
     where revision.application_id = queue_job.application_id;
    if next_revision > 50 then
        raise exception using errcode = 'P0001', message = 'form_revision_limit_reached';
    end if;

    update public.application_form_revisions revision
       set status = 'superseded'
     where revision.application_id = queue_job.application_id
       and revision.status <> 'superseded';

    insert into public.application_form_revisions (
        user_id, application_id, job_id, resume_id, provider, form_url,
        revision, schema_hash, question_schema, answers, status
    ) values (
        queue_job.user_id, queue_job.application_id, target_application.job_id,
        selected_resume.id, p_provider, p_form_url, next_revision,
        p_schema_hash, p_question_schema, p_answers,
        case when p_answers = '{}'::jsonb then 'scanned' else 'prefilled' end
    )
    returning * into saved_revision;

    update public.automation_jobs automation
       set progress = automation.progress || jsonb_build_object(
               'scan_revision_id', saved_revision.id,
               'scan_revision', saved_revision.revision,
               'schema_hash', saved_revision.schema_hash
           )
     where automation.id = queue_job.id
       and automation.status = 'running'
       and automation.locked_by = p_worker_id;
    return next saved_revision;
end;
$$;

create or replace function public.update_application_job_progress(
    job_id uuid,
    worker_id text,
    progress jsonb
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_progress alias for $3;
begin
    if p_job_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128
       or p_progress is null or jsonb_typeof(p_progress) <> 'object'
       or octet_length(p_progress::text) > 16384 then
        raise exception using errcode = '22023', message = 'job_progress_invalid';
    end if;
    return query
    update public.automation_jobs automation
       set progress = automation.progress || p_progress
     where automation.id = p_job_id
       and automation.kind in ('application_scan', 'application_prefill', 'application_submit')
       and automation.status = 'running'
       and automation.locked_by = p_worker_id
       and automation.lease_expires_at >= clock_timestamp()
       and automation.cancel_requested_at is null
    returning automation.*;
end;
$$;

-- Return only the tenant bundle bound to the caller's current worker lease.
-- Decrypted provider secrets are never stored in PostgreSQL.
create or replace function public.get_application_job_bundle(
    job_id uuid,
    worker_id text
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    queue_job public.automation_jobs%rowtype;
    target_application public.applications%rowtype;
    target_job public.jobs%rowtype;
    target_revision public.application_form_revisions%rowtype;
    target_resume public.resumes%rowtype;
    target_connection public.connections%rowtype;
    target_secret public.connection_secrets%rowtype;
    requested_resume_id uuid;
begin
    if p_job_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128 then
        raise exception using errcode = '22023', message = 'application_job_invalid';
    end if;
    select automation.* into queue_job
      from public.automation_jobs automation
     where automation.id = p_job_id
       and automation.kind in ('application_scan', 'application_prefill', 'application_submit')
       and automation.status = 'running'
       and automation.locked_by = p_worker_id
       and automation.lease_expires_at >= clock_timestamp()
       and automation.cancel_requested_at is null
     for share;
    if not found or queue_job.application_id is null then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;

    select application.* into target_application
      from public.applications application
     where application.id = queue_job.application_id
       and application.user_id = queue_job.user_id;
    if not found or target_application.job_id is null then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'application-form:' || queue_job.application_id::text, 0
    ));
    select job.* into target_job
      from public.jobs job
     where job.id = target_application.job_id and job.user_id = queue_job.user_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'job_not_found';
    end if;

    if queue_job.form_revision_id is not null then
        select revision.* into target_revision
          from public.application_form_revisions revision
         where revision.id = queue_job.form_revision_id
           and revision.user_id = queue_job.user_id
           and revision.application_id = queue_job.application_id
           and revision.job_id = target_job.id
           and revision.provider = queue_job.provider;
        if not found then
            raise exception using errcode = 'P0002', message = 'form_revision_not_found';
        end if;
        if queue_job.kind in ('application_prefill', 'application_submit') and not (
            target_revision.status = 'approved'
            and target_revision.approved_revision = target_revision.revision
            and target_revision.approved_schema_hash = target_revision.schema_hash
            and target_revision.approved_at is not null
            and not exists (
                select 1 from public.application_form_revisions newer
                 where newer.application_id = target_revision.application_id
                   and newer.revision > target_revision.revision
            )
        ) then
            raise exception using errcode = 'P0001', message = 'form_approval_required';
        end if;
        select resume.* into target_resume
          from public.resumes resume
         where resume.id = target_revision.resume_id
           and resume.user_id = queue_job.user_id;
    else
        begin
            requested_resume_id := nullif(queue_job.payload ->> 'resume_id', '')::uuid;
        exception when invalid_text_representation then
            raise exception using errcode = '22023', message = 'resume_reference_invalid';
        end;
        select resume.* into target_resume
          from public.resumes resume
         where resume.user_id = queue_job.user_id
           and (
               (requested_resume_id is not null and resume.id = requested_resume_id)
               or (requested_resume_id is null and resume.is_active)
           )
         order by resume.created_at desc
         limit 1;
    end if;
    if target_resume.id is null then
        raise exception using errcode = 'P0002', message = 'resume_not_found';
    end if;

    select connection.* into target_connection
      from public.connections connection
     where connection.user_id = queue_job.user_id
       and connection.provider = queue_job.provider
       and connection.mode = 'managed_browser';
    if found then
        select secret.* into target_secret
          from public.connection_secrets secret
         where secret.connection_id = target_connection.id
           and secret.user_id = queue_job.user_id;
    end if;

    return jsonb_build_object(
        'user_id', queue_job.user_id,
        'application_id', queue_job.application_id,
        'provider', queue_job.provider,
        'target_url', coalesce(target_revision.form_url, target_job.apply_url, target_job.normalized_url),
        'browser_context_id_ciphertext', target_secret.browser_context_id_ciphertext,
        'automation_job', to_jsonb(queue_job),
        'application', to_jsonb(target_application),
        'job', to_jsonb(target_job),
        'form_revision', case
            when target_revision.id is null then null
            else to_jsonb(target_revision)
        end,
        'approved_answers', case
            when target_revision.approved_revision = target_revision.revision
             and target_revision.approved_schema_hash = target_revision.schema_hash
             and target_revision.approved_at is not null
                then target_revision.answers
            else null
        end,
        'resume', jsonb_build_object(
            'id', target_resume.id,
            'storage_path', target_resume.storage_path,
            'original_name', target_resume.original_name,
            'mime_type', target_resume.mime_type,
            'size_bytes', target_resume.size_bytes,
            'sha256', target_resume.sha256
        ),
        'connection', case
            when target_connection.id is null then null
            else jsonb_build_object(
                'id', target_connection.id,
                'status', target_connection.status,
                'generation', target_secret.browser_lifecycle_generation
            )
        end
    );
end;
$$;

-- Commit the exact approved revision after the provider confirms submission.
create or replace function public.record_application_form_submission(
    job_id uuid,
    worker_id text,
    provider_submission_id text,
    result jsonb
)
returns setof public.application_form_revisions
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    p_job_id alias for $1;
    p_worker_id alias for $2;
    p_provider_submission_id alias for $3;
    p_result alias for $4;
    queue_job public.automation_jobs%rowtype;
    target_revision public.application_form_revisions%rowtype;
begin
    if p_job_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128
       or (p_provider_submission_id is not null
           and char_length(p_provider_submission_id) > 1024)
       or p_result is null or jsonb_typeof(p_result) <> 'object'
       or octet_length(p_result::text) > 32768 then
        raise exception using errcode = '22023', message = 'submission_result_invalid';
    end if;
    select automation.* into queue_job
      from public.automation_jobs automation
     where automation.id = p_job_id
       and automation.kind = 'application_submit'
       and automation.status = 'running'
       and automation.locked_by = p_worker_id
       and automation.lease_expires_at >= clock_timestamp()
       and automation.cancel_requested_at is null
     for update;
    if not found or queue_job.form_revision_id is null then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;
    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = queue_job.form_revision_id
       and revision.user_id = queue_job.user_id
       and revision.application_id = queue_job.application_id
       and revision.provider = queue_job.provider
       and revision.status = 'approved'
       and revision.approved_revision = revision.revision
       and revision.approved_schema_hash = revision.schema_hash
       and revision.approved_at is not null
       and not exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = revision.application_id
               and newer.revision > revision.revision
       )
     for update;
    if not found then
        raise exception using errcode = 'P0001', message = 'form_approval_required';
    end if;

    update public.applications application
       set status = 'applied'
     where application.id = queue_job.application_id
       and application.user_id = queue_job.user_id;
    update public.jobs job
       set status = 'applied'
     where job.id = target_revision.job_id and job.user_id = queue_job.user_id;
    return query
    update public.application_form_revisions revision
       set status = 'submitted', submitted_at = clock_timestamp(),
           provider_submission_id = nullif(btrim(p_provider_submission_id), ''),
           submission_result = p_result, last_error = null
     where revision.id = target_revision.id
    returning revision.*;
end;
$$;

revoke all on function public.store_application_form_scan(
    uuid, text, text, text, text, jsonb, jsonb
) from public, anon, authenticated;
revoke all on function public.update_application_job_progress(uuid, text, jsonb)
    from public, anon, authenticated;
revoke all on function public.get_application_job_bundle(uuid, text)
    from public, anon, authenticated;
revoke all on function public.record_application_form_submission(uuid, text, text, jsonb)
    from public, anon, authenticated;
grant execute on function public.store_application_form_scan(
    uuid, text, text, text, text, jsonb, jsonb
) to service_role;
grant execute on function public.update_application_job_progress(uuid, text, jsonb)
    to service_role;
grant execute on function public.get_application_job_bundle(uuid, text)
    to service_role;
grant execute on function public.record_application_form_submission(uuid, text, text, jsonb)
    to service_role;

-- Deployment assertions: no anonymous worker/approval surface and no accidental
-- browser mutation grant on immutable revision rows.
do $migration_assert_hosted_applications$
begin
    if has_table_privilege(
        'authenticated', 'public.application_form_revisions', 'INSERT'
    ) or has_table_privilege(
        'authenticated', 'public.application_form_revisions', 'UPDATE'
    ) or has_table_privilege(
        'authenticated', 'public.application_form_revisions', 'DELETE'
    ) then
        raise exception 'application form revision mutations must remain RPC-managed';
    end if;
    if has_function_privilege(
        'anon', 'public.get_application_job_bundle(uuid,text)', 'EXECUTE'
    ) then
        raise exception 'application bundle must remain service-role-only';
    end if;
    if has_function_privilege(
        'anon', 'public.approve_application_form_revision(uuid,bigint,text,jsonb)',
        'EXECUTE'
    ) then
        raise exception 'anonymous form approval must remain forbidden';
    end if;
end;
$migration_assert_hosted_applications$;

commit;
