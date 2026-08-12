-- Internal trigger helpers must not inherit PostgreSQL's default PUBLIC execute
-- privilege. Triggers and event triggers continue to invoke their functions after
-- these grants are removed; browser roles should never call them through PostgREST.

begin;

revoke all on function public.enforce_tenant_row_quota()
    from public, anon, authenticated;
revoke all on function public.enforce_audit_retention()
    from public, anon, authenticated;
revoke all on function public.prune_expired_oauth_states()
    from public, anon, authenticated;
revoke all on function public.handle_new_auth_user()
    from public, anon, authenticated;

-- Supabase's "automatic RLS" project option creates this event-trigger helper.
-- Keep the migration portable to projects where that option was not selected.
do $revoke_auto_rls$
begin
    if to_regprocedure('public.rls_auto_enable()') is not null then
        execute 'revoke all on function public.rls_auto_enable() from public, anon, authenticated';
    end if;
end;
$revoke_auto_rls$;

commit;
