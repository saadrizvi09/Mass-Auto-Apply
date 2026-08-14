from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608130004_company_form_provider.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()


def test_company_form_binding_is_service_owned_and_ignores_job_metadata() -> None:
    assert "create table public.company_form_targets" in SQL
    assert "job_id uuid primary key references public.jobs(id) on delete cascade" in SQL
    assert "job.apply_url = new.source_url" in SQL
    assert "alter table public.company_form_targets enable row level security" in SQL
    assert "revoke all on public.company_form_targets from public, anon, authenticated" in SQL
    assert "grant all on public.company_form_targets to service_role" in SQL
    assert "create policy" not in SQL
    assert "job.metadata" not in SQL


def test_company_form_target_validation_rejects_private_address_shapes() -> None:
    helper = SQL.split(
        "create or replace function public.validated_company_form_host", 1
    )[1].split("revoke all on function", 1)[0]

    assert "^https://" in helper
    assert "strpos(value, '@') = 0" in helper
    assert "strpos(value, ':') = 0" in helper
    assert "localhost|local|internal|test|invalid|example|home|lan|arpa|onion" in helper
    assert "nip\\.io|sslip\\.io|localtest\\.me|lvh\\.me" in helper
    assert "^[0-9]+(\\.[0-9]+){3}$" in helper


def test_company_form_is_automatable_but_not_a_login_or_discovery_provider() -> None:
    hosted_helper = SQL.split(
        "create or replace function public.is_hosted_form_automation_provider", 1
    )[1].split("revoke all on function", 1)[0]
    assert "'company_form'" in hosted_helper
    assert "'yc'" not in hosted_helper

    enqueue_extension = SQL.split("do $extend_company_form_enqueue_provider$", 1)[1]
    enqueue_extension = enqueue_extension.split(
        "$extend_company_form_enqueue_provider$;", 1
    )[0]
    assert "message = ''automation_provider_invalid''" in enqueue_extension
    assert "'company_form'" in enqueue_extension
    assert "discover_public_feeds" not in enqueue_extension
    assert "provider_discovery_unavailable" not in enqueue_extension

    assert "public.is_managed_application_provider('company_form')" in SQL
    assert "company_form must not enter saved-login lifecycle" in SQL


def test_scan_can_change_path_but_not_the_explicit_host() -> None:
    revision_guard = SQL.split(
        "create or replace function public.guard_company_form_revision_target", 1
    )[1].split("revoke all on function", 1)[0]

    assert "job.apply_url = binding.source_url" in revision_guard
    assert "public.validated_company_form_host(new.form_url)" in revision_guard
    assert "bound_target.exact_host" in revision_guard
    assert "new.form_url = bound_target.target_url" not in revision_guard


def test_queue_binds_scan_target_and_exact_approved_revision() -> None:
    queue_guard = SQL.split(
        "create or replace function public.guard_company_form_automation_job", 1
    )[1].split("revoke all on function", 1)[0]

    for kind in ("application_scan", "application_prefill", "application_submit"):
        assert f"'{kind}'" in queue_guard
    assert "new.payload -> 'company_form_host'" in queue_guard
    assert "new.payload -> 'company_form_target_url'" in queue_guard
    assert "payload_target_url is distinct from bound_target.target_url" in queue_guard
    assert "revision.form_url = payload_target_url" in queue_guard
    assert "revision.status = 'approved'" in queue_guard
    assert "revision.approved_revision = revision.revision" in queue_guard
    assert "revision.approved_schema_hash = revision.schema_hash" in queue_guard
    assert "newer.revision > revision.revision" in queue_guard


def test_scan_storage_uses_the_narrow_hosted_provider_gate() -> None:
    extension = SQL.split("do $extend_company_form_scan_storage$", 1)[1]
    extension = extension.split("$extend_company_form_scan_storage$;", 1)[0]

    assert "not public.is_managed_application_provider(p_provider)" in extension
    assert "not public.is_hosted_form_automation_provider(p_provider)" in extension
    assert "gate_occurrences <> 1" in extension
