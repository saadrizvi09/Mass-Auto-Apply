-- A form scan may replace an approved fallback revision while its submit job is
-- still queued.  The worker already revalidates the immutable revision before
-- opening a browser, but stale work should never be claimed in the first place.
-- Cancel it at the revision transition and retain a claim-time fence for old
-- rows, concurrent transitions, and installations upgraded from older builds.

begin;

create or replace function public.cancel_superseded_form_submit_jobs()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    timestamp_now timestamptz := clock_timestamp();
begin
    if new.status = 'superseded' and old.status <> 'superseded' then
        update public.automation_jobs job
           set status = case
                   when job.status = 'queued' then 'cancelled'
                   else job.status
               end,
               cancel_requested_at = coalesce(
                   job.cancel_requested_at, timestamp_now
               ),
               error_code = 'form_revision_superseded',
               error_message = 'The form revision was superseded before submission.',
               locked_by = case
                   when job.status = 'queued' then null else job.locked_by
               end,
               locked_at = case
                   when job.status = 'queued' then null else job.locked_at
               end,
               lease_expires_at = case
                   when job.status = 'queued' then null else job.lease_expires_at
               end,
               updated_at = timestamp_now
         where job.user_id = new.user_id
           and job.application_id = new.application_id
           and job.form_revision_id = new.id
           and job.kind = 'application_submit'
           and job.status in ('queued', 'running')
           and (
                job.status = 'queued'
                or job.cancel_requested_at is null
           );
    end if;
    return new;
end;
$$;

revoke all on function public.cancel_superseded_form_submit_jobs()
    from public, anon, authenticated;

drop trigger if exists application_form_revisions_cancel_submit_jobs
    on public.application_form_revisions;
create trigger application_form_revisions_cancel_submit_jobs
    after update of status on public.application_form_revisions
    for each row
    when (new.status = 'superseded' and old.status <> 'superseded')
    execute function public.cancel_superseded_form_submit_jobs();

-- Repair any active stale work created before this trigger existed. Queued
-- rows are terminally cancelled. Running rows are cancelled cooperatively so
-- their current lease holder observes the request at its next heartbeat.
update public.automation_jobs job
   set status = case
           when job.status = 'queued' then 'cancelled'
           else job.status
       end,
       cancel_requested_at = coalesce(
           job.cancel_requested_at, clock_timestamp()
       ),
       error_code = 'form_revision_not_current',
       error_message = 'The approved form revision is no longer current.',
       locked_by = case
           when job.status = 'queued' then null else job.locked_by
       end,
       locked_at = case
           when job.status = 'queued' then null else job.locked_at
       end,
       lease_expires_at = case
           when job.status = 'queued' then null else job.lease_expires_at
       end,
       updated_at = clock_timestamp()
 where job.kind = 'application_submit'
   and job.status in ('queued', 'running')
   and (
        job.status = 'queued'
        or job.cancel_requested_at is null
   )
   and not exists (
        select 1
          from public.application_form_revisions revision
         where revision.id = job.form_revision_id
           and revision.user_id = job.user_id
           and revision.application_id = job.application_id
           and revision.provider = job.provider
           and (
                (
                    revision.status = 'approved'
                    and revision.approved_revision = revision.revision
                    and revision.approved_schema_hash = revision.schema_hash
                    and revision.approved_at is not null
                    and not exists (
                        select 1
                          from public.application_form_revisions newer
                         where newer.application_id = revision.application_id
                           and newer.revision > revision.revision
                    )
                )
                or (
                    job.status = 'running'
                    and revision.status = 'submitted'
                    and revision.submission_result
                        ->> 'submission_state' = 'confirmed'
                )
           )
   );

-- Claim one due job atomically, recovering expired leases first. In addition
-- to the worker's lease-bound bundle check, make stale form submissions
-- ineligible at the queue boundary itself.
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

    -- Cancel any queued submit whose immutable approval is no longer the
    -- latest revision. This also repairs rows missed by a concurrent status
    -- transition or created by a pre-migration deployment.
    update public.automation_jobs job
       set status = 'cancelled',
           cancel_requested_at = coalesce(
               job.cancel_requested_at, timestamp_now
           ),
           error_code = 'form_revision_not_current',
           error_message = 'The approved form revision is no longer current.',
           locked_by = null, locked_at = null, lease_expires_at = null,
           updated_at = timestamp_now
     where job.status = 'queued'
       and job.kind = 'application_submit'
       and not exists (
            select 1
              from public.application_form_revisions revision
             where revision.id = job.form_revision_id
               and revision.user_id = job.user_id
               and revision.application_id = job.application_id
               and revision.provider = job.provider
               and revision.status = 'approved'
               and revision.approved_revision = revision.revision
               and revision.approved_schema_hash = revision.schema_hash
               and revision.approved_at is not null
               and not exists (
                    select 1
                      from public.application_form_revisions newer
                     where newer.application_id = revision.application_id
                       and newer.revision > revision.revision
               )
       );

    -- A supersession that races with an already-held lease is cooperative:
    -- preserve the running history row, request cancellation, and let the
    -- heartbeat/get-bundle fences stop the browser before another click.
    update public.automation_jobs job
       set cancel_requested_at = timestamp_now,
           error_code = 'form_revision_not_current',
           error_message = 'The approved form revision is no longer current.',
           updated_at = timestamp_now
     where job.status = 'running'
       and job.kind = 'application_submit'
       and job.cancel_requested_at is null
       and not exists (
            select 1
              from public.application_form_revisions revision
             where revision.id = job.form_revision_id
               and revision.user_id = job.user_id
               and revision.application_id = job.application_id
               and revision.provider = job.provider
               and (
                    (
                        revision.status = 'approved'
                        and revision.approved_revision = revision.revision
                        and revision.approved_schema_hash = revision.schema_hash
                        and revision.approved_at is not null
                        and not exists (
                            select 1
                              from public.application_form_revisions newer
                             where newer.application_id = revision.application_id
                               and newer.revision > revision.revision
                        )
                    )
                    or (
                        revision.status = 'submitted'
                        and revision.submission_result
                            ->> 'submission_state' = 'confirmed'
                    )
               )
       );

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
            job.kind <> 'application_submit'
            or exists (
                select 1
                  from public.application_form_revisions revision
                 where revision.id = job.form_revision_id
                   and revision.user_id = job.user_id
                   and revision.application_id = job.application_id
                   and revision.provider = job.provider
                   and revision.status = 'approved'
                   and revision.approved_revision = revision.revision
                   and revision.approved_schema_hash = revision.schema_hash
                   and revision.approved_at is not null
                   and not exists (
                        select 1
                          from public.application_form_revisions newer
                         where newer.application_id = revision.application_id
                           and newer.revision > revision.revision
                   )
            )
       )
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

revoke all on function public.claim_automation_job(text, integer, text[])
    from public, anon, authenticated;
grant execute on function public.claim_automation_job(text, integer, text[])
    to service_role;

commit;
