from __future__ import annotations

from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202609050001_outreach_email_queue.sql"
).read_text(encoding="utf-8").lower()


def test_external_research_import_and_email_queue_are_durable_and_bounded() -> None:
    assert "alter column daily_send_cap set default 150" in SQL
    assert "check (daily_send_cap between 0 and 150)" in SQL
    assert "create or replace function public.enqueue_email_send(" in SQL
    assert "returns setof public.automation_jobs" in SQL
    assert "job.kind = 'send_email'" in SQL
    assert "daily_count >= 150" in SQL
    assert "jsonb_build_object('attach_resume', coalesce(attach_resume_input, true))" in SQL
    assert "grant execute on function public.enqueue_email_send(uuid, text, boolean) to authenticated" in SQL
    assert "revoke all on function public.enqueue_email_send(uuid, text, boolean) from public, anon" in SQL
    assert "access_token" not in SQL.split("create or replace function public.enqueue_email_send(", 1)[1]
    assert "message body" not in SQL.split("create or replace function public.enqueue_email_send(", 1)[1]
