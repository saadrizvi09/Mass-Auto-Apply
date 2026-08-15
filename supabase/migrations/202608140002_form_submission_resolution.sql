-- Give a tenant an explicit, audited way out of an uncertain provider submit.
-- The sealed revision is never reopened. A verified success is finalized in
-- place; a verified non-submission creates a fresh revision for review.

begin;

-- No scan, profile refresh, or worker retry may silently supersede a revision
-- after a submit click became uncertain. The only allowed supersession writes
-- a durable user-verified not_submitted resolution in the same transaction.
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

    -- A provider-confirmed submission is final. It can never be relabelled as
    -- "not submitted", even by a later manual resolution or scan.
    if new.status = 'superseded'
       and old.status <> 'superseded'
       and (
            old.submission_result ->> 'submission_state' = 'confirmed'
            or exists (
                select 1
                  from public.automation_jobs attempt
                 where attempt.form_revision_id = old.id
                   and attempt.user_id = old.user_id
                   and attempt.application_id = old.application_id
                   and attempt.kind = 'application_submit'
                   and attempt.result ->> 'submission_state' = 'confirmed'
            )
       ) then
        raise exception using
            errcode = 'P0001', message = 'application_already_submitted';
    end if;

    -- An uncertain click normally requires an explicit user decision. The
    -- exception is recovery of another revision whose provider confirmation
    -- proves that this application has already been submitted.
    if new.status = 'superseded'
       and old.status <> 'superseded'
       and (
            old.submission_result ->> 'submission_state' = 'uncertain'
            or exists (
                select 1
                  from public.automation_jobs attempt
                 where attempt.form_revision_id = old.id
                   and attempt.user_id = old.user_id
                   and attempt.application_id = old.application_id
                   and attempt.kind = 'application_submit'
                   and attempt.result ->> 'submission_state' = 'uncertain'
            )
       )
       and coalesce(
            new.submission_result ->> 'resolution_outcome', ''
       ) <> 'not_submitted'
       and not exists (
            select 1
              from public.application_form_revisions confirmed_revision
             where confirmed_revision.user_id = old.user_id
               and confirmed_revision.application_id = old.application_id
               and confirmed_revision.id <> old.id
               and confirmed_revision.submission_result
                    ->> 'submission_state' = 'confirmed'
       )
       and not exists (
            select 1
              from public.automation_jobs confirmed_attempt
             where confirmed_attempt.user_id = old.user_id
               and confirmed_attempt.application_id = old.application_id
               and confirmed_attempt.form_revision_id is distinct from old.id
               and confirmed_attempt.kind = 'application_submit'
               and confirmed_attempt.result
                    ->> 'submission_state' = 'confirmed'
       ) then
        raise exception using
            errcode = 'P0001', message = 'form_submission_resolution_required';
    end if;
    return new;
end;
$$;

-- Serialize the one allowed submit attempt for each immutable revision. A new
-- idempotency key must not create a second provider click against the same
-- approved snapshot. Also reject scans while an older click is unresolved.
create or replace function public.guard_application_form_automation_attempt()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    binding_changed boolean;
begin
    if tg_op = 'INSERT' then
        binding_changed := true;
    else
        binding_changed := new.user_id is distinct from old.user_id
            or new.application_id is distinct from old.application_id
            or new.form_revision_id is distinct from old.form_revision_id
            or new.kind is distinct from old.kind;
    end if;

    -- Confirmation is application-wide, including old/superseded revisions.
    -- Serialize scans and submits on the application before consulting it.
    if binding_changed
       and new.kind in (
            'application_scan', 'application_prefill', 'application_submit'
       )
       and new.application_id is not null then
        perform pg_advisory_xact_lock(hashtextextended(
            'application-form:' || new.application_id::text, 0
        ));
        if exists (
            select 1
              from public.application_form_revisions confirmed_revision
             where confirmed_revision.user_id = new.user_id
               and confirmed_revision.application_id = new.application_id
               and confirmed_revision.submission_result
                    ->> 'submission_state' = 'confirmed'
        ) or exists (
            select 1
              from public.automation_jobs confirmed_attempt
             where confirmed_attempt.user_id = new.user_id
               and confirmed_attempt.application_id = new.application_id
               and confirmed_attempt.kind = 'application_submit'
               and confirmed_attempt.result
                    ->> 'submission_state' = 'confirmed'
               and confirmed_attempt.id <> new.id
        ) then
            raise exception using
                errcode = 'P0001', message = 'application_already_submitted';
        end if;
    end if;

    if binding_changed
       and new.kind = 'application_submit'
       and new.form_revision_id is not null then
        perform pg_advisory_xact_lock(hashtextextended(
            'application-form-submit:' || new.form_revision_id::text, 0
        ));
        if exists (
            select 1
              from public.automation_jobs attempt
             where attempt.user_id = new.user_id
               and attempt.form_revision_id = new.form_revision_id
               and attempt.kind = 'application_submit'
               and attempt.id <> new.id
        ) then
            raise exception using
                errcode = 'P0001', message = 'form_submit_attempt_exists';
        end if;
    end if;

    if binding_changed
       and new.kind = 'application_scan'
       and new.application_id is not null then
        if exists (
            select 1
              from public.application_form_revisions revision
             where revision.user_id = new.user_id
               and revision.application_id = new.application_id
               and revision.status in ('approved', 'needs_attention')
               and coalesce(
                    revision.submission_result ->> 'resolution_outcome', ''
               ) <> 'not_submitted'
               and (
                    revision.submission_result ->> 'submission_state' = 'uncertain'
                    or exists (
                        select 1
                          from public.automation_jobs attempt
                         where attempt.user_id = revision.user_id
                           and attempt.application_id = revision.application_id
                           and attempt.form_revision_id = revision.id
                           and attempt.kind = 'application_submit'
                           and attempt.result ->> 'submission_state' = 'uncertain'
                    )
               )
        ) then
            raise exception using
                errcode = 'P0001', message = 'form_submission_resolution_required';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists automation_jobs_guard_form_attempt
    on public.automation_jobs;
create trigger automation_jobs_guard_form_attempt
    before insert or update of user_id, application_id, form_revision_id, kind
    on public.automation_jobs
    for each row execute function public.guard_application_form_automation_attempt();

