-- 批次 6 最终修复（2026-09-28 user 批准）：hollow 同轮两阶段的原子更新函数。
-- 这是实现批次 6 已有「同轮多行原子编辑」语义所需的最小数据库事务能力，
-- 不是新产品功能：业务校验（生命周期门控、窗口领域校验、boundary、可行性、
-- 零自由度、说明/实际时间格式、混合字段限制、no-clear）全部仍由 Python 层
-- 在调用前完成（validation-before-write）；本函数只负责：
--   * patch 硬白名单（未知 key 直接拒绝，绝不动态拼接 SQL）；
--   * 按稳定 id 顺序 FOR UPDATE 行锁（避免并发反向锁对死锁）；
--   * round 身份确认（两行存在、同 task、同 round_key、同 phase_group、
--     构成 start/end 两阶段 hollow pair）；
--   * 锁内生命周期二次校验（最终修复问题 1）：Python 检查与 RPC 执行之间
--     存在并发窗口——锁内重新确认两行仍处于可编辑生命周期，任意阶段不可
--     编辑则整个 RPC 失败、零写入。两档门：
--       - 严格门（窗口/预估编辑，补丁不携带 status）：两行均为尚未开始的
--         开放状态（pending/deferred）且无任何开始/处理/关闭事实；
--       - 宽松门（补丁携带 status 的开放状态流转，如延后）：两行仍为开放
--         状态且无关闭事实（closed_at/handled_at 为空）——执行中/部分完成
--         仍可延后（既有语义），并发完成/关闭则拒绝；
--     携带 status 时其值必须是开放状态——本函数永远不能写入终态。
--   * 窗口一致性（最终修复问题 7）：任一补丁触及窗口字段时，两阶段生效后
--     的窗口必须一致，不一致整体失败。
--   * 两行各自消费自己的补丁（原子事务不等于两行 payload 相同；RPC 不主动
--     扩大任何一行的写集合）；
--   * 任一失败整体 rollback（函数体无异常吞没，PL/pgSQL 异常自动回滚）。
-- sibling 白名单刻意小于 target：联动真正必要的时间/所有权字段 + updated_at
-- 与展示周期一致字段；partial_note / actual_* / partial_at / handled_at /
-- closed_at / status / round_key / schedule_date / fixed_due_at /
-- fixed_expires_at 等生命周期与身份字段永不通过 sibling 补丁写入。
-- 重放安全：仅 create or replace function；不新增表列 / 触发器 / 数据回填。

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

    -- ── expected snapshot 复核（最终修复问题 3） ───────────────────
    -- 重算以读取快照计算排程结果；锁内复核本次计算所依赖的输入（窗口 /
    -- est 预态 / 所有权元组 / 状态）在写入时仍与快照一致，任一漂移 =
    -- 旧计算作废（stale schedule 不得写回）。固定字段清单，非通用框架。
    if p_expected is not null then
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
                or (e.value ? 'schedule_managed' and o.schedule_managed is distinct from (e.value->>'schedule_managed')::boolean))
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
