from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202608110007_user_google_oauth_clients.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()


def _function_body(name: str, next_marker: str) -> str:
    return SQL.split(f"create or replace function public.{name}(", 1)[1].split(
        next_marker, 1
    )[0]


def test_tenant_google_client_secrets_are_server_only_and_encrypted() -> None:
    table = SQL.split(
        "create table public.user_google_oauth_clients (", 1
    )[1].split("\n);", 1)[0]

    assert "user_id uuid primary key references auth.users(id) on delete cascade" in table
    assert "client_id_ciphertext text not null" in table
    assert "client_secret_ciphertext text not null" in table
    assert "client_id text" not in table
    assert "client_secret text" not in table
    assert "generation bigint not null check (generation > 0)" in table
    assert "alter table public.user_google_oauth_clients enable row level security;" in SQL
    assert (
        "revoke all on public.user_google_oauth_clients "
        "from public, anon, authenticated;"
    ) in SQL
    assert "grant all on public.user_google_oauth_clients to service_role;" in SQL
    assert "create policy" not in SQL
    assert "user google oauth clients must remain service-role-only" in SQL


def test_oauth_states_bind_the_selected_credential_source_and_generation() -> None:
    binding = SQL.split("alter table public.oauth_states", 1)[1].split(
        "create or replace function public.save_user_google_oauth_client(", 1
    )[0]

    assert "credential_source text not null default 'platform'" in binding
    assert "credential_source in ('platform', 'user')" in binding
    assert "add column credential_generation bigint" in binding
    assert "credential_source = 'platform' and credential_generation is null" in binding
    assert "credential_source = 'user'" in binding
    assert "credential_generation is not null" in binding
    assert "credential_generation > 0" in binding


def test_credential_save_is_serialized_and_invalidates_old_flows() -> None:
    save = _function_body(
        "save_user_google_oauth_client",
        "create or replace function public.delete_user_google_oauth_client(",
    )

    assert "gmail-lifecycle:" in save
    assert "pg_advisory_xact_lock" in save
    assert "from public.connection_lifecycles lifecycle" in save
    assert "for update" in save
    assert "connection.provider = 'gmail'" in save
    assert "message = 'google_connection_must_disconnect'" in save
    assert "event.outcome = 'pending_provider'" in save
    assert "message = 'gmail_send_in_progress'" in save
    assert "greatest(" in save
    assert "coalesce(prior_credential_generation, 0)" in save
    assert "coalesce(lifecycle_generation, 0)" in save
    assert ") + 1" in save
    assert "state.provider = 'google'" in save
    assert "insert into public.user_google_oauth_clients" in save
    assert "on conflict (user_id) do update" in save


def test_credential_delete_preserves_a_monotonic_generation_floor() -> None:
    delete = _function_body(
        "delete_user_google_oauth_client",
        "create or replace function public.create_google_oauth_state_v2(",
    )

    assert "gmail-lifecycle:" in delete
    assert "pg_advisory_xact_lock" in delete
    assert "connection.provider = 'gmail'" in delete
    assert "message = 'google_connection_must_disconnect'" in delete
    assert "event.outcome = 'pending_provider'" in delete
    assert "message = 'gmail_send_in_progress'" in delete
    assert "prior_credential_generation" in delete
    assert "coalesce(lifecycle_generation, 0)" in delete
    assert ") + 1" in delete
    assert "set generation = next_generation, status = 'disconnected'" in delete
    assert "state.provider = 'google'" in delete
    assert "delete from public.user_google_oauth_clients" in delete


def test_oauth_state_v2_rejects_stale_tenant_credentials() -> None:
    oauth_start = _function_body(
        "create_google_oauth_state_v2",
        "revoke all on function public.save_user_google_oauth_client",
    )

    assert "credential_source_input not in ('platform', 'user')" in oauth_start
    assert "credential_source_input = 'platform'" in oauth_start
    assert "credential_generation_input is not null" in oauth_start
    assert "credential_source_input = 'user'" in oauth_start
    assert "client.generation = credential_generation_input" in oauth_start
    assert "for share" in oauth_start
    assert "message = 'google_oauth_client_stale'" in oauth_start
    assert "gmail-lifecycle:" in oauth_start
    assert "event.outcome = 'pending_provider'" in oauth_start
    assert "credential_source, credential_generation" in oauth_start
    assert "credential_source_input, credential_generation_input" in oauth_start


def test_all_credential_rpcs_are_service_role_only() -> None:
    signatures = (
        "public.save_user_google_oauth_client(uuid, text, text)",
        "public.delete_user_google_oauth_client(uuid)",
        "public.create_google_oauth_state_v2(\n"
        "    uuid, text, text, text, timestamptz, text, bigint\n)",
    )
    for signature in signatures:
        assert f"revoke all on function {signature}" in SQL
        assert f"grant execute on function {signature}" in SQL

    assert "to authenticated" not in SQL
