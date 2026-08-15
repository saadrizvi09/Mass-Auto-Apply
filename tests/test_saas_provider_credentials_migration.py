from pathlib import Path
import re


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/202608150002_user_provider_credentials.sql"
).read_text(encoding="utf-8").lower()


def _squash(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\(\s+", "(", value)
    return re.sub(r"\s+\)", ")", value)


def _function(name: str, next_name: str | None = None) -> str:
    marker = f"create or replace function public.{name}"
    assert marker in MIGRATION, f"missing migration function: {name}"
    section = MIGRATION.split(marker, 1)[1]
    if next_name is not None:
        section = section.split(
            f"create or replace function public.{next_name}", 1
        )[0]
    return _squash(section)


def test_provider_credentials_and_epoch_are_service_role_only_and_encrypted() -> None:
    assert "create table public.user_provider_credentials" in MIGRATION
    assert "credential_ciphertext text not null" in MIGRATION
    credential_table = MIGRATION.split(
        "create table public.user_provider_credentials", 1
    )[1].split(");", 1)[0]
    assert "api_key" not in credential_table
    assert "binding_fingerprint text" in credential_table
    assert "create table public.browserbase_credential_states" in MIGRATION
    assert "epoch bigint not null default 0" in MIGRATION
    for table in ("user_provider_credentials", "browserbase_credential_states"):
        assert f"alter table public.{table} enable row level security" in MIGRATION
        assert (
            f"revoke all on public.{table} from public, anon, authenticated"
            in MIGRATION
        )
        assert f"grant all on public.{table} to service_role" in MIGRATION


def test_every_browserbase_mutation_uses_one_account_wide_lock() -> None:
    for function_name, next_name, account_id in (
        (
            "get_browserbase_credential_state",
            "save_user_provider_credential",
            "user_id_input",
        ),
        (
            "save_user_provider_credential",
            "delete_user_provider_credential",
            "user_id_input",
        ),
        (
            "delete_user_provider_credential",
            "get_application_job_browserbase_credential",
            "user_id_input",
        ),
        (
            "get_application_job_browserbase_credential",
            "get_application_job_browser_context_binding",
            "user_id_input",
        ),
        (
            "get_application_job_browser_context_binding",
            "begin_browser_start",
            "user_id_input",
        ),
        (
            "begin_browser_start",
            "save_browser_connection_context_bound",
            "user_id_input",
        ),
        (
            "save_browser_connection_context_bound",
            "begin_browser_disconnect",
            "user_id_input",
        ),
        ("begin_browser_disconnect", "begin_account_deletion", "user_id_input"),
        ("abandon_browserbase_resources", None, "user_id_input"),
    ):
        account_lock = f"'browserbase-account:' || {account_id}::text"
        assert account_lock in _function(function_name, next_name), function_name

    deletion = _function("begin_account_deletion", "abandon_browserbase_resources")
    assert "'browserbase-account:' || current_user_id::text" in deletion


def test_browserbase_rotation_has_epoch_cas_lifecycle_and_remote_context_fences() -> None:
    save = _function("save_user_provider_credential", "delete_user_provider_credential")
    delete = _function(
        "delete_user_provider_credential",
        "get_application_job_browserbase_credential",
    )
    for function in (save, delete):
        assert "expected_browserbase_epoch_input" in function
        assert "browserbase_credential_binding_stale" in function
        assert "lifecycle.status in ('connecting', 'disconnecting')" in function
        assert "browserbase_connection_operation_in_progress" in function
        assert "browserbase_jobs_active" in function
        assert "browserbase_disconnect_required" in function
        assert "job.status in ('queued', 'running')" in function
        assert "browser_context_id_ciphertext is not null" in function
        assert "browser_session_id_ciphertext is not null" in function
    assert "set epoch = next_generation" in save
    assert "set epoch = state.epoch + 1" in delete


def test_application_jobs_are_bound_to_browserbase_epoch_at_insert_and_claim() -> None:
    assert "add column browserbase_credential_epoch bigint" in MIGRATION
    trigger = _function(
        "bind_automation_job_browserbase_epoch", "get_browserbase_credential_state"
    )
    assert "returns trigger" in trigger
    assert "'browserbase-account:' || new.user_id::text" in trigger
    assert "new.browserbase_credential_epoch := current_epoch" in trigger
    assert "credential.generation <> current_epoch" in trigger
    assert "credential.binding_fingerprint is null" in trigger
    assert (
        "create trigger automation_jobs_bind_browserbase_epoch before insert on public.automation_jobs"
        in _squash(MIGRATION)
    )

    worker = _function(
        "get_application_job_browserbase_credential",
        "get_application_job_browser_context_binding",
    )
    assert "queue_job.browserbase_credential_epoch <> current_epoch" in worker
    assert "browserbase_credential_binding_stale" in worker
    assert "'credential_source', 'user'" in worker
    assert "'binding_fingerprint', credential.binding_fingerprint" in worker
    assert "'epoch', current_epoch" in worker


def test_browser_start_and_context_save_bind_source_generation_epoch_and_project() -> None:
    start = _function("begin_browser_start", "save_browser_connection_context_bound")
    for field in (
        "'credential_epoch', current_epoch",
        "'active_credential_source'",
        "'active_credential_generation'",
        "'active_project_fingerprint'",
        "'context_credential_source'",
        "'context_credential_generation'",
        "'context_credential_epoch'",
        "'context_project_fingerprint'",
    ):
        assert field in start
    assert "prior_status in ('connecting', 'disconnecting')" in start
    assert "browser_connection_operation_in_progress" in start

    bound_save = _function(
        "save_browser_connection_context_bound", "begin_browser_disconnect"
    )
    assert "state.epoch = credential_epoch_input" in bound_save
    assert "credential.generation = credential_generation_input" in bound_save
    assert "credential.binding_fingerprint = project_fingerprint_input" in bound_save
    assert "browser_credential_source" in bound_save
    assert "browser_credential_generation" in bound_save
    assert "browser_credential_epoch" in bound_save
    assert "browser_project_fingerprint" in bound_save


def test_worker_credential_rpc_is_service_only_and_lease_bound() -> None:
    function = _function(
        "get_application_job_browserbase_credential",
        "get_application_job_browser_context_binding",
    )
    assert "job.status = 'running'" in function
    assert "job.locked_by = worker_id" in function
    assert "job.lease_expires_at >= clock_timestamp()" in function
    assert "job.cancel_requested_at is null" in function
    assert "credential_ciphertext" in function
    assert "select job.user_id into user_id_input" in function
    assert "and job.user_id = user_id_input" in function
    assert function.index("'browserbase-account:' || user_id_input::text") < function.index(
        "for share"
    )

    grants = _squash(MIGRATION)
    signature = (
        "public.get_application_job_browserbase_credential(uuid, text)"
    )
    assert (
        f"revoke all on function {signature} from public, anon, authenticated"
        in grants
    )
    assert f"grant execute on function {signature} to service_role" in grants

    context_function = _function(
        "get_application_job_browser_context_binding", "begin_browser_start"
    )
    assert "job.status = 'running'" in context_function
    assert "job.locked_by = worker_id" in context_function
    assert "job.lease_expires_at >= clock_timestamp()" in context_function
    assert "job.cancel_requested_at is null" in context_function
    assert "queue_job.browserbase_credential_epoch <> current_epoch" in context_function
    assert "target_secret.browser_project_fingerprint" in context_function
    assert "select job.user_id into user_id_input" in context_function
    assert "and job.user_id = user_id_input" in context_function
    assert context_function.index(
        "'browserbase-account:' || user_id_input::text"
    ) < context_function.index("for share")


def test_browser_start_never_reuses_an_unbound_legacy_context() -> None:
    start = _function("begin_browser_start", "save_browser_connection_context_bound")
    legacy_guard = (
        "if current_secret.browser_context_id_ciphertext is not null "
        "and current_secret.browser_credential_source is null "
        "and current_secret.browser_credential_generation is null "
        "and current_secret.browser_credential_epoch is null "
        "and current_secret.browser_project_fingerprint is null then "
        "reuse_context := false"
    )
    assert legacy_guard in start


def test_account_deletion_cancels_queue_and_drains_active_worker_leases() -> None:
    function = _function("begin_account_deletion", "abandon_browserbase_resources")
    assert "job.status = 'queued'" in function
    assert "set status = 'cancelled'" in function
    assert "job.status = 'running'" in function
    assert "job.lease_expires_at is null or job.lease_expires_at < timestamp_now" in function
    assert "set cancel_requested_at = coalesce(job.cancel_requested_at, timestamp_now)" in function
    assert "'account.deletion_drain'" in function
    assert "return false" in function
    assert "set account_status = 'deleting'" in function
    # Authenticated retry remains possible, while ordinary tenant work is
    # blocked before the worker-drain conflict is returned.
    assert function.index("set account_status = 'deleting'") < function.index(
        "return false"
    )


def test_explicit_browserbase_abandon_is_confirmed_audited_and_service_only() -> None:
    function = _function("abandon_browserbase_resources")
    assert "confirmation_input <> 'abandon remote browser data'" in function
    assert "browserbase_abandon_confirmation_invalid" in function
    assert "job.status in ('queued', 'running')" in function
    assert "lifecycle.status in ('connecting', 'disconnecting')" in function
    assert "'browserbase.resources_abandoned'" in function
    assert "'remote_cleanup_confirmed', false" in function
    assert "'needs_attention'" in function

    grants = _squash(MIGRATION)
    signature = "public.abandon_browserbase_resources(uuid, text)"
    assert (
        f"revoke all on function {signature} from public, anon, authenticated"
        in grants
    )
    assert f"grant execute on function {signature} to service_role" in grants


def test_privileged_rpc_grants_use_exact_hardened_signatures() -> None:
    grants = _squash(MIGRATION)
    service_only_signatures = (
        "public.get_browserbase_credential_state(uuid)",
        "public.get_application_job_browser_context_binding(uuid, text)",
        "public.save_user_provider_credential(uuid, text, text, text, text, timestamptz, text, bigint)",
        "public.delete_user_provider_credential(uuid, text, bigint)",
        "public.save_browser_connection_context_bound(uuid, text, bigint, text, text, text, bigint, bigint, text)",
        "public.abandon_browserbase_resources(uuid, text)",
    )
    for signature in service_only_signatures:
        assert (
            f"revoke all on function {signature} from public, anon, authenticated"
            in grants
        ), signature
        assert f"grant execute on function {signature} to service_role" in grants, signature

    # The application must not retain execute permission on the pre-binding
    # context persistence RPC after this migration is applied.
    assert (
        "revoke all on function public.save_browser_connection_context(uuid, text, bigint, text, text) from service_role"
        in grants
    )
