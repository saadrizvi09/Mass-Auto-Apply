from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608110006_gate_incomplete_form_automation.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()


def test_form_automation_gate_is_distinct_from_browser_login_registry() -> None:
    helper = SQL.split(
        "create or replace function public.is_hosted_form_automation_provider", 1
    )[1].split("revoke all on function", 1)[0]

    for provider in ("google_forms", "greenhouse", "lever", "ashby", "wellfound"):
        assert f"'{provider}'" in helper
    for provider in ("yc", "cutshort", "instahyre", "linkedin", "ziprecruiter"):
        assert f"'{provider}'" not in helper


def test_authenticated_enqueue_is_rewritten_to_the_narrow_form_gate() -> None:
    assert "pg_get_functiondef(routine)" in SQL
    assert "gate_occurrences <> 1" in SQL
    assert "not public.is_managed_application_provider(provider_input)" in SQL
    assert "not public.is_hosted_form_automation_provider(provider_input)" in SQL
    assert (
        "revoke all on function public.is_hosted_form_automation_provider(text)"
        in SQL
    )
    assert (
        "grant execute on function public.is_hosted_form_automation_provider(text) "
        "to service_role"
    ) in SQL
