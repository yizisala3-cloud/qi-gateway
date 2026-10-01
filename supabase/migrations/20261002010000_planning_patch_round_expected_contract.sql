-- #13（2026-10-02）：planning_patch_occurrence_round 的 expected 契约收紧。
-- 同签名 create or replace（不产生新 overload；重放安全；不新增表列 /
-- 触发器 / 数据回填）。函数职责与其余语句与迁移 20260928020000 一致，本次
-- 仅收紧 expected snapshot 复核块：
--   * expected 非 NULL 时必须是**恰好两个元素**的 JSON 数组，每个元素为
--     JSON 对象且携带全部必填键（id / status / window_start_at /
--     window_end_at / est_start / est_end / estimated_time_source /
--     fixed_source / is_fixed / schedule_managed / sort_order）；
--   * 两个 id 必须恰为目标行与兄弟行各一次（空数组 / 单元素 / 未知 id /
--     重复 id 一律拒绝）；
--   * 漂移比较新增 sort_order——它是 compute_schedule 的遍历顺序输入，
--     读取后 save_order 改序即旧计算作废（与单行条件 UPDATE 的内联
--     sort_order 等值守卫同源，不再让中空轮次绕过排序复核）；
--   * 人工编辑路径 expected = NULL（默认值）不受影响。
-- 拒绝统一使用固定 ERRCODE 'PC001'（乐观并发拒绝族），由应用层映射为
-- ConcurrencyRejected → 重算 stale_skipped（等待标记保留）。

begin;

