from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/202608140002_form_submission_resolution.sql"
).read_text(encoding="utf-8")


def test_resolution_rpc_is_authenticated_and_tenant_scoped() -> None:
    assert "security definer" in SQL
    assert "current_user_id uuid := public.assert_active_user()" in SQL
    assert "revision.user_id = current_user_id" in SQL
    assert "from public, anon, authenticated" in SQL
    assert ") to authenticated;" in SQL
    assert "anonymous users must not resolve form submissions" in SQL


def test_resolution_requires_current_uncertain_snapshot_and_no_active_work() -> None:
    assert "pg_advisory_xact_lock" in SQL
    assert "target_revision.status not in ('approved', 'needs_attention')" in SQL
    assert "target_revision.submitted_at is not null" in SQL
    assert "newer.revision > target_revision.revision" in SQL
    assert "active.status in ('queued', 'running')" in SQL
    assert "attempt.kind = 'application_submit'" in SQL
    assert "durable_state = 'uncertain'" in SQL
    assert "latest_state = 'uncertain'" in SQL
    assert "durable_state = 'confirmed'" in SQL
    assert "confirmed_attempt.result ->> 'submission_state' = 'confirmed'" in SQL


def test_unresolved_uncertainty_cannot_be_superseded_by_a_scan_or_refresh() -> None:
    guard = SQL.split(
        "create or replace function public.guard_application_form_revision()", 1
    )[1].split("create or replace function public.resolve_application_form_submission", 1)[0]
    assert "new.status = 'superseded'" in guard
    assert "old.submission_result ->> 'submission_state' = 'uncertain'" in guard
    assert "old.submission_result ->> 'submission_state' = 'confirmed'" in guard
    assert "attempt.kind = 'application_submit'" in guard
    assert "attempt.result ->> 'submission_state' = 'uncertain'" in guard
    assert "attempt.result ->> 'submission_state' = 'confirmed'" in guard
    assert "new.submission_result ->> 'resolution_outcome'" in guard
    assert "form_submission_resolution_required" in guard
    confirmed_guard = guard.split(
        "-- A provider-confirmed submission is final.", 1
    )[1].split("-- An uncertain click normally requires", 1)[0]
    assert "application_already_submitted" in confirmed_guard
    assert "resolution_outcome" not in confirmed_guard


def test_legacy_approved_uncertain_revision_is_backfilled() -> None:
    assert "with legacy_uncertain as (" in SQL
    legacy = SQL.split("with legacy_uncertain as (", 1)[1].split(
        "create or replace function public.resolve_application_form_submission", 1
    )[0]
    assert "revision.status = 'approved'" in legacy
    assert "attempt.status = 'needs_attention'" in legacy
    assert "attempt.result ->> 'phase' = 'submit'" in legacy
    assert "attempt.result ->> 'submission_state' = 'uncertain'" in legacy
    assert "set status = 'needs_attention'" in legacy
    assert "'application.form.submit_attention_backfilled'" in legacy
    assert "legacy_form_submission_uncertainty_requires_manual_repair" in SQL


def test_not_attempted_submit_is_snapshotted_and_legacy_rows_are_backfilled() -> None:
    completion = SQL.split(
        "create or replace function public.complete_application_form_submit_attention", 1
    )[1].split("create or replace function public.record_application_form_submission", 1)[0]
    assert "state not in ('not_attempted', 'uncertain', 'confirmed')" in completion
    assert "set status = 'needs_attention'" in completion
    assert "submission_result = durable_result" in completion
    assert "incoming_rank >= existing_rank" in completion
    assert "to service_role" in completion

    legacy = SQL.split("with legacy_not_attempted as (", 1)[1].split(
        "create or replace function public.resolve_application_form_submission", 1
    )[0]
    assert "attempt.result ->> 'submission_state' = 'not_attempted'" in legacy
    assert "revision.status = 'approved'" in legacy
    assert "set status = 'needs_attention'" in legacy
    assert "'submission_state', 'not_attempted'" in legacy


def test_confirmed_submit_attention_atomically_finalizes_revision() -> None:
    completion = SQL.split(
        "create or replace function public.complete_application_form_submit_attention", 1
    )[1].split("create or replace function public.record_application_form_submission", 1)[0]
    confirmed = completion.split("if state = 'confirmed' then", 2)[-1].split(
        "elsif target_revision.status = 'approved'", 1
    )[0]
    assert "set status = 'applied', last_error = null" in confirmed
    assert "application.status in (" in confirmed
    assert "job.status in ('saved', 'drafting', 'ready')" in confirmed
    assert "set status = 'submitted'" in confirmed
    assert "submitted_at = coalesce(revision.submitted_at, timestamp_now)" in confirmed
    assert "submission_result = durable_result" in confirmed
    assert "'verification_source', 'provider'" in completion
    assert "'resolution_outcome', 'submitted'" in completion
    # Queue-level attention remains visible even though provider truth closes
    # the immutable revision and application state.
    assert "set status = 'needs_attention', result = p_result" in completion


def test_provider_confirmation_wins_and_is_recovered() -> None:
    confirmed = SQL.split("with confirmed_candidates as (", 1)[1].split(
        "-- A recovered confirmation also seals", 1
    )[0]
    assert "revision.status in ('approved', 'needs_attention', 'superseded')" in confirmed
    assert "newer.revision > revision.revision" not in confirmed
    assert "attempt.result ->> 'submission_state' = 'confirmed'" in SQL
    assert "'application.form.provider_confirmation_recovered'" in SQL
    assert "confirmed_submit.id is not null" in SQL
    assert "'verification_source', 'provider'" in SQL
    assert "revision.status in ('scanned', 'prefilled', 'approved', 'needs_attention')" in SQL


