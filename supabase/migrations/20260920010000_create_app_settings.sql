-- Global key-value application settings. Currently the single consumer is
-- the Eventide body-state injection switch (eventide.inject_enabled), read
-- by gateway.app_settings with a 60s TTL cache on every chat request.
--
-- The gateway connects with an elevated server key; no browser/client role
-- ever touches this table directly -- the admin panel goes through the
-- token-checked /admin/api/eventide/* endpoints instead. Fail-open semantics
-- live in the gateway layer (query error or missing row => treat as enabled).

create table if not exists public.app_settings (
    key text primary key,
    value jsonb not null,
    updated_at timestamptz not null default now()
);

comment on table public.app_settings is
    'Server-owned application settings; the gateway reads/writes via an elevated key.';

insert into public.app_settings (key, value)
values ('eventide.inject_enabled', 'true'::jsonb)
on conflict (key) do nothing;

alter table public.app_settings enable row level security;

-- Browser/client roles do not access this table directly. The gateway uses an
-- elevated server key and exposes only purpose-built authenticated endpoints.
revoke all on table public.app_settings from anon, authenticated;
grant select, insert, update on table public.app_settings to service_role;