create or replace function public.planning_patch_occurrence_round(
    p_target_id bigint,
    p_sibling_id bigint,
    p_target_patch jsonb,
    p_sibling_patch jsonb,
    p_expected jsonb default null
) returns void
language plpgsql as $$
begin
    -- ── patch 硬白名单（未知 key 一律拒绝） ─────────────────────────
    if exists (
        select 1
        from jsonb_object_keys(coalesce(p_target_patch, '{}'::jsonb)) as k
        where k not in (
            'window_start_at', 'window_end_at', 'est_start', 'est_end',
            'nominal_start', 'estimated_time_source', 'fixed_source',
            'schedule_managed', 'is_fixed', 'partial_note',
            'actual_start', 'actual_end', 'actual_minutes',
            'status', 'closed_at', 'handled_at', 'partial_at',
            'display_cycle_date', 'display_reason', 'updated_at'
        )
    ) then
        raise exception 'planning_patch_occurrence_round: target patch contains unsupported field';
    end if;
    if exists (
        select 1
        from jsonb_object_keys(coalesce(p_sibling_patch, '{}'::jsonb)) as k
        where k not in (
            'window_start_at', 'window_end_at', 'est_start', 'est_end',
            'nominal_start', 'estimated_time_source', 'fixed_source',
            'schedule_managed', 'is_fixed',
            'display_cycle_date', 'display_reason', 'updated_at'
        )
    ) then
        raise exception 'planning_patch_occurrence_round: sibling patch contains unsupported field';
    end if;
    -- 携带 status 时只允许开放状态：本函数是同轮开放生命周期的原子编辑
    -- 载体，永远不能成为终态（completed/timeout/discarded*）写入通道。
    if p_target_patch ? 'status'
       and coalesce(p_target_patch->>'status') not in
           ('pending', 'in_progress', 'deferred', 'partial') then
        raise exception 'planning_patch_occurrence_round: only open statuses can be written';
    end if;

    -- ── 稳定 id 顺序行锁（并发反向锁对不会死锁） ───────────────────
    perform 1
    from public.planning_occurrence
    where id in (p_target_id, p_sibling_id)
    order by id
    for update;

    -- ── round 身份确认（锁后） ─────────────────────────────────────
    if (select count(*)
        from public.planning_occurrence
        where id in (p_target_id, p_sibling_id)) <> 2 then
        raise exception 'planning_patch_occurrence_round: occurrence rows not found';
    end if;
    if exists (
        select 1
        from public.planning_occurrence a
        join public.planning_occurrence b on b.id = p_sibling_id
        where a.id = p_target_id
          and (a.task_id is distinct from b.task_id
               or a.round_key is distinct from b.round_key
               or a.phase_group is distinct from b.phase_group
               or a.phase is null
               or b.phase is null
               or a.phase = b.phase)
    ) then
        raise exception 'planning_patch_occurrence_round: rows are not a hollow start/end pair';
    end if;

    -- ── 锁内生命周期二次校验（最终修复问题 1；问题 5 收紧） ─────────
    -- Python 检查与 RPC 执行之间的并发窗口内，实例可能已被完成 / 开始 /
    -- 部分完成 / 关闭；锁内重新确认两行仍处于可编辑生命周期，任意阶段
    -- 不可编辑则整体失败、零写入。
    -- 问题 5：调用者不得通过携带 status 字段降低保护——只要补丁触及
    -- 窗口字段（window_start_at / window_end_at），无论是否携带 status
    -- 都必须执行严格门（pending/deferred 且无任何事实字段）；status
    -- 携带的宽松门（开放且无关闭事实）仅适用于不触及窗口字段的开放
    -- 状态流转（延后等，其 est 联动是流转自身的组成部分）。
    if (p_target_patch ? 'window_start_at'
        or p_target_patch ? 'window_end_at'
        or p_sibling_patch ? 'window_start_at'
        or p_sibling_patch ? 'window_end_at'
        or not (p_target_patch ? 'status')) then
        -- 严格门：窗口编辑与纯预估编辑——两行均须尚未开始且无任何事实。
        if exists (
            select 1
            from public.planning_occurrence
            where id in (p_target_id, p_sibling_id)
              and (status not in ('pending', 'deferred')
                   or actual_start is not null
                   or actual_end is not null
                   or partial_at is not null
                   or handled_at is not null
                   or closed_at is not null)
        ) then
            raise exception 'planning_patch_occurrence_round: round is no longer editable (concurrent lifecycle change)'
                using errcode = 'PC001';
        end if;
    else
        -- 宽松门：开放状态流转（延后等）——两行仍开放且无关闭事实。
        if exists (
            select 1
            from public.planning_occurrence
            where id in (p_target_id, p_sibling_id)
              and (status not in ('pending', 'in_progress', 'deferred', 'partial')
                   or closed_at is not null
                   or handled_at is not null)
        ) then
            raise exception 'planning_patch_occurrence_round: round is no longer editable (concurrent lifecycle change)'
                using errcode = 'PC001';
        end if;
    end if;

    -- ── expected snapshot 复核（最终修复问题 3；#13 契约收紧） ─────
    -- 重算以读取快照计算排程结果；锁内复核本次计算所依赖的输入在写入时
    -- 仍与快照一致，任一漂移 = 旧计算作废（stale schedule 不得写回）。
    -- #13（2026-10-02）收紧：expected 非 NULL 时必须是恰好两个元素的
    -- 数组、每个元素为对象且携带全部必填键（含两阶段 sort_order）、
    -- 两个 id 恰为目标行与兄弟行各一次；空数组 / 缺键 / 未知 id / 重复
    -- id 一律拒绝——不再让形状残缺的快照静默缩水成「只复核提供键」。
    -- 固定字段清单，非通用框架。
    if p_expected is not null then
        if jsonb_typeof(p_expected) is distinct from 'array'
           or jsonb_array_length(p_expected) <> 2 then
            raise exception 'planning_patch_occurrence_round: invalid expected snapshot'
                using errcode = 'PC001';
        end if;
        if exists (
            select 1
            from jsonb_array_elements(p_expected) as e
            where jsonb_typeof(e.value) is distinct from 'object'
               or not e.value ?& array[
                   'id', 'status', 'window_start_at', 'window_end_at',
                   'est_start', 'est_end', 'estimated_time_source',
                   'fixed_source', 'is_fixed', 'schedule_managed', 'sort_order']
        ) then
            raise exception 'planning_patch_occurrence_round: invalid expected snapshot'
                using errcode = 'PC001';
        end if;
        if (select count(*) from jsonb_array_elements(p_expected) as e
            where (e.value->>'id')::bigint in (p_target_id, p_sibling_id)) <> 2
           or (select count(distinct (e.value->>'id')::bigint)
               from jsonb_array_elements(p_expected) as e) <> 2 then
            raise exception 'planning_patch_occurrence_round: invalid expected snapshot'
                using errcode = 'PC001';
        end if;
        if exists (
            select 1
            from jsonb_array_elements(p_expected) as e
            join public.planning_occurrence o on o.id = (e.value->>'id')::bigint
            where ((e.value ? 'status' and o.status is distinct from e.value->>'status')
                or (e.value ? 'window_start_at' and o.window_start_at is distinct from (e.value->>'window_start_at')::timestamptz)
                or (e.value ? 'window_end_at' and o.window_end_at is distinct from (e.value->>'window_end_at')::timestamptz)
                or (e.value ? 'est_start' and o.est_start is distinct from (e.value->>'est_start')::timestamptz)
                or (e.value ? 'est_end' and o.est_end is distinct from (e.value->>'est_end')::timestamptz)
                or (e.value ? 'estimated_time_source' and o.estimated_time_source is distinct from e.value->>'estimated_time_source')
                or (e.value ? 'fixed_source' and o.fixed_source is distinct from e.value->>'fixed_source')
                or (e.value ? 'is_fixed' and o.is_fixed is distinct from (e.value->>'is_fixed')::boolean)
                or (e.value ? 'schedule_managed' and o.schedule_managed is distinct from (e.value->>'schedule_managed')::boolean)
                or (e.value ? 'sort_order' and o.sort_order is distinct from (e.value->>'sort_order')::integer))
        ) then
            raise exception 'planning_patch_occurrence_round: schedule inputs drifted (stale recompute result)'
                using errcode = 'PC001';
        end if;
    end if;

    -- ── 窗口一致性（最终修复问题 7） ───────────────────────────────
    -- 任一补丁触及窗口字段时，两阶段生效后的窗口必须一致（轮次级约束）。
    if (p_target_patch ? 'window_start_at'
        or p_target_patch ? 'window_end_at'
        or p_sibling_patch ? 'window_start_at'
        or p_sibling_patch ? 'window_end_at') then
        if exists (
            select 1
            from public.planning_occurrence a
            cross join public.planning_occurrence b
            where a.id = p_target_id
              and b.id = p_sibling_id
              and ((case when p_target_patch ? 'window_start_at'
                         then (p_target_patch->>'window_start_at')::timestamptz
                         else a.window_start_at end)
                   is distinct from
                   (case when p_sibling_patch ? 'window_start_at'
                         then (p_sibling_patch->>'window_start_at')::timestamptz
                         else b.window_start_at end)
                   or (case when p_target_patch ? 'window_end_at'
                         then (p_target_patch->>'window_end_at')::timestamptz
                         else a.window_end_at end)
                   is distinct from
                   (case when p_sibling_patch ? 'window_end_at'
                         then (p_sibling_patch->>'window_end_at')::timestamptz
                         else b.window_end_at end))
        ) then
            raise exception 'planning_patch_occurrence_round: hollow phases disagree on window';
        end if;
    end if;

    -- ── 目标行：白名单逐列显式更新（patch 缺键 = 保持现值） ─────────
    update public.planning_occurrence set
        window_start_at = case when p_target_patch ? 'window_start_at'
            then (p_target_patch->>'window_start_at')::timestamptz else window_start_at end,
        window_end_at = case when p_target_patch ? 'window_end_at'
            then (p_target_patch->>'window_end_at')::timestamptz else window_end_at end,
        est_start = case when p_target_patch ? 'est_start'
            then (p_target_patch->>'est_start')::timestamptz else est_start end,
        est_end = case when p_target_patch ? 'est_end'
            then (p_target_patch->>'est_end')::timestamptz else est_end end,
        nominal_start = case when p_target_patch ? 'nominal_start'
            then (p_target_patch->>'nominal_start')::timestamptz else nominal_start end,
        estimated_time_source = case when p_target_patch ? 'estimated_time_source'
            then p_target_patch->>'estimated_time_source' else estimated_time_source end,
        fixed_source = case when p_target_patch ? 'fixed_source'
            then p_target_patch->>'fixed_source' else fixed_source end,
        schedule_managed = case when p_target_patch ? 'schedule_managed'
            then (p_target_patch->>'schedule_managed')::boolean else schedule_managed end,
        is_fixed = case when p_target_patch ? 'is_fixed'
            then (p_target_patch->>'is_fixed')::boolean else is_fixed end,
        partial_note = case when p_target_patch ? 'partial_note'
            then p_target_patch->>'partial_note' else partial_note end,
        actual_start = case when p_target_patch ? 'actual_start'
            then (p_target_patch->>'actual_start')::timestamptz else actual_start end,
        actual_end = case when p_target_patch ? 'actual_end'
            then (p_target_patch->>'actual_end')::timestamptz else actual_end end,
        actual_minutes = case when p_target_patch ? 'actual_minutes'
            then (p_target_patch->>'actual_minutes')::integer else actual_minutes end,
        status = case when p_target_patch ? 'status'
            then p_target_patch->>'status' else status end,
        closed_at = case when p_target_patch ? 'closed_at'
            then (p_target_patch->>'closed_at')::timestamptz else closed_at end,
        handled_at = case when p_target_patch ? 'handled_at'
            then (p_target_patch->>'handled_at')::timestamptz else handled_at end,
        partial_at = case when p_target_patch ? 'partial_at'
            then (p_target_patch->>'partial_at')::timestamptz else partial_at end,
        display_cycle_date = case when p_target_patch ? 'display_cycle_date'
            then (p_target_patch->>'display_cycle_date')::date else display_cycle_date end,
        display_reason = case when p_target_patch ? 'display_reason'
            then p_target_patch->>'display_reason' else display_reason end,
        updated_at = case when p_target_patch ? 'updated_at'
            then (p_target_patch->>'updated_at')::timestamptz else updated_at end
    where id = p_target_id;

    -- ── 兄弟行：白名单刻意更小（联动必要字段 + 展示一致；无说明/实际/生命周期） ──
    update public.planning_occurrence set
        window_start_at = case when p_sibling_patch ? 'window_start_at'
            then (p_sibling_patch->>'window_start_at')::timestamptz else window_start_at end,
        window_end_at = case when p_sibling_patch ? 'window_end_at'
            then (p_sibling_patch->>'window_end_at')::timestamptz else window_end_at end,
        est_start = case when p_sibling_patch ? 'est_start'
            then (p_sibling_patch->>'est_start')::timestamptz else est_start end,
        est_end = case when p_sibling_patch ? 'est_end'
            then (p_sibling_patch->>'est_end')::timestamptz else est_end end,
        nominal_start = case when p_sibling_patch ? 'nominal_start'
            then (p_sibling_patch->>'nominal_start')::timestamptz else nominal_start end,
        estimated_time_source = case when p_sibling_patch ? 'estimated_time_source'
            then p_sibling_patch->>'estimated_time_source' else estimated_time_source end,
        fixed_source = case when p_sibling_patch ? 'fixed_source'
            then p_sibling_patch->>'fixed_source' else fixed_source end,
        schedule_managed = case when p_sibling_patch ? 'schedule_managed'
            then (p_sibling_patch->>'schedule_managed')::boolean else schedule_managed end,
        is_fixed = case when p_sibling_patch ? 'is_fixed'
            then (p_sibling_patch->>'is_fixed')::boolean else is_fixed end,
        display_cycle_date = case when p_sibling_patch ? 'display_cycle_date'
            then (p_sibling_patch->>'display_cycle_date')::date else display_cycle_date end,
        display_reason = case when p_sibling_patch ? 'display_reason'
            then p_sibling_patch->>'display_reason' else display_reason end,
        updated_at = case when p_sibling_patch ? 'updated_at'
            then (p_sibling_patch->>'updated_at')::timestamptz else updated_at end
    where id = p_sibling_id;
end;
$$;

commit;
