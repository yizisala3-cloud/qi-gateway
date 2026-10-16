-- BUG-22: a memo detail is one read-only snapshot, without foreground cleanup.
-- Keep the existing serializer (including all tags) and the 72-hour expiry gate.
-- BUG-16: background cleanup follows entry-id lock order and skips busy rows.
begin;

create or replace function public.memo_get_entry(
    p_entry_id bigint,
    p_now timestamptz
)
returns jsonb
language sql
stable
set search_path = public
as $$
    select public.memo_serialize_entry(e)
      from public.memo_entry e
     where e.id = p_entry_id
       and not (e.status = 'deleted' and e.deleted_at is not null
                and e.deleted_at <= p_now - interval '72 hours');
$$;

revoke all on function public.memo_get_entry(bigint, timestamptz)
    from public, anon, authenticated;
grant execute on function public.memo_get_entry(bigint, timestamptz) to service_role;

create or replace function public.memo_purge_expired_trash(p_now timestamptz)
returns integer
language plpgsql
as $$
declare
    v_id bigint;
    v_deleted integer;
    v_purged integer := 0;
begin
    for v_id in
        select e.id from public.memo_entry e
         where e.status = 'deleted' and e.deleted_at is not null
           and e.deleted_at <= p_now - interval '72 hours'
         order by e.id
         for update skip locked
    loop
        -- Recheck under the row lock; retain cascading deletion and idempotency.
        delete from public.memo_entry e
         where e.id = v_id and e.status = 'deleted' and e.deleted_at is not null
           and e.deleted_at <= p_now - interval '72 hours';
        get diagnostics v_deleted = row_count;
        v_purged := v_purged + v_deleted;
    end loop;
    return v_purged;
end;
$$;

notify pgrst, 'reload schema';
commit;
