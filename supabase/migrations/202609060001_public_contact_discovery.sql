-- Public-source recruiting contact discovery.
--
-- Contact rows are evidence records, not mailbox verification records. The
-- worker may only insert an address together with the exact public page where
-- it appeared. No mailbox probe, guessed address, LinkedIn member scrape, or
-- Telegram member scrape is represented by this schema.

create table public.job_contacts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    job_id uuid not null references public.jobs(id) on delete cascade,
    company_key text not null check (char_length(company_key) between 1 and 160),
    email text not null check (
        char_length(email) <= 320
        and email = lower(email)
        and email ~ '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
    ),
    normalized_email text not null check (
        char_length(normalized_email) <= 320
        and normalized_email = lower(normalized_email)
        and normalized_email ~ '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
    ),
    person_name text check (person_name is null or char_length(person_name) <= 160),
    person_title text check (person_title is null or char_length(person_title) <= 160),
    contact_type text not null check (
        contact_type in ('named_person', 'recruiting_inbox', 'company_contact')
    ),
    source_url text not null check (
        char_length(source_url) between 12 and 2048
        and source_url ~* '^https://'
    ),
    source_date date not null default current_date,
    contact_source text not null check (char_length(contact_source) between 1 and 300),
    email_verification_status text not null check (
        email_verification_status in ('public_source_verified', 'public_source_unverified')
    ),
    status text not null default 'candidate' check (status in ('candidate', 'rejected')),
    metadata jsonb not null default '{}'::jsonb check (
        jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 8192
    ),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (user_id, company_key, normalized_email)
);

create index job_contacts_user_job_idx
    on public.job_contacts (user_id, job_id, created_at desc);
create index job_contacts_user_company_idx
    on public.job_contacts (user_id, company_key, created_at desc);
create index job_contacts_user_email_idx
    on public.job_contacts (user_id, normalized_email);

alter table public.job_contacts enable row level security;

create policy job_contacts_select_own on public.job_contacts for select to authenticated
    using ((select auth.uid()) = user_id);

revoke all on public.job_contacts from public, anon, authenticated;
grant select on public.job_contacts to authenticated;
grant all on public.job_contacts to service_role;

-- One queue row can process up to thirty saved roles. This avoids creating one
-- queue row per role when the user clicks Find contacts and keeps the work out of
-- the Vercel request lifecycle.
create or replace function public.enqueue_public_contact_discovery(
    job_ids_input uuid[],
    idempotency_key_input text,
    max_contacts_input integer default 30,
    max_pages_input integer default 8,
    timeout_seconds_input integer default 60
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    existing_job public.automation_jobs%rowtype;
    payload_value jsonb;
    active_count integer;
    daily_count integer;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if job_ids_input is null
       or cardinality(job_ids_input) < 1
       or cardinality(job_ids_input) > 30
       or exists (
            select 1 from unnest(job_ids_input) requested(job_id)
             where requested.job_id is null
       )
       or exists (
            select 1 from unnest(job_ids_input) requested(job_id)
             group by requested.job_id having count(*) > 1
       )
       or (
            select count(*) from public.jobs job
             where job.user_id = current_user_id and job.id = any(job_ids_input)
          ) <> cardinality(job_ids_input) then
        raise exception using errcode = '22023', message = 'contact_jobs_invalid';
    end if;
    if max_contacts_input is null or max_contacts_input not between 1 and 50
       or max_pages_input is null or max_pages_input not between 1 and 12
       or timeout_seconds_input is null or timeout_seconds_input not between 15 and 120 then
        raise exception using errcode = '22023', message = 'contact_limits_invalid';
    end if;
    if idempotency_key_input is null
       or idempotency_key_input !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$' then
        raise exception using errcode = '22023', message = 'idempotency_key_invalid';
    end if;

    payload_value := jsonb_build_object(
        'job_ids', to_jsonb(job_ids_input),
        'max_contacts', max_contacts_input,
        'max_pages', max_pages_input,
        'timeout_seconds', timeout_seconds_input
    );

    select job.* into existing_job
      from public.automation_jobs job
     where job.user_id = current_user_id
       and job.idempotency_key = idempotency_key_input;
    if found then
        if existing_job.kind is distinct from 'discover_public_contacts'
           or existing_job.provider is distinct from 'public_contacts'
           or existing_job.application_id is not null
           or existing_job.payload is distinct from payload_value then
            raise exception using errcode = '23505', message = 'idempotency_key_conflict';
        end if;
        return next existing_job;
        return;
    end if;

    insert into public.user_settings (user_id) values (current_user_id)
    on conflict (user_id) do nothing;
    perform 1 from public.user_settings settings
     where settings.user_id = current_user_id for update;

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
        current_user_id, null, 'discover_public_contacts', 'public_contacts',
        payload_value, idempotency_key_input
    )
    returning automation_jobs.*;
end;
$$;

revoke all on function public.enqueue_public_contact_discovery(uuid[], text, integer, integer, integer)
    from public, anon;
grant execute on function public.enqueue_public_contact_discovery(uuid[], text, integer, integer, integer)
    to authenticated;