revoke all on function public.guard_application_form_automation_attempt()
    from public, anon, authenticated;

-- Persist every terminal submit-attention outcome on the immutable revision.
-- "not_attempted" is safe to rescan, but the approved snapshot that produced
-- it remains sealed and cannot be submitted directly after queue-row pruning.
create or replace function public.complete_application_form_submit_attention(
    job_id uuid,
    worker_id text,
    result jsonb
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
    timestamp_now timestamptz := clock_timestamp();
    queue_job public.automation_jobs%rowtype;
    target_revision public.application_form_revisions%rowtype;
    durable_result jsonb;
    compact_result jsonb;
    state text;
    existing_state text;
    result_code text;
    incoming_rank integer;
    existing_rank integer;
begin
    state := p_result ->> 'submission_state';
    result_code := nullif(btrim(coalesce(p_result ->> 'code', '')), '');
    if p_job_id is null
       or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128
       or p_result is null
       or jsonb_typeof(p_result) <> 'object'
       or octet_length(p_result::text) > 262144
       or p_result ->> 'outcome' is distinct from 'needs_attention'
       or p_result ->> 'phase' is distinct from 'submit'
       or state is null
       or state not in ('not_attempted', 'uncertain', 'confirmed')
       or result_code is null
       or result_code !~ '^[a-z][a-z0-9_]{1,63}$' then
        raise exception using errcode = '22023', message = 'form_submit_attention_invalid';
    end if;

    select job.* into queue_job
      from public.automation_jobs job
     where job.id = p_job_id
       and job.kind = 'application_submit'
       and job.status = 'running'
       and job.locked_by = p_worker_id
       and job.lease_expires_at >= timestamp_now
       and job.cancel_requested_at is null
       and job.form_revision_id is not null
     for update;
    if not found then
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'application-form:' || queue_job.application_id::text, 0
    ));

    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = queue_job.form_revision_id
       and revision.user_id = queue_job.user_id
       and revision.application_id = queue_job.application_id
       and revision.provider = queue_job.provider
       and revision.status in ('approved', 'submitted', 'needs_attention')
       and revision.approved_revision = revision.revision
       and revision.approved_schema_hash = revision.schema_hash
       and revision.approved_at is not null
     for update;
    if not found then
        return;
    end if;

    compact_result := jsonb_strip_nulls(jsonb_build_object(
        'outcome', 'needs_attention',
        'code', result_code,
        'phase', 'submit',
        'provider', p_result ->> 'provider',
        'form_url', p_result ->> 'form_url',
        'schema_hash', p_result ->> 'schema_hash',
        'field_count', p_result -> 'field_count',
        'filled_count', p_result -> 'filled_count',
        'missing_required', p_result -> 'missing_required',
        'missing_fields', p_result -> 'missing_fields',
        'question_schema', p_result -> 'question_schema',
        'submission_state', state,
        'message', p_result ->> 'message',
        'automation_job_id', queue_job.id
    ));
    if state = 'confirmed' then
        -- The provider click is authoritative even when the worker could not
        -- finish its ordinary record RPC. Keep the warning as telemetry while
        -- storing a canonical confirmed submission on the revision.
        compact_result := compact_result || jsonb_strip_nulls(jsonb_build_object(
            'outcome', 'succeeded',
            'code', 'application_submitted',
            'provider', target_revision.provider,
            'form_url', target_revision.form_url,
            'schema_hash', target_revision.schema_hash,
            'submission_state', 'confirmed',
            'verification_source', 'provider',
            'resolution_outcome', 'submitted',
            'resolved_at', timestamp_now,
            'attention_code', result_code,
            'attention_message', p_result ->> 'message'
        ));
    end if;
    durable_result := coalesce(target_revision.submission_result, '{}'::jsonb)
        || compact_result;
    if octet_length(durable_result::text) > 32768 then
        compact_result := compact_result - 'question_schema';
        durable_result := coalesce(target_revision.submission_result, '{}'::jsonb)
            || compact_result;
    end if;
    if octet_length(durable_result::text) > 32768 then
        durable_result := compact_result;
    end if;
    if octet_length(durable_result::text) > 32768 then
        if state = 'confirmed' then
            durable_result := jsonb_build_object(
                'outcome', 'succeeded',
                'code', 'application_submitted',
                'phase', 'submit',
                'provider', target_revision.provider,
                'form_url', target_revision.form_url,
                'schema_hash', target_revision.schema_hash,
                'submission_state', 'confirmed',
                'verification_source', 'provider',
                'resolution_outcome', 'submitted',
                'resolved_at', timestamp_now,
                'attention_code', result_code,
                'automation_job_id', queue_job.id
            );
        else
            durable_result := jsonb_build_object(
                'outcome', 'needs_attention',
                'code', result_code,
                'phase', 'submit',
                'provider', target_revision.provider,
                'form_url', target_revision.form_url,
                'schema_hash', target_revision.schema_hash,
                'submission_state', state,
                'automation_job_id', queue_job.id
            );
        end if;
    end if;

    existing_state := coalesce(
        target_revision.submission_result ->> 'submission_state', ''
    );
    incoming_rank := case state
        when 'confirmed' then 3 when 'uncertain' then 2 else 1 end;
    existing_rank := case existing_state
        when 'confirmed' then 3 when 'uncertain' then 2
        when 'not_attempted' then 1 else 0 end;

    -- A confirmed provider click atomically closes the revision and advances
    -- only pre-application pipeline states. The queue row remains
    -- needs_attention below so the record-RPC failure is still observable.
    if state = 'confirmed' then
        update public.applications application
           set status = 'applied', last_error = null
         where application.id = target_revision.application_id
           and application.user_id = queue_job.user_id
           and application.status in (
                'draft_pending', 'drafted', 'approved', 'queued', 'manual', 'failed'
           );
        update public.jobs job
           set status = 'applied'
         where job.id = target_revision.job_id
           and job.user_id = queue_job.user_id
           and job.status in ('saved', 'drafting', 'ready');
        update public.application_form_revisions revision
           set status = 'submitted',
               submitted_at = coalesce(revision.submitted_at, timestamp_now),
               submission_result = durable_result,
               last_error = null
         where revision.id = target_revision.id;
    -- Never overwrite a stronger observation with a weaker worker result.
    elsif target_revision.status = 'approved'
       or (
            target_revision.status = 'needs_attention'
            and incoming_rank >= existing_rank
       ) then
        update public.application_form_revisions revision
           set status = 'needs_attention',
               submission_result = durable_result,
               last_error = left(result_code, 500)
         where revision.id = target_revision.id;
    end if;

    insert into public.audit_events (
        user_id, event_type, resource_type, resource_id, metadata
    ) values (
        queue_job.user_id,
        'application.form.submit_attention_recorded',
        'application_form_revision',
        target_revision.id,
        jsonb_build_object(
            'application_id', target_revision.application_id,
            'automation_job_id', queue_job.id,
            'revision', target_revision.revision,
            'schema_hash', target_revision.schema_hash,
            'submission_state', state,
            'code', result_code
        )
    );

    return query
    update public.automation_jobs job
       set status = 'needs_attention', result = p_result,
           progress = '{}'::jsonb, error_code = null, error_message = null,
           locked_by = null, locked_at = null, lease_expires_at = null,
           updated_at = timestamp_now
     where job.id = queue_job.id
       and job.status = 'running'
       and job.locked_by = p_worker_id
       and job.lease_expires_at >= timestamp_now
       and job.cancel_requested_at is null
    returning job.*;
