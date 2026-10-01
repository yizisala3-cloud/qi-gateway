-- 后续待修复清单 #1（2026-10-02）：拆分的条件关闭、1～10 个单次待办创建
-- 与 after_completion 基准推进收进**同一数据库事务**。
-- 原缺陷（故障注入复现）：Python 路径分三次独立提交——关闭原轮（提交）、
-- 逐个 INSERT once 任务（各自提交）、推进基准（提交）。注入第 2 次 task
-- INSERT 失败：原轮已 discarded_this、第 1 个 once 已持久化、第 2 个未建；
-- 重试被「该待办已被并发操作关闭，不能再次拆分」拒绝，缺口不能自动补完。
-- 与批次 6 二轮裁决 F 同理，PostgREST 资源 API 无跨行 / 跨表不同值原子写
-- 能力，故经 user 既有的最小 RPC 模式新增本函数（非新产品功能，实现既有
-- 「拆分 = 一次完整业务操作」语义的事务能力）。
-- 语义保持（需求 §26 / §35）：
--   * 仅开放实例可拆分：条件关闭（开放状态集合内命中），已关闭 / 已超时 /
--     已被并发拆分收口 → 0 行命中 → PC001 并发拒绝（应用层映射 409，
--     重复 / 双击请求不产生第二组拆分任务）；
--   * 原轮以 discarded_this + handled_at（拆分处理时间）关闭，同轮中空
--     两阶段一起关闭；已有 partial / actual 事实不被覆盖（关闭语句不触及
--     partial_* / actual_* 列）；
--   * after_completion 从拆分处理时间推进下一轮：锁内按关闭后的轮次行
--     重算（全部行均有 handled_at 时取 max），与旧「关闭后读行再写基准」
--     同语义，且基准写入失败 / 被跳过时 _after_completion_due 仍可从轮次
--     行自愈；fixed refresh 时间轴不动（非 after_completion 传 NULL）；
--   * 拆出任务均为 task_type='once'：不继承周期规则、无父子任务 / 血缘；
--   * 校验先行：parts 形状（1～10 个对象、恰含 content 与 estimated_minutes、
--     内容非空 ≤500 字符、耗时 1–1440 整数）在第一个写之前完成，违规零写入。
-- 锁序（与 #20 同一纪律）：任务行 → 同轮开放行按 id 升序 → 关闭 / 插入，
-- 与 planning_patch_occurrence_round 的 id 升序锁两阶段一致，不引入新的
-- 反向锁序。
-- Python 侧保留全部业务校验（validation-before-write）；即时生成与重算
-- 登记仍在提交后执行（与 API 成功反馈一致）。
-- 部署状态：新函数（此前无任何签名，无 overload 风险）；重放安全（仅
-- create or replace）；不新增表列 / 触发器 / 数据回填。

begin;

create or replace function public.planning_split_occurrence(
    p_task_id bigint,
    p_round_key text,
    p_target_date date,
    p_now timestamptz,
    p_parts jsonb,
    p_after_completion_days integer default null
) returns jsonb
language plpgsql as $$
declare
    v_ids jsonb := '[]'::jsonb;
    v_part jsonb;
    v_id bigint;
    v_handled timestamptz;
