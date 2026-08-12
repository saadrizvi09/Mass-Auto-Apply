from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "202608110004_fix_owned_reference_trigger.sql"
).read_text()


def test_owned_reference_trigger_branches_by_table_before_field_access() -> None:
    assert "if tg_table_name = 'applications' then" in SQL
    assert "elsif tg_table_name = 'automation_jobs' then" in SQL
    assert "elsif tg_table_name = 'connection_secrets' then" in SQL
    assert "elsif tg_table_name = 'send_events' then" in SQL

    # The unsafe shape in the original migration combined table dispatch and a
    # field that does not exist on every NEW record in one Boolean predicate.
    assert "tg_table_name = 'applications' and new.job_id" not in SQL
    assert "tg_table_name = 'automation_jobs' and new.application_id" not in SQL


def test_owned_reference_trigger_fails_closed_if_attached_elsewhere() -> None:
    assert "owned_reference_trigger_misconfigured" in SQL
    assert "return new;" in SQL