end;
$$;

revoke all on function public.complete_application_form_submit_attention(
    uuid, text, jsonb
) from public, anon, authenticated;
grant execute on function public.complete_application_form_submit_attention(
    uuid, text, jsonb
) to service_role;

-- Preserve every original lease/revision check while ensuring a delayed
-- provider confirmation cannot downgrade a later interview/rejection/archive
-- pipeline state back to "applied".
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
    timestamp_now timestamptz := clock_timestamp();
    queue_job public.automation_jobs%rowtype;
    target_revision public.application_form_revisions%rowtype;
    durable_result jsonb;
begin
    if p_job_id is null or nullif(btrim(p_worker_id), '') is null
       or char_length(p_worker_id) > 128
       or (p_provider_submission_id is not null
           and char_length(p_provider_submission_id) > 1024)
       or p_result is null or jsonb_typeof(p_result) <> 'object'
       or octet_length(p_result::text) > 32768
       or p_result ->> 'submission_state' is distinct from 'confirmed' then
        raise exception using errcode = '22023', message = 'submission_result_invalid';
    end if;

    select automation.* into queue_job
      from public.automation_jobs automation
     where automation.id = p_job_id
       and automation.kind = 'application_submit'
       and automation.status = 'running'
       and automation.locked_by = p_worker_id
       and automation.lease_expires_at >= timestamp_now
       and automation.cancel_requested_at is null
     for update;
    if not found or queue_job.form_revision_id is null then
        raise exception using errcode = 'P0002', message = 'application_job_not_owned';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'application-form:' || queue_job.application_id::text, 0
    ));

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

    durable_result := coalesce(target_revision.submission_result, '{}'::jsonb)
        || p_result
        || jsonb_build_object(
            'submission_state', 'confirmed',
            'verification_source', 'provider',
            'resolution_outcome', 'submitted',
            'resolved_at', timestamp_now
        );
    if octet_length(durable_result::text) > 32768 then
        durable_result := p_result || jsonb_build_object(
            'submission_state', 'confirmed',
            'verification_source', 'provider',
            'resolution_outcome', 'submitted',
            'resolved_at', timestamp_now
        );
    end if;
    if octet_length(durable_result::text) > 32768 then
        durable_result := jsonb_build_object(
            'code', 'application_submitted',
            'provider', target_revision.provider,
            'form_url', target_revision.form_url,
            'schema_hash', target_revision.schema_hash,
            'submission_state', 'confirmed',
            'verification_source', 'provider',
            'resolution_outcome', 'submitted',
            'resolved_at', timestamp_now
        );
    end if;

    update public.applications application
       set status = 'applied', last_error = null
     where application.id = queue_job.application_id
       and application.user_id = queue_job.user_id
       and application.status in (
            'draft_pending', 'drafted', 'approved', 'queued', 'manual', 'failed'
       );
    update public.jobs job
       set status = 'applied'
     where job.id = target_revision.job_id
       and job.user_id = queue_job.user_id
       and job.status in ('saved', 'drafting', 'ready');

    return query
    update public.application_form_revisions revision
       set status = 'submitted', submitted_at = timestamp_now,
           provider_submission_id = nullif(btrim(p_provider_submission_id), ''),
           submission_result = durable_result, last_error = null
     where revision.id = target_revision.id
    returning revision.*;
end;
$$;

revoke all on function public.record_application_form_submission(
    uuid, text, text, jsonb
) from public, anon, authenticated;
grant execute on function public.record_application_form_submission(
    uuid, text, text, jsonb
) to service_role;

