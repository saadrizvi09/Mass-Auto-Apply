-- Replace the temporary Google Forms manual-submit prohibition with a strict,
-- approval-bound submit gate.  Submission is allowed only for the latest exact
-- revision approved by the owning tenant.  The queue RPC remains the public
-- entry point and its user/idempotency uniqueness keeps retries deterministic.

begin;

drop trigger if exists automation_jobs_google_forms_manual_submit
    on public.automation_jobs;
drop trigger if exists automation_jobs_google_forms_approved_submit
    on public.automation_jobs;

drop function if exists public.guard_google_forms_manual_submit();

-- Defensive cleanup for deployments that represented the temporary prohibition
-- as a CHECK constraint rather than the trigger shipped by this repository.
alter table public.automation_jobs
    drop constraint if exists automation_jobs_google_forms_manual_submit_check;

create or replace function public.guard_google_forms_approved_submit()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.kind = 'application_submit' and new.provider = 'google_forms' then
        if new.application_id is null or new.form_revision_id is null then
            raise exception using errcode = 'P0001', message = 'form_approval_required';
        end if;
        if new.payload is null
           or jsonb_typeof(new.payload) is distinct from 'object'
           or new.payload ->> 'form_revision_id' is distinct from new.form_revision_id::text
           or jsonb_typeof(new.payload -> 'required_answer_preflight') is distinct from 'object'
           or jsonb_typeof(new.payload -> 'required_answer_preflight' -> 'complete') is distinct from 'boolean'
           or jsonb_typeof(new.payload -> 'required_answer_preflight' -> 'missing_count') is distinct from 'number'
           or jsonb_typeof(new.payload -> 'required_answer_preflight' -> 'missing_keys') is distinct from 'array'
           or octet_length((new.payload -> 'required_answer_preflight')::text) > 16384 then
            raise exception using errcode = '22023', message = 'submit_preflight_invalid';
        end if;
        if new.payload -> 'required_answer_preflight' -> 'complete' <> 'true'::jsonb
           or new.payload -> 'required_answer_preflight' -> 'missing_count' <> '0'::jsonb
           or jsonb_array_length(
                new.payload -> 'required_answer_preflight' -> 'missing_keys'
           ) <> 0 then
            raise exception using errcode = 'P0001', message = 'form_required_answers_missing';
        end if;
        if not exists (
            select 1
              from public.application_form_revisions revision
             where revision.id = new.form_revision_id
               and revision.user_id = new.user_id
               and revision.application_id = new.application_id
               and revision.provider = 'google_forms'
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
        ) then
            raise exception using errcode = 'P0001', message = 'form_approval_required';
        end if;
    end if;
    return new;
end;
$$;

revoke all on function public.guard_google_forms_approved_submit()
    from public, anon, authenticated;
grant execute on function public.guard_google_forms_approved_submit()
    to service_role;

create trigger automation_jobs_google_forms_approved_submit
    before insert or update of user_id, application_id, form_revision_id, kind, provider, payload
    on public.automation_jobs
    for each row
    execute function public.guard_google_forms_approved_submit();

-- The base schema already has UNIQUE (user_id, idempotency_key).  This partial
-- index makes the narrower approved-revision submit invariant explicit and
-- protects it if the broader queue uniqueness is ever refactored.
create unique index if not exists automation_jobs_one_submit_revision_key_idx
    on public.automation_jobs (user_id, form_revision_id, idempotency_key)
    where kind = 'application_submit' and form_revision_id is not null;

commit;
