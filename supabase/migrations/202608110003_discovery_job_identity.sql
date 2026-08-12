-- Make discovery ingestion idempotent when a source provides a stable external
-- identifier but no canonical URL. Existing URL-based identity remains in
-- force; both authenticated requests and leased workers already delegate to the
-- internal function replaced below.

begin;

-- Block tenant job/application writes while legacy duplicates are merged and
-- the new uniqueness invariant is installed. This migration runs once and the
-- locks are released at commit.
lock table public.jobs in share row exclusive mode;
lock table public.applications in share row exclusive mode;
lock table public.application_form_revisions in share row exclusive mode;

create temporary table discovery_job_identity_merge (
    duplicate_id uuid primary key,
    canonical_id uuid not null,
    user_id uuid not null,
    source text not null,
    external_id text not null
) on commit drop;

insert into discovery_job_identity_merge (
    duplicate_id, canonical_id, user_id, source, external_id
)
with ranked as (
    select
        job.id,
        job.user_id,
        job.source,
        job.external_id,
        first_value(job.id) over identity_order as canonical_id,
        row_number() over identity_order as identity_rank
    from public.jobs job
    where job.external_id is not null
    window identity_order as (
        partition by job.user_id, job.source, job.external_id
        order by
            (job.status <> 'saved') desc,
            job.updated_at desc,
            (job.normalized_url is not null) desc,
            job.created_at asc,
            job.id asc
    )
)
select id, canonical_id, user_id, source, external_id
from ranked
where identity_rank > 1;

-- Preserve every application and immutable form revision by repointing them to
-- the selected canonical job before deleting duplicate job rows. The form
-- revision's job reference is normally immutable. The legacy generic
-- applications ownership trigger evaluates fields from unrelated tables after
-- a successful ownership match, so both affected guards are disabled only for
-- these tenant-matched updates; the dedicated revision ownership trigger stays
-- active throughout.
alter table public.applications
    disable trigger applications_owned_job;

update public.applications application
   set job_id = merge.canonical_id
  from discovery_job_identity_merge merge
 where application.job_id = merge.duplicate_id
   and application.user_id = merge.user_id;

alter table public.applications
    enable trigger applications_owned_job;

alter table public.application_form_revisions
    disable trigger application_form_revisions_guard_immutable;

update public.application_form_revisions revision
   set job_id = merge.canonical_id
  from discovery_job_identity_merge merge
 where revision.job_id = merge.duplicate_id
   and revision.user_id = merge.user_id;

alter table public.application_form_revisions
    enable trigger application_form_revisions_guard_immutable;

-- Retain the newest discovery timestamp on each canonical row. Other job
-- fields, including any user-controlled status/content, come from the most
-- recently updated non-saved row selected above.
with latest_discovery as (
    select
        merge.canonical_id,
        max(job.last_discovered_at) as last_discovered_at
    from discovery_job_identity_merge merge
    join public.jobs job
      on job.id in (merge.canonical_id, merge.duplicate_id)
    group by merge.canonical_id
)
update public.jobs canonical
   set last_discovered_at = greatest(
           canonical.last_discovered_at,
           latest.last_discovered_at
       )
  from latest_discovery latest
 where canonical.id = latest.canonical_id
   and latest.last_discovered_at is not null
   and canonical.last_discovered_at is distinct from greatest(
           canonical.last_discovered_at,
           latest.last_discovered_at
       );

delete from public.jobs duplicate
using discovery_job_identity_merge merge
where duplicate.id = merge.duplicate_id
  and duplicate.user_id = merge.user_id;

create unique index jobs_user_source_external_id_uidx
    on public.jobs (user_id, source, external_id)
    where external_id is not null;

comment on index public.jobs_user_source_external_id_uidx is
    'Tenant-scoped discovery identity for sources that omit a canonical URL.';

-- Internal race-safe ingestion. The public one-argument RPC derives the tenant
-- from auth.uid(); the three-argument service RPC derives it from a valid worker
-- lease. Their signatures and authorization boundaries are intentionally
-- unchanged, so replacing this shared implementation hardens both paths.
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
    external_id_input text;
    normalized_url_job_id uuid;
    external_id_job_id uuid;
    prior_job_id uuid;
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

    -- Serialize all ingestion calls for one tenant. Unique indexes plus the
    -- conflict fallback below also protect against writes outside this RPC.
    perform pg_advisory_xact_lock(hashtextextended(
        'discovered-jobs:' || current_user_id::text, 0
    ));

    for item in select value from jsonb_array_elements(jobs_input)
    loop
        if jsonb_typeof(item) <> 'object' then
            raise exception using errcode = '22023', message = 'discovered_job_invalid';
        end if;
        source_input := lower(nullif(btrim(item ->> 'source'), ''));
        external_id_input := nullif(btrim(item ->> 'external_id'), '');
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
           or (external_id_input is not null and char_length(external_id_input) > 255)
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

        normalized_url_job_id := null;
        external_id_job_id := null;
        if normalized_url_input is not null then
            select job.id into normalized_url_job_id
              from public.jobs job
             where job.user_id = current_user_id
               and job.normalized_url = normalized_url_input
             for update;
        end if;
        if external_id_input is not null then
            select job.id into external_id_job_id
              from public.jobs job
             where job.user_id = current_user_id
               and job.source = source_input
               and job.external_id = external_id_input
             for update;
        end if;
        if normalized_url_job_id is not null
           and external_id_job_id is not null
           and normalized_url_job_id <> external_id_job_id then
            raise exception using
                errcode = '23505', message = 'discovered_job_identity_conflict';
        end if;
        prior_job_id := coalesce(normalized_url_job_id, external_id_job_id);

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
                external_id_input,
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
            on conflict do nothing
            returning jobs.* into saved_job;

            if found then
                was_inserted := true;
            else
                -- A concurrent direct write may win either uniqueness race after
                -- the initial lookup. Resolve it under a row lock and count this
                -- invocation as an update.
                normalized_url_job_id := null;
                external_id_job_id := null;
                if normalized_url_input is not null then
                    select job.id into normalized_url_job_id
                      from public.jobs job
                     where job.user_id = current_user_id
                       and job.normalized_url = normalized_url_input
                     for update;
                end if;
                if external_id_input is not null then
                    select job.id into external_id_job_id
                      from public.jobs job
                     where job.user_id = current_user_id
                       and job.source = source_input
                       and job.external_id = external_id_input
                     for update;
                end if;
                if normalized_url_job_id is not null
                   and external_id_job_id is not null
                   and normalized_url_job_id <> external_id_job_id then
                    raise exception using
                        errcode = '23505', message = 'discovered_job_identity_conflict';
                end if;
                prior_job_id := coalesce(normalized_url_job_id, external_id_job_id);
                if prior_job_id is null then
                    raise exception using
                        errcode = '40001', message = 'discovered_job_upsert_retry';
                end if;
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
            end if;
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

commit;
