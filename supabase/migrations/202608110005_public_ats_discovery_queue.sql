-- Add credential-free public ATS board enumeration to the durable queue while
-- preserving the existing authenticated enqueue and lease-bound ingestion RPC
-- signatures.  The worker may enumerate only the allowlisted public endpoints;
-- tenant ownership still comes exclusively from the current queue lease.

begin;

do $extend_public_ats_enqueue$
declare
    routine regprocedure :=
        'public.enqueue_automation_job(text,text,uuid,jsonb,text)'::regprocedure;
    routine_definition text;
    extended_definition text;
    old_kind_pair constant text :=
        '''discover_public_feeds'', ''discover_linkedin_guest''';
    provider_gate constant text :=
        '    if kind_input = ''discover_linkedin_guest'' and provider_input <> ''linkedin'' then
        raise exception using errcode = ''P0001'', message = ''provider_discovery_unavailable'';
    end if;';
    extended_provider_gate constant text :=
        '    if kind_input = ''discover_linkedin_guest'' and provider_input <> ''linkedin'' then
        raise exception using errcode = ''P0001'', message = ''provider_discovery_unavailable'';
    end if;
    if kind_input = ''discover_public_ats'' and provider_input <> ''public_ats'' then
        raise exception using errcode = ''P0001'', message = ''provider_discovery_unavailable'';
    end if;';
    kind_pair_occurrences integer;
begin
    routine_definition := pg_get_functiondef(routine);
    kind_pair_occurrences := (
        length(routine_definition)
        - length(replace(routine_definition, old_kind_pair, ''))
    ) / length(old_kind_pair);
    if kind_pair_occurrences <> 2 then
        raise exception
            'expected two discovery kind gates in %, found %',
            routine,
            kind_pair_occurrences;
    end if;
    if strpos(routine_definition, provider_gate) = 0 then
        raise exception 'LinkedIn discovery provider gate missing from %', routine;
    end if;
    extended_definition := replace(
        routine_definition,
        old_kind_pair,
        old_kind_pair || ', ''discover_public_ats'''
    );
    extended_definition := replace(
        extended_definition,
        provider_gate,
        extended_provider_gate
    );
    execute extended_definition;
end;
$extend_public_ats_enqueue$;

do $extend_public_ats_ingestion_lease$
declare
    routine regprocedure :=
        'public.ingest_discovered_jobs(uuid,text,jsonb)'::regprocedure;
    routine_definition text;
    old_kind_pair constant text :=
        '''discover_public_feeds'', ''discover_linkedin_guest''';
    kind_pair_occurrences integer;
begin
    routine_definition := pg_get_functiondef(routine);
    kind_pair_occurrences := (
        length(routine_definition)
        - length(replace(routine_definition, old_kind_pair, ''))
    ) / length(old_kind_pair);
    if kind_pair_occurrences <> 1 then
        raise exception
            'expected one discovery lease gate in %, found %',
            routine,
            kind_pair_occurrences;
    end if;
    routine_definition := replace(
        routine_definition,
        old_kind_pair,
        old_kind_pair || ', ''discover_public_ats'''
    );
    execute routine_definition;
end;
$extend_public_ats_ingestion_lease$;

commit;
