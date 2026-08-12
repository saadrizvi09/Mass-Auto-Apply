from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202608110002_hosted_discovery_applications.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()


def _function(name: str, next_marker: str) -> str:
    return SQL.split(f"create or replace function public.{name}(", 1)[1].split(
        next_marker, 1
    )[0]


def test_discovery_preferences_are_bounded_tenant_data() -> None:
    table = SQL.split("create table public.discovery_preferences (", 1)[1].split(
        "\n);", 1
    )[0]
    assert "user_id uuid primary key references auth.users(id) on delete cascade" in table
    assert "max_results_per_run integer not null default 100" in table
    assert "max_results_per_run between 1 and 200" in table
    assert "schedule_interval_minutes between 15 and 1440" in table
    for source in (
        "telegram",
        "rss",
        "referral_digest",
        "csv",
        "xlsx",
        "public_ats",
        "linkedin_guest",
    ):
        assert f"'{source}'" in table
    assert "alter table public.discovery_preferences enable row level security;" in SQL
    assert "create policy discovery_preferences_select_own" in SQL
    assert "create policy discovery_preferences_update_own" in SQL
    assert (
        "grant select, insert, update, delete on public.discovery_preferences "
        "to authenticated;"
    ) in SQL


def test_form_revisions_are_owned_immutable_and_review_bounded() -> None:
    table = SQL.split("create table public.application_form_revisions (", 1)[1].split(
        "\n);", 1
    )[0]
    for reference in ("application_id", "job_id", "resume_id"):
        assert reference in table
    for provider in (
        "google_forms",
        "greenhouse",
        "lever",
        "ashby",
        "yc",
        "wellfound",
        "cutshort",
        "instahyre",
    ):
        assert f"'{provider}'" in table
    assert "schema_hash ~ '^[0-9a-f]{64}$'" in table
    assert "jsonb_typeof(question_schema) = 'array'" in table
    assert "jsonb_typeof(answers) = 'object'" in table
    assert "approved_revision = revision" in table
    assert "approved_schema_hash = schema_hash" in table
    assert "submitted_at is not null" in table
    assert "unique (application_id, revision)" in table

    guard = _function(
        "guard_application_form_revision",
        "create trigger application_form_revisions_guard_immutable",
    )
    assert "message = 'form_revision_immutable'" in guard
    assert "old.status in ('scanned', 'prefilled')" in guard
    assert "new.status = 'approved'" in guard
    assert "old.approved_at is not null" in guard
    assert "message = 'form_approval_immutable'" in guard

    assert "alter table public.application_form_revisions enable row level security;" in SQL
    assert "create policy application_form_revisions_select_own" in SQL
    assert "grant select on public.application_form_revisions to authenticated;" in SQL
    assert "grant all on public.application_form_revisions to authenticated" not in SQL


def test_approval_seals_exact_latest_revision_and_is_idempotent() -> None:
    approval = _function(
        "approve_application_form_revision",
        "revoke all on function public.approve_application_form_revision",
    )
    assert "current_user_id uuid := public.assert_active_user()" in approval
    assert "revision.user_id = current_user_id" in approval
    assert "target_revision.revision <> revision_input" in approval
    assert "target_revision.schema_hash <> schema_hash_input" in approval
    assert "newer.revision > target_revision.revision" in approval
    assert "if target_revision.status = 'approved'" in approval
    assert "target_revision.answers <> answers_input" in approval
    assert "return next target_revision" in approval
    assert "set answers = answers_input, status = 'approved'" in approval
    assert "approved_revision = revision.revision" in approval
    assert "approved_schema_hash = revision.schema_hash" in approval
    assert (
        "grant execute on function public.approve_application_form_revision"
        "(uuid, bigint, text, jsonb)\n    to authenticated;"
    ) in SQL


def test_queue_adds_discovery_and_reviewed_application_kinds_without_ziprecruiter() -> None:
    enqueue = _function(
        "enqueue_automation_job",
        "revoke all on function public.enqueue_automation_job",
    )
    for kind in (
        "discover_public_feeds",
        "discover_linkedin_guest",
        "application_scan",
        "application_prefill",
        "application_submit",
    ):
        assert f"'{kind}'" in enqueue
    assert "provider_input = 'ziprecruiter'" in enqueue
    assert "'public_feeds'" in enqueue
    assert "form_revision_id_input := nullif(payload_input ->> 'form_revision_id'" in enqueue
    assert "revision.user_id = current_user_id" in enqueue
    assert "revision.application_id = application_id_input" in enqueue
    assert "kind_input in ('application_prefill', 'application_submit') and not" in enqueue
    assert "target_revision.approved_revision = target_revision.revision" in enqueue
    assert "target_revision.approved_schema_hash = target_revision.schema_hash" in enqueue
    assert "message = 'form_approval_required'" in enqueue
    assert "user_id, application_id, form_revision_id, kind, provider, payload" in enqueue

    assert "add column form_revision_id uuid" in SQL
    assert "references public.application_form_revisions(id) on delete set null" in SQL
    assert "create trigger automation_jobs_owned_form_revision" in SQL


