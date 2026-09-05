-- Durable reviewed-email delivery for the external-AI outreach workflow.
-- Gmail is never called by the Vercel request. The persistent worker claims the
-- send_email row and finalizes the existing send ledger after Gmail responds.

alter table public.user_settings
    alter column daily_send_cap set default 150;
alter table public.user_settings
    drop constraint if exists user_settings_daily_send_cap_check;
alter table public.user_settings
    add constraint user_settings_daily_send_cap_check
    check (daily_send_cap between 0 and 150);

-- Keep the existing reservation contract, but raise the provider-account guard to
-- the app's 150/day safety ceiling. Gmail may impose a lower account-specific limit.
create or replace function public.reserve_application_send(
    application_id uuid,
    idempotency_key text
)
returns setof public.send_events
language plpgsql
security definer
set search_path = 'public', 'extensions'
as $$
declare
    p_application_id alias for $1;
    p_idempotency_key alias for $2;
    current_user_id uuid := public.assert_active_user();
    target_application public.applications%rowtype;
    tenant_settings public.user_settings%rowtype;
    gmail_connection public.connections%rowtype;
    existing_event public.send_events%rowtype;
    new_event public.send_events%rowtype;
    address_hash text;
    account_hash text;
    sent_count integer;
    provider_sent_count integer;
    day_start timestamptz;
    reservation_time timestamptz := clock_timestamp();
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if p_idempotency_key is null or char_length(p_idempotency_key) not between 8 and 200 then
        raise exception using errcode = '22023', message = 'idempotency_key_invalid';
    end if;

    select application.* into target_application
      from public.applications application
     where application.id = p_application_id and application.user_id = current_user_id
     for update;
    if not found then
        raise exception using errcode = 'P0002', message = 'application_not_found';
    end if;

    select event.* into existing_event
      from public.send_events event
     where event.user_id = current_user_id and event.idempotency_key = p_idempotency_key;
    if found then
        if existing_event.application_id is distinct from target_application.id then
            raise exception using errcode = '23505', message = 'idempotency_key_conflict';
        end if;
        if existing_event.outcome = 'pending_provider' then
            raise exception using errcode = 'P0001', message = 'send_in_progress';
        end if;
        return next existing_event;
        return;
    end if;

    if target_application.channel <> 'email'
       or target_application.status <> 'approved'
       or target_application.approved_at is null
       or target_application.approved_revision is distinct from target_application.content_revision
       or nullif(btrim(target_application.recipient), '') is null
       or target_application.recipient !~* '^[^[:space:]@,;<>()]+@[^[:space:]@,;<>()]+\.[^[:space:]@,;<>()]+$'
       or nullif(btrim(target_application.subject), '') is null
       or nullif(btrim(target_application.body), '') is null then
        raise exception using errcode = 'P0001', message = 'application_not_sendable';
    end if;

    perform 1 from public.connection_lifecycles lifecycle
     where lifecycle.user_id = current_user_id
       and lifecycle.provider = 'gmail'
       and lifecycle.status = 'connected'
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'gmail_not_connected';
    end if;

    select connection.* into gmail_connection
      from public.connections connection
     where connection.user_id = current_user_id
       and connection.provider = 'gmail'
       and connection.status = 'connected'
       and nullif(btrim(connection.external_account_id), '') is not null
     for share;
    if not found then
        raise exception using errcode = 'P0001', message = 'gmail_not_connected';
    end if;

    select settings.* into tenant_settings
      from public.user_settings settings
     where settings.user_id = current_user_id
     for update;
    if not found then
        insert into public.user_settings (user_id) values (current_user_id)
        returning * into tenant_settings;
    end if;

    begin
        day_start := date_trunc(
            'day', reservation_time at time zone tenant_settings.timezone
        ) at time zone tenant_settings.timezone;
    exception when invalid_parameter_value then
        day_start := date_trunc('day', reservation_time at time zone 'UTC') at time zone 'UTC';
    end;

    select count(*) into sent_count
      from public.send_events event
     where event.user_id = current_user_id
       and event.outcome in ('pending_provider', 'sent', 'needs_attention')
       and event.created_at >= day_start;
    if sent_count >= tenant_settings.daily_send_cap then
        raise exception using errcode = 'P0001', message = 'daily_send_cap_reached';
    end if;

    address_hash := encode(
        extensions.digest(
            'gmail-recipient:' || lower(btrim(target_application.recipient)), 'sha256'
        ),
        'hex'
    );
    account_hash := encode(
        extensions.digest('gmail:' || btrim(gmail_connection.external_account_id), 'sha256'),
        'hex'
    );

    perform pg_advisory_xact_lock(hashtextextended(account_hash, 0));
    delete from public.provider_send_events provider_event
     where provider_event.expires_at <= reservation_time;

    select count(*) into provider_sent_count
      from public.provider_send_events provider_event
     where provider_event.provider = 'gmail'
       and provider_event.provider_account_hash = account_hash
       and provider_event.outcome in ('pending_provider', 'sent', 'needs_attention')
       and provider_event.created_at >= reservation_time - interval '24 hours'
       and provider_event.expires_at > reservation_time;
    if provider_sent_count >= 150 then
        raise exception using errcode = 'P0001', message = 'provider_daily_send_cap_reached';
    end if;

    if exists (
        select 1 from public.send_events event
         where event.user_id = current_user_id
           and event.recipient_hash = address_hash
           and event.outcome in ('pending_provider', 'sent', 'needs_attention')
           and event.created_at >= reservation_time
               - make_interval(days => tenant_settings.duplicate_window_days)
    ) then
        raise exception using errcode = 'P0001', message = 'duplicate_recipient_window';
    end if;
    if exists (
        select 1 from public.provider_send_events provider_event
         where provider_event.provider = 'gmail'
           and provider_event.provider_account_hash = account_hash
           and provider_event.recipient_hash = address_hash
           and provider_event.outcome in ('pending_provider', 'sent', 'needs_attention')
           and provider_event.expires_at > reservation_time
    ) then
        raise exception using errcode = 'P0001', message = 'duplicate_recipient_window';
    end if;

    update public.applications application
       set status = 'queued', send_idempotency_key = p_idempotency_key,
           last_error = null, updated_at = reservation_time
     where application.id = target_application.id;

    insert into public.send_events (
        user_id, application_id, provider, outcome, recipient_hash, idempotency_key,
        created_at, updated_at
    ) values (
        current_user_id, target_application.id, 'gmail', 'pending_provider',
        address_hash, p_idempotency_key, reservation_time, reservation_time
    )
    returning * into new_event;

    insert into public.provider_send_events (
        send_event_id, provider, provider_account_hash, recipient_hash, outcome,
        created_at, updated_at, expires_at
    ) values (
        new_event.id, 'gmail', account_hash, address_hash, 'pending_provider',
        reservation_time, reservation_time,
        reservation_time + make_interval(days => tenant_settings.duplicate_window_days)
    );

    return next new_event;
    return;
