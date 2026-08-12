-- Store tenant-supplied Google OAuth web-client credentials without exposing
-- them through PostgREST. Credential changes share Gmail's lifecycle lock so a
-- callback can never exchange its code with a different client secret.

begin;

create table public.user_google_oauth_clients (
    user_id uuid primary key references auth.users(id) on delete cascade,
    client_id_ciphertext text not null check (
        char_length(client_id_ciphertext) between 16 and 16384
    ),
    client_secret_ciphertext text not null check (
        char_length(client_secret_ciphertext) between 16 and 16384
    ),
    generation bigint not null check (generation > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create trigger user_google_oauth_clients_set_updated_at
    before update on public.user_google_oauth_clients
    for each row execute function public.set_updated_at();

alter table public.user_google_oauth_clients enable row level security;

-- There are deliberately no browser policies. The API encrypts both values
-- before this table is touched and accesses it only with the service role.
revoke all on public.user_google_oauth_clients from public, anon, authenticated;
grant all on public.user_google_oauth_clients to service_role;

-- Existing in-flight states predate tenant credentials and therefore remain
-- bound to the deployment's platform client. A user binding always carries the
-- exact credential generation selected at OAuth-start time.
alter table public.oauth_states
    add column credential_source text not null default 'platform' check (
        credential_source in ('platform', 'user')
    ),
    add column credential_generation bigint,
    add constraint oauth_states_credential_binding_check check (
        (credential_source = 'platform' and credential_generation is null)
        or
        (credential_source = 'user'
         and credential_generation is not null
         and credential_generation > 0)
    );

-- Create or replace a user's encrypted OAuth client. A credential cannot be
-- changed while Gmail tokens or an unresolved provider send still depend on it.
-- Advancing Gmail's durable lifecycle generation keeps credential generations
-- monotonic even after a credential row is deleted and later recreated.
create or replace function public.save_user_google_oauth_client(
    user_id_input uuid,
    client_id_ciphertext_input text,
    client_secret_ciphertext_input text
)
returns bigint
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    prior_credential_generation bigint;
    lifecycle_generation bigint;
    lifecycle_exists boolean;
    next_generation bigint;
begin
    if user_id_input is null
       or char_length(coalesce(client_id_ciphertext_input, '')) not between 16 and 16384
       or char_length(coalesce(client_secret_ciphertext_input, '')) not between 16 and 16384 then
        raise exception using errcode = '22023', message = 'google_oauth_client_invalid';
    end if;

    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    if exists (
        select 1 from public.send_events event
         where event.user_id = user_id_input and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'gmail_send_in_progress';
    end if;
    if exists (
        select 1 from public.connections connection
         where connection.user_id = user_id_input and connection.provider = 'gmail'
    ) then
        raise exception using errcode = 'P0001', message = 'google_connection_must_disconnect';
    end if;

    select client.generation into prior_credential_generation
      from public.user_google_oauth_clients client
     where client.user_id = user_id_input
     for update;

    select lifecycle.generation into lifecycle_generation
      from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail'
     for update;
    lifecycle_exists := found;

    next_generation := greatest(
        coalesce(prior_credential_generation, 0),
        coalesce(lifecycle_generation, 0)
    ) + 1;

    if lifecycle_exists then
        update public.connection_lifecycles lifecycle
           set generation = next_generation, status = 'disconnected'
         where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail';
    else
        insert into public.connection_lifecycles (
            user_id, provider, generation, status
        ) values (user_id_input, 'gmail', next_generation, 'disconnected');
    end if;

    delete from public.oauth_states state
     where state.user_id = user_id_input and state.provider = 'google';

    insert into public.user_google_oauth_clients as client (
        user_id, client_id_ciphertext, client_secret_ciphertext, generation
    ) values (
        user_id_input, client_id_ciphertext_input,
        client_secret_ciphertext_input, next_generation
    )
    on conflict (user_id) do update
       set client_id_ciphertext = excluded.client_id_ciphertext,
           client_secret_ciphertext = excluded.client_secret_ciphertext,
           generation = excluded.generation;

    return next_generation;
end;
$$;

-- Delete the tenant client only after Gmail has been disconnected. The durable
-- lifecycle is advanced before the secret row disappears, preventing generation
-- reuse if the user later configures another client.
create or replace function public.delete_user_google_oauth_client(
    user_id_input uuid
)
returns bigint
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    prior_credential_generation bigint;
    lifecycle_generation bigint;
    lifecycle_exists boolean;
    next_generation bigint;
begin
    if user_id_input is null then
        raise exception using errcode = '22023', message = 'google_oauth_client_invalid';
    end if;

    perform 1 from public.profiles profile
     where profile.user_id = user_id_input
     for share;
    if not found then
        raise exception using errcode = 'P0002', message = 'profile_not_found';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    if exists (
        select 1 from public.send_events event
         where event.user_id = user_id_input and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'gmail_send_in_progress';
    end if;
    if exists (
        select 1 from public.connections connection
         where connection.user_id = user_id_input and connection.provider = 'gmail'
    ) then
        raise exception using errcode = 'P0001', message = 'google_connection_must_disconnect';
    end if;

    select client.generation into prior_credential_generation
      from public.user_google_oauth_clients client
     where client.user_id = user_id_input
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'google_oauth_client_not_found';
    end if;

    select lifecycle.generation into lifecycle_generation
      from public.connection_lifecycles lifecycle
     where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail'
     for update;
    lifecycle_exists := found;

    next_generation := greatest(
        prior_credential_generation,
        coalesce(lifecycle_generation, 0)
    ) + 1;

    if lifecycle_exists then
        update public.connection_lifecycles lifecycle
           set generation = next_generation, status = 'disconnected'
         where lifecycle.user_id = user_id_input and lifecycle.provider = 'gmail';
    else
        insert into public.connection_lifecycles (
            user_id, provider, generation, status
        ) values (user_id_input, 'gmail', next_generation, 'disconnected');
    end if;

    delete from public.oauth_states state
     where state.user_id = user_id_input and state.provider = 'google';
    delete from public.user_google_oauth_clients client
     where client.user_id = user_id_input;

    return next_generation;
end;
$$;

-- Version two binds each state to the credential selected by the API. For a
-- tenant credential the current generation is checked under the same Gmail
-- lifecycle lock used by save/delete, closing both read/change and callback races.
create or replace function public.create_google_oauth_state_v2(
    user_id_input uuid,
    state_hash_input text,
    return_path_input text,
    pkce_verifier_ciphertext_input text,
    expires_at_input timestamptz,
    credential_source_input text,
    credential_generation_input bigint default null
)
returns bigint
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    next_generation bigint;
begin
    if user_id_input is null
       or state_hash_input !~ '^[0-9a-f]{64}$'
       or return_path_input not like '/%'
       or return_path_input like '//%'
       or char_length(return_path_input) > 1024
       or nullif(pkce_verifier_ciphertext_input, '') is null
       or expires_at_input <= clock_timestamp()
       or expires_at_input > clock_timestamp() + interval '30 minutes'
       or credential_source_input not in ('platform', 'user')
       or (credential_source_input = 'platform' and credential_generation_input is not null)
       or (credential_source_input = 'user' and coalesce(credential_generation_input, 0) < 1) then
        raise exception using errcode = '22023', message = 'google_oauth_state_invalid';
    end if;

    perform 1 from public.profiles profile
     where profile.user_id = user_id_input and profile.account_status = 'active'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'account_deletion_in_progress';
    end if;

    perform pg_advisory_xact_lock(
        hashtextextended('gmail-lifecycle:' || user_id_input::text, 0)
    );
    if credential_source_input = 'user' then
        perform 1 from public.user_google_oauth_clients client
         where client.user_id = user_id_input
           and client.generation = credential_generation_input
         for share;
        if not found then
            raise exception using errcode = 'P0001', message = 'google_oauth_client_stale';
        end if;
    end if;

    insert into public.connection_lifecycles as lifecycle (
        user_id, provider, generation, status
    ) values (user_id_input, 'gmail', 1, 'connecting')
    on conflict (user_id, provider) do update
       set generation = lifecycle.generation + 1,
           status = 'connecting'
     where lifecycle.status <> 'disconnecting'
    returning generation into next_generation;
    if not found then
        raise exception using errcode = 'P0001', message = 'connection_operation_in_progress';
    end if;
    if exists (
        select 1 from public.send_events event
         where event.user_id = user_id_input and event.outcome = 'pending_provider'
    ) then
        raise exception using errcode = 'P0001', message = 'gmail_send_in_progress';
    end if;

    delete from public.oauth_states state
     where state.user_id = user_id_input and state.provider = 'google';
    insert into public.oauth_states (
        state_hash, user_id, provider, generation, return_path,
        pkce_verifier_ciphertext, expires_at,
        credential_source, credential_generation
    ) values (
        state_hash_input, user_id_input, 'google', next_generation,
        return_path_input, pkce_verifier_ciphertext_input, expires_at_input,
        credential_source_input, credential_generation_input
    );

    return next_generation;
end;
$$;

revoke all on function public.save_user_google_oauth_client(uuid, text, text)
    from public, anon, authenticated;
revoke all on function public.delete_user_google_oauth_client(uuid)
    from public, anon, authenticated;
revoke all on function public.create_google_oauth_state_v2(
    uuid, text, text, text, timestamptz, text, bigint
) from public, anon, authenticated;

grant execute on function public.save_user_google_oauth_client(uuid, text, text)
    to service_role;
grant execute on function public.delete_user_google_oauth_client(uuid)
    to service_role;
grant execute on function public.create_google_oauth_state_v2(
    uuid, text, text, text, timestamptz, text, bigint
) to service_role;

do $assert_user_google_oauth_clients_server_only$
begin
    if has_table_privilege('anon', 'public.user_google_oauth_clients', 'SELECT')
       or has_table_privilege('anon', 'public.user_google_oauth_clients', 'INSERT')
       or has_table_privilege('anon', 'public.user_google_oauth_clients', 'UPDATE')
       or has_table_privilege('anon', 'public.user_google_oauth_clients', 'DELETE')
       or has_table_privilege('authenticated', 'public.user_google_oauth_clients', 'SELECT')
       or has_table_privilege('authenticated', 'public.user_google_oauth_clients', 'INSERT')
       or has_table_privilege('authenticated', 'public.user_google_oauth_clients', 'UPDATE')
       or has_table_privilege('authenticated', 'public.user_google_oauth_clients', 'DELETE') then
        raise exception 'user Google OAuth clients must remain service-role-only';
    end if;
end;
$assert_user_google_oauth_clients_server_only$;

commit;
