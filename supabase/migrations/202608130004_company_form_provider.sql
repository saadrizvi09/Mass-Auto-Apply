-- Add explicitly saved, public company application forms without turning the
-- generic discovery pipeline into an arbitrary-URL browser.  A service-owned
-- binding is the authority for the original job URL and exact hostname; job
-- metadata is deliberately not trusted for automation authorization.

begin;

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
       and value !~ '^[0-9]+(\.[0-9]+){3}$'
$$;

revoke all on function public.validated_company_form_host(text)
    from public, anon, authenticated;
grant execute on function public.validated_company_form_host(text) to service_role;

create table public.company_form_targets (
    job_id uuid primary key references public.jobs(id) on delete cascade,
    user_id uuid not null references auth.users(id) on delete cascade,
    source_url text not null check (char_length(source_url) between 9 and 2048),
    target_url text not null check (
        char_length(target_url) between 9 and 2048
        and strpos(target_url, '#') = 0
    ),
    exact_host text not null check (
        exact_host = lower(exact_host)
        and char_length(exact_host) between 4 and 253
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, job_id),
    check (public.validated_company_form_host(source_url) = exact_host),
    check (public.validated_company_form_host(target_url) = exact_host)
);

comment on table public.company_form_targets is
    'Service-owned authority binding an explicitly saved job URL to one exact public HTTPS hostname.';

create index company_form_targets_user_idx
    on public.company_form_targets (user_id, updated_at desc);

create trigger company_form_targets_set_updated_at
    before update on public.company_form_targets
    for each row execute function public.set_updated_at();

create or replace function public.guard_company_form_target()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if public.validated_company_form_host(new.source_url) is distinct from new.exact_host
       or public.validated_company_form_host(new.target_url) is distinct from new.exact_host
       or strpos(new.target_url, '#') <> 0 then
        raise exception using errcode = '22023', message = 'company_form_target_invalid';
    end if;
    if not exists (
        select 1
          from public.jobs job
         where job.id = new.job_id
           and job.user_id = new.user_id
           and job.apply_url = new.source_url
    ) then
        raise exception using errcode = '23503', message = 'company_form_source_not_owned';
    end if;
    return new;
end;
$$;

revoke all on function public.guard_company_form_target()
    from public, anon, authenticated;
grant execute on function public.guard_company_form_target() to service_role;

create trigger company_form_targets_guard
    before insert or update of job_id, user_id, source_url, target_url, exact_host
    on public.company_form_targets
    for each row execute function public.guard_company_form_target();

alter table public.company_form_targets enable row level security;
revoke all on public.company_form_targets from public, anon, authenticated;
grant all on public.company_form_targets to service_role;

-- Preserve the fixed provider list while allowing revision snapshots produced
-- by the separately gated company-form path.
alter table public.application_form_revisions
    drop constraint if exists application_form_revisions_provider_check;
alter table public.application_form_revisions
    add constraint application_form_revisions_provider_check check (provider in (
        'google_forms', 'greenhouse', 'lever', 'ashby', 'yc', 'wellfound',
        'cutshort', 'instahyre', 'company_form'
    ));

-- This capability registry is intentionally distinct from the managed-browser
-- login registry.  company_form needs no stored login or persistent context;
-- YC remains excluded until its application adapter is production-ready.
create or replace function public.is_hosted_form_automation_provider(provider_input text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select provider_input = any (array[
        'google_forms', 'greenhouse', 'lever', 'ashby', 'wellfound',
        'company_form'
    ]::text[])
$$;

revoke all on function public.is_hosted_form_automation_provider(text)
    from public, anon, authenticated;
grant execute on function public.is_hosted_form_automation_provider(text) to service_role;

-- Extend only the enqueue function's global syntactic provider allowlist.  Its
-- discover_public_feeds provider list is a different clause and is untouched,
-- so arbitrary company URLs cannot enter through discovery.
do $extend_company_form_enqueue_provider$
declare
    routine regprocedure :=
        'public.enqueue_automation_job(text,text,uuid,jsonb,text)'::regprocedure;
    routine_definition text;
    old_provider_gate constant text :=
        '''lever'', ''ashby'', ''yc'', ''wellfound'', ''cutshort'', ''instahyre''
    ) then
        raise exception using errcode = ''22023'', message = ''automation_provider_invalid'';';
    new_provider_gate constant text :=
        '''lever'', ''ashby'', ''yc'', ''wellfound'', ''cutshort'', ''instahyre'',
        ''company_form''
    ) then
        raise exception using errcode = ''22023'', message = ''automation_provider_invalid'';';
    provider_gate_occurrences integer;
begin
    routine_definition := pg_get_functiondef(routine);
    provider_gate_occurrences := (
        length(routine_definition)
        - length(replace(routine_definition, old_provider_gate, ''))
    ) / length(old_provider_gate);
    if provider_gate_occurrences <> 1 then
        raise exception
            'expected one global provider gate in %, found %',
            routine,
            provider_gate_occurrences;
    end if;
    execute replace(routine_definition, old_provider_gate, new_provider_gate);
end;
$extend_company_form_enqueue_provider$;

-- Worker scan storage must use the hosted-automation capability gate.  This
-- keeps YC and every login-only provider gated while admitting company_form.
do $extend_company_form_scan_storage$
declare
    routine regprocedure :=
        'public.store_application_form_scan(uuid,text,text,text,text,jsonb,jsonb)'::regprocedure;
    routine_definition text;
    old_gate constant text :=
        'or not public.is_managed_application_provider(p_provider)';
    new_gate constant text :=
        'or not public.is_hosted_form_automation_provider(p_provider)';
    gate_occurrences integer;