end;
$$;

-- Reserve the send and create its durable worker row in one transaction. The
-- payload intentionally contains only a boolean; tokens and message content stay
-- in the tenant tables and are read by the trusted worker at claim time.
create or replace function public.enqueue_email_send(
    application_id_input uuid,
    idempotency_key_input text,
    attach_resume_input boolean default true
)
returns setof public.automation_jobs
language plpgsql
security definer
set search_path = 'public'
as $$
declare
    current_user_id uuid := public.assert_active_user();
    existing_job public.automation_jobs%rowtype;
    reservation public.send_events%rowtype;
    active_count integer;
    daily_count integer;
begin
    if current_user_id is null then
        raise exception using errcode = '42501', message = 'authentication_required';
    end if;
    if idempotency_key_input is null
       or idempotency_key_input !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$' then
        raise exception using errcode = '22023', message = 'idempotency_key_invalid';
    end if;

    select job.* into existing_job
      from public.automation_jobs job
     where job.user_id = current_user_id
       and job.idempotency_key = idempotency_key_input;
    if found then
        if existing_job.kind <> 'send_email'
           or existing_job.application_id is distinct from application_id_input then
            raise exception using errcode = '23505', message = 'idempotency_key_conflict';
        end if;
        return next existing_job;
        return;
    end if;

    select count(*) into active_count
      from public.automation_jobs job
     where job.user_id = current_user_id and job.status in ('queued', 'running');
    if active_count >= 200 then
        raise exception using errcode = 'P0001', message = 'automation_queue_full';
    end if;
    select count(*) into daily_count
      from public.automation_jobs job
     where job.user_id = current_user_id
       and job.kind = 'send_email'
       and job.created_at >= clock_timestamp() - interval '24 hours';
    if daily_count >= 150 then
        raise exception using errcode = 'P0001', message = 'automation_daily_limit_reached';
    end if;

    select event.* into reservation
      from public.reserve_application_send(application_id_input, idempotency_key_input) event;
    if reservation.outcome <> 'pending_provider' then
        raise exception using errcode = 'P0001', message = 'send_in_progress';
    end if;

    return query
    insert into public.automation_jobs (
        user_id, application_id, kind, provider, payload, idempotency_key
    ) values (
        current_user_id, application_id_input, 'send_email', 'gmail',
        jsonb_build_object('attach_resume', coalesce(attach_resume_input, true)),
        idempotency_key_input
    )
    returning automation_jobs.*;
end;
$$;

revoke all on function public.enqueue_email_send(uuid, text, boolean) from public, anon;
grant execute on function public.enqueue_email_send(uuid, text, boolean) to authenticated;
