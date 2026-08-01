-- Cover the memory_requests -> memories foreign key used by review history
-- and ON DELETE SET NULL. This table remains private to service_role.
create index if not exists memory_requests_memory_id_idx
    on public.memory_requests (memory_id)
    where memory_id is not null;

