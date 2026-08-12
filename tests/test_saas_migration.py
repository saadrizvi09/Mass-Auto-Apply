from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202608080001_autoapply_cloud.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()
HARDENING_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202608110001_harden_internal_functions.sql"
)
HARDENING_SQL = HARDENING_MIGRATION.read_text(encoding="utf-8").lower()
PROFILE_FIELDS_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202608110008_resume_profile_fields.sql"
)
PROFILE_FIELDS_SQL = PROFILE_FIELDS_MIGRATION.read_text(encoding="utf-8").lower()


def test_every_tenant_table_enables_rls() -> None:
    tables = {
        "profiles",
        "user_settings",
        "resumes",
        "jobs",
        "applications",
        "connections",
        "connection_secrets",
        "connection_lifecycles",
        "oauth_states",
        "automation_jobs",
        "send_events",
        "provider_send_events",
        "answer_bank",
        "audit_events",
    }
    for table in tables:
        assert f"alter table public.{table} enable row level security;" in SQL


def test_secret_tables_have_no_browser_grants() -> None:
    assert "revoke all on public.connection_secrets from anon, authenticated;" in SQL
    assert "revoke all on public.connection_lifecycles from anon, authenticated;" in SQL
    assert "revoke all on public.oauth_states from anon, authenticated;" in SQL
    assert "grant execute on function public.consume_oauth_state(text, text) to service_role;" in SQL
    assert "grant execute on function public.consume_oauth_state(text, text) to authenticated" not in SQL


def test_internal_security_definer_helpers_are_not_browser_callable() -> None:
    for function_name in (
        "enforce_tenant_row_quota",
        "enforce_audit_retention",
        "prune_expired_oauth_states",
        "handle_new_auth_user",
    ):
        assert (
            f"revoke all on function public.{function_name}()\n"
            "    from public, anon, authenticated;"
        ) in HARDENING_SQL
    assert "to_regprocedure('public.rls_auto_enable()') is not null" in HARDENING_SQL
    assert (
        "revoke all on function public.rls_auto_enable() "
        "from public, anon, authenticated"
    ) in HARDENING_SQL


def test_provider_send_ledger_is_pseudonymous_private_and_bounded() -> None:
    ledger = SQL.split("create table public.provider_send_events (", 1)[1].split("\n);", 1)[0]
    assert "user_id" not in ledger
    assert "application_id" not in ledger
    assert "references " not in ledger
    assert "external_account_id" not in ledger
    assert "recipient text" not in ledger
    assert "provider_account_hash ~ '^[0-9a-f]{64}$'" in ledger
    assert "recipient_hash ~ '^[0-9a-f]{64}$'" in ledger
    assert "expires_at <= created_at + interval '90 days'" in ledger
    assert "revoke all on public.provider_send_events from public, anon, authenticated;" in SQL
    assert "provider send abuse ledger must remain service-role-only" in SQL
    assert "create extension if not exists pg_cron;" in SQL
    assert "create or replace function public.prune_provider_send_events()" in SQL
    assert "where event.expires_at <= clock_timestamp()" in SQL
    assert "'autoapply-prune-provider-send-events'" in SQL
    assert "'17 * * * *'" in SQL


def test_provider_send_limits_survive_user_deletion_and_finalize_atomically() -> None:
    reserve = SQL.split(
        "create or replace function public.reserve_application_send(", 1
    )[1].split("create or replace function public.reconcile_stale_application_send(", 1)[0]
    reconcile = SQL.split(
        "create or replace function public.reconcile_stale_application_send(", 1
    )[1].split("create or replace function public.finalize_application_send(", 1)[0]
    finalize = SQL.split(
        "create or replace function public.finalize_application_send(", 1
    )[1].split("revoke all on function public.reserve_application_send", 1)[0]

    assert "extensions.digest('gmail:' || btrim(gmail_connection.external_account_id), 'sha256')" in reserve
    assert "'gmail-recipient:' || lower(btrim(target_application.recipient))" in reserve
    assert "pg_advisory_xact_lock(hashtextextended(account_hash, 0))" in reserve
    assert "provider_sent_count >= 25" in reserve
    assert "message = 'provider_daily_send_cap_reached'" in reserve
    assert "insert into public.provider_send_events" in reserve
    assert "make_interval(days => tenant_settings.duplicate_window_days)" in reserve
    assert "update public.provider_send_events provider_event" in reconcile
    assert "outcome = 'needs_attention'" in reconcile
    assert "update public.provider_send_events provider_event" in finalize
    assert "where provider_event.send_event_id = existing_event.id" in finalize