-- Recover any provider-confirmed legacy state first. Provider confirmation is
-- stronger than every later uncertain observation and must never offer retry.
with confirmed_candidates as (
    select revision.id,
           confirmation.result as provider_result,
           jsonb_strip_nulls(jsonb_build_object(
               'outcome', 'succeeded',
               'code', 'application_submitted',
               'phase', 'submit',
               'provider', revision.provider,
               'form_url', revision.form_url,
               'schema_hash', revision.schema_hash,
               'submission_state', 'confirmed',
               'verification_source', 'provider',
               'resolution_outcome', 'submitted',
               'resolved_at', clock_timestamp(),
               'provider_result_code', confirmation.result ->> 'code',
               'field_count', confirmation.result -> 'field_count',
               'filled_count', confirmation.result -> 'filled_count',
               'missing_required', confirmation.result -> 'missing_required',
               'question_schema', confirmation.result -> 'question_schema'
           )) as compact_result
      from public.application_form_revisions revision
      left join lateral (
          select attempt.result
            from public.automation_jobs attempt
           where attempt.user_id = revision.user_id
             and attempt.application_id = revision.application_id
             and attempt.form_revision_id = revision.id
             and attempt.kind = 'application_submit'
             and attempt.result ->> 'submission_state' = 'confirmed'
           order by attempt.created_at desc, attempt.id desc
           limit 1
      ) confirmation on true
     where revision.status in ('approved', 'needs_attention', 'superseded')
       and revision.approved_revision = revision.revision
       and revision.approved_schema_hash = revision.schema_hash
       and revision.approved_at is not null
       and (
            revision.submission_result ->> 'submission_state' = 'confirmed'
            or confirmation.result is not null
       )
), finalized as (
    update public.application_form_revisions revision
       set status = case
               when revision.status = 'superseded' then 'superseded'
               else 'submitted'
           end,
           submitted_at = clock_timestamp(),
           last_error = null,
           submission_result = case
               when octet_length((
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || candidate.compact_result
               )::text) <= 32768 then
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || candidate.compact_result
               when octet_length((
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || (candidate.compact_result - 'question_schema')
               )::text) <= 32768 then
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || (candidate.compact_result - 'question_schema')
               else jsonb_build_object(
                   'outcome', 'succeeded',
                   'code', 'application_submitted',
                   'phase', 'submit',
                   'provider', revision.provider,
                   'form_url', revision.form_url,
                   'schema_hash', revision.schema_hash,
                   'submission_state', 'confirmed',
                   'verification_source', 'provider',
                   'resolution_outcome', 'submitted'
               )
           end
      from confirmed_candidates candidate
     where revision.id = candidate.id
    returning revision.*
)
insert into public.audit_events (
    user_id, event_type, resource_type, resource_id, metadata
)
select finalized.user_id,
       'application.form.provider_confirmation_recovered',
       'application_form_revision',
       finalized.id,
       jsonb_build_object(
           'application_id', finalized.application_id,
           'provider', finalized.provider,
           'revision', finalized.revision,
           'schema_hash', finalized.schema_hash,
           'outcome', 'submitted',
           'verification_source', 'provider'
       )
  from finalized;

-- A recovered confirmation also seals every later unsubmitted snapshot. The
-- guard allows this specific cleanup because the application-wide provider
-- confirmation is now durable; no uncertain revision is being guessed away.
update public.application_form_revisions revision
   set status = 'superseded',
       last_error = null
 where revision.status in ('scanned', 'prefilled', 'approved', 'needs_attention')
   and revision.submission_result ->> 'submission_state' is distinct from 'confirmed'
   and exists (
        select 1
          from public.application_form_revisions confirmed_revision
         where confirmed_revision.user_id = revision.user_id
           and confirmed_revision.application_id = revision.application_id
           and confirmed_revision.id <> revision.id
           and confirmed_revision.submission_result
                ->> 'submission_state' = 'confirmed'
   )
   and not exists (
        select 1
          from public.automation_jobs confirmed_attempt
         where confirmed_attempt.user_id = revision.user_id
           and confirmed_attempt.application_id = revision.application_id
           and confirmed_attempt.form_revision_id = revision.id
           and confirmed_attempt.kind = 'application_submit'
           and confirmed_attempt.result ->> 'submission_state' = 'confirmed'
   );

update public.applications application
   set status = 'applied', last_error = null
 where application.status in (
        'draft_pending', 'drafted', 'approved', 'queued', 'manual', 'failed'
   )
   and exists (
        select 1 from public.application_form_revisions revision
         where revision.application_id = application.id
           and revision.user_id = application.user_id
           and revision.submission_result ->> 'submission_state' = 'confirmed'
   );

update public.jobs job
   set status = 'applied'
 where job.status in ('saved', 'drafting', 'ready')
   and exists (
        select 1 from public.application_form_revisions revision
         where revision.job_id = job.id
           and revision.user_id = job.user_id
           and revision.submission_result ->> 'submission_state' = 'confirmed'
   );

-- Migration 140001 deliberately did not mutate rows retroactively. Backfill
-- the exact legacy shape used by already-running tenants: an approved revision
-- plus a terminal submit job whose click outcome is uncertain.
with legacy_uncertain as (
    select distinct on (revision.id)
           revision.id,
           attempt.id as automation_job_id,
           attempt.result,
           jsonb_strip_nulls(jsonb_build_object(
               'outcome', coalesce(
                    attempt.result ->> 'outcome', 'needs_attention'
               ),
               'code', coalesce(
                    attempt.result ->> 'code', 'submission_unconfirmed'
               ),
               'phase', 'submit',
               'provider', coalesce(
                    attempt.result ->> 'provider', revision.provider
               ),
               'form_url', coalesce(
                    attempt.result ->> 'form_url', revision.form_url
               ),
               'schema_hash', coalesce(
                    attempt.result ->> 'schema_hash', revision.schema_hash
               ),
               'field_count', attempt.result -> 'field_count',
               'filled_count', attempt.result -> 'filled_count',
               'missing_required', attempt.result -> 'missing_required',
               'missing_fields', attempt.result -> 'missing_fields',
               'question_schema', attempt.result -> 'question_schema',
               'submission_state', 'uncertain',
               'message', attempt.result ->> 'message',
               'automation_job_id', attempt.id
           )) as compact_result
      from public.application_form_revisions revision
      join public.automation_jobs attempt
        on attempt.user_id = revision.user_id
       and attempt.application_id = revision.application_id
       and attempt.form_revision_id = revision.id
       and attempt.kind = 'application_submit'
       and attempt.status = 'needs_attention'
       and attempt.result ->> 'phase' = 'submit'
       and attempt.result ->> 'submission_state' = 'uncertain'
     where revision.status = 'approved'
       and revision.approved_revision = revision.revision
       and revision.approved_schema_hash = revision.schema_hash
       and revision.approved_at is not null
       and not exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = revision.application_id
               and newer.revision > revision.revision
       )
       and not exists (
            select 1 from public.automation_jobs confirmed_attempt
             where confirmed_attempt.user_id = revision.user_id
               and confirmed_attempt.application_id = revision.application_id
               and confirmed_attempt.form_revision_id = revision.id
               and confirmed_attempt.kind = 'application_submit'
               and confirmed_attempt.result ->> 'submission_state' = 'confirmed'
       )
     order by revision.id, attempt.created_at desc, attempt.id desc
), backfilled as (
    update public.application_form_revisions revision
       set status = 'needs_attention',
           last_error = left(
               coalesce(legacy.result ->> 'code', 'submission_unconfirmed'), 500
           ),
           submission_result = case
               when octet_length((
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || legacy.compact_result
               )::text) <= 32768 then
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || legacy.compact_result
               when octet_length((
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || (legacy.compact_result - 'question_schema')
               )::text) <= 32768 then
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || (legacy.compact_result - 'question_schema')
               else jsonb_build_object(
                   'outcome', 'needs_attention',
                   'code', 'submission_unconfirmed',
                   'phase', 'submit',
                   'provider', revision.provider,
                   'form_url', revision.form_url,
                   'schema_hash', revision.schema_hash,
                   'submission_state', 'uncertain',
                   'automation_job_id', legacy.automation_job_id
               )
           end
      from legacy_uncertain legacy
     where revision.id = legacy.id
    returning revision.*
)
insert into public.audit_events (
    user_id, event_type, resource_type, resource_id, metadata
)
select backfilled.user_id,
       'application.form.submit_attention_backfilled',
       'application_form_revision',
       backfilled.id,
       jsonb_build_object(
           'application_id', backfilled.application_id,
           'provider', backfilled.provider,
           'revision', backfilled.revision,
           'schema_hash', backfilled.schema_hash,
           'submission_state', 'uncertain'
       )
  from backfilled;

