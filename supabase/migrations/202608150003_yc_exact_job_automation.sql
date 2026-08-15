-- Enable reviewed YC applications without adding a YC scraper or search job.
-- The authority is one service-bound, tenant-owned exact job-detail URL. The
-- existing browser lifecycle, Browserbase credential epoch, immutable revision,
-- approval, lease, cancellation, and one-time submit fences remain authoritative.

begin;

-- YC-controlled hosts belong exclusively to the stricter YC adapter. They must
-- never be rebound through the generic exact-host company-form escape hatch.
create or replace function public.validated_company_form_host(url_input text)
returns text
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    with authority as (
        select split_part(
                   split_part(
                       split_part(substring(url_input from 9), '/', 1),
                       '?', 1
                   ),
                   '#', 1
               ) as value
    ), candidate as (
        select lower(value) as value
          from authority
         where url_input ~ '^https://[^[:space:]]+$'
           and char_length(url_input) between 9 and 2048
           and char_length(value) between 4 and 253
           and strpos(value, '@') = 0
           and strpos(value, ':') = 0
    )
    select value
      from candidate
     where value ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'
       and value ~ '\.[a-z]{2,63}$'
       and value !~ '(^|\.)(localhost|local|internal|test|invalid|example|home|lan|arpa|onion)$'
       and value !~ '(^|\.)(nip\.io|sslip\.io|localtest\.me|lvh\.me)$'
       and value !~ '(^|\.)(ycombinator\.com|workatastartup\.com)$'
       and value !~ '^[0-9]+(\.[0-9]+){3}$'
$$;

revoke all on function public.validated_company_form_host(text)
    from public, anon, authenticated;
grant execute on function public.validated_company_form_host(text) to service_role;

-- Retire any pre-migration generic binding or in-flight work on a YC-owned
-- host before the stricter provider authority is installed.
update public.automation_jobs job
   set status = case when job.status = 'queued' then 'cancelled' else job.status end,
       cancel_requested_at = coalesce(job.cancel_requested_at, clock_timestamp()),
       error_code = 'company_form_target_invalid',
       error_message = 'YC-owned URLs require the exact YC job workflow.',
       locked_by = case when job.status = 'queued' then null else job.locked_by end,
       locked_at = case when job.status = 'queued' then null else job.locked_at end,
       lease_expires_at = case when job.status = 'queued' then null else job.lease_expires_at end,
       updated_at = clock_timestamp()
 where job.provider = 'company_form'
   and job.status in ('queued', 'running')
   and lower(coalesce(job.payload ->> 'company_form_host', ''))
       ~ '(^|\.)(ycombinator\.com|workatastartup\.com)$';

delete from public.company_form_targets binding
 where lower(binding.exact_host)
       ~ '(^|\.)(ycombinator\.com|workatastartup\.com)$';

create or replace function public.canonical_yc_job_url(url_input text)
returns text
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select case
        when btrim(url_input) ~* '^https://(www\.)?ycombinator\.com/companies/[a-z0-9]([a-z0-9-]{0,98}[a-z0-9])?/jobs/[a-z0-9]{5,64}(-[a-z0-9]+)*/?$'
            then regexp_replace(
                regexp_replace(
                    btrim(url_input),
                    '^https://(www\.)?ycombinator\.com',
                    'https://www.ycombinator.com',
                    'i'
                ),
                '/$',
                ''
            )
        else null
    end
$$;

comment on function public.canonical_yc_job_url(text) is
    'Returns only exact public YC job-detail URLs; never account, collection, search, or discovery URLs.';

revoke all on function public.canonical_yc_job_url(text)
    from public, anon, authenticated;
grant execute on function public.canonical_yc_job_url(text) to service_role;

create or replace function public.canonical_yc_application_url(
    form_url_input text,
    target_url_input text
)
returns text
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select case
        when public.canonical_yc_job_url(form_url_input) = target_url_input
            then target_url_input
        when btrim(form_url_input) ~* '^https://(www\.)?workatastartup\.com/application/?\?signup_job_id=[1-9][0-9]{0,18}$'
            then 'https://www.workatastartup.com/application?signup_job_id='
                || substring(
                    btrim(form_url_input)
                    from '(?i)signup_job_id=([1-9][0-9]{0,18})$'
                )
        else null
    end
$$;

comment on function public.canonical_yc_application_url(text, text) is
    'Returns one canonical application identity reached from a bound YC job target; account/login handoffs are never identities.';

