-- 备忘录并发修复第三轮（BUG-04 复审残留，2026-10-04）：重排锁内复核
-- 「初始受保护集合」，缺组路径 ensure 后同样复核实际成员。
--
-- 背景：20261003010000_memo_lock_order_unify.sql 已把分组锁序与创建路径
-- 同构、并为模式切换加入锁内成员复核，但 memo_reorder_group 在取得分组
-- 行锁后直接用重读集合覆盖 v_members，只核对「新集合 == 提交集合」：
--   等组锁期间并发 RPC 把新成员 N 加回分组并提交 → 重排的新集合与提交
--   集合一致 → 为 N 写位次。N 从未受本事务的记录行锁保护：位次外键
--   （memo_position.entry_id → memo_entry FOR KEY SHARE）会反向等待 N 的
--   行锁；并发改用途持 N 行锁等分组锁，形成记录↔分组环，真实 PostgreSQL
--   返回 40P01，错误上下文落在 memo_position INSERT 的外键检查。
--
-- 本迁移整体替换 memo_reorder_group / memo_set_note_mode，不改表结构：
--   ★ 只有「加锁窗口前已属于分组、且被本事务锁定记录行」的成员才允许
--     在锁内写出位次；锁内重读集合与初始加锁集合不一致 → ME004 干净
--     退出（可重试），不持组锁后补记录锁。
--   ★ 缺失分组的 ensure 在所属标签行锁内进行（维持第二轮不变量），
--     ensure 后复核实际成员：等锁期间并发建组并加入成员的，同样以
--     ME004 退出，不静默为未复核成员物化位次或翻转排序模式。
-- 模式切换沿用第二轮的 v_recheck 复核，并补上缺组路径的 ensure 后复核；
-- 重排新增 v_locked_members 标记（真库集成测试以 prosrc 作安装守卫）。

begin;

