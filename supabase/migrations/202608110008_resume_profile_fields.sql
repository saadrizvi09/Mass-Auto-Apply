-- Add first-class education facts commonly required by application forms.
begin;

alter table public.profiles
    add column college text
        check (college is null or char_length(college) <= 300),
    add column degree text
        check (degree is null or char_length(degree) <= 300),
    add column graduation_year smallint
        check (graduation_year is null or graduation_year between 1950 and 2100);

grant update (college, degree, graduation_year) on public.profiles to authenticated;

comment on column public.profiles.graduation_year is
    'User-reviewed graduation/passout year; may be suggested from a parsed resume.';

commit;