revoke all on function public.canonical_yc_application_url(text, text)
    from public, anon, authenticated;
grant execute on function public.canonical_yc_application_url(text, text)
    to service_role;

create table public.provider_application_preferences (
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null check (provider = 'yc'),
    query text check (
        query is null or (
            char_length(query) between 1 and 160
            and query = btrim(query)
            and query !~ '[[:cntrl:]]'
        )
    ),
    remote_only boolean not null default false,
    result_limit integer not null default 10 check (result_limit between 1 and 20),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (user_id, provider)
);

comment on table public.provider_application_preferences is
    'Tenant-owned matching/display preferences only. Rows never authorize or enqueue provider discovery.';

create trigger provider_application_preferences_set_updated_at
    before update on public.provider_application_preferences
    for each row execute function public.set_updated_at();

alter table public.provider_application_preferences enable row level security;
create policy provider_application_preferences_select_own
    on public.provider_application_preferences for select
    to authenticated
    using (auth.uid() = user_id);
create policy provider_application_preferences_insert_own
    on public.provider_application_preferences for insert
    to authenticated
    with check (auth.uid() = user_id);
create policy provider_application_preferences_update_own
    on public.provider_application_preferences for update
    to authenticated
    using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy provider_application_preferences_delete_own
    on public.provider_application_preferences for delete
    to authenticated
    using (auth.uid() = user_id);

revoke all on public.provider_application_preferences from public, anon;
grant select, insert, update, delete on public.provider_application_preferences
    to authenticated;
grant all on public.provider_application_preferences to service_role;

create table public.yc_application_targets (
    job_id uuid primary key references public.jobs(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    target_url text not null check (
        char_length(target_url) between 32 and 2048
        and target_url = public.canonical_yc_job_url(target_url)
    ),
    application_url text,
    application_bound_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, job_id),
    check (
        (application_url is null and application_bound_at is null)
        or (
            application_url is not null
            and application_bound_at is not null
            and char_length(application_url) between 32 and 2048
            and coalesce(
                application_url = public.canonical_yc_application_url(
                    application_url, target_url
                ),
                false
            )
        )
    )
);

comment on table public.yc_application_targets is
    'Service-owned authority for one explicitly saved exact YC job URL and the one resolved application identity atomically bound by its first accepted scan.';

create index yc_application_targets_user_idx
    on public.yc_application_targets (user_id, updated_at desc);

create trigger yc_application_targets_set_updated_at
    before update on public.yc_application_targets
    for each row execute function public.set_updated_at();

create or replace function public.guard_yc_application_target()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if public.canonical_yc_job_url(new.target_url) is distinct from new.target_url then
        raise exception using errcode = '22023', message = 'yc_exact_job_url_required';
    end if;

    if tg_op = 'INSERT' then
        if new.application_url is not null or new.application_bound_at is not null then
            raise exception using
                errcode = 'P0001', message = 'yc_application_identity_untrusted';
        end if;
    elsif new.job_id is distinct from old.job_id
       or new.user_id is distinct from old.user_id
       or new.target_url is distinct from old.target_url then
        -- Changing or recreating the exact target is the only reset boundary.
        new.application_url := null;
        new.application_bound_at := null;
    elsif new.application_url is distinct from old.application_url
       or new.application_bound_at is distinct from old.application_bound_at then
        if old.application_url is not null
           or old.application_bound_at is not null
           or new.application_url is null
           or new.application_bound_at is null
           or public.canonical_yc_application_url(
                new.application_url, new.target_url
              ) is distinct from new.application_url
           or not exists (
                select 1
                  from public.application_form_revisions revision
                  join public.applications application
                    on application.id = revision.application_id
                   and application.user_id = revision.user_id
                   and application.job_id = revision.job_id
                 where revision.user_id = new.user_id
                   and revision.job_id = new.job_id
                   and revision.provider = 'yc'
                   and revision.form_url = new.application_url
                   and revision.created_at >= old.updated_at
           ) then
            raise exception using
                errcode = 'P0001', message = 'yc_application_identity_immutable';
        end if;
    end if;

    if not exists (
        select 1
          from public.jobs job
         where job.id = new.job_id
           and job.user_id = new.user_id
           and job.apply_url = new.target_url
    ) then
        raise exception using errcode = '23503', message = 'yc_exact_job_url_changed';
    end if;
    return new;
