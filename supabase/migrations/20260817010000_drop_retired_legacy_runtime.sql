-- Remove database storage for the retired jiwen, gateway timer, and legacy
-- proactive-message runtimes. Deploy the application retirement before
-- applying this migration.
--
-- DROP TABLE removes the historical rows held by these four tables. The
-- default RESTRICT behavior is intentional: an unknown remaining dependency
-- must stop the migration instead of being removed transitively.

begin;

drop table if exists public.busy_inbox;
drop table if exists public.timers;
drop table if exists public.proactive_messages;
drop table if exists public.jiwen_state;

commit;
