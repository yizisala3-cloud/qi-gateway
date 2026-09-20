-- Seed the context-injection settings consumed by gateway.app_settings:
--   recent_chat.inject_enabled  recent-chat replay switch (streaming context)
--   recent_chat.inject_limit    how many chat_messages rows to replay (1-100)
--   timestamp.inject_enabled    built-in "current time" block switch
--
-- Idempotent: re-runs never overwrite values the admin already tuned through
-- /admin/api/context/settings (a saved limit or a deliberately toggled
-- switch must win over these defaults). This file only seeds app_settings;
-- chat_messages is read-only for the gateway and must never be touched here.

insert into public.app_settings (key, value)
values
    ('recent_chat.inject_enabled', 'true'::jsonb),
    ('recent_chat.inject_limit', '10'::jsonb),
    ('timestamp.inject_enabled', 'true'::jsonb)
on conflict (key) do nothing;