begin
    -- 语句 1：锁任务行（与 planning_discard_task 同序：任务行先于实例行）。
    if not exists (
        select 1 from public.planning_task
        where id = p_task_id
        for update
    ) then
        raise exception 'planning_split_occurrence: task not found';
    end if;

    -- 语句 2：parts 形状校验（写前完整校验，违规零写入）。content 上限
    -- 500 与应用层 MAX_CONTENT_LENGTH 同源；耗时 1–1440 与
    -- parse_duration_shorthand 契约同源。
    if jsonb_typeof(p_parts) is distinct from 'array'
       or jsonb_array_length(p_parts) < 1
       or jsonb_array_length(p_parts) > 10 then
        raise exception 'planning_split_occurrence: parts must be an array of 1-10 items';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(p_parts) as e
        where jsonb_typeof(e.value) is distinct from 'object'
           or exists (
                select 1 from jsonb_object_keys(e.value) as k
                where k not in ('content', 'estimated_minutes'))
           or not e.value ?& array['content', 'estimated_minutes']
           or jsonb_typeof(e.value->'content') is distinct from 'string'
           or btrim(e.value->>'content') = ''
           or char_length(e.value->>'content') > 500
           or jsonb_typeof(e.value->'estimated_minutes') is distinct from 'number'
           or (e.value->>'estimated_minutes')::numeric
              <> floor((e.value->>'estimated_minutes')::numeric)
           or (e.value->>'estimated_minutes')::int < 1
           or (e.value->>'estimated_minutes')::int > 1440
    ) then
        raise exception 'planning_split_occurrence: invalid part item';
    end if;
    if p_after_completion_days is not null
       and (p_after_completion_days < 1 or p_after_completion_days > 365) then
        raise exception 'planning_split_occurrence: invalid after_completion interval';
    end if;

    -- 语句 3：按稳定 id 序锁同轮全部开放行（与 round patch 的 id 升序一致，
    -- 条件关闭不再产生新的锁等待；行锁获取时 EvalPlanQual 以最新已提交
    -- 版本复核开放谓词）。
    perform 1
      from public.planning_occurrence
     where task_id = p_task_id
       and round_key = p_round_key
       and status in ('pending', 'in_progress', 'deferred', 'partial')
     order by id
     for update;

    -- 语句 4：条件关闭当前轮（discarded_this + handled_at 拆分处理时间；
    -- 同轮中空两阶段由同一语句一起关闭）。0 行命中 = 已被并发操作关闭 /
    -- 已拆分收口 → PC001 并发拒绝，整体回滚、零任务创建。
    update public.planning_occurrence
       set status = 'discarded_this', closed_at = p_now,
           handled_at = p_now, updated_at = p_now
     where task_id = p_task_id
       and round_key = p_round_key
       and status in ('pending', 'in_progress', 'deferred', 'partial');
    if not found then
        raise exception 'planning_split_occurrence: round already closed (concurrent change)'
            using errcode = 'PC001';
    end if;

    -- 语句 5：创建 1～10 个单次待办（同一事务；任一失败整体回滚——原轮
    -- 关闭、已建任务与基准更新全部不落库，重试可完整重放）。
    for v_part in select * from jsonb_array_elements(p_parts) loop
        insert into public.planning_task (
            content, task_type, time_mode, estimated_minutes,
            target_date, refresh_mode, is_active, created_at, updated_at
        ) values (
            btrim(v_part->>'content'), 'once', 'duration',
            (v_part->>'estimated_minutes')::int, p_target_date, 'none',
            true, p_now, p_now
        ) returning id into v_id;
        v_ids := v_ids || to_jsonb(v_id);
    end loop;

    -- 语句 6（可选）：after_completion 基准推进——锁内按关闭后的轮次行
    -- 重算（与旧「关闭后读行、全部行均有 handled_at 才写」同语义：基准
    -- 来自轮次行事实，不使用请求携带值）；任务行已锁、is_active 稳定。
    -- 非 after_completion 调用传 NULL，不做任何事；fixed refresh 时间轴
    -- 不动。基准即便缺失，应用层 _after_completion_due 仍可从轮次行自愈。
    if p_after_completion_days is not null then
        v_handled := (
            select max(handled_at)
              from public.planning_occurrence
             where task_id = p_task_id
               and round_key = p_round_key
               and handled_at is not null
        );
        if v_handled is not null
           and not exists (
                select 1
                  from public.planning_occurrence
                 where task_id = p_task_id
                   and round_key = p_round_key
                   and handled_at is null
           ) then
            update public.planning_task
               set last_handled_at = v_handled,
                   refresh_next_due_at = v_handled
                       + make_interval(days => p_after_completion_days),
                   updated_at = p_now
             where id = p_task_id
               and is_active;
            if not found then
                raise exception 'planning_split_occurrence: task already inactive (concurrent change)'
                    using errcode = 'PC001';
            end if;
        end if;
    end if;

    return v_ids;
end;
$$;

commit;
