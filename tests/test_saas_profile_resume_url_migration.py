from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/202608130003_profile_public_resume_url.sql"
)
SQL = MIGRATION.read_text(encoding="utf-8").lower()


def test_profile_public_resume_url_is_https_and_browser_updateable() -> None:
    assert "add column if not exists resume_url text" in SQL
    assert "resume_url ~ '^https://[^[:space:]]+$'" in SQL
    assert "grant update (resume_url) on public.profiles to authenticated" in SQL
    assert "never a private storage path" in SQL