def test_review_and_connection_cleanup_are_server_managed() -> None:
    assert "grant select on public.applications to authenticated;" in SQL
    assert "grant select on public.connections to authenticated;" in SQL
    assert "grant select, insert, update, delete on public.applications" not in SQL
    assert "grant select, delete on public.connections" not in SQL
    assert "require_review boolean not null default true check (require_review)" in SQL
    assert "revoke all on public.resumes from authenticated;" in SQL
    assert "grant select on public.resumes to authenticated;" in SQL
    assert "grant select, insert, update, delete on public.resumes" not in SQL
    assert "resume mutations must remain api/rpc-managed" in SQL


def test_resume_bucket_is_private_bounded_and_owner_prefixed() -> None:
    assert "values ('resumes', 'resumes', false, 6291456" in SQL
    assert "array['application/pdf']::text[]" in SQL
    assert SQL.count("(storage.foldername(name))[1] = (select auth.uid())::text") >= 5
    quota = SQL.split(
        "create or replace function public.resume_object_quota_available()", 1
    )[1].split("revoke all on function public.resume_object_quota_available()", 1)[0]
    assert "profile.account_status = 'active'" in quota
    assert "for share" in quota
    assert "pg_advisory_xact_lock" in quota


def test_resume_registration_is_atomic_and_rpc_managed() -> None:
    registration = SQL.split(
        "create or replace function public.register_resume(", 1
    )[1].split("revoke all on function public.register_resume", 1)[0]
    assert "pg_advisory_xact_lock" in registration
    assert "from storage.objects object" in registration
    assert "set is_active = false" in registration
    assert "return next saved_resume" in registration
    assert "grant execute on function public.register_resume(text, text, text, bigint, text)" in SQL


def test_resume_education_facts_are_bounded_and_user_editable() -> None:
    assert "add column college text" in PROFILE_FIELDS_SQL
    assert "add column degree text" in PROFILE_FIELDS_SQL
    assert "add column graduation_year smallint" in PROFILE_FIELDS_SQL
    assert "graduation_year between 1950 and 2100" in PROFILE_FIELDS_SQL
    assert (
        "grant update (college, degree, graduation_year) on public.profiles to authenticated;"
        in PROFILE_FIELDS_SQL
    )


def test_google_connection_lifecycle_rejects_stale_callbacks() -> None:
    assert "generation bigint not null check (generation > 0)" in SQL
    assert "create or replace function public.create_google_oauth_state(" in SQL
    assert "create or replace function public.begin_google_disconnect(" in SQL
    assert "create or replace function public.finish_google_disconnect(" in SQL
    save = SQL.split(
        "create or replace function public.save_google_connection(", 1
    )[1].split("create or replace function public.begin_browser_start(", 1)[0]
    assert "expected_generation_input bigint" in save
    assert "message = 'google_oauth_flow_stale'" in save
    assert "set status = 'connected'" in save
    assert "message = 'gmail_send_in_progress'" in save
    oauth_start = SQL.split(
        "create or replace function public.create_google_oauth_state(", 1
    )[1].split("create or replace function public.begin_google_disconnect(", 1)[0]
    disconnect = SQL.split(
        "create or replace function public.begin_google_disconnect(", 1
    )[1].split("create or replace function public.finish_google_disconnect(", 1)[0]
    assert "event.outcome = 'pending_provider'" in oauth_start
    assert "event.outcome = 'pending_provider'" in disconnect


def test_gmail_send_reservation_locks_connected_lifecycle() -> None:
    reserve = SQL.split(
        "create or replace function public.reserve_application_send(", 1
    )[1].split("create or replace function public.reconcile_stale_application_send(", 1)[0]
    assert "from public.connection_lifecycles lifecycle" in reserve
    assert "lifecycle.provider = 'gmail'" in reserve
    assert "lifecycle.status = 'connected'" in reserve
    assert "for share" in reserve


