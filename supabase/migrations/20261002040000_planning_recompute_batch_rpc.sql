-- 后续待修复清单 #6（2026-10-02）：手动 / 自动重算的整批事务与完整计算
-- 输入复核。
-- 缺口 A（故障注入复现）：recompute_today 逐个普通行（条件 UPDATE）、逐个
-- 中空轮（round patch RPC）提交——两任务原排程 08:00 / 08:30，09:00 重算
-- 注入第 2 行写失败，实得 09:00 / 08:30，第一行未回滚（§19.1 的混合状态）。
-- 缺口 B：待写行自身守卫不能证明整次计算依赖仍有效——固定槽、其它行状态 /
-- 排序、参与计算的成员集合等任一在读取后漂移，旧计算结果不应落库。
-- 修复（本函数）：一次 RPC 内完成——
--   * 全量 expected 快照复核（语句 4）：快照必须覆盖**全部参与计算行**
--     （不只待写行），每行含状态 / 生命周期事实（#25：actual_start /
--     actual_end / partial_at / handled_at / closed_at 显式参与复核）/
--     所有权元组 / 冻结窗口 / 既有 est 预态 / sort_order（#13 同一必填键
--     契约）；任一行缺失或漂移 → PC001 整批放弃；
--   * 稳定锁序（语句 3）：全部参与计算行按 id 升序加锁（与 round patch 的
--     id 升序、#20/#1 的锁序纪律一致）；
--   * 普通行补丁（语句 5）：est / 所有权元组 / nominal_start / updated_at
--     键硬白名单，patch 缺键 = 保持现值；#25：写入时锁内重核同一可排程
--     条件（状态 pending + 生命周期事实全空 + 可重排所有权元组）——旧
--     _conditional_schedulable_update 的生命周期门在批量路径的承接，待写
--     行任一条件未命中 → PC001 整批放弃；
--   * 中空轮补丁（语句 6）：逐轮复用 planning_patch_occurrence_round（行
--     已在语句 3 锁定、expected 已在语句 4 全量复核，传 NULL 跳过其内部
--     expected 复核）——其白名单 / 严格门 / 窗口一致性 / 原子回滚逐字保留；
--   * 任一失败整体回滚：不存在「部分行新排程、部分行旧排程」（§19.1）。
-- 应用层契约：recompute_today 一次性提交全部写集合；PC001 → 整批放弃
-- （updated=0、stale_skipped=待写行数，重算等待标记保留，下一次重算按最新
-- 输入重新执行）；基础设施失败如实向上传播，不伪装成功。
-- 部署状态：新函数（此前无签名，无 overload 风险）；重放安全（仅 create
-- or replace）；不新增表列 / 触发器 / 数据回填。

begin;

create or replace function public.planning_apply_recompute_batch(
    p_expected jsonb,
    p_singles jsonb,
    p_rounds jsonb
) returns jsonb
language plpgsql as $$
declare
    v_entry jsonb;
    v_written jsonb := '[]'::jsonb;
    v_count bigint;
