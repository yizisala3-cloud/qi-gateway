-- 创建请求幂等（2026-10-04 清单 #9 修复）：任务创建携带 Idempotency-Key
-- 时，请求身份与请求内容随任务行原子落库——结果未知的重试（响应丢失、
-- 代理重发）按同键收敛到同一任务，不再重复建任务。
--
-- * creation_request_key：本次创建的幂等身份；NULL = 无键创建（既有行为，
--   不收敛）。部分唯一索引兜底顺序与并发重试：同键第二次 INSERT 唯一
--   冲突，应用层按「重读 + 内容核对」收敛（同内容重放 / 不同内容 409）。
-- * creation_request_content：创建请求的规范化语义内容快照（校验归一化
--   后的用户输入，服务端缺省注入前），同键同内容重放、同键不同内容拒绝。
-- * creation_feedback：创建事件的真实反馈（first_round_skipped /
--   schedule_conflict），重放返回创建时的口径，不按重放时刻重算。
begin;

alter table public.planning_task
    add column if not exists creation_request_key text,
    add column if not exists creation_request_content jsonb,
    add column if not exists creation_feedback jsonb;

create unique index if not exists planning_task_creation_key_uq
    on public.planning_task (creation_request_key)
    where creation_request_key is not null;

comment on column public.planning_task.creation_request_key is
    '本次创建的幂等请求键（清单 #9）；NULL = 无键创建。同键唯一（部分唯一索引 '
    'planning_task_creation_key_uq），结果未知的重试按键收敛到同一任务。';
comment on column public.planning_task.creation_request_content is
    '创建请求的规范化语义内容快照（jsonb）；同键同内容重放、同键不同内容 409。';
comment on column public.planning_task.creation_feedback is
    '创建事件真实反馈 {first_round_skipped, schedule_conflict}；重放按创建口径返回。';

commit;
