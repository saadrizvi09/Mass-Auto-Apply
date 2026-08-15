-- Repair the deployed Profile-to-form synchronization RPC after Supabase's
-- linked schema linter identified a nonexistent JSON-object length call.
-- The state, tenant, approval, and durable submit fences remain unchanged.

begin;

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
            'answer_count', (
                select count(*) from jsonb_object_keys(answers_input)
            )
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

do $profile_form_answer_count_assertions$
begin
    if has_function_privilege(
        'authenticated',
        'public.refresh_application_form_profile_answers_for_user(uuid,uuid,bigint,text,jsonb)',
        'EXECUTE'
    ) then
        raise exception 'profile form sync must remain service-role-only';
    end if;
end;
$profile_form_answer_count_assertions$;

commit;
