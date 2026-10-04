-- 备忘录回收站保留期（M19，2026-10-03 确认）：进入回收站的备忘录自删除
-- 时刻起保留 72 小时，到期彻底删除且无法恢复。一期无「彻底删除」手动
-- 入口，72 小时为唯一清除路径。
--
-- 本迁移不改表结构，只新增清扫 RPC 并整体替换生命周期 RPC：
--   - memo_purge_expired_trash(p_now)：硬删除 status='deleted' 且
--     deleted_at ≤ p_now - 72 小时的记录行；memo_entry_tag /
--     memo_position 以 ON DELETE CASCADE 随之清除，正文与幂等键不再
--     保留（无法恢复）。由服务层在读取与生命周期入口尽力调用（惰性
--     清扫），也可手动执行。
--   - memo_set_entry_lifecycle：恢复分支加入保留期门——已过期的回收站
--     记录以 ME003 拒绝恢复，独立于清扫是否已执行，保证「无法恢复」
--     不依赖调用方先清扫。归档与正常内容不受影响。
--
-- 清扫是删除型写操作，与其它 memo_* 写路径一致在单事务内完成；并发
-- 调用各自按谓词删除，重复执行幂等（第二次删除 0 行）。

begin;

-- ── 回收站到期清扫（M19）：72 小时保留期，到期彻底删除 ───────────
create or replace function public.memo_purge_expired_trash(
    p_now timestamptz
)
returns integer
language plpgsql
as $$
declare
    v_purged integer := 0;
begin
    with gone as (
        delete from public.memo_entry e
         where e.status = 'deleted'
           and e.deleted_at is not null
           and e.deleted_at <= p_now - interval '72 hours'
        returning 1
    )
    select count(*) into v_purged from gone;
    return v_purged;
end;
$$;

-- ── 生命周期（归档 / 删除 / 恢复）：恢复门加入 72 小时保留期 ──────
create or replace function public.memo_set_entry_lifecycle(
    p_entry_id bigint,
    p_action text,
    p_expected_version integer,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_entry public.memo_entry;
begin
    if p_action not in ('archive', 'delete', 'restore') then
        raise exception 'memo_set_entry_lifecycle: unknown action'
            using errcode = 'ME005';
    end if;
    if p_expected_version is null then
        raise exception 'memo_set_entry_lifecycle: expected_version is required'
            using errcode = 'ME005';
    end if;

    select * into v_entry
      from public.memo_entry e
     where e.id = p_entry_id
     for update;
    if not found then
        raise exception 'memo_set_entry_lifecycle: entry not found'
            using errcode = 'ME001';
    end if;
    if v_entry.content_version is distinct from p_expected_version then
        raise exception 'memo_set_entry_lifecycle: stale content_version'
            using errcode = 'ME002';
    end if;

    if p_action = 'archive' then
        if v_entry.status <> 'active' then
            raise exception 'memo_set_entry_lifecycle: cannot archive from %', v_entry.status
                using errcode = 'ME003';
        end if;
        update public.memo_entry e
           set status = 'archived', archived_at = p_now,
               content_version = e.content_version + 1, updated_at = p_now
         where e.id = p_entry_id;
    elsif p_action = 'delete' then
        if v_entry.status not in ('active', 'archived') then
            raise exception 'memo_set_entry_lifecycle: cannot delete from %', v_entry.status
                using errcode = 'ME003';
        end if;
        update public.memo_entry e
           set status = 'deleted', deleted_at = p_now,
               content_version = e.content_version + 1, updated_at = p_now
         where e.id = p_entry_id;
    else
        if v_entry.status not in ('archived', 'deleted') then
            raise exception 'memo_set_entry_lifecycle: cannot restore from %', v_entry.status
                using errcode = 'ME003';
        end if;
        -- 回收站保留期（M19）：自删除时刻起 72 小时后彻底删除、无法恢复。
        -- 该门独立于清扫是否已执行：清扫尚未跑到时，过期记录同样不能
        -- 复活；正常归档记录不经过此分支，不受保留期影响。
        if v_entry.status = 'deleted' and v_entry.deleted_at is not null
           and v_entry.deleted_at <= p_now - interval '72 hours' then
            raise exception 'memo_set_entry_lifecycle: trash retention (72h) expired'
                using errcode = 'ME003';
        end if;
        update public.memo_entry e
           set status = 'active', archived_at = null, deleted_at = null,
               content_version = e.content_version + 1, updated_at = p_now
         where e.id = p_entry_id;
    end if;

    return public.memo_serialize_entry(
        (select e from public.memo_entry e where e.id = p_entry_id));
end;
$$;

commit;