def test_managed_browser_lifecycle_is_generation_checked_and_server_only() -> None:
    begin_start = SQL.split(
        "create or replace function public.begin_browser_start(", 1
    )[1].split("create or replace function public.save_browser_connection_context(", 1)[0]
    save_context = SQL.split(
        "create or replace function public.save_browser_connection_context(", 1
    )[1].split("create or replace function public.save_browser_connection_session(", 1)[0]
    save_session = SQL.split(
        "create or replace function public.save_browser_connection_session(", 1
    )[1].split("create or replace function public.confirm_browser_start(", 1)[0]
    confirm_start = SQL.split(
        "create or replace function public.confirm_browser_start(", 1
    )[1].split("create or replace function public.abort_browser_start(", 1)[0]
    abort_start = SQL.split(
        "create or replace function public.abort_browser_start(", 1
    )[1].split("create or replace function public.finish_browser_start(", 1)[0]
    begin_disconnect = SQL.split(
        "create or replace function public.begin_browser_disconnect(", 1
    )[1].split("create or replace function public.finish_browser_disconnect(", 1)[0]
    finish_disconnect = SQL.split(
        "create or replace function public.finish_browser_disconnect(", 1
    )[1].split("revoke all on function public.begin_browser_start", 1)[0]

    for body in (
        begin_start,
        save_context,
        save_session,
        confirm_start,
        abort_start,
        begin_disconnect,
        finish_disconnect,
    ):
        assert "provider_input not in ('greenhouse', 'lever', 'ashby')" in body
        assert "browser-lifecycle:" in body
        assert "pg_advisory_xact_lock" in body
    assert "prior_status = 'disconnecting'" in begin_start
    assert "message = 'browser_connection_operation_in_progress'" in begin_start
    assert "message = 'browser_start_rate_limited'" in begin_start
    assert "'browser.start', 'connection'" in begin_start
    assert "function public.reserve_browser_start" not in SQL
    assert "next_generation := next_generation + 1" in begin_start
    assert "lifecycle.generation = expected_generation_input" in save_context
    assert "lifecycle.status = 'connecting'" in save_context
    assert "browser_lifecycle_generation = expected_generation_input" in save_session
    assert "secret.browser_session_id_ciphertext = expected_session_ciphertext_input" in confirm_start
    assert "for share of lifecycle, connection, secret" in confirm_start
    assert "return false" in abort_start
    assert "lifecycle_status <> 'disconnecting'" in begin_disconnect
    assert "current_connection_id is distinct from expected_connection_id_input" in finish_disconnect
    assert "connection.id = expected_connection_id_input" in finish_disconnect
    assert "browser_lifecycle_generation bigint" in SQL
    assert "grant execute on function public.begin_browser_start(uuid, text) to service_role;" in SQL
    assert "grant execute on function public.begin_browser_start(uuid, text) to authenticated" not in SQL
    assert "grant execute on function public.confirm_browser_start(uuid, text, bigint, uuid, text, text)" in SQL
    assert "grant execute on function public.finish_browser_disconnect(uuid, text, bigint, uuid)" in SQL


def test_linkedin_is_not_a_managed_browser_lifecycle_provider() -> None:
    lifecycle_sql = SQL.split(
        "create or replace function public.begin_browser_start(", 1
    )[1].split("-- queue insertion", 1)[0]
    assert "provider_input not in ('greenhouse', 'lever', 'ashby')" in lifecycle_sql
    assert "('greenhouse', 'lever', 'ashby', 'linkedin')" not in lifecycle_sql


def test_send_and_worker_concurrency_guards_are_migration_enforced() -> None:
    assert "for update skip locked" in SQL
    assert "message = 'send_in_progress'" in SQL
    assert "create or replace function public.cancel_automation_job(job_id uuid)" in SQL
    assert "has_table_privilege('authenticated', 'public.automation_jobs', 'update')" in SQL
    assert "unique (user_id, idempotency_key)" in SQL
