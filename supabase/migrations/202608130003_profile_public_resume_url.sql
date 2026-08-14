-- Store the user-provided public resume link used by URL-based application
-- questions. This is deliberately separate from the private `resumes` bucket:
-- a private object path or expiring signed URL must never be presented to an
-- employer as the candidate's durable resume link.

begin;

alter table public.profiles
    add column if not exists resume_url text
        check (
            resume_url is null
            or (
                char_length(resume_url) between 9 and 2048
                and resume_url ~ '^https://[^[:space:]]+$'
            )
        );

grant update (resume_url) on public.profiles to authenticated;

comment on column public.profiles.resume_url is
    'User-provided public HTTPS resume URL for employer-visible link fields; never a private storage path.';

commit;
