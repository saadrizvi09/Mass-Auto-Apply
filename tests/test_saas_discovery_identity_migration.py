from __future__ import annotations

from pathlib import Path


MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
HISTORICAL_MIGRATION = MIGRATIONS / "202608110002_hosted_discovery_applications.sql"
MIGRATION = MIGRATIONS / "202608110003_discovery_job_identity.sql"
SQL = MIGRATION.read_text(encoding="utf-8").lower()


def _ingestion_function() -> str:
    return SQL.split(
        "create or replace function public.ingest_discovered_jobs_for_user(", 1
    )[1].split(
        "revoke all on function public.ingest_discovered_jobs_for_user", 1
    )[0]


def test_identity_fix_is_a_forward_only_migration() -> None:
    assert MIGRATION.exists()
    assert MIGRATION.name > HISTORICAL_MIGRATION.name
    assert SQL.startswith("-- make discovery ingestion idempotent")
    assert "begin;" in SQL
    assert SQL.rstrip().endswith("commit;")


def test_existing_external_identity_duplicates_are_safely_merged() -> None:
    assert "create temporary table discovery_job_identity_merge" in SQL
    assert "partition by job.user_id, job.source, job.external_id" in SQL
    assert "where job.external_id is not null" in SQL
    assert "where identity_rank > 1" in SQL
    assert "set job_id = merge.canonical_id" in SQL
    assert "update public.applications application" in SQL
    assert "update public.application_form_revisions revision" in SQL
    assert "disable trigger applications_owned_job" in SQL
    assert "enable trigger applications_owned_job" in SQL
    assert (
        "disable trigger application_form_revisions_guard_immutable" in SQL
    )
    assert "enable trigger application_form_revisions_guard_immutable" in SQL
    assert SQL.index("update public.applications application") < SQL.index(
        "delete from public.jobs duplicate"
    )
    assert SQL.index("update public.application_form_revisions revision") < SQL.index(
        "delete from public.jobs duplicate"
    )


def test_external_identity_has_a_tenant_scoped_partial_unique_index() -> None:
    assert "create unique index jobs_user_source_external_id_uidx" in SQL
    assert "on public.jobs (user_id, source, external_id)" in SQL
    assert "where external_id is not null" in SQL


def test_ingestion_resolves_url_and_source_external_id_identities() -> None:
    ingestion = _ingestion_function()
    assert "external_id_input := nullif(btrim(item ->> 'external_id'), '')" in ingestion
    assert "job.normalized_url = normalized_url_input" in ingestion
    assert "job.source = source_input" in ingestion
    assert "job.external_id = external_id_input" in ingestion
    assert "prior_job_id := coalesce(normalized_url_job_id, external_id_job_id)" in ingestion
    assert "message = 'discovered_job_identity_conflict'" in ingestion


def test_ingestion_upsert_is_race_safe_and_preserves_user_content() -> None:
    ingestion = _ingestion_function()
    assert "pg_advisory_xact_lock" in ingestion
    assert "on conflict do nothing" in ingestion
    assert "message = 'discovered_job_upsert_retry'" in ingestion
    assert ingestion.count("for update;") >= 4
    updates = ingestion.split("update public.jobs job", 1)[1]
    assert "last_discovered_at = timestamp_now" in updates
    assert "status =" not in updates
    assert "title =" not in updates
    assert "company =" not in updates


def test_public_and_service_rpc_boundaries_remain_in_prior_migration() -> None:
    historical_sql = HISTORICAL_MIGRATION.read_text(encoding="utf-8").lower()
    assert "create or replace function public.ingest_discovered_jobs(jobs_input jsonb)" in historical_sql
    assert "current_user_id uuid := public.assert_active_user()" in historical_sql
    assert "create or replace function public.ingest_discovered_jobs(\n    job_id uuid," in historical_sql
    assert "automation.locked_by = p_worker_id" in historical_sql
    assert "automation.lease_expires_at >= clock_timestamp()" in historical_sql
    assert "queue_job.user_id, p_jobs" in historical_sql
    assert "create or replace function public.ingest_discovered_jobs(jobs_input jsonb)" not in SQL
    assert "create or replace function public.ingest_discovered_jobs(\n    job_id uuid," not in SQL
