from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/202608130005_profile_form_answer_sync.sql"
).read_text(encoding="utf-8")


def test_profile_form_sync_creates_a_new_unapproved_revision() -> None:
    assert "target_revision.status not in ('scanned', 'prefilled', 'approved')" in SQL
    assert "set status = 'superseded'" in SQL
    assert "next_revision, target_revision.schema_hash" in SQL
    assert "target_revision.question_schema, answers_input" in SQL
    assert "case when answers_input = '{}'::jsonb then 'scanned' else 'prefilled' end" in SQL
    assert "approved_revision" not in SQL.split("insert into public.application_form_revisions", 1)[1].split("returning", 1)[0]


def test_profile_form_sync_never_replaces_an_uncertain_submission() -> None:
    assert "attempt.result ->> 'submission_state' = 'not_attempted'" in SQL
    assert "attempt.status = 'needs_attention'" in SQL
    assert "operation.status in ('queued', 'running')" in SQL
    assert "target_revision.submitted_at is not null" in SQL


def test_profile_form_sync_is_service_role_only() -> None:
    signature = (
        "public.refresh_application_form_profile_answers_for_user("
        "uuid,uuid,bigint,text,jsonb)"
    )
    assert f"'{signature}'" in SQL
    assert ") from public, anon, authenticated;" in SQL
    assert ") to service_role;" in SQL