end;
$$;

revoke all on function public.guard_yc_application_target()
    from public, anon, authenticated;
grant execute on function public.guard_yc_application_target() to service_role;

create trigger yc_application_targets_guard
    before insert or update of
        job_id, user_id, target_url, application_url, application_bound_at
    on public.yc_application_targets
    for each row execute function public.guard_yc_application_target();

alter table public.yc_application_targets enable row level security;
revoke all on public.yc_application_targets from public, anon, authenticated;
grant all on public.yc_application_targets to service_role;

-- YC becomes a hosted form provider only at the scan/revision pipeline. It is
-- deliberately not added to any discovery source, ATS-board, or feed registry.
create or replace function public.is_hosted_form_automation_provider(provider_input text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select provider_input = any (array[
        'google_forms', 'greenhouse', 'lever', 'ashby', 'yc', 'wellfound',
        'company_form'
    ]::text[])
$$;

revoke all on function public.is_hosted_form_automation_provider(text)
    from public, anon, authenticated;
grant execute on function public.is_hosted_form_automation_provider(text) to service_role;

-- A successful scan may keep YC's application dialog on the exact job page or
-- follow the page's controlled handoff to one numeric Work at a Startup
-- application. Account/login URLs are navigation-only and are never persisted
-- as form authority.
create or replace function public.is_yc_application_form_url(
    form_url_input text,
    target_url_input text
)
returns boolean
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select coalesce(
        public.canonical_yc_application_url(
            form_url_input, target_url_input
        ) = form_url_input,
        false
    )
$$;

revoke all on function public.is_yc_application_form_url(text, text)
    from public, anon, authenticated;
grant execute on function public.is_yc_application_form_url(text, text) to service_role;

create or replace function public.guard_yc_form_revision_target()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    bound_target public.yc_application_targets%rowtype;
    resolved_application_url text;
begin
    if new.provider <> 'yc' then
        return new;
    end if;

    select binding.* into bound_target
      from public.yc_application_targets binding
      join public.jobs job
        on job.id = binding.job_id
       and job.user_id = binding.user_id
       and job.apply_url = binding.target_url
      join public.applications application
        on application.id = new.application_id
       and application.user_id = binding.user_id
       and application.job_id = binding.job_id
     where binding.job_id = new.job_id
       and binding.user_id = new.user_id
     for update of binding;
    if not found then
        raise exception using errcode = 'P0001', message = 'yc_exact_job_url_required';
    end if;
    resolved_application_url := public.canonical_yc_application_url(
        new.form_url, bound_target.target_url
    );
    if resolved_application_url is null
       or resolved_application_url is distinct from new.form_url then
        raise exception using
            errcode = 'P0001', message = 'yc_application_identity_invalid';
    end if;
    if bound_target.application_url is not null
       and bound_target.application_url is distinct from resolved_application_url then
        raise exception using
            errcode = 'P0001', message = 'yc_application_identity_changed';
    end if;
    return new;
end;
$$;

revoke all on function public.guard_yc_form_revision_target()
    from public, anon, authenticated;
grant execute on function public.guard_yc_form_revision_target() to service_role;

create trigger application_form_revisions_yc_target
    before insert or update of user_id, application_id, job_id, provider, form_url
    on public.application_form_revisions
    for each row execute function public.guard_yc_form_revision_target();

create or replace function public.bind_yc_application_identity_from_revision()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    durable_application_url text;
begin
    if new.provider <> 'yc' then
        return new;
    end if;

    update public.yc_application_targets binding
       set application_url = new.form_url,
           application_bound_at = clock_timestamp()
     where binding.job_id = new.job_id
       and binding.user_id = new.user_id
       and binding.application_url is null
    returning binding.application_url into durable_application_url;

    if not found then
        select binding.application_url into durable_application_url
          from public.yc_application_targets binding
         where binding.job_id = new.job_id
           and binding.user_id = new.user_id;
    end if;
    if durable_application_url is distinct from new.form_url then
        raise exception using
            errcode = 'P0001', message = 'yc_application_identity_changed';
    end if;
    return new;
end;
$$;

revoke all on function public.bind_yc_application_identity_from_revision()
    from public, anon, authenticated;
grant execute on function public.bind_yc_application_identity_from_revision()
    to service_role;

create trigger application_form_revisions_yc_identity_bind
    after insert on public.application_form_revisions
    for each row execute function public.bind_yc_application_identity_from_revision();

-- The queue repeats only the non-secret exact job URL. Every stage rebinds it
-- to the same user/application/job, while prefill and submit additionally bind
-- the latest immutable approval. All other YC kinds, including discovery, fail.
create or replace function public.guard_yc_automation_job()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    bound_target public.yc_application_targets%rowtype;
    approved_revision public.application_form_revisions%rowtype;
    payload_target_url text;
begin
    if new.provider <> 'yc' then
        return new;
    end if;
    if new.kind not in ('application_scan', 'application_prefill', 'application_submit')
       or new.application_id is null then
        raise exception using errcode = 'P0001', message = 'provider_discovery_unavailable';
    end if;
    if new.payload is null
       or jsonb_typeof(new.payload) is distinct from 'object'
       or jsonb_typeof(new.payload -> 'yc_job_target_url') is distinct from 'string' then
        raise exception using errcode = '22023', message = 'yc_exact_job_url_required';
    end if;
    payload_target_url := new.payload ->> 'yc_job_target_url';
    if public.canonical_yc_job_url(payload_target_url) is distinct from payload_target_url then
        raise exception using errcode = '22023', message = 'yc_exact_job_url_required';
    end if;

    select binding.* into bound_target
      from public.yc_application_targets binding
      join public.jobs job
        on job.id = binding.job_id
       and job.user_id = binding.user_id
       and job.apply_url = binding.target_url
      join public.applications application
        on application.id = new.application_id
       and application.user_id = binding.user_id
       and application.job_id = binding.job_id
       and application.channel = 'ats'
     where binding.user_id = new.user_id
       and binding.target_url = payload_target_url;
    if not found then
        raise exception using errcode = 'P0001', message = 'yc_exact_job_url_changed';
    end if;

    if new.kind = 'application_scan' then
        if new.form_revision_id is not null then
            raise exception using errcode = 'P0001', message = 'form_revision_not_allowed';
        end if;
        return new;
    end if;

    if bound_target.application_url is null then
        raise exception using
            errcode = 'P0001', message = 'yc_application_identity_required';
    end if;

    if new.form_revision_id is null
       or new.payload ->> 'form_revision_id' is distinct from new.form_revision_id::text then
        raise exception using errcode = 'P0001', message = 'form_approval_required';
    end if;
    select revision.* into approved_revision
      from public.application_form_revisions revision
     where revision.id = new.form_revision_id
       and revision.user_id = new.user_id
       and revision.application_id = new.application_id
       and revision.job_id = bound_target.job_id
       and revision.provider = 'yc'
       and revision.form_url = bound_target.application_url
       and public.canonical_yc_application_url(
            revision.form_url, bound_target.target_url
       ) = bound_target.application_url
       and revision.status = 'approved'
       and revision.approved_revision = revision.revision
       and revision.approved_schema_hash = revision.schema_hash
       and revision.approved_at is not null
       and not exists (
            select 1
              from public.application_form_revisions newer
             where newer.application_id = revision.application_id
               and newer.revision > revision.revision
       );
    if not found then
        raise exception using errcode = 'P0001', message = 'form_approval_required';
    end if;
    return new;
end;
$$;

revoke all on function public.guard_yc_automation_job()
    from public, anon, authenticated;
grant execute on function public.guard_yc_automation_job() to service_role;

create trigger automation_jobs_yc_exact_target
    before insert or update of user_id, application_id, form_revision_id, kind, provider, payload
    on public.automation_jobs
    for each row execute function public.guard_yc_automation_job();

-- For prefill/submit the generic bundle resolver normally navigates directly
-- to the captured revision URL. YC must instead begin again at the exact,
-- server-bound public job URL and follow that page's controlled Apply handoff.
-- This full replacement is intentionally deterministic: migrations never
-- rewrite server-side function source text at runtime.
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
    target_yc_binding public.yc_application_targets%rowtype;
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

    if queue_job.provider = 'yc' then
        select binding.* into target_yc_binding
          from public.yc_application_targets binding
         where binding.job_id = target_job.id
           and binding.user_id = queue_job.user_id
           and binding.target_url = queue_job.payload ->> 'yc_job_target_url'
           and target_job.apply_url = binding.target_url
           and public.canonical_yc_job_url(binding.target_url) = binding.target_url
         for share of binding;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'yc_exact_job_url_changed';
        end if;
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
        if queue_job.provider = 'yc'
           and queue_job.kind in ('application_prefill', 'application_submit') then
            if target_yc_binding.application_url is null then
                raise exception using
                    errcode = 'P0001', message = 'yc_application_identity_required';
            end if;
            if target_revision.form_url is distinct from target_yc_binding.application_url
               or public.canonical_yc_application_url(
                    target_revision.form_url, target_yc_binding.target_url
                  ) is distinct from target_yc_binding.application_url then
                raise exception using
                    errcode = 'P0001', message = 'yc_application_identity_changed';
            end if;
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
        'target_url', case
            when queue_job.provider = 'yc' then target_yc_binding.target_url
            else coalesce(
                target_revision.form_url,
                target_job.apply_url,
                target_job.normalized_url
            )
        end,
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

revoke all on function public.get_application_job_bundle(uuid, text)
    from public, anon, authenticated;
grant execute on function public.get_application_job_bundle(uuid, text)
    to service_role;

-- Nothing queued while YC storage was disabled has the new exact target
-- authority. Cancel queued rows and cooperatively stop any currently leased row.
update public.automation_jobs job
   set status = case when job.status = 'queued' then 'cancelled' else job.status end,
       cancel_requested_at = coalesce(job.cancel_requested_at, clock_timestamp()),
       error_code = 'yc_exact_job_url_required',
       error_message = 'Save the exact YC job URL and start a fresh reviewed application.',
       locked_by = case when job.status = 'queued' then null else job.locked_by end,
       locked_at = case when job.status = 'queued' then null else job.locked_at end,
       lease_expires_at = case when job.status = 'queued' then null else job.lease_expires_at end,
       updated_at = clock_timestamp()
 where job.provider = 'yc'
   and job.kind in ('application_scan', 'application_prefill', 'application_submit')
   and job.status in ('queued', 'running');

do $assert_yc_exact_job_boundaries$
begin
    if not public.is_hosted_form_automation_provider('yc') then
        raise exception 'YC exact-job form automation should be enabled';
    end if;
    if public.validated_company_form_host('https://www.ycombinator.com/jobs') is not null
       or public.validated_company_form_host(
            'https://account.ycombinator.com/authenticate'
          ) is not null
       or public.validated_company_form_host(
            'https://www.workatastartup.com/jobs/91924'
          ) is not null then
        raise exception 'YC-owned hosts must never enter generic company-form automation';
    end if;
    if public.canonical_yc_job_url('https://www.ycombinator.com/companies/acme/jobs/12345-engineer')
       is distinct from 'https://www.ycombinator.com/companies/acme/jobs/12345-engineer' then
        raise exception 'current YC job detail URL should be accepted';
    end if;
    if public.canonical_yc_job_url('https://account.ycombinator.com/apply/123') is not null
       or public.canonical_yc_job_url('https://www.ycombinator.com/jobs') is not null
       or public.canonical_yc_job_url('https://www.workatastartup.com/jobs') is not null
       or public.canonical_yc_job_url('https://www.workatastartup.com/jobs/91924') is not null
       or public.canonical_yc_job_url('https://www.ycombinator.com/companies/acme/jobs/1234') is not null then
        raise exception 'YC collection/account/legacy URLs must never become saved targets';
    end if;
    if not public.is_yc_application_form_url(
        'https://www.workatastartup.com/application?signup_job_id=91924',
        'https://www.ycombinator.com/companies/acme/jobs/12345-engineer'
    ) or public.is_yc_application_form_url(
        'https://account.ycombinator.com/apply/91924',
        'https://www.ycombinator.com/companies/acme/jobs/12345-engineer'
    ) then
        raise exception 'YC revision authority must admit only the controlled application identity';
    end if;
    if has_table_privilege('authenticated', 'public.yc_application_targets', 'INSERT')
       or has_table_privilege('authenticated', 'public.yc_application_targets', 'UPDATE')
       or has_table_privilege('authenticated', 'public.yc_application_targets', 'DELETE') then
        raise exception 'authenticated must not mutate YC application targets';
    end if;
    if not has_table_privilege(
        'authenticated', 'public.provider_application_preferences', 'SELECT'
    ) or not has_table_privilege(
        'authenticated', 'public.provider_application_preferences', 'UPDATE'
    ) then
        raise exception 'authenticated must manage tenant-scoped YC preferences';
    end if;
end;
$assert_yc_exact_job_boundaries$;

commit;