-- Snapshot legacy pre-click failures as well. They do not require an
-- uncertainty decision, so a new scan may supersede them; nevertheless the
-- exact approved revision that failed remains sealed against direct reuse.
with legacy_not_attempted as (
    select distinct on (revision.id)
           revision.id,
           attempt.id as automation_job_id,
           attempt.result,
           jsonb_strip_nulls(jsonb_build_object(
               'outcome', coalesce(
                    attempt.result ->> 'outcome', 'needs_attention'
               ),
               'code', coalesce(
                    attempt.result ->> 'code', 'submission_not_attempted'
               ),
               'phase', 'submit',
               'provider', coalesce(
                    attempt.result ->> 'provider', revision.provider
               ),
               'form_url', coalesce(
                    attempt.result ->> 'form_url', revision.form_url
               ),
               'schema_hash', coalesce(
                    attempt.result ->> 'schema_hash', revision.schema_hash
               ),
               'field_count', attempt.result -> 'field_count',
               'filled_count', attempt.result -> 'filled_count',
               'missing_required', attempt.result -> 'missing_required',
               'missing_fields', attempt.result -> 'missing_fields',
               'question_schema', attempt.result -> 'question_schema',
               'submission_state', 'not_attempted',
               'message', attempt.result ->> 'message',
               'automation_job_id', attempt.id
           )) as compact_result
      from public.application_form_revisions revision
      join public.automation_jobs attempt
        on attempt.user_id = revision.user_id
       and attempt.application_id = revision.application_id
       and attempt.form_revision_id = revision.id
       and attempt.kind = 'application_submit'
       and attempt.status = 'needs_attention'
       and attempt.result ->> 'phase' = 'submit'
       and attempt.result ->> 'submission_state' = 'not_attempted'
     where revision.status = 'approved'
       and revision.approved_revision = revision.revision
       and revision.approved_schema_hash = revision.schema_hash
       and revision.approved_at is not null
       and not exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = revision.application_id
               and newer.revision > revision.revision
       )
       and not exists (
            select 1 from public.application_form_revisions confirmed_revision
             where confirmed_revision.user_id = revision.user_id
               and confirmed_revision.application_id = revision.application_id
               and confirmed_revision.submission_result
                    ->> 'submission_state' = 'confirmed'
       )
       and not exists (
            select 1 from public.automation_jobs confirmed_attempt
             where confirmed_attempt.user_id = revision.user_id
               and confirmed_attempt.application_id = revision.application_id
               and confirmed_attempt.kind = 'application_submit'
               and confirmed_attempt.result ->> 'submission_state' = 'confirmed'
       )
     order by revision.id, attempt.created_at desc, attempt.id desc
), backfilled_not_attempted as (
    update public.application_form_revisions revision
       set status = 'needs_attention',
           last_error = left(
               coalesce(legacy.result ->> 'code', 'submission_not_attempted'), 500
           ),
           submission_result = case
               when octet_length((
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || legacy.compact_result
               )::text) <= 32768 then
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || legacy.compact_result
               when octet_length((
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || (legacy.compact_result - 'question_schema')
               )::text) <= 32768 then
                    coalesce(revision.submission_result, '{}'::jsonb)
                    || (legacy.compact_result - 'question_schema')
               else jsonb_build_object(
                   'outcome', 'needs_attention',
                   'code', 'submission_not_attempted',
                   'phase', 'submit',
                   'provider', revision.provider,
                   'form_url', revision.form_url,
                   'schema_hash', revision.schema_hash,
                   'submission_state', 'not_attempted',
                   'automation_job_id', legacy.automation_job_id
               )
           end
      from legacy_not_attempted legacy
     where revision.id = legacy.id
    returning revision.*
)
insert into public.audit_events (
    user_id, event_type, resource_type, resource_id, metadata
)
select backfilled.user_id,
       'application.form.submit_attention_backfilled',
       'application_form_revision',
       backfilled.id,
       jsonb_build_object(
           'application_id', backfilled.application_id,
           'provider', backfilled.provider,
           'revision', backfilled.revision,
           'schema_hash', backfilled.schema_hash,
           'submission_state', 'not_attempted'
       )
  from backfilled_not_attempted backfilled;

-- Historical superseded uncertainty cannot be guessed safely because a newer
-- revision may already have been prepared. Current installations have no such
-- rows; fail the migration rather than silently reopening one if that invariant
-- is ever violated by an older/private deployment.
do $legacy_form_uncertainty_assertion$
begin
    if exists (
        select 1
          from public.application_form_revisions revision
          join public.automation_jobs attempt
            on attempt.user_id = revision.user_id
           and attempt.application_id = revision.application_id
           and attempt.form_revision_id = revision.id
           and attempt.kind = 'application_submit'
           and attempt.result ->> 'submission_state' = 'uncertain'
         where coalesce(
                revision.submission_result ->> 'resolution_outcome', ''
               ) <> 'not_submitted'
           and (
                revision.status = 'superseded'
                or exists (
                    select 1
                      from public.application_form_revisions newer
                     where newer.application_id = revision.application_id
                       and newer.revision > revision.revision
                )
           )
           and not exists (
                select 1
                  from public.application_form_revisions confirmed_revision
                 where confirmed_revision.user_id = revision.user_id
                   and confirmed_revision.application_id = revision.application_id
                   and confirmed_revision.submission_result
                        ->> 'submission_state' = 'confirmed'
           )
           and not exists (
                select 1
                  from public.automation_jobs confirmed_attempt
                 where confirmed_attempt.user_id = revision.user_id
                   and confirmed_attempt.application_id = revision.application_id
                   and confirmed_attempt.kind = 'application_submit'
                   and confirmed_attempt.result
                        ->> 'submission_state' = 'confirmed'
           )
    ) then
        raise exception 'legacy_form_submission_uncertainty_requires_manual_repair';
    end if;
