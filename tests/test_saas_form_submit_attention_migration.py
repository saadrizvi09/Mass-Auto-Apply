from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase/migrations/202608140001_form_submit_attention_snapshot.sql"
).read_text(encoding="utf-8")


def test_uncertain_or_confirmed_submit_is_fenced_in_the_form_revision() -> None:
    assert "complete_application_form_submit_attention" in SQL
    assert "p_result ->> 'outcome' is distinct from 'needs_attention'" in SQL
    assert "p_result ->> 'phase' is distinct from 'submit'" in SQL
    assert "state is null" in SQL
    assert "state not in ('uncertain', 'confirmed')" in SQL
    assert "set status = 'needs_attention'" in SQL
    assert "submission_result = durable_result" in SQL
    assert "status = 'needs_attention', result = p_result" in SQL
    assert "target_revision.submission_result ->> 'submission_state'" in SQL
    assert "in ('uncertain', 'confirmed')" in SQL


def test_submit_attention_completion_is_service_role_only() -> None:
    signature = "public.complete_application_form_submit_attention(uuid,text,jsonb)"
    assert f"'{signature}'" in SQL
    assert ") from public, anon, authenticated;" in SQL
    assert ") to service_role;" in SQL


def test_submit_attention_preserves_a_prior_confirmed_submission() -> None:
    assert "revision.status in ('approved', 'submitted', 'needs_attention')" in SQL
    assert "if target_revision.status = 'approved'" in SQL
    assert "Preserve that stronger submitted state" in SQL