-- The worker reads current job data through the queue lease, not through an
-- untrusted copy of the job included in the browser request.
create or replace function public.get_public_contact_discovery_bundle(
    queue_job_id_input uuid,
    worker_id_input text
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    queue_job public.automation_jobs%rowtype;
    rows jsonb;
begin
    select job.* into queue_job
      from public.automation_jobs job
     where job.id = queue_job_id_input
       and job.kind = 'discover_public_contacts'
       and job.status = 'running'
       and job.cancel_requested_at is null
       and job.locked_by = worker_id_input
       and job.lease_expires_at >= clock_timestamp()
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'contact_queue_lease_invalid';
    end if;

    select coalesce(
        jsonb_agg(
            jsonb_build_object(
                'id', job.id,
                'source', job.source,
                'apply_url', job.apply_url,
                'title', job.title,
                'company', job.company,
                'location', job.location,
                'description', job.description,
                'metadata', job.metadata
            ) order by job.created_at desc
        ),
        '[]'::jsonb
    ) into rows
      from public.jobs job
     where job.user_id = queue_job.user_id
       and job.id in (
            select value::uuid
              from jsonb_array_elements_text(queue_job.payload -> 'job_ids')
       );

    return jsonb_build_object(
        'jobs', rows,
        'max_contacts', coalesce((queue_job.payload ->> 'max_contacts')::integer, 30),
        'max_pages', coalesce((queue_job.payload ->> 'max_pages')::integer, 8),
        'timeout_seconds', coalesce((queue_job.payload ->> 'timeout_seconds')::integer, 60)
    );
end;
$$;

revoke all on function public.get_public_contact_discovery_bundle(uuid, text)
    from public, anon, authenticated;
grant execute on function public.get_public_contact_discovery_bundle(uuid, text)
    to service_role;

-- Persist only normalized evidence records after the worker has fetched the page.
-- The queue lease makes this service-only write tenant-bound.
create or replace function public.store_public_job_contacts(
    queue_job_id_input uuid,
    worker_id_input text,
    contacts_input jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    queue_job public.automation_jobs%rowtype;
    item jsonb;
    target_job_id uuid;
    company_key_value text;
    email_value text;
    source_url_value text;
    saved_count integer := 0;
    current_time timestamptz := clock_timestamp();
begin
    select job.* into queue_job
      from public.automation_jobs job
     where job.id = queue_job_id_input
       and job.kind = 'discover_public_contacts'
       and job.status = 'running'
       and job.cancel_requested_at is null
       and job.locked_by = worker_id_input
       and job.lease_expires_at >= current_time
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'contact_queue_lease_invalid';
    end if;
    if contacts_input is null
       or jsonb_typeof(contacts_input) <> 'array'
       or jsonb_array_length(contacts_input) > 1_500
       or octet_length(contacts_input::text) > 1_500_000 then
        raise exception using errcode = '22023', message = 'contact_results_invalid';
    end if;

    for item in select value from jsonb_array_elements(contacts_input)
    loop
        if jsonb_typeof(item) <> 'object' then
            raise exception using errcode = '22023', message = 'contact_result_invalid';
        end if;
        begin
            target_job_id := nullif(item ->> 'job_id', '')::uuid;
        exception when invalid_text_representation then
            raise exception using errcode = '22023', message = 'contact_job_invalid';
        end;
        company_key_value := lower(nullif(btrim(item ->> 'company_key'), ''));
        email_value := lower(nullif(btrim(item ->> 'email'), ''));
        source_url_value := nullif(btrim(item ->> 'source_url'), '');
        if target_job_id is null
           or not exists (
                select 1 from public.jobs job
                 where job.id = target_job_id and job.user_id = queue_job.user_id
           )
           or company_key_value is null or char_length(company_key_value) > 160
           or email_value is null or char_length(email_value) > 320
           or email_value !~ '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
           or source_url_value is null or char_length(source_url_value) not between 12 and 2048
           or source_url_value !~* '^https://'
           or item ->> 'contact_type' not in ('named_person', 'recruiting_inbox', 'company_contact')
           or item ->> 'email_verification_status' not in ('public_source_verified', 'public_source_unverified')
           or nullif(btrim(item ->> 'contact_source'), '') is null
           or char_length(item ->> 'contact_source') > 300 then
            raise exception using errcode = '22023', message = 'contact_result_invalid';
        end if;

        insert into public.job_contacts (
            user_id, job_id, company_key, email, normalized_email,
            person_name, person_title, contact_type, source_url, source_date,
            contact_source, email_verification_status, metadata, updated_at
        ) values (
            queue_job.user_id, target_job_id, company_key_value, email_value, email_value,
            nullif(btrim(item ->> 'person_name'), ''),
            nullif(btrim(item ->> 'person_title'), ''),
            item ->> 'contact_type', source_url_value,
            coalesce(nullif(item ->> 'source_date', '')::date, current_date),
            left(btrim(item ->> 'contact_source'), 300),
            item ->> 'email_verification_status',
            coalesce(item -> 'metadata', '{}'::jsonb), current_time
        )
        on conflict (user_id, company_key, normalized_email)
        do update set
            job_id = excluded.job_id,
            person_name = coalesce(excluded.person_name, public.job_contacts.person_name),
            person_title = coalesce(excluded.person_title, public.job_contacts.person_title),
            contact_type = excluded.contact_type,
            source_url = excluded.source_url,
            source_date = excluded.source_date,
            contact_source = excluded.contact_source,
            email_verification_status = excluded.email_verification_status,
            metadata = excluded.metadata,
            updated_at = current_time;
        saved_count := saved_count + 1;
    end loop;

    return jsonb_build_object('count', saved_count);
end;
$$;

revoke all on function public.store_public_job_contacts(uuid, text, jsonb)
    from public, anon, authenticated;
grant execute on function public.store_public_job_contacts(uuid, text, jsonb)
    to service_role;
