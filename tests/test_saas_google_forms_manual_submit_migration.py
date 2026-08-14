from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608130001_google_forms_manual_submit.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()
APPROVED_SUBMIT_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608130002_google_forms_approved_submit.sql"
)
APPROVED_SUBMIT_SQL = APPROVED_SUBMIT_MIGRATION.read_text(encoding="utf-8").lower()


def test_database_rejects_direct_google_forms_submit_enqueue() -> None:
    assert "new.kind = 'application_submit'" in SQL
    assert "new.provider = 'google_forms'" in SQL
    assert "raise exception 'provider_automation_unavailable'" in SQL
    assert "before insert or update of kind, provider" in SQL
    assert "on public.automation_jobs" in SQL


def test_google_forms_submit_guard_is_not_browser_callable() -> None:
    assert (
        "revoke all on function public.guard_google_forms_manual_submit()\n"
        "    from public, anon, authenticated;"
    ) in SQL
    assert (
        "grant execute on function public.guard_google_forms_manual_submit() "
        "to service_role;"
    ) in SQL


def test_forward_migration_replaces_manual_submit_rejection_with_approval_gate() -> None:
    assert "drop trigger if exists automation_jobs_google_forms_manual_submit" in APPROVED_SUBMIT_SQL
    assert "drop function if exists public.guard_google_forms_manual_submit()" in APPROVED_SUBMIT_SQL
    assert "create or replace function public.guard_google_forms_approved_submit()" in APPROVED_SUBMIT_SQL
    assert "revision.user_id = new.user_id" in APPROVED_SUBMIT_SQL
    assert "revision.application_id = new.application_id" in APPROVED_SUBMIT_SQL
    assert "revision.provider = 'google_forms'" in APPROVED_SUBMIT_SQL
    assert "revision.status = 'approved'" in APPROVED_SUBMIT_SQL
    assert "revision.approved_revision = revision.revision" in APPROVED_SUBMIT_SQL
    assert "revision.approved_schema_hash = revision.schema_hash" in APPROVED_SUBMIT_SQL
    assert "newer.revision > revision.revision" in APPROVED_SUBMIT_SQL


def test_google_forms_submit_queue_requires_worker_preflight_and_is_idempotent() -> None:
    assert "new.payload -> 'required_answer_preflight'" in APPROVED_SUBMIT_SQL
    assert "-> 'complete' <> 'true'::jsonb" in APPROVED_SUBMIT_SQL
    assert "-> 'missing_count' <> '0'::jsonb" in APPROVED_SUBMIT_SQL
    assert "jsonb_array_length(" in APPROVED_SUBMIT_SQL
    assert "message = 'form_required_answers_missing'" in APPROVED_SUBMIT_SQL
    assert "new.payload ->> 'form_revision_id' is distinct from new.form_revision_id::text" in APPROVED_SUBMIT_SQL
    assert "create unique index if not exists automation_jobs_one_submit_revision_key_idx" in APPROVED_SUBMIT_SQL
    assert "(user_id, form_revision_id, idempotency_key)" in APPROVED_SUBMIT_SQL
    assert "where kind = 'application_submit'" in APPROVED_SUBMIT_SQL
