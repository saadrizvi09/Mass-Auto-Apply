-- Keep every tenant-owned foreign reference check scoped to the row type that
-- fired the shared trigger.  The original IF/ELSIF predicates combined the
-- table-name test with row-specific field access.  When a valid predicate was
-- false, PostgreSQL evaluated the next branch against the wrong NEW record and
-- could raise "record new has no field ...".

begin;

create or replace function public.enforce_owned_reference()
returns trigger
language plpgsql
set search_path = 'public'
as $$
begin
    if tg_table_name = 'applications' then
        if new.job_id is not null and not exists (
            select 1 from public.jobs
             where id = new.job_id and user_id = new.user_id
        ) then
            raise exception using errcode = '23503', message = 'owned_job_not_found';
        end if;
    elsif tg_table_name = 'automation_jobs' then
        if new.application_id is not null and not exists (
            select 1 from public.applications
             where id = new.application_id and user_id = new.user_id
        ) then
            raise exception using
                errcode = '23503', message = 'owned_application_not_found';
        end if;
    elsif tg_table_name = 'connection_secrets' then
        if not exists (
            select 1 from public.connections
             where id = new.connection_id and user_id = new.user_id
        ) then
            raise exception using
                errcode = '23503', message = 'owned_connection_not_found';
        end if;
    elsif tg_table_name = 'send_events' then
        if new.application_id is not null and not exists (
            select 1 from public.applications
             where id = new.application_id and user_id = new.user_id
        ) then
            raise exception using
                errcode = '23503', message = 'owned_application_not_found';
        end if;
    else
        raise exception using
            errcode = 'P0001', message = 'owned_reference_trigger_misconfigured';
    end if;
    return new;
end;
$$;

commit;
