-- Durably fence any form submission whose provider click may have happened.
-- The form snapshot and queue completion are committed in one transaction so
-- retention of terminal automation_jobs rows is never the safety boundary.

begin;

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
    state text;
    result_code text;
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
       or state not in ('uncertain', 'confirmed')
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

    durable_result := jsonb_strip_nulls(jsonb_build_object(
        'outcome', 'needs_attention',
        'code', result_code,
        'provider', p_result ->> 'provider',
        'form_url', p_result ->> 'form_url',
        'schema_hash', p_result ->> 'schema_hash',
        'filled_count', p_result -> 'filled_count',
        'submission_state', state
    ));

    -- A prior record_application_form_submission call may have committed even
    -- when its HTTP response was lost. Preserve that stronger submitted state.
    if target_revision.status = 'approved' then
        update public.application_form_revisions revision
           set status = 'needs_attention',
               submission_result = durable_result,
               last_error = left(result_code, 500)
         where revision.id = target_revision.id;
    elsif target_revision.status = 'needs_attention'
          and coalesce(target_revision.submission_result ->> 'submission_state', '')
              not in ('uncertain', 'confirmed') then
        update public.application_form_revisions revision
           set submission_result = durable_result,
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

-- Reinstall the Profile synchronization RPC with a durable form-revision
-- fence. The automation_jobs check remains useful for active work, but terminal
-- job retention is no longer relied upon to remember an uncertain click.
create or replace function public.refresh_application_form_profile_answers_for_user(
    user_id_input uuid,
    revision_id_input uuid,
    expected_revision_input bigint,
    expected_schema_hash_input text,
    answers_input jsonb
)
returns setof public.application_form_revisions
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    target_revision public.application_form_revisions%rowtype;
    saved_revision public.application_form_revisions%rowtype;
    next_revision bigint;
begin
    if user_id_input is null or revision_id_input is null
       or expected_revision_input is null or expected_revision_input < 1
       or expected_schema_hash_input is null
       or expected_schema_hash_input !~ '^[0-9a-f]{64}$'
       or answers_input is null or jsonb_typeof(answers_input) <> 'object'
       or octet_length(answers_input::text) > 262144 then
        raise exception using errcode = '22023', message = 'form_profile_sync_invalid';
    end if;

    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = revision_id_input
       and revision.user_id = user_id_input;
    if not found then
        return;
    end if;

    perform pg_advisory_xact_lock(hashtextextended(
        'application-form:' || target_revision.application_id::text, 0
    ));
    select revision.* into target_revision
      from public.application_form_revisions revision
     where revision.id = revision_id_input
       and revision.user_id = user_id_input
     for update;

    if not found
       or target_revision.revision <> expected_revision_input
       or target_revision.schema_hash <> expected_schema_hash_input
       or target_revision.status not in ('scanned', 'prefilled', 'approved')
       or target_revision.submitted_at is not null
       or target_revision.submission_result ->> 'submission_state'
            in ('uncertain', 'confirmed')
       or target_revision.answers = answers_input
       or exists (
            select 1 from public.application_form_revisions newer
             where newer.application_id = target_revision.application_id
               and newer.revision > target_revision.revision
       ) then
        return;
    end if;

    if exists (
        select 1 from public.automation_jobs operation
         where operation.user_id = user_id_input
           and operation.application_id = target_revision.application_id
           and operation.status in ('queued', 'running')
    ) then
        return;
    end if;

    if exists (
        select 1 from public.automation_jobs attempt
         where attempt.user_id = user_id_input
           and attempt.application_id = target_revision.application_id
           and attempt.form_revision_id = target_revision.id
           and attempt.kind = 'application_submit'
           and not (
                attempt.status = 'needs_attention'
                and attempt.result ->> 'submission_state' = 'not_attempted'
           )
    ) then
        return;
    end if;

    select coalesce(max(revision.revision), 0) + 1 into next_revision
      from public.application_form_revisions revision
     where revision.application_id = target_revision.application_id;
    if next_revision > 50 then
        return;
    end if;

    update public.application_form_revisions revision
       set status = 'superseded'
     where revision.id = target_revision.id
       and revision.user_id = user_id_input;

    insert into public.application_form_revisions (
        user_id, application_id, job_id, resume_id, provider, form_url,
        revision, schema_hash, question_schema, answers, status
    ) values (
        target_revision.user_id, target_revision.application_id,
        target_revision.job_id, target_revision.resume_id,
        target_revision.provider, target_revision.form_url,
        next_revision, target_revision.schema_hash,
        target_revision.question_schema, answers_input,
        case when answers_input = '{}'::jsonb then 'scanned' else 'prefilled' end
    )
    returning * into saved_revision;

    insert into public.audit_events (
        user_id, event_type, resource_type, resource_id, metadata
    ) values (
        user_id_input, 'application.form.profile_answers_refreshed',
        'application_form_revision', saved_revision.id,
        jsonb_build_object(
            'application_id', saved_revision.application_id,
            'previous_revision_id', target_revision.id,
            'previous_revision', target_revision.revision,
            'revision', saved_revision.revision,
            'schema_hash', saved_revision.schema_hash,
            'answer_count', jsonb_object_length(answers_input)
        )
    );

    return next saved_revision;
end;
$$;

revoke all on function public.refresh_application_form_profile_answers_for_user(
    uuid, uuid, bigint, text, jsonb
) from public, anon, authenticated;
grant execute on function public.refresh_application_form_profile_answers_for_user(
    uuid, uuid, bigint, text, jsonb
) to service_role;

do $form_submit_attention_assertions$
begin
    if has_function_privilege(
        'authenticated',
        'public.complete_application_form_submit_attention(uuid,text,jsonb)',
        'EXECUTE'
    ) then
        raise exception 'form submit attention completion must remain service-role-only';
    end if;
    if has_function_privilege(
        'authenticated',
        'public.refresh_application_form_profile_answers_for_user(uuid,uuid,bigint,text,jsonb)',
        'EXECUTE'
    ) then
        raise exception 'profile form sync must remain service-role-only';
    end if;
end;
$form_submit_attention_assertions$;

commit;
