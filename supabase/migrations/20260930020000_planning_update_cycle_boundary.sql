-- 批次 7（2026-09-30）：修改每日刷新 boundary 与关联待办窗口调整的原子 RPC
-- （一期规范 §5.2.2 / §5.2.1 / 不变量 39；施工计划 §五、§3.5）。
--
-- 职责边界：只服务 boundary 这一个业务操作，不是通用配置事务框架。
-- 单事务内完成：
--   1. 锁定 / 读取当前 boundary 状态行（app_settings 单行；行不存在时
--      以默认状态参与 CAS 与校验，缺省行的补插在全部校验通过后、任何
--      任务写入之前执行——保证拒绝路径严格零写入，且首次修改的并发
--      stale 判定不会落在任务 UPDATE 之后（批次 9 Review HIGH 修复））；
--   2. 状态 CAS：调用方基于其读取的状态预计算过渡计划；状态已被其他
--      worker 改变时返回 stale_state（零写入），由应用层重新计划；
--   3. 调整项 payload 校验（完整双端窗口形状，tod 可空、start==end 拒绝）；
--   4. 以准备生效的新 boundary 重新全量校验全部启用中任务（is_active）：
--      本请求调整者用提交的新值、未调整者用数据库当前值；双侧窗口
--      start==end 无效；boundary 落在开始/结束时刻的顺时针开区间内
--      即跨越（端点接触合法；单侧约束不构成区间不做跨越校验）——
--      与 gateway/planning_window.py 的 window_crosses_boundary 同一规则；
--      暂停刷新（refresh_enabled=false）不豁免；已生成实例的冻结窗口
--      不参与、不受影响（本函数不触碰 planning_occurrence）；
--   5. 仍有任何非法窗口 → 返回 conflicts 清单（零写入，整体拒绝）；
--   5.5 缺省状态行补插 + CAS 重核（仍在任何写入之前）；
--   6. 写入 boundary 过渡状态（transition / absorbed 结构由应用层按
--      BoundaryTransition 预计算传入，SQL 不重算过渡）——先于任务更新，
--      使步骤 7 的守卫触发器按新 boundary 重校验；
--   7. 更新关联任务的窗口模板（仅 window_*_tod + updated_at；不触碰
--      target_date / 轮次身份 / 生成游标 / planning_occurrence）；
--   8. 任一步失败（约束违反 / 注入异常）→ 整体回滚，零部分提交。
--
-- 并发不变量（批次 9 Review HIGH）：boundary 修改与 active 任务创建 /
-- 模板窗口编辑 / inactive→active 经同一把事务级 advisory lock 串行化，
-- 后三类写入由 planning_boundary_window_guard 守卫在持锁后按已提交
-- boundary 重校验——不存在「boundary 与任务写入穿过彼此校验窗口」的
-- 交错（真库双连接验证见 pgserver 套件）。
--
-- 重放安全：仅 create or replace function；不新增表列 / 触发器 / 数据
-- 回填；未在 production / Supabase 执行。

begin;

create or replace function public.planning_update_cycle_boundary(
    p_new_boundary time,
    p_expected_state jsonb,
    p_transition jsonb,
    p_absorbed jsonb,
    p_adjustments jsonb
) returns jsonb
language plpgsql as $$
declare
    v_key constant text := 'planning.refresh_boundary_state';
    v_state jsonb;
    v_default_state jsonb :=
        '{"boundary": "06:00", "transition": null, "absorbed": []}'::jsonb;
    v_adjustment jsonb;
    v_adjustment_keys text[];
    v_task_id bigint;
    v_adj_start time;
    v_adj_end time;
    v_task record;
    v_eff_start time;
    v_eff_end time;
    v_conflicts jsonb := '[]'::jsonb;
    v_updated integer := 0;
    v_row_count bigint;
    v_state_found boolean;
    v_adjustment_ids bigint[];
