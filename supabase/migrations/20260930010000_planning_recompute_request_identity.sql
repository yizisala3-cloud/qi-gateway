-- 批次 6 收尾（A1，2026-09-30）：重算请求必须拥有真正唯一的消费身份。
--
-- 背景：requested_at 来自业务 now 捕获，两次业务操作可能捕获同一个时间戳
-- T（同一秒内的两次用户操作、或两次请求使用同一业务时间）。原实现以
-- requested_at 等值条件清除等待标记：旧 recompute 开始时捕获 T，执行期间
-- 并发登记的新请求（requested_at 同为 T）会在旧 recompute 完成时被等值
-- 条件一并命中清除——新请求被旧消费吞掉，最终新排序不会自动重算。
--
-- 方案（最小实现，不建通用版本框架）：每个新请求登记时由数据库原子生成
-- uuid 消费身份 request_token（SQL 函数内 gen_random_uuid，无 Python
-- 读-改-写）；清除 RPC 只命中 token 等值的行。同一时间戳的两次登记必然
-- 得到不同 token：旧 recompute 完成⽐时按捕获 token 清除 → 数据库当前为
-- 新 token → 0 rows，新请求保留给下一轮消费。
--
-- requested_at 继续保存（展示等待时长 / 原因），但不再是消费身份。
-- 重放安全：仅 add column if not exists + create or replace function；
-- 未在 production / Supabase 执行。

begin;

alter table public.planning_recompute_state
    add column if not exists request_token uuid;

comment on column public.planning_recompute_state.request_token is
    '当前待处理重算请求的消费身份（数据库原子生成，批次 6 A1）；清除仅命中 token 等值行';

-- 登记新请求：token 在函数体内生成——每次调用必然产生与前一请求不同的
-- 消费身份，即使两次调用的 p_requested_at 完全相同。
create or replace function public.planning_request_recompute(
    p_reason text,
    p_requested_at timestamptz
) returns uuid
language plpgsql as $$
declare
    v_token uuid;
begin
    v_token := gen_random_uuid();
    insert into public.planning_recompute_state
        (id, requested_at, reason, request_token, updated_at)
    values (1, p_requested_at, left(p_reason, 100), v_token, now())
    on conflict (id) do update
        set requested_at = excluded.requested_at,
            reason = excluded.reason,
            request_token = excluded.request_token,
            updated_at = now();
    return v_token;
end;
$$;

-- 条件清除：只消费开始时捕获的那一版请求。token 不等值（执行期间已产生
-- 新请求）时命中 0 行，标记保留给下一轮；token 为 NULL（捕获时本无待处理
-- 请求）时同样不清除。
create or replace function public.planning_clear_recompute_mark(
    p_request_token uuid
) returns integer
language plpgsql as $$
declare
    v_cleared integer := 0;
begin
    if p_request_token is null then
        return 0;
    end if;
    update public.planning_recompute_state
       set requested_at = null,
           reason = null,
           request_token = null,
           updated_at = now()
     where id = 1
       and request_token = p_request_token;
    get diagnostics v_cleared = row_count;
    return v_cleared;
end;
$$;

commit;
