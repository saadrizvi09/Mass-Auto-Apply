-- Keep managed-browser login lifecycle support separate from public form
-- automation capability.  YC, Cutshort, and Instahyre may create isolated
-- contexts for controlled development, but authenticated clients must not be
-- able to enqueue their unfinished multi-step application flows directly.

begin;

create or replace function public.is_hosted_form_automation_provider(provider_input text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path = ''
as $$
    select provider_input = any (array[
        'google_forms', 'greenhouse', 'lever', 'ashby', 'wellfound'
    ]::text[])
$$;

revoke all on function public.is_hosted_form_automation_provider(text)
    from public, anon, authenticated;
grant execute on function public.is_hosted_form_automation_provider(text) to service_role;

do $gate_incomplete_form_automation$
declare
    routine regprocedure :=
        'public.enqueue_automation_job(text,text,uuid,jsonb,text)'::regprocedure;
    routine_definition text;
    old_gate constant text :=
        'not public.is_managed_application_provider(provider_input)';
    new_gate constant text :=
        'not public.is_hosted_form_automation_provider(provider_input)';
    gate_occurrences integer;
begin
    routine_definition := pg_get_functiondef(routine);
    gate_occurrences := (
        length(routine_definition)
        - length(replace(routine_definition, old_gate, ''))
    ) / length(old_gate);
    if gate_occurrences <> 1 then
        raise exception
            'expected one managed application enqueue gate in %, found %',
            routine,
            gate_occurrences;
    end if;
    execute replace(routine_definition, old_gate, new_gate);
end;
$gate_incomplete_form_automation$;

commit;