begin
    -- ── 0. boundary 写入互斥锁（批次 9 Review HIGH：并发窗口关闭） ────
    -- 全部「会改变 boundary/window 合法性的写入」（boundary 修改、active
    -- 任务创建、模板窗口编辑、inactive→active）经同一把事务级 advisory
    -- lock 串行化：任务侧写入由 planning_boundary_window_guard 触发器在
    -- 持锁后按已提交 boundary 重校验，不能穿过本校验窗口。同事务重入
    -- （本 RPC 步骤 6 的任务 UPDATE 再次触发守卫）无害。
    perform pg_advisory_xact_lock(
        hashtextextended('planning.refresh_boundary_state', 0));

    -- ── 1. 锁定 / 读取当前状态行 ────────────────────────────────────
    -- 行不存在时先按「缺省状态」参与 CAS 与校验；缺省行的补插推迟到
    -- 提交阶段（校验全部通过、确定要写入时）——冲突 / stale 返回路径
    -- 严格零写入。
    select value into v_state
      from public.app_settings
     where key = v_key
     for update;
    v_state_found := found;

    -- ── 2. 状态 CAS：预计算所依据的状态必须仍是当前状态 ─────────────
    -- 两个 worker 同时修改 boundary：后提交者的计划基于旧状态 → CAS 未命中
    -- → stale_state（零写入），应用层按落库后的最新状态重新计划。
    if p_expected_state is null
       or coalesce(v_state->>'boundary', '06:00')
          <> coalesce(p_expected_state->>'boundary', '06:00')
       or coalesce(v_state->'transition'->>'spanning_key', '')::text
          <> coalesce(p_expected_state->'transition'->>'spanning_key', '')::text
       or coalesce(v_state->'transition'->>'change_at', '')::text
          <> coalesce(p_expected_state->'transition'->>'change_at', '')::text then
        return jsonb_build_object('status', 'stale_state');
    end if;

    -- ── 3. 调整项 payload 校验（完整双端窗口；形状非法整体拒绝） ────
    if p_adjustments is null then
        p_adjustments := '[]'::jsonb;
    end if;
    if jsonb_typeof(p_adjustments) <> 'array' then
        raise exception 'planning_update_cycle_boundary: adjustments must be an array';
    end if;
    for v_adjustment in select * from jsonb_array_elements(p_adjustments) loop
        if jsonb_typeof(v_adjustment) <> 'object' then
            raise exception 'planning_update_cycle_boundary: adjustment must be an object';
        end if;
        select array_agg(k) into v_adjustment_keys
          from jsonb_object_keys(v_adjustment) as k;
        if exists (
            select 1 from unnest(v_adjustment_keys) as k
            where k not in ('task_id', 'window_start_tod', 'window_end_tod')
        ) then
            raise exception 'planning_update_cycle_boundary: adjustment contains unsupported field';
        end if;
        if not v_adjustment ? 'task_id'
           or not v_adjustment ? 'window_start_tod'
           or not v_adjustment ? 'window_end_tod' then
            raise exception 'planning_update_cycle_boundary: adjustment requires task_id and both window ends';
        end if;
        v_task_id := (v_adjustment->>'task_id')::bigint;
        if v_task_id is null or v_task_id = any(v_adjustment_ids) then
            raise exception 'planning_update_cycle_boundary: duplicate or missing adjustment task';
        end if;
        v_adjustment_ids := array_append(v_adjustment_ids, v_task_id);
        if jsonb_typeof(v_adjustment->'window_start_tod') = 'null' then
            v_adj_start := null;
        elsif jsonb_typeof(v_adjustment->'window_start_tod') = 'string' then
            v_adj_start := (v_adjustment->>'window_start_tod')::time;
        else
            raise exception 'planning_update_cycle_boundary: invalid window_start_tod';
        end if;
        if jsonb_typeof(v_adjustment->'window_end_tod') = 'null' then
            v_adj_end := null;
        elsif jsonb_typeof(v_adjustment->'window_end_tod') = 'string' then
            v_adj_end := (v_adjustment->>'window_end_tod')::time;
        else
            raise exception 'planning_update_cycle_boundary: invalid window_end_tod';
        end if;
        if v_adj_start is not null and v_adj_end is not null
           and v_adj_start = v_adj_end then
            raise exception 'planning_update_cycle_boundary: window start and end must differ';
        end if;
    end loop;

    -- ── 4. 锁定启用中任务行并按新 boundary 全量校验 ─────────────────
    -- 锁集合：启用中且带任一窗口端的任务 + 本请求调整的任务（稳定 id 序
    -- 防死锁）。校验对象：调整者用提交值、未调整者用数据库当前值；
    -- 双侧窗口才做跨越判断（单侧约束不构成区间，§6.7）。
    for v_task in
        select t.id, t.content, t.window_start_tod, t.window_end_tod
          from public.planning_task t
         where t.is_active
           and (
                 (t.window_start_tod is not null or t.window_end_tod is not null)
                 or t.id = any(coalesce(v_adjustment_ids, '{}'::bigint[]))
               )
         order by t.id
         for update
    loop
        v_eff_start := v_task.window_start_tod;
        v_eff_end := v_task.window_end_tod;
        if v_task.id = any(coalesce(v_adjustment_ids, '{}'::bigint[])) then
            select (a->>'window_start_tod')::time,
                   case when jsonb_typeof(a->'window_end_tod') = 'null'
                        then null else (a->>'window_end_tod')::time end
              into v_eff_start, v_eff_end
              from jsonb_array_elements(p_adjustments) a
             where (a->>'task_id')::bigint = v_task.id;
        end if;
        -- 双侧窗口：start == end 无效；boundary 在顺时针开区间内即跨越
        if v_eff_start is not null and v_eff_end is not null then
            if (v_eff_start < v_eff_end
                    and p_new_boundary > v_eff_start and p_new_boundary < v_eff_end)
               or (v_eff_start > v_eff_end
                    and (p_new_boundary > v_eff_start or p_new_boundary < v_eff_end)) then
                v_conflicts := v_conflicts || jsonb_build_object(
                    'task_id', v_task.id,
                    'content', v_task.content,
                    'window_start_tod', to_char(v_eff_start, 'HH24:MI'),
                    'window_end_tod', to_char(v_eff_end, 'HH24:MI'));
            end if;
        end if;
    end loop;

    -- ── 5. 存在跨越模板 → 整体拒绝（零写入） ────────────────────────
    if jsonb_array_length(v_conflicts) > 0 then
        return jsonb_build_object('status', 'conflicts', 'conflicts', v_conflicts);
    end if;

    -- ── 5.5 缺省行补插 + CAS 重核（必须在任何任务写入之前） ──────────
    -- 批次 9 Review HIGH（首写半写修复）：state 行不存在时的补插原本
    -- 推迟到任务更新之后——补插阻塞期间另一 worker 抢先建立并修改状态行
    -- 时，重核 stale 在任务 UPDATE 之后才返回，而 RETURN 不回滚事务，
    -- 任务窗口调整已被提交（被拒绝的请求留下半写）。补插 / 重核提前到
    -- 全部校验之后、任何写入之前：补插撞上并发首写时 on conflict 无操作
    -- （零写入），重核 stale 在任务更新前返回；自补成功时重核读到的是
    -- 本事务自己的缺省行，必然与缺省期望一致，不再存在「写入后拒绝」
    -- 路径。冲突 / stale / 校验异常路径严格零写入（异常整体回滚）。
    if not v_state_found then
        insert into public.app_settings (key, value)
        values (v_key, v_default_state)
        on conflict (key) do nothing;
        select value into v_state
          from public.app_settings
         where key = v_key
         for update;
        if coalesce(v_state->>'boundary', '06:00')
           <> coalesce(p_expected_state->>'boundary', '06:00')
           or coalesce(v_state->'transition'->>'spanning_key', '')::text
              <> coalesce(p_expected_state->'transition'->>'spanning_key', '')::text
           or coalesce(v_state->'transition'->>'change_at', '')::text
              <> coalesce(p_expected_state->'transition'->>'change_at', '')::text then
            return jsonb_build_object('status', 'stale_state');
        end if;
    end if;

    -- ── 6. 保存 boundary 过渡状态（transition / absorbed 预计算传入） ─
    -- 状态写入先于任务更新（批次 9 Review HIGH）：步骤 7 的任务 UPDATE
    -- 会触发 planning_boundary_window_guard 守卫，守卫按 app_settings 中的
    -- configured boundary 重校验——必须让它读到本事务写入的新 boundary，
    -- 否则「新 boundary 下合法、旧 boundary 下跨越」的调整会被守卫误拒。
    -- 同一事务内先后顺序不影响原子性；缺省行已在任何写入前补插（5.5）。
    update public.app_settings
       set value = jsonb_build_object(
               'boundary', to_char(p_new_boundary, 'HH24:MI'),
               'transition', p_transition,
               'absorbed', coalesce(p_absorbed, '[]'::jsonb)),
           updated_at = now()
     where key = v_key;

    -- ── 7. 更新关联任务的窗口模板（只写 window_*_tod + updated_at） ──
    -- 值无变化的幂等调整不产生行更新；get diagnostics 累计实际更新数。
    for v_adjustment in select * from jsonb_array_elements(p_adjustments) loop
        update public.planning_task
           set window_start_tod = case
                   when jsonb_typeof(v_adjustment->'window_start_tod') = 'null'
                   then null else (v_adjustment->>'window_start_tod')::time end,
               window_end_tod = case
                   when jsonb_typeof(v_adjustment->'window_end_tod') = 'null'
                   then null else (v_adjustment->>'window_end_tod')::time end,
               updated_at = now()
         where id = (v_adjustment->>'task_id')::bigint
           and is_active
           and (
                 window_start_tod is distinct from case
                     when jsonb_typeof(v_adjustment->'window_start_tod') = 'null'
                     then null else (v_adjustment->>'window_start_tod')::time end
                 or window_end_tod is distinct from case
                     when jsonb_typeof(v_adjustment->'window_end_tod') = 'null'
                     then null else (v_adjustment->>'window_end_tod')::time end
               );
        get diagnostics v_row_count = row_count;
        v_updated := v_updated + v_row_count;
    end loop;

    return jsonb_build_object('status', 'ok', 'updated_tasks', v_updated);
end;
$$;

commit;