begin
    routine_definition := pg_get_functiondef(routine);
    gate_occurrences := (
        length(routine_definition)
        - length(replace(routine_definition, old_gate, ''))
    ) / length(old_gate);
    if gate_occurrences <> 1 then
        raise exception
            'expected one scan storage provider gate in %, found %',
            routine,
            gate_occurrences;
    end if;
    execute replace(routine_definition, old_gate, new_gate);
end;
$extend_company_form_scan_storage$;

-- Every company-form scan result remains on the exact bound host.  Redirects
-- to another path or query on that host are allowed; cross-host redirects are
-- rejected before a revision can be persisted.
create or replace function public.guard_company_form_revision_target()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    bound_target public.company_form_targets%rowtype;
begin
    if new.provider <> 'company_form' then
        return new;
    end if;

    select binding.* into bound_target
      from public.company_form_targets binding
      join public.jobs job
        on job.id = binding.job_id
       and job.user_id = binding.user_id
       and job.apply_url = binding.source_url
      join public.applications application
        on application.id = new.application_id
       and application.user_id = binding.user_id
       and application.job_id = binding.job_id
     where binding.job_id = new.job_id
       and binding.user_id = new.user_id;
    if not found then
        raise exception using errcode = 'P0001', message = 'company_form_target_unbound';
    end if;
    if public.validated_company_form_host(new.form_url)
       is distinct from bound_target.exact_host then
        raise exception using errcode = 'P0001', message = 'company_form_host_changed';
    end if;
    return new;
end;
$$;

revoke all on function public.guard_company_form_revision_target()
    from public, anon, authenticated;
grant execute on function public.guard_company_form_revision_target() to service_role;

create trigger application_form_revisions_company_form_target
    before insert or update of user_id, application_id, job_id, provider, form_url
    on public.application_form_revisions
    for each row execute function public.guard_company_form_revision_target();

-- The queue payload is the worker's browser boundary.  It must repeat the
-- server-bound exact host and URL.  Scan starts at the saved target; later
-- stages use the exact latest approved revision URL, which may have a different
-- path/query but never a different host.
create or replace function public.guard_company_form_automation_job()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    bound_target public.company_form_targets%rowtype;
    approved_revision public.application_form_revisions%rowtype;
    payload_host text;
    payload_target_url text;
begin
    if new.provider <> 'company_form' then
        return new;
    end if;
    if new.kind not in ('application_scan', 'application_prefill', 'application_submit')
       or new.application_id is null then
        raise exception using errcode = 'P0001', message = 'provider_automation_unavailable';
    end if;
    if new.payload is null
       or jsonb_typeof(new.payload) is distinct from 'object'
       or jsonb_typeof(new.payload -> 'company_form_host') is distinct from 'string'
       or jsonb_typeof(new.payload -> 'company_form_target_url') is distinct from 'string' then
        raise exception using errcode = '22023', message = 'company_form_payload_invalid';
    end if;

    payload_host := new.payload ->> 'company_form_host';
    payload_target_url := new.payload ->> 'company_form_target_url';
    if payload_host is distinct from lower(payload_host)
       or public.validated_company_form_host(payload_target_url)
          is distinct from payload_host
       or strpos(payload_target_url, '#') <> 0 then
        raise exception using errcode = '22023', message = 'company_form_payload_invalid';
    end if;

    select binding.* into bound_target
      from public.company_form_targets binding
      join public.jobs job
        on job.id = binding.job_id
       and job.user_id = binding.user_id
       and job.apply_url = binding.source_url
      join public.applications application
        on application.id = new.application_id
       and application.user_id = binding.user_id
       and application.job_id = binding.job_id
     where binding.user_id = new.user_id
       and binding.exact_host = payload_host;
    if not found then
        raise exception using errcode = 'P0001', message = 'company_form_target_unbound';
    end if;

    if new.kind = 'application_scan' then
        if new.form_revision_id is not null
           or payload_target_url is distinct from bound_target.target_url then
            raise exception using errcode = 'P0001', message = 'company_form_target_changed';
        end if;
        return new;
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
       and revision.provider = 'company_form'
       and revision.form_url = payload_target_url
       and public.validated_company_form_host(revision.form_url) = bound_target.exact_host
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

revoke all on function public.guard_company_form_automation_job()
    from public, anon, authenticated;
grant execute on function public.guard_company_form_automation_job() to service_role;

create trigger automation_jobs_company_form_target
    before insert or update of user_id, application_id, form_revision_id, kind, provider, payload
    on public.automation_jobs
    for each row execute function public.guard_company_form_automation_job();

-- Deployment assertions: this table must never become a user-controlled
-- substitute for the server's explicit-save decision.
do $assert_company_form_boundaries$
begin
    if has_table_privilege('authenticated', 'public.company_form_targets', 'INSERT')
       or has_table_privilege('authenticated', 'public.company_form_targets', 'UPDATE')
       or has_table_privilege('authenticated', 'public.company_form_targets', 'DELETE') then
        raise exception 'authenticated must not mutate company_form_targets';
    end if;
    if public.is_managed_application_provider('company_form') then
        raise exception 'company_form must not enter saved-login lifecycle';
    end if;
    if public.is_hosted_form_automation_provider('yc') then
        raise exception 'YC form automation must remain gated';
    end if;
end;
$assert_company_form_boundaries$;

commit;