end;
$legacy_form_uncertainty_assertion$;

create or replace function public.resolve_application_form_submission(
    revision_id_input uuid,
    expected_revision_input bigint,
    expected_schema_hash_input text,
    outcome_input text
)
returns setof public.application_form_revisions
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    target_revision public.application_form_revisions%rowtype;
    latest_submit public.automation_jobs%rowtype;
    confirmed_submit public.automation_jobs%rowtype;
    saved_revision public.application_form_revisions%rowtype;
    next_revision bigint;
    retry_revision_id uuid;
    durable_state text;
    latest_state text;
    resolution_outcome text;
    resolved_at timestamptz := clock_timestamp();
    manual_result jsonb;
    non_submission_result jsonb;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if revision_id_input is null
       or expected_revision_input is null or expected_revision_input < 1
       or expected_schema_hash_input is null
       or expected_schema_hash_input !~ '^[0-9a-f]{64}$'
       or outcome_input is null
       or outcome_input not in ('submitted', 'not_submitted') then
        raise exception using errcode = '22023', message = 'form_submission_resolution_invalid';
    end if;

    -- Resolve the tenant-owned application before taking its transaction lock,
    -- then re-read the revision under that lock so concurrent resolutions and
    -- scans cannot both advance the same form history.
    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = revision_id_input
       and revision.user_id = current_user_id;
    if not found then
        raise exception using errcode = 'P0002', message = 'form_revision_not_found';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'application-form:' || target_revision.application_id::text, 0
    ));

    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = revision_id_input
       and revision.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'form_revision_not_found';
    end if;

    if target_revision.revision <> expected_revision_input
       or target_revision.schema_hash <> expected_schema_hash_input then
        raise exception using errcode = 'P0001', message = 'form_submission_resolution_stale';
    end if;

    durable_state := coalesce(
        target_revision.submission_result ->> 'submission_state', ''
    );
    resolution_outcome := coalesce(
        target_revision.submission_result ->> 'resolution_outcome', ''
    );

    select attempt.* into confirmed_submit
      from public.automation_jobs attempt
     where attempt.user_id = current_user_id
       and attempt.application_id = target_revision.application_id
       and attempt.form_revision_id = target_revision.id
       and attempt.kind = 'application_submit'
       and attempt.result ->> 'submission_state' = 'confirmed'
     order by attempt.created_at desc, attempt.id desc
     limit 1;

    -- Confirmation on any older/newer revision wins for the entire
    -- application. In particular, an idempotent old "not submitted" response
    -- cannot create another retry after delayed provider evidence arrives.
    if exists (
        select 1
          from public.application_form_revisions confirmed_revision
         where confirmed_revision.user_id = current_user_id
           and confirmed_revision.application_id = target_revision.application_id
           and confirmed_revision.id <> target_revision.id
           and confirmed_revision.submission_result
                ->> 'submission_state' = 'confirmed'
    ) or exists (
        select 1
          from public.automation_jobs confirmed_attempt
         where confirmed_attempt.user_id = current_user_id
           and confirmed_attempt.application_id = target_revision.application_id
           and confirmed_attempt.kind = 'application_submit'
           and confirmed_attempt.result ->> 'submission_state' = 'confirmed'
           and confirmed_attempt.form_revision_id is distinct from target_revision.id
    ) then
        raise exception using
            errcode = 'P0001', message = 'application_already_submitted';
    end if;

    if target_revision.status = 'superseded'
       and (durable_state = 'confirmed' or confirmed_submit.id is not null) then
        raise exception using
            errcode = 'P0001', message = 'application_already_submitted';
    end if;

    -- A retried HTTP response is idempotent. The opposite decision can never
    -- rewrite a completed resolution.
    if target_revision.status = 'submitted' and durable_state = 'confirmed' then
        if outcome_input <> 'submitted' then
            raise exception using
                errcode = 'P0001', message = 'form_submission_resolution_conflict';
        end if;
        return next target_revision;
        return;
    end if;

    if target_revision.status = 'superseded'
       and resolution_outcome = 'not_submitted' then
        if outcome_input <> 'not_submitted' then
            raise exception using
                errcode = 'P0001', message = 'form_submission_resolution_conflict';
        end if;
        begin
            retry_revision_id := nullif(
                target_revision.submission_result ->> 'retry_revision_id', ''
            )::uuid;
        exception when invalid_text_representation then
            retry_revision_id := null;
        end;
        select revision.* into saved_revision
          from public.application_form_revisions revision
         where revision.id = retry_revision_id
           and revision.user_id = current_user_id
           and revision.application_id = target_revision.application_id;
        if not found then
            raise exception using
                errcode = 'P0001', message = 'form_submission_resolution_stale';
        end if;
        return next saved_revision;
        return;
    end if;

    -- Provider confirmation is stronger than every uncertain observation. If
    -- a legacy current row missed finalization, recover it before checking for
    -- newer unsubmitted revisions and seal those revisions too.
    if durable_state = 'confirmed' or confirmed_submit.id is not null then
        if target_revision.status not in ('approved', 'needs_attention')
           or target_revision.approved_revision <> target_revision.revision
           or target_revision.approved_schema_hash <> target_revision.schema_hash
           or target_revision.approved_at is null then
            raise exception using
                errcode = 'P0001', message = 'application_already_submitted';
        end if;

        manual_result := coalesce(
                target_revision.submission_result, '{}'::jsonb
            ) || jsonb_strip_nulls(jsonb_build_object(
                'outcome', 'succeeded',
                'code', 'application_submitted',
                'phase', 'submit',
                'provider', target_revision.provider,
                'form_url', target_revision.form_url,
                'schema_hash', target_revision.schema_hash,
                'submission_state', 'confirmed',
                'verification_source', 'provider',
                'resolution_outcome', 'submitted',
                'resolved_at', resolved_at,
                'provider_result_code', confirmed_submit.result ->> 'code',
                'message', 'The provider confirmed this application submission.'
            ));
        if octet_length(manual_result::text) > 32768 then
            manual_result := jsonb_strip_nulls(jsonb_build_object(
                'outcome', 'succeeded',
                'code', 'application_submitted',
                'phase', 'submit',
                'provider', target_revision.provider,
                'form_url', target_revision.form_url,
                'schema_hash', target_revision.schema_hash,
                'submission_state', 'confirmed',
                'verification_source', 'provider',
                'resolution_outcome', 'submitted',
                'resolved_at', resolved_at,
                'provider_result_code', confirmed_submit.result ->> 'code',
                'message', 'The provider confirmed this application submission.'
            ));
        end if;

        update public.applications application
           set status = 'applied', last_error = null
         where application.id = target_revision.application_id
           and application.user_id = current_user_id
           and application.status in (
                'draft_pending', 'drafted', 'approved', 'queued', 'manual', 'failed'
           );
        update public.jobs job
           set status = 'applied'
         where job.id = target_revision.job_id
           and job.user_id = current_user_id
           and job.status in ('saved', 'drafting', 'ready');
        update public.application_form_revisions revision
           set status = 'submitted',
               submitted_at = resolved_at,
               submission_result = manual_result,
               last_error = null
         where revision.id = target_revision.id
           and revision.user_id = current_user_id
        returning * into saved_revision;

        update public.application_form_revisions revision
           set status = 'superseded',
               last_error = null
         where revision.user_id = current_user_id
           and revision.application_id = target_revision.application_id
           and revision.id <> target_revision.id
           and revision.status in (
                'scanned', 'prefilled', 'approved', 'needs_attention'
           )
           and revision.submission_result
                ->> 'submission_state' is distinct from 'confirmed'
           and not exists (
                select 1
                  from public.automation_jobs confirmed_attempt
                 where confirmed_attempt.user_id = revision.user_id
                   and confirmed_attempt.application_id = revision.application_id
                   and confirmed_attempt.form_revision_id = revision.id
                   and confirmed_attempt.kind = 'application_submit'
                   and confirmed_attempt.result
                        ->> 'submission_state' = 'confirmed'
           );

        insert into public.audit_events (
            user_id, event_type, resource_type, resource_id, metadata
        ) values (
            current_user_id,
            'application.form.provider_confirmation_recovered',
            'application_form_revision',
            saved_revision.id,
            jsonb_build_object(
                'application_id', saved_revision.application_id,
                'provider', saved_revision.provider,
                'revision', saved_revision.revision,
                'schema_hash', saved_revision.schema_hash,
                'outcome', 'submitted',
                'verification_source', 'provider',
                'automation_job_id', confirmed_submit.id
            )
        );
        return next saved_revision;
        return;
    end if;

    if target_revision.status not in ('approved', 'needs_attention')
       or target_revision.submitted_at is not null
       or target_revision.approved_revision <> target_revision.revision
       or target_revision.approved_schema_hash <> target_revision.schema_hash
       or target_revision.approved_at is null
       or exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = target_revision.application_id
               and newer.revision > target_revision.revision
       ) then
        raise exception using errcode = 'P0001', message = 'form_submission_resolution_stale';
    end if;

    if exists (
        select 1 from public.automation_jobs active
         where active.user_id = current_user_id
           and active.application_id = target_revision.application_id
           and active.status in ('queued', 'running')
    ) then
        raise exception using errcode = 'P0001', message = 'application_operation_in_progress';
    end if;

    select attempt.* into latest_submit
      from public.automation_jobs attempt
     where attempt.user_id = current_user_id
       and attempt.application_id = target_revision.application_id
       and attempt.form_revision_id = target_revision.id
       and attempt.kind = 'application_submit'
     order by attempt.created_at desc, attempt.id desc
     limit 1;

    latest_state := coalesce(latest_submit.result ->> 'submission_state', '');

    -- A retained latest queue result or the durable revision snapshot must say
    -- that the provider click was uncertain.
    if not (
            durable_state = 'uncertain'
            or (
                latest_submit.id is not null
                and latest_submit.status = 'needs_attention'
                and latest_state = 'uncertain'
            )
       ) then
        raise exception using errcode = 'P0001', message = 'form_submission_not_uncertain';
    end if;

    if outcome_input = 'submitted' then
        manual_result := coalesce(
                target_revision.submission_result, '{}'::jsonb
            ) || jsonb_strip_nulls(jsonb_build_object(
            'outcome', 'succeeded',
            'code', 'application_submitted',
            'phase', 'submit',
            'provider', target_revision.provider,
            'form_url', target_revision.form_url,
            'schema_hash', target_revision.schema_hash,
            'submission_state', 'confirmed',
            'verification_source', 'user',
            'resolution_outcome', 'submitted',
            'resolved_at', resolved_at,
            'prior_submission_code', coalesce(
                target_revision.submission_result ->> 'code',
                latest_submit.result ->> 'code'
            ),
            'field_count', coalesce(
                target_revision.submission_result -> 'field_count',
                latest_submit.result -> 'field_count'
            ),
            'filled_count', coalesce(
                target_revision.submission_result -> 'filled_count',
                latest_submit.result -> 'filled_count'
            ),
            'missing_required', coalesce(
                target_revision.submission_result -> 'missing_required',
                latest_submit.result -> 'missing_required'
            ),
            'question_schema', coalesce(
                target_revision.submission_result -> 'question_schema',
                latest_submit.result -> 'question_schema'
            ),
            'message', 'The user verified the submission directly with the provider.'
        ));
        if octet_length(manual_result::text) > 32768 then
            manual_result := manual_result - 'question_schema';
        end if;
        if octet_length(manual_result::text) > 32768 then
            manual_result := jsonb_build_object(
                'outcome', 'succeeded',
                'code', 'application_submitted',
                'phase', 'submit',
                'provider', target_revision.provider,
                'form_url', target_revision.form_url,
                'schema_hash', target_revision.schema_hash,
                'submission_state', 'confirmed',
                'verification_source', 'user',
                'resolution_outcome', 'submitted',
                'resolved_at', resolved_at,
                'message', 'The user verified the submission directly with the provider.'
            );
        end if;

        update public.applications application
           set status = 'applied', last_error = null
         where application.id = target_revision.application_id
           and application.user_id = current_user_id
           and application.status in (
                'draft_pending', 'drafted', 'approved', 'queued', 'manual', 'failed'
           );

        update public.jobs job
           set status = 'applied'
         where job.id = target_revision.job_id
           and job.user_id = current_user_id
           and job.status in ('saved', 'drafting', 'ready');

        update public.application_form_revisions revision
           set status = 'submitted',
               submitted_at = resolved_at,
               submission_result = manual_result,
               last_error = null
         where revision.id = target_revision.id
           and revision.user_id = current_user_id
        returning * into saved_revision;

        insert into public.audit_events (
            user_id, event_type, resource_type, resource_id, metadata
        ) values (
            current_user_id,
            'application.form.submit_outcome_resolved',
            'application_form_revision',
            saved_revision.id,
            jsonb_build_object(
                'application_id', saved_revision.application_id,
                'provider', saved_revision.provider,
                'revision', saved_revision.revision,
                'schema_hash', saved_revision.schema_hash,
                'outcome', 'submitted',
                'verification_source', 'user',
                'automation_job_id', latest_submit.id
            )
        );

        return next saved_revision;
        return;
    end if;

    select coalesce(max(revision.revision), 0) + 1 into next_revision
      from public.application_form_revisions revision
     where revision.application_id = target_revision.application_id;
    -- The UI immediately rescans the provider after creating this unapproved
    -- fallback copy. Reserve the final revision slot for that fresh scan.
    if next_revision >= 50 then
        raise exception using errcode = 'P0001', message = 'form_revision_limit_reached';
    end if;

    retry_revision_id := gen_random_uuid();
    non_submission_result := coalesce(
            target_revision.submission_result, '{}'::jsonb
        ) || jsonb_strip_nulls(jsonb_build_object(
        'outcome', coalesce(
            target_revision.submission_result ->> 'outcome',
            latest_submit.result ->> 'outcome',
            'needs_attention'
        ),
        'code', coalesce(
            target_revision.submission_result ->> 'code',
            latest_submit.result ->> 'code',
            'submission_unconfirmed'
        ),
        'phase', 'submit',
        'provider', target_revision.provider,
        'form_url', target_revision.form_url,
        'schema_hash', target_revision.schema_hash,
        'filled_count', coalesce(
            target_revision.submission_result -> 'filled_count',
            latest_submit.result -> 'filled_count'
        ),
        'field_count', coalesce(
            target_revision.submission_result -> 'field_count',
            latest_submit.result -> 'field_count'
        ),
        'missing_required', coalesce(
            target_revision.submission_result -> 'missing_required',
            latest_submit.result -> 'missing_required'
        ),
        'question_schema', coalesce(
            target_revision.submission_result -> 'question_schema',
            latest_submit.result -> 'question_schema'
        ),
        'submission_state', 'uncertain',
        'message', coalesce(
            target_revision.submission_result ->> 'message',
            latest_submit.result ->> 'message'
        ),
        'verification_source', 'user',
        'resolution_outcome', 'not_submitted',
        'resolved_at', resolved_at,
        'retry_revision_id', retry_revision_id
    ));
    if octet_length(non_submission_result::text) > 32768 then
        non_submission_result := non_submission_result - 'question_schema';
    end if;
    if octet_length(non_submission_result::text) > 32768 then
        non_submission_result := jsonb_build_object(
            'outcome', 'needs_attention',
            'code', 'submission_unconfirmed',
            'phase', 'submit',
            'provider', target_revision.provider,
            'form_url', target_revision.form_url,
            'schema_hash', target_revision.schema_hash,
            'submission_state', 'uncertain',
            'verification_source', 'user',
            'resolution_outcome', 'not_submitted',
            'resolved_at', resolved_at,
            'retry_revision_id', retry_revision_id
        );
    end if;

    -- Preserve the uncertain revision as an immutable audit snapshot. The
    -- copied revision has no approval or submission fields, so it must pass
    -- through the normal review and approval flow before a fresh attempt.
    update public.application_form_revisions revision
       set status = 'superseded',
           submission_result = non_submission_result,
           last_error = null
     where revision.id = target_revision.id
       and revision.user_id = current_user_id;

    insert into public.application_form_revisions (
        id, user_id, application_id, job_id, resume_id, provider, form_url,
        revision, schema_hash, question_schema, answers, status
    ) values (
        retry_revision_id,
        target_revision.user_id,
        target_revision.application_id,
        target_revision.job_id,
        target_revision.resume_id,
        target_revision.provider,
        target_revision.form_url,
        next_revision,
        target_revision.schema_hash,
        target_revision.question_schema,
        target_revision.answers,
        'prefilled'
    )
    returning * into saved_revision;

    insert into public.audit_events (
        user_id, event_type, resource_type, resource_id, metadata
    ) values (
        current_user_id,
        'application.form.submit_outcome_resolved',
        'application_form_revision',
        target_revision.id,
        jsonb_build_object(
            'application_id', target_revision.application_id,
            'provider', target_revision.provider,
            'previous_revision', target_revision.revision,
            'previous_schema_hash', target_revision.schema_hash,
            'outcome', 'not_submitted',
            'verification_source', 'user',
            'retry_revision_id', saved_revision.id,
            'retry_revision', saved_revision.revision,
            'automation_job_id', latest_submit.id
        )
    );

    return next saved_revision;
end;
$$;

revoke all on function public.resolve_application_form_submission(
    uuid, bigint, text, text
) from public, anon, authenticated;
grant execute on function public.resolve_application_form_submission(
    uuid, bigint, text, text
) to authenticated;

do $form_submission_resolution_assertions$
begin
    if has_function_privilege(
        'anon',
        'public.resolve_application_form_submission(uuid,bigint,text,text)',
        'EXECUTE'
    ) then
        raise exception 'anonymous users must not resolve form submissions';
    end if;
    if not has_function_privilege(
        'authenticated',
        'public.resolve_application_form_submission(uuid,bigint,text,text)',
        'EXECUTE'
    ) then
        raise exception 'authenticated users must be able to resolve their own form submissions';
    end if;
end;
$form_submission_resolution_assertions$;

commit;