begin
    -- 语句 1：expected 快照形状（#13 同契约：对象数组、每元素携带全部必填
    -- 键，含 sort_order——排序是 compute_schedule 的遍历输入）。
    if jsonb_typeof(p_expected) is distinct from 'array'
       or jsonb_array_length(p_expected) < 1 then
        raise exception 'planning_apply_recompute_batch: invalid expected snapshot';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(p_expected) as e
        where jsonb_typeof(e.value) is distinct from 'object'
           or not e.value ?& array[
               'id', 'status', 'window_start_at', 'window_end_at',
               'est_start', 'est_end', 'estimated_time_source',
               'fixed_source', 'is_fixed', 'schedule_managed', 'sort_order',
               'actual_start', 'actual_end', 'partial_at', 'handled_at',
               'closed_at']
    ) then
        raise exception 'planning_apply_recompute_batch: invalid expected snapshot';
    end if;

    -- 语句 2：p_singles / p_rounds 形状与键硬白名单；待写行 id 必须已在
    -- expected 集合内（未参与复核的行不得写）。
    if jsonb_typeof(p_singles) is distinct from 'array'
       or jsonb_typeof(p_rounds) is distinct from 'array' then
        raise exception 'planning_apply_recompute_batch: invalid batch payload';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(p_singles) as e
        where jsonb_typeof(e.value) is distinct from 'object'
           or not e.value ?& array['id', 'updated_at']
           or exists (
                select 1 from jsonb_object_keys(e.value) as k
                where k not in ('id', 'est_start', 'est_end', 'nominal_start',
                                'estimated_time_source', 'fixed_source',
                                'schedule_managed', 'is_fixed', 'updated_at'))
           or not exists (
                select 1 from jsonb_array_elements(p_expected) as x
                where (x.value->>'id')::bigint = (e.value->>'id')::bigint)
    ) then
        raise exception 'planning_apply_recompute_batch: invalid single write';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(p_rounds) as e
        where jsonb_typeof(e.value) is distinct from 'object'
           or not e.value ?& array['target_id', 'sibling_id',
                                   'target_patch', 'sibling_patch']
           or exists (
                select 1 from jsonb_object_keys(e.value) as k
                where k not in ('target_id', 'sibling_id',
                                'target_patch', 'sibling_patch'))
           or not exists (
                select 1 from jsonb_array_elements(p_expected) as x
                where (x.value->>'id')::bigint = (e.value->>'target_id')::bigint)
           or not exists (
                select 1 from jsonb_array_elements(p_expected) as x
                where (x.value->>'id')::bigint = (e.value->>'sibling_id')::bigint)
    ) then
        raise exception 'planning_apply_recompute_batch: invalid round write';
    end if;

    -- 语句 3：锁全部参与计算行（id 升序——与同轮编辑同一锁序纪律）。
    perform 1
      from public.planning_occurrence
     where id in (
            select (e.value->>'id')::bigint
              from jsonb_array_elements(p_expected) as e)
     order by id
     for update;

    -- 语句 4：全量快照复核（缺口 B）——任一参与计算行在读取后漂移（状态 /
    -- 生命周期事实（#25：读取后并发补录的实际开始 / 结束、部分完成、处理
    -- 与关闭事实同样使计算输入失效）/ 窗口 / est 预态 / 所有权元组 / 排
    -- 序），或行消失，整批放弃（§19.1：旧计算输入已失效，不得部分落库）。
    select count(*) into v_count
      from jsonb_array_elements(p_expected) as e
      join public.planning_occurrence o on o.id = (e.value->>'id')::bigint;
    if v_count <> jsonb_array_length(p_expected) then
        raise exception 'planning_apply_recompute_batch: schedule inputs drifted (stale recompute result)'
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
            or (e.value ? 'sort_order' and o.sort_order is distinct from (e.value->>'sort_order')::integer)
            or (e.value ? 'actual_start' and o.actual_start is distinct from (e.value->>'actual_start')::timestamptz)
            or (e.value ? 'actual_end' and o.actual_end is distinct from (e.value->>'actual_end')::timestamptz)
            or (e.value ? 'partial_at' and o.partial_at is distinct from (e.value->>'partial_at')::timestamptz)
            or (e.value ? 'handled_at' and o.handled_at is distinct from (e.value->>'handled_at')::timestamptz)
            or (e.value ? 'closed_at' and o.closed_at is distinct from (e.value->>'closed_at')::timestamptz))
    ) then
        raise exception 'planning_apply_recompute_batch: schedule inputs drifted (stale recompute result)'
            using errcode = 'PC001';
    end if;

    -- 语句 5：普通行 / 单阶段补丁（est / 所有权元组 / nominal_start /
    -- updated_at；patch 缺键 = 保持现值）。#25：写入时锁内重核同一可排程
    -- 条件（与 _freely_schedulable 同源：状态 pending + 生命周期事实全空 +
    -- 可重排所有权元组）——expected 复核只证明「行与读取时一致」，本条件
    -- 证明「待写行确实可被自动重排」；任一未命中 → PC001 整批放弃（旧
    -- _conditional_schedulable_update 生命周期门在批量路径的承接）。
    for v_entry in select * from jsonb_array_elements(p_singles) loop
        update public.planning_occurrence set
            est_start = case when v_entry ? 'est_start'
                then (v_entry->>'est_start')::timestamptz else est_start end,
            est_end = case when v_entry ? 'est_end'
                then (v_entry->>'est_end')::timestamptz else est_end end,
            nominal_start = case when v_entry ? 'nominal_start'
                then (v_entry->>'nominal_start')::timestamptz else nominal_start end,
            estimated_time_source = case when v_entry ? 'estimated_time_source'
                then v_entry->>'estimated_time_source' else estimated_time_source end,
            fixed_source = case when v_entry ? 'fixed_source'
                then v_entry->>'fixed_source' else fixed_source end,
            schedule_managed = case when v_entry ? 'schedule_managed'
                then (v_entry->>'schedule_managed')::boolean else schedule_managed end,
            is_fixed = case when v_entry ? 'is_fixed'
                then (v_entry->>'is_fixed')::boolean else is_fixed end,
            updated_at = case when v_entry ? 'updated_at'
                then (v_entry->>'updated_at')::timestamptz else updated_at end
          where id = (v_entry->>'id')::bigint
            and status = 'pending'
            and actual_start is null
            and actual_end is null
            and partial_at is null
            and handled_at is null
            and closed_at is null
            and is_fixed = false
            and schedule_managed = true
            and fixed_source is null
            and estimated_time_source in ('unassigned', 'automatic', 'rule');
        if not found then
            raise exception 'planning_apply_recompute_batch: schedule inputs drifted (stale recompute result)'
                using errcode = 'PC001';
        end if;
        v_written := v_written || to_jsonb((v_entry->>'id')::bigint);
    end loop;

    -- 语句 6：中空轮补丁——逐轮复用 planning_patch_occurrence_round（行已
    -- 在语句 3 锁定、expected 已在语句 4 全量复核，传 NULL 跳过其内部
    -- expected 复核；其硬白名单 / 锁内生命周期门 / 窗口一致性 / 原子回滚
    -- 逐字保留）。任一轮拒绝 → 整批回滚。
    for v_entry in select * from jsonb_array_elements(p_rounds) loop
        perform public.planning_patch_occurrence_round(
            (v_entry->>'target_id')::bigint,
            (v_entry->>'sibling_id')::bigint,
            v_entry->'target_patch',
            v_entry->'sibling_patch',
            null);
        v_written := v_written || to_jsonb((v_entry->>'target_id')::bigint)
                               || to_jsonb((v_entry->>'sibling_id')::bigint);
    end loop;

    return v_written;
end;
$$;

commit;
