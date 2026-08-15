from pathlib import Path

from pglast import parse_sql


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/202608150004_profile_form_answer_count_lint.sql"
).read_text(encoding="utf-8")


def test_profile_form_answer_count_forward_migration_is_valid_sql() -> None:
    assert parse_sql(SQL)
    assert "jsonb_object_length" not in SQL
    assert "select count(*) from jsonb_object_keys(answers_input)" in SQL


def test_profile_form_answer_count_repair_preserves_submission_fences() -> None:
    assert "submission_result ->> 'submission_state'" in SQL
    assert "in ('uncertain', 'confirmed')" in SQL
    assert "operation.status in ('queued', 'running')" in SQL
    assert "attempt.result ->> 'submission_state' = 'not_attempted'" in SQL
    assert "next_revision > 50" in SQL


def test_profile_form_answer_count_repair_remains_service_role_only() -> None:
    signature = (
        "public.refresh_application_form_profile_answers_for_user("
        "uuid,uuid,bigint,text,jsonb)"
    )
    assert f"'{signature}'" in SQL
    assert ") from public, anon, authenticated;" in SQL
    assert ") to service_role;" in SQL
