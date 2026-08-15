from pathlib import Path


SQL = Path(
    "supabase/migrations/202608150003_yc_exact_job_automation.sql"
).read_text(encoding="utf-8").lower()


def _function(name: str) -> str:
    return SQL.split(f"create or replace function public.{name}", 1)[1].split(
        "$$;", 1
    )[0]


def test_yc_target_is_service_owned_and_exact_job_only() -> None:
    canonical = _function("canonical_yc_job_url")
    assert "ycombinator\\.com/companies/" in canonical
    assert "/jobs/" in canonical
    assert "workatastartup\\.com/jobs/" not in canonical
    assert "account.ycombinator.com" not in canonical
    assert "query" not in canonical
    assert "create table public.yc_application_targets" in SQL
    assert "application_url text" in SQL
    assert "application_bound_at timestamptz" in SQL
    assert "application_url = public.canonical_yc_application_url(" in SQL
    assert "job.apply_url = new.target_url" in SQL
    assert "revoke all on public.yc_application_targets from public, anon, authenticated" in SQL
    assert "grant all on public.yc_application_targets to service_role" in SQL


def test_yc_owned_hosts_are_reserved_from_generic_company_form_automation() -> None:
    validator = _function("validated_company_form_host")
    assert "ycombinator\\.com|workatastartup\\.com" in validator
    cleanup = SQL.split("update public.automation_jobs job", 1)[1].split(
        "create or replace function public.canonical_yc_job_url", 1
    )[0]
    assert "job.provider = 'company_form'" in cleanup
    assert "job.status in ('queued', 'running')" in cleanup
    assert "company_form_target_invalid" in cleanup
    assert "delete from public.company_form_targets" in cleanup
    assert "yc-owned hosts must never enter generic company-form automation" in SQL


def test_yc_preferences_are_tenant_scoped_matching_data_not_discovery_authority() -> None:
    table = SQL.split("create table public.provider_application_preferences", 1)[
        1
    ].split(");", 1)[0]
    assert "provider text not null check (provider = 'yc')" in table
    assert "query text" in table
    assert "remote_only boolean" in table
    assert "result_limit integer" in table
    assert "primary key (user_id, provider)" in table
    assert "provider_application_preferences_select_own" in SQL
    assert "auth.uid() = user_id" in SQL
    assert "grant select, insert, update, delete" in SQL
    assert "never authorize or enqueue provider discovery" in SQL


def test_yc_enters_hosted_revision_pipeline_but_not_discovery() -> None:
    hosted = _function("is_hosted_form_automation_provider")
    guard = _function("guard_yc_automation_job")
    assert "'yc'" in hosted
    assert "application_scan" in guard
    assert "application_prefill" in guard
    assert "application_submit" in guard
    assert "yc_job_target_url" in guard
    assert "provider_discovery_unavailable" in guard
    assert "discover_public_feeds" not in guard
    assert "discover_linkedin_guest" not in guard
    assert "automation_jobs_yc_exact_target" in SQL


def test_yc_revision_accepts_only_the_exact_controlled_application_identity() -> None:
    canonical = _function("canonical_yc_application_url")
    predicate = _function("is_yc_application_form_url")
    assert "canonical_yc_job_url(form_url_input) = target_url_input" in canonical
    assert "workatastartup\\.com/application/?\\?signup_job_id=" in canonical
    assert "[1-9][0-9]{0,18}" in canonical
    assert "account\\.ycombinator\\.com" not in canonical
    assert "canonical_yc_application_url(" in predicate
    assert "= form_url_input" in predicate


def test_worker_bundle_restarts_yc_at_the_bound_exact_job_url() -> None:
    bundle = _function("get_application_job_bundle")
    assert "target_yc_binding public.yc_application_targets%rowtype" in bundle
    assert "binding.target_url = queue_job.payload ->> 'yc_job_target_url'" in bundle
    assert "target_job.apply_url = binding.target_url" in bundle
    assert "when queue_job.provider = 'yc' then target_yc_binding.target_url" in bundle
    assert "target_revision.form_url is distinct from target_yc_binding.application_url" in bundle
    assert "yc_application_identity_required" in bundle
    assert "yc_application_identity_changed" in bundle
    assert "pg_get_functiondef" not in SQL
    assert "execute replace(routine_definition" not in SQL


def test_yc_prefill_and_submit_require_latest_exact_approval() -> None:
    guard = _function("guard_yc_automation_job")
    assert "revision.status = 'approved'" in guard
    assert "revision.approved_revision = revision.revision" in guard
    assert "revision.approved_schema_hash = revision.schema_hash" in guard
    assert "revision.approved_at is not null" in guard
    assert "newer.revision > revision.revision" in guard
    assert "bound_target.application_url is null" in guard
    assert "revision.form_url = bound_target.application_url" in guard
    assert "canonical_yc_application_url(" in guard
    assert "form_approval_required" in guard


def test_yc_revision_must_belong_to_bound_user_application_and_job() -> None:
    guard = _function("guard_yc_form_revision_target")
    assert "application.id = new.application_id" in guard
    assert "application.user_id = binding.user_id" in guard
    assert "application.job_id = binding.job_id" in guard
    assert "binding.job_id = new.job_id" in guard
    assert "binding.user_id = new.user_id" in guard
    assert "for update of binding" in guard
    assert "canonical_yc_application_url(" in guard
    assert "bound_target.application_url is distinct from resolved_application_url" in guard


def test_first_yc_scan_atomically_binds_one_application_identity() -> None:
    revision_guard = _function("guard_yc_form_revision_target")
    binder = _function("bind_yc_application_identity_from_revision")
    assert "for update of binding" in revision_guard
    assert "application_form_revisions_yc_identity_bind" in SQL
    assert "after insert on public.application_form_revisions" in SQL
    assert "binding.application_url is null" in binder
    assert "set application_url = new.form_url" in binder
    assert "durable_application_url is distinct from new.form_url" in binder
    assert "yc_application_identity_changed" in binder


def test_yc_application_identity_is_immutable_until_exact_target_rebind() -> None:
    guard = _function("guard_yc_application_target")
    assert "yc_application_identity_untrusted" in guard
    assert "new.target_url is distinct from old.target_url" in guard
    assert "new.application_url := null" in guard
    assert "new.application_bound_at := null" in guard
    assert "old.application_url is not null" in guard
    assert "revision.form_url = new.application_url" in guard
    assert "revision.created_at >= old.updated_at" in guard
    assert "yc_application_identity_immutable" in guard


def test_target_to_application_mismatch_is_rejected_at_every_later_gate() -> None:
    revision_guard = _function("guard_yc_form_revision_target")
    queue_guard = _function("guard_yc_automation_job")
    bundle = _function("get_application_job_bundle")
    assert "bound_target.application_url is distinct from resolved_application_url" in revision_guard
    assert "revision.form_url = bound_target.application_url" in queue_guard
    assert "target_revision.form_url is distinct from target_yc_binding.application_url" in bundle
    assert "yc_application_identity_changed" in revision_guard
    assert "yc_application_identity_changed" in bundle


def test_old_unbound_yc_work_is_cancelled_or_cooperatively_stopped() -> None:
    repair = SQL.split(
        "-- nothing queued while yc storage was disabled", 1
    )[1].split("update public.automation_jobs job", 1)[1].split(
        "do $assert_yc_exact_job_boundaries$", 1
    )[0]
    assert "job.provider = 'yc'" in repair
    assert "job.status in ('queued', 'running')" in repair
    assert "when job.status = 'queued' then 'cancelled'" in repair
    assert "cancel_requested_at" in repair
