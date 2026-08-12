from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "202608110005_public_ats_discovery_queue.sql"
).read_text()
BASE_SQL = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "202608110002_hosted_discovery_applications.sql"
).read_text()


def test_public_ats_discovery_extends_authenticated_queue_contract() -> None:
    assert "public.enqueue_automation_job(text,text,uuid,jsonb,text)" in SQL
    assert "old_kind_pair || ', ''discover_public_ats'''" in SQL
    assert "kind_pair_occurrences <> 2" in SQL
    assert "kind_input = ''discover_public_ats''" in SQL
    assert "provider_input <> ''public_ats''" in SQL
    enqueue = BASE_SQL.split(
        "create or replace function public.enqueue_automation_job(", 1
    )[1].split("$$;", 1)[0]
    assert enqueue.count("'discover_public_feeds', 'discover_linkedin_guest'") == 2
    assert (
        "if kind_input = 'discover_linkedin_guest' and provider_input <> 'linkedin' then"
        in BASE_SQL
    )


def test_public_ats_ingestion_remains_bound_to_current_worker_lease() -> None:
    assert "public.ingest_discovered_jobs(uuid,text,jsonb)" in SQL
    assert "kind_pair_occurrences <> 1" in SQL
    assert "discovery lease gate" in SQL
    assert "service_role" not in SQL
    ingestion = BASE_SQL.split(
        "create or replace function public.ingest_discovered_jobs(\n"
        "    job_id uuid,",
        1,
    )[1].split("$$;", 1)[0]
    assert ingestion.count("'discover_public_feeds', 'discover_linkedin_guest'") == 1
