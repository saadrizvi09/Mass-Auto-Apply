from pathlib import Path


SQL = (Path(__file__).parents[1] / "supabase/migrations/202609060001_public_contact_discovery.sql").read_text()


def test_public_contact_migration_has_tenant_bound_queue_and_evidence_table() -> None:
    assert "create table public.job_contacts" in SQL
    assert "unique (user_id, company_key, normalized_email)" in SQL
    assert "create or replace function public.enqueue_public_contact_discovery" in SQL
    assert "create or replace function public.get_public_contact_discovery_bundle" in SQL
    assert "create or replace function public.store_public_job_contacts" in SQL
    assert "grant execute on function public.enqueue_public_contact_discovery" in SQL
    assert "grant execute on function public.get_public_contact_discovery_bundle" in SQL
    assert "grant execute on function public.store_public_job_contacts" in SQL
    assert "mailbox probe" in SQL.casefold()
    assert "linkedin member scrape" in SQL.casefold()
    assert "telegram member scrape" in SQL.casefold()
