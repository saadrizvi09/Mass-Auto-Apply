-- Google Forms is review-first.  Authenticated users may enqueue scans and
-- reviewed prefills, but the final Submit action belongs to the user inside the
-- retained Browserbase Live View.  The worker repeats this guard so an already
-- queued row also fails closed without touching the provider.

begin;

create or replace function public.guard_google_forms_manual_submit()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.kind = 'application_submit' and new.provider = 'google_forms' then
        raise exception 'provider_automation_unavailable';
    end if;
    return new;
end;
$$;

revoke all on function public.guard_google_forms_manual_submit()
    from public, anon, authenticated;
grant execute on function public.guard_google_forms_manual_submit() to service_role;

drop trigger if exists automation_jobs_google_forms_manual_submit
    on public.automation_jobs;
create trigger automation_jobs_google_forms_manual_submit
    before insert or update of kind, provider
    on public.automation_jobs
    for each row
    execute function public.guard_google_forms_manual_submit();

commit;