-- ── 分组内重排（常驻区 / 随笔区）：锁内复核初始受保护集合 ─────────
create or replace function public.memo_reorder_group(
    p_scope jsonb,
    p_section text,
    p_ordered_entry_ids jsonb,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_tag_id bigint;
    v_untagged boolean;
    v_group_id bigint;
    v_locked_members bigint[];
    v_members bigint[];
    v_submitted bigint[];
    v_entry_id bigint;
    v_i integer;
begin
    if p_scope is null or jsonb_typeof(p_scope) is distinct from 'object' then
        raise exception 'memo_reorder_group: scope must be an object'
            using errcode = 'ME005';
    end if;
    if p_section not in ('pinned', 'note') then
        raise exception 'memo_reorder_group: section must be pinned or note'
            using errcode = 'ME005';
    end if;
    if jsonb_typeof(p_ordered_entry_ids) is distinct from 'array'
       or exists (
            select 1 from jsonb_array_elements(p_ordered_entry_ids) as e
            where jsonb_typeof(e.value) is distinct from 'number'
               or e.value::text !~ '^-?[0-9]+$'
       ) then
        raise exception 'memo_reorder_group: ordered_entry_ids must be an array of integers'
            using errcode = 'ME005';
    end if;
    -- 提交顺序即业务语义：dedupe 保留首次出现顺序（select distinct 不保序，
    -- 必须 with ordinality 固化提交顺序）。
    v_submitted := array(
        select s.id
          from (
            select (e.value::text)::bigint as id, min(e.ord) as first_ord
              from jsonb_array_elements(p_ordered_entry_ids) with ordinality as e(value, ord)
             group by 1
          ) s
         order by s.first_ord);
    if coalesce(array_length(v_submitted, 1), 0) <> jsonb_array_length(p_ordered_entry_ids) then
        raise exception 'memo_reorder_group: duplicate entry ids'
            using errcode = 'ME005';
    end if;

    v_untagged := coalesce((p_scope->>'untagged')::boolean, false);
    if v_untagged then
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id is null
         limit 1;
        if v_group_id is null then
            raise exception 'memo_reorder_group: untagged group missing'
                using errcode = 'ME001';
        end if;
    else
        v_tag_id := (p_scope->>'tag_id')::bigint;
        if v_tag_id is null then
            raise exception 'memo_reorder_group: scope.tag_id is required'
                using errcode = 'ME005';
        end if;
        if not exists (select 1 from public.memo_tag t where t.id = v_tag_id) then
            raise exception 'memo_reorder_group: tag not found'
                using errcode = 'ME001';
        end if;
        -- 只读定位分组，不在此处 ensure：分组锁必须晚于成员记录锁
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id = v_tag_id;
    end if;

    -- 统一锁序（BUG-04）：成员记录行（id 升序）→ 分组行 → 位置行。
    -- 成员行锁先于分组行锁，与 memo_update_entry（entry → tag → group）
    -- 同向；等锁期间的关联变化无法逃过锁内复核，不再按陈旧快照为非
    -- 成员写出幽灵位次（原 23505 链）。
    if v_group_id is not null then
        -- 初始受保护集合（BUG-04 第三轮）：只有此刻已在分组内、且被本
        -- 事务锁定记录行的成员，才允许在锁内写出位次。
        v_locked_members := public.memo_group_member_ids(v_group_id, p_section);
        foreach v_entry_id in array v_locked_members loop
            perform 1 from public.memo_entry e where e.id = v_entry_id for update;
        end loop;
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        -- 锁内复核其一：成员集合必须与加锁集合一致。等组锁期间并发加入
        -- 的成员只受分组锁保护——为它写位次的外键会反向等待该成员的行锁，
        -- 与「改用途持行等组」形成记录↔分组环（真实 40P01）。集合变化以
        -- ME004 干净退出（可重试）：不持组锁后补记录锁。
        v_members := public.memo_group_member_ids(v_group_id, p_section);
        if v_members is distinct from v_locked_members then
            raise exception 'memo_reorder_group: member set changed (concurrent change)'
                using errcode = 'ME004';
        end if;
    else
        -- 标签尚无分组行 = 该分组没有成员。缺失分组的 ensure 在所属
        -- 标签行锁内进行（BUG-04 第二轮：分组工作一律在标签行锁之内）；
        -- ensure 后复核实际成员（BUG-04 第三轮）：初始受保护集合为空，
        -- 等标签锁期间并发建组并加入的任何成员都未经本事务锁定——
        -- 与加锁集合不一致即 ME004 退出，不物化位次、不翻转排序模式。
        v_locked_members := array[]::bigint[];
        perform 1 from public.memo_tag t where t.id = v_tag_id for update;
        v_group_id := public.memo_ensure_group(v_tag_id);
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        v_members := public.memo_group_member_ids(v_group_id, p_section);
        if v_members is distinct from v_locked_members then
            raise exception 'memo_reorder_group: member set changed (concurrent change)'
                using errcode = 'ME004';
        end if;
    end if;

    -- 全量一致才生效（与规划重排同一防陈旧策略）：成员集合不一致 =
    -- 有并发增删，让客户端基于最新数据重排，不静默覆盖。
    if coalesce(array_length(v_members, 1), 0) <> coalesce(array_length(v_submitted, 1), 0)
       or exists (select unnest(v_members) except select unnest(v_submitted))
       or exists (select unnest(v_submitted) except select unnest(v_members)) then
        raise exception 'memo_reorder_group: member set changed (concurrent change)'
            using errcode = 'ME004';
    end if;

    if p_section = 'note'
       and (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'latest' then
        -- 随笔区在最新模式下被拖动 = 进入手动排序模式（§4.3），
        -- 提交的顺序即手动顺序的落位。
        update public.memo_group g
           set note_sort_mode = 'manual'
         where g.id = v_group_id;
    end if;

    v_i := 1;
    foreach v_entry_id in array v_submitted loop
        insert into public.memo_position (group_id, entry_id, position)
        values (v_group_id, v_entry_id, v_i)
        on conflict (group_id, entry_id)
        do update set position = excluded.position;
        v_i := v_i + 1;
    end loop;

    return jsonb_build_object(
        'group_id', v_group_id,
        'section', p_section,
        'note_sort_mode', (select g.note_sort_mode from public.memo_group g where g.id = v_group_id),
        'order', to_jsonb(v_submitted));
end;
$$;

-- ── 随笔排序模式切换：同一锁协议，缺组路径补 ensure 后成员复核 ────
create or replace function public.memo_set_note_mode(
    p_scope jsonb,
    p_mode text,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_tag_id bigint;
    v_untagged boolean;
    v_group_id bigint;
    v_members bigint[];
    v_recheck bigint[];
    v_positioned bigint[];
    v_entry_id bigint;
    v_next integer;
begin
    if p_mode not in ('latest', 'manual') then
        raise exception 'memo_set_note_mode: mode must be latest or manual'
            using errcode = 'ME005';
    end if;
    if p_scope is null or jsonb_typeof(p_scope) is distinct from 'object' then
        raise exception 'memo_set_note_mode: scope must be an object'
            using errcode = 'ME005';
    end if;

    v_untagged := coalesce((p_scope->>'untagged')::boolean, false);
    if v_untagged then
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id is null
         limit 1;
        if v_group_id is null then
            raise exception 'memo_set_note_mode: untagged group missing'
                using errcode = 'ME001';
        end if;
    else
        v_tag_id := (p_scope->>'tag_id')::bigint;
        if v_tag_id is null then
            raise exception 'memo_set_note_mode: scope.tag_id is required'
                using errcode = 'ME005';
        end if;
        if not exists (select 1 from public.memo_tag t where t.id = v_tag_id) then
            raise exception 'memo_set_note_mode: tag not found'
                using errcode = 'ME001';
        end if;
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id = v_tag_id;
    end if;

    -- 统一锁序（BUG-04）：与 memo_reorder_group 相同——成员记录行
    -- （id 升序）→ 分组行 → 位置行。
    if v_group_id is not null then
        v_members := public.memo_group_member_ids(v_group_id, 'note');
        foreach v_entry_id in array v_members loop
            perform 1 from public.memo_entry e where e.id = v_entry_id for update;
        end loop;
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        -- 锁内复核（BUG-04 第二轮）：成员集合必须与加锁时一致。等锁期间
        -- 并发加入的成员只受分组锁保护——物化位次的外键会反向等待该成员
        -- 的行锁，与「改用途持行等组」形成记录↔分组环（原 40P01）。集合
        -- 变化时干净退出（ME004 可重试）：不持组锁后补记录锁，也不为未
        -- 受记录锁保护的成员物化位次。
        v_recheck := public.memo_group_member_ids(v_group_id, 'note');
        if v_recheck is distinct from v_members then
            raise exception 'memo_set_note_mode: member set changed (concurrent change)'
                using errcode = 'ME004';
        end if;
    else
        v_members := array[]::bigint[];
    end if;

    if p_mode = 'manual' then
        v_positioned := array(
            select p.entry_id
              from public.memo_position p
              join public.memo_entry e on e.id = p.entry_id
             where p.group_id = v_group_id
               and e.kind = 'note'
               and e.status = 'active'
               and p.entry_id = any(v_members));
        if coalesce(array_length(v_positioned, 1), 0) = 0
           and coalesce(array_length(v_members, 1), 0) > 0 then
            -- 从未手排过：按当前最新顺序（创建时间倒序）落位。
            v_next := 1;
            for v_entry_id in
                select e.id
                  from public.memo_entry e
                 where e.id = any(v_members)
                 order by e.created_at desc, e.id desc
            loop
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, v_entry_id, v_next)
                on conflict (group_id, entry_id)
                do update set position = excluded.position;
                v_next := v_next + 1;
            end loop;
        else
            -- 切回手动：原手动顺序继续保留（§4.3）；未定位成员（手动模式
            -- 期间新增等）按创建先后补在末尾。
            v_next := coalesce((
                select max(p.position)
                  from public.memo_position p
                  join public.memo_entry e on e.id = p.entry_id
                 where p.group_id = v_group_id
                   and e.kind = 'note'
                   and e.status = 'active'
                   and p.entry_id = any(v_members)
            ), 0) + 1;
            for v_entry_id in
                select e.id
                  from public.memo_entry e
                 where e.id = any(v_members)
                   and not (e.id = any(v_positioned))
                 order by e.created_at asc, e.id asc
            loop
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, v_entry_id, v_next)
                on conflict (group_id, entry_id)
                do update set position = excluded.position;
                v_next := v_next + 1;
            end loop;
        end if;
    end if;
    -- 切回 latest 只改模式：位次行原样保留，供再次切回手动时恢复（§4.3）。

    if v_group_id is null then
        -- 标签尚无分组行且没有成员：仅落分组行并设置模式。缺失分组的
        -- ensure 在所属标签行锁内进行（BUG-04 第二轮）；ensure 后复核
        -- 实际成员（BUG-04 第三轮）：等标签锁期间并发建组并加入成员的，
        -- 同样以 ME004 干净退出，不静默为未复核成员翻转排序模式。
        perform 1 from public.memo_tag t where t.id = v_tag_id for update;
        v_group_id := public.memo_ensure_group(v_tag_id);
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        v_recheck := public.memo_group_member_ids(v_group_id, 'note');
        if v_recheck is distinct from v_members then
            raise exception 'memo_set_note_mode: member set changed (concurrent change)'
                using errcode = 'ME004';
        end if;
    end if;

    update public.memo_group g
       set note_sort_mode = p_mode
     where g.id = v_group_id;

    return jsonb_build_object(
        'group_id', v_group_id,
        'note_sort_mode', p_mode);
end;
$$;

commit;