def test_confirmation_is_enforced_application_wide() -> None:
    attempt_guard = SQL.split(
        "create or replace function public.guard_application_form_automation_attempt", 1
    )[1].split("create or replace function public.complete_application_form_submit_attention", 1)[0]
    assert "'application_scan', 'application_prefill', 'application_submit'" in attempt_guard
    assert "'application-form:' || new.application_id::text" in attempt_guard
    assert "confirmed_revision.application_id = new.application_id" in attempt_guard
    assert "confirmed_attempt.application_id = new.application_id" in attempt_guard
    assert "message = 'application_already_submitted'" in attempt_guard

    resolver = SQL.split(
        "create or replace function public.resolve_application_form_submission", 1
    )[1]
    assert "confirmed_revision.id <> target_revision.id" in resolver
    assert "confirmed_attempt.form_revision_id is distinct from target_revision.id" in resolver
    assert "message = 'application_already_submitted'" in resolver


def test_one_submit_attempt_is_enforced_per_immutable_revision() -> None:
    assert "guard_application_form_automation_attempt" in SQL
    assert "'application-form-submit:' || new.form_revision_id::text" in SQL
    assert "attempt.form_revision_id = new.form_revision_id" in SQL
    assert "attempt.kind = 'application_submit'" in SQL
    assert "message = 'form_submit_attempt_exists'" in SQL
    assert "new.kind = 'application_scan'" in SQL


def test_user_verified_submission_is_durable_without_rewriting_worker_job() -> None:
    submitted_branch = SQL.split("if outcome_input = 'submitted' then", 1)[1].split(
        "select coalesce(max(revision.revision)", 1
    )[0]
    assert "set status = 'submitted'" in submitted_branch
    assert "submitted_at = resolved_at" in submitted_branch
    assert "'submission_state', 'confirmed'" in submitted_branch
    assert "'verification_source', 'user'" in submitted_branch
    assert "set status = 'applied'" in submitted_branch
    assert "update public.automation_jobs" not in submitted_branch


def test_verified_non_submission_creates_new_unapproved_revision() -> None:
    retry_branch = SQL.split("select coalesce(max(revision.revision)", 1)[1]
    assert "set status = 'superseded'" in retry_branch
    assert "submission_result = non_submission_result" in retry_branch
    assert "'submission_state', 'uncertain'" in retry_branch
    assert "'resolution_outcome', 'not_submitted'" in retry_branch
    assert "target_revision.submission_result ->> 'code'" in retry_branch
    assert "latest_submit.result ->> 'message'" in retry_branch
    assert "'verification_source', 'user'" in retry_branch
    assert "target_revision.question_schema" in retry_branch
    assert "target_revision.answers" in retry_branch
    assert "'prefilled'" in retry_branch
    insert_columns = retry_branch.split(
        "insert into public.application_form_revisions (", 1
    )[1].split(") values", 1)[0]
    assert "approved_revision" not in insert_columns
    assert "submitted_at" not in insert_columns
    assert "submission_result" not in insert_columns
    assert "retry_revision_id" in retry_branch


def test_resolution_retries_are_idempotent_and_conflicts_fail_closed() -> None:
    assert "target_revision.status = 'submitted' and durable_state = 'confirmed'" in SQL
    assert "target_revision.status = 'superseded'" in SQL
    assert "resolution_outcome = 'not_submitted'" in SQL
    assert "target_revision.submission_result ->> 'retry_revision_id'" in SQL
    assert "form_submission_resolution_conflict" in SQL


def test_marking_submitted_does_not_downgrade_later_pipeline_states() -> None:
    assert "application.status in (" in SQL
    assert "'draft_pending', 'drafted', 'approved', 'queued', 'manual', 'failed'" in SQL
    assert "job.status in ('saved', 'drafting', 'ready')" in SQL


def test_provider_submission_rpc_preserves_checks_and_updates_monotonically() -> None:
    record = SQL.split(
        "create or replace function public.record_application_form_submission", 1
    )[1].split("-- Recover any provider-confirmed legacy state first", 1)[0]
    assert "automation.kind = 'application_submit'" in record
    assert "automation.status = 'running'" in record
    assert "automation.locked_by = p_worker_id" in record
    assert "automation.lease_expires_at >= timestamp_now" in record
    assert "automation.cancel_requested_at is null" in record
    assert "revision.status = 'approved'" in record
    assert "revision.approved_revision = revision.revision" in record
    assert "newer.revision > revision.revision" in record
    assert "p_result ->> 'submission_state' is distinct from 'confirmed'" in record
    assert "coalesce(target_revision.submission_result, '{}'::jsonb)" in record
    assert "application.status in (" in record
    assert "job.status in ('saved', 'drafting', 'ready')" in record
    assert "'verification_source', 'provider'" in record


def test_resolution_retry_has_bounded_revision_capacity() -> None:
    resolver = SQL.split(
        "create or replace function public.resolve_application_form_submission", 1
    )[1]
    assert "select coalesce(max(revision.revision), 0) + 1 into next_revision" in resolver
    assert "if next_revision >= 50 then" in resolver
    assert "Reserve the final revision slot" in resolver
    assert "message = 'form_revision_limit_reached'" in resolver
