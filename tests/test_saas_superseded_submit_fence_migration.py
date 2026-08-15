from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/202608150001_cancel_superseded_form_submits.sql"
).read_text(encoding="utf-8")


def test_superseding_revision_cancels_only_active_bound_submit_jobs() -> None:
    trigger = SQL.split(
        "create or replace function public.cancel_superseded_form_submit_jobs()", 1
    )[1].split("-- Repair any active stale work", 1)[0]

    assert "new.status = 'superseded'" in trigger
    assert "old.status <> 'superseded'" in trigger
    assert "job.user_id = new.user_id" in trigger
    assert "job.application_id = new.application_id" in trigger
    assert "job.form_revision_id = new.id" in trigger
    assert "job.kind = 'application_submit'" in trigger
    assert "job.status in ('queued', 'running')" in trigger
    assert "when job.status = 'queued' then 'cancelled'" in trigger
    assert "cancel_requested_at = coalesce" in trigger
    assert "error_code = 'form_revision_superseded'" in trigger
    assert "after update of status on public.application_form_revisions" in trigger


def test_migration_backfills_active_invalid_submit_jobs_without_rewriting_history() -> None:
    backfill = SQL.split("-- Repair any active stale work", 1)[1].split(
        "-- Claim one due job atomically", 1
    )[0]

    assert "job.status in ('queued', 'running')" in backfill
    assert "job.kind = 'application_submit'" in backfill
    assert "when job.status = 'queued' then 'cancelled'" in backfill
    assert "or job.cancel_requested_at is null" in backfill
    assert "revision.status = 'approved'" in backfill
    assert "newer.revision > revision.revision" in backfill
    assert "job.status = 'running'" in backfill
    assert "revision.status = 'submitted'" in backfill
    assert "->> 'submission_state' = 'confirmed'" in backfill
    assert "'succeeded'" not in backfill
    assert "'failed'" not in backfill
    assert "'needs_attention'" not in backfill


def test_claim_rpc_cancels_and_excludes_stale_queued_submit_jobs() -> None:
    claim = SQL.split(
        "create or replace function public.claim_automation_job(", 1
    )[1]
    queued_cleanup = claim.split(
        "-- Cancel any queued submit", 1
    )[1].split("-- A supersession that races", 1)[0]
    selection = claim.split("select job.id into claimed_id", 1)[1].split(
        "if claimed_id is null", 1
    )[0]

    assert "set status = 'cancelled'" in queued_cleanup
    assert "job.status = 'queued'" in queued_cleanup
    assert "job.kind = 'application_submit'" in queued_cleanup
    assert "not exists (" in queued_cleanup
    assert "revision.status = 'approved'" in queued_cleanup
    assert "newer.revision > revision.revision" in queued_cleanup

    assert "job.kind <> 'application_submit'" in selection
    assert "revision.id = job.form_revision_id" in selection
    assert "revision.status = 'approved'" in selection
    assert "revision.approved_revision = revision.revision" in selection
    assert "revision.approved_schema_hash = revision.schema_hash" in selection
    assert "newer.revision > revision.revision" in selection


def test_claim_rpc_cooperatively_cancels_stale_running_submit_jobs() -> None:
    claim = SQL.split(
        "create or replace function public.claim_automation_job(", 1
    )[1]
    running_cleanup = claim.split(
        "-- A supersession that races", 1
    )[1].split("update public.automation_jobs job", 2)[1]

    assert "job.status = 'running'" in running_cleanup
    assert "job.cancel_requested_at is null" in running_cleanup
    assert "set cancel_requested_at = timestamp_now" in running_cleanup
    assert "revision.status = 'approved'" in running_cleanup
    assert "revision.status = 'submitted'" in running_cleanup
    assert "->> 'submission_state' = 'confirmed'" in running_cleanup


def test_claim_rpc_remains_service_role_only() -> None:
    assert (
        "revoke all on function public.claim_automation_job(text, integer, text[])\n"
        "    from public, anon, authenticated;"
    ) in SQL
    assert (
        "grant execute on function public.claim_automation_job(text, integer, text[])\n"
        "    to service_role;"
    ) in SQL
