-- 批次 9（2026-09-30）：boundary/window 合法性写入互斥守卫
-- （§5.2.2 / §6.7；批次 9 Review HIGH #3 / HIGH #4）。
--
-- 不变量：任意成功提交后，所有启用中（is_active）任务的双侧模板窗口
-- 都必须合法于当前正式 boundary（app_settings 的 configured 值——
-- boundary RPC 在同一事务内原子切换它，与 Python 创建/编辑校验同源）。
--
-- 批次 9 Review 确认的两个绕过路径：
-- * HIGH #3：boundary RPC 校验与提交之间存在并发窗口——并发的 active
--   任务创建不触碰 boundary 状态行/任务行锁，可以带着跨越「准备生效的
--   新 boundary」的窗口插入，与 RPC 提交交错留下非法组合；
-- * HIGH #4：inactive 任务不参与 boundary 全量扫描（§5.2.2），重新启用
--   （is_active false→true）此前无任何 boundary 重校验——非法模板窗口
--   直接复活，generation 随后生成跨越 boundary 的冻结实例。
--
-- 方案（最小并发互斥，不建通用锁框架）：
-- * 同一把事务级 advisory lock（hashtextextended('planning.refresh_
--   boundary_state', 0)）串行化双方——boundary RPC（步骤 0，见
--   20260930020000）与下方守卫触发器；两事务不再能穿过彼此的校验窗口；
-- * 守卫在**持锁之后**按已提交 boundary 重校验新行（校验与写入之间不再
--   有可交错窗口）；boundary RPC 的状态写入先于其任务更新（同迁移集），
--   守卫在其任务 UPDATE 上读到本事务的新 boundary（同事务重入锁无害）。
--
-- 校验范围（§6.7 单侧约束不构成区间）：
-- * 仅 is_active 且 window_start_tod / window_end_tod 双侧非空的行参与；
--   inactive 行零干预（停用不参与校验，§5.2.2）；单侧 / 无窗口放行；
-- * start == end 形状非法由既有 CHECK（planning_task_window_tod_shape_
--   check）拒绝，守卫放行交给 CHECK（BEFORE 触发器先于约束求值）；
-- * 跨越判定与 Python / boundary RPC 同一规则：boundary 落在开始/结束
--   时刻的顺时针开区间内即非法（端点接触合法）；
-- * UPDATE 早退：窗口两端与 is_active 均未变化（生成游标、内容、规则等
--   写入）不取锁、不校验——只有真正改变 boundary/window 合法性的写入
--   （创建、模板窗口编辑、inactive→active）参与串行化。
--
-- 重放安全：仅 create trigger + create or replace function；不新增表列 /
-- 约束 / 数据回填；未在 production / Supabase 执行。

begin;

create or replace function public.planning_validate_window_boundary_guard()
returns trigger
language plpgsql as $$
declare
    v_boundary_text text;
    v_boundary time;
begin
    if tg_op = 'UPDATE'
       and new.window_start_tod is not distinct from old.window_start_tod
       and new.window_end_tod is not distinct from old.window_end_tod
       and new.is_active is not distinct from old.is_active then
        return new;
    end if;
    if not new.is_active
       or new.window_start_tod is null
       or new.window_end_tod is null
       or new.window_start_tod = new.window_end_tod then
        return new;
    end if;
    -- HIGH #3：与 boundary RPC 互斥——持锁后再读 boundary，杜绝
    -- 「校验用旧值、写入落在新 boundary 下」的交错。
    perform pg_advisory_xact_lock(
        hashtextextended('planning.refresh_boundary_state', 0));
    select coalesce(value->>'boundary', '06:00') into v_boundary_text
      from public.app_settings
     where key = 'planning.refresh_boundary_state';
    v_boundary := coalesce(v_boundary_text, '06:00')::time;
    if (new.window_start_tod < new.window_end_tod
            and v_boundary > new.window_start_tod
            and v_boundary < new.window_end_tod)
       or (new.window_start_tod > new.window_end_tod
            and (v_boundary > new.window_start_tod
                 or v_boundary < new.window_end_tod)) then
        raise exception using
            errcode = 'P0001',
            message = format(
                'planning window template crosses the daily refresh boundary %s',
                to_char(v_boundary, 'HH24:MI'));
    end if;
    return new;
end;
$$;

create trigger planning_boundary_window_guard
before insert or update on public.planning_task
for each row execute function public.planning_validate_window_boundary_guard();

comment on function public.planning_validate_window_boundary_guard() is
    '批次 9：启用中任务的双侧模板窗口禁止跨越 configured daily refresh boundary；与 planning_update_cycle_boundary 经同一 advisory lock 串行化（HIGH #3 并发创建 / HIGH #4 重新启用）';

commit;