def test_managed_browser_lifecycle_gate_is_extended_but_excludes_linkedin() -> None:
    helper = _function(
        "is_managed_application_provider",
        "revoke all on function public.is_managed_application_provider",
    )
    for provider in (
        "google_forms",
        "greenhouse",
        "lever",
        "ashby",
        "yc",
        "wellfound",
        "cutshort",
        "instahyre",
    ):
        assert f"'{provider}'" in helper
    assert "'linkedin'" not in helper
    assert "'ziprecruiter'" not in helper
    assert "pg_get_functiondef(routine)" in SQL
    assert SQL.count("'public.") >= 8
    assert "not public.is_managed_application_provider(provider_input)" in SQL


def test_discovery_ingestion_is_atomic_bounded_and_preserves_user_state() -> None:
    ingestion = _function(
        "ingest_discovered_jobs_for_user",
        "revoke all on function public.ingest_discovered_jobs_for_user",
    )
    assert "jsonb_array_length(jobs_input) > 200" in ingestion
    assert "source_input = 'ziprecruiter'" in ingestion
    assert "message = 'discovered_job_url_invalid'" in ingestion
    assert "pg_advisory_xact_lock" in ingestion
    assert "on conflict (user_id, normalized_url)" in ingestion
    update_clause = ingestion.split("do update set", 1)[1].split("returning *", 1)[0]
    assert "last_discovered_at = timestamp_now" in update_clause
    assert "status =" not in update_clause
    assert "title =" not in update_clause
    assert "company =" not in update_clause
    assert "'items', result_items" in ingestion
    assert "'inserted', inserted_count" in ingestion
    assert "'updated', updated_count" in ingestion
    assert "grant execute on function public.ingest_discovered_jobs(jsonb) to authenticated" in SQL

    service_ingestion = SQL.split(
        "create or replace function public.ingest_discovered_jobs(\n"
        "    job_id uuid,", 1
    )[1].split(
        "revoke all on function public.ingest_discovered_jobs(jsonb)", 1
    )[0]
    assert "automation.kind in ('discover_public_feeds', 'discover_linkedin_guest')" in service_ingestion
    assert "automation.locked_by = p_worker_id" in service_ingestion
    assert "automation.lease_expires_at >= clock_timestamp()" in service_ingestion
    assert "queue_job.user_id, p_jobs" in service_ingestion
    assert (
        "revoke all on function public.ingest_discovered_jobs(uuid, text, jsonb)\n"
        "    from public, anon, authenticated;"
    ) in SQL
    assert (
        "grant execute on function public.ingest_discovered_jobs(uuid, text, jsonb)\n"
        "    to service_role;"
    ) in SQL


def test_worker_rpcs_are_lease_bound_service_only_and_return_owned_bundle() -> None:
    scan = _function(
        "store_application_form_scan",
        "create or replace function public.update_application_job_progress",
    )
    assert "job_id uuid" in scan
    assert "worker_id text" in scan
    assert "automation.kind = 'application_scan'" in scan
    assert "automation.locked_by = p_worker_id" in scan
    assert "automation.lease_expires_at >= clock_timestamp()" in scan
    assert "set status = 'superseded'" in scan
    assert "select coalesce(max(revision.revision), 0) + 1" in scan

    bundle = _function(
        "get_application_job_bundle",
        "-- commit the exact approved revision",
    )
    for field in (
        "'user_id'",
        "'application_id'",
        "'provider'",
        "'target_url'",
        "'browser_context_id_ciphertext'",
        "'form_revision'",
        "'approved_answers'",
        "'storage_path'",
        "'original_name'",
        "'size_bytes'",
    ):
        assert field in bundle
    assert "revision.user_id = queue_job.user_id" in bundle
    assert "queue_job.kind in ('application_prefill', 'application_submit')" in bundle
    assert "target_revision.status = 'approved'" in bundle
    assert "newer.revision > target_revision.revision" in bundle
    assert "resume.user_id = queue_job.user_id" in bundle
    assert "secret.user_id = queue_job.user_id" in bundle

    for signature in (
        "public.store_application_form_scan(\n    uuid, text, text, text, text, jsonb, jsonb\n)",
        "public.update_application_job_progress(uuid, text, jsonb)",
        "public.get_application_job_bundle(uuid, text)",
        "public.record_application_form_submission(uuid, text, text, jsonb)",
    ):
        assert f"revoke all on function {signature}" in SQL
        assert f"grant execute on function {signature}" in SQL
    assert "from public, anon, authenticated" in SQL
    assert "to service_role" in SQL


def test_new_auth_users_receive_discovery_defaults() -> None:
    provisioning = _function(
        "handle_new_auth_user", "revoke all on function public.handle_new_auth_user"
    )
    assert "insert into public.discovery_preferences (user_id)" in provisioning
    assert "values (new.id)" in provisioning
    assert "on conflict (user_id) do nothing" in provisioning
