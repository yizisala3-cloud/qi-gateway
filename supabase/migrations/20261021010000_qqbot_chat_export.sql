-- QQ inbox/outbox lives in its own persistent SQLite ledger. This bridge only
-- appends real user messages and confirmed assistant messages to the existing
-- digest input, with database-level idempotence across ambiguous RPC responses.
-- Prepare and test locally; this task does not apply migrations to production.
begin;

create table if not exists public.qqbot_chat_exports (
    message_key text primary key,
    payload_hash text not null,
    chat_message_id bigint not null,
    created_at timestamptz not null default now()
);
alter table public.qqbot_chat_exports enable row level security;
revoke all on public.qqbot_chat_exports from public, anon, authenticated;
grant select, insert on public.qqbot_chat_exports to service_role;

create or replace function public.qqbot_publish_chat_message(
    p_message_key text, p_assistant_id text, p_conversation_id text,
    p_role text, p_content text, p_created_at timestamptz
) returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_hash text;
    v_old public.qqbot_chat_exports%rowtype;
    v_id bigint;
begin
    if p_message_key is null or p_message_key !~ '^qqbot:[a-f0-9]{64}$'
       or nullif(trim(p_assistant_id),'') is null or length(p_assistant_id)>200
       or p_conversation_id is null or p_conversation_id !~ '^qq:[1-9][0-9]{4,19}:private:[1-9][0-9]{4,19}$'
       or p_role is null or p_role not in ('user','assistant')
       or nullif(trim(p_content),'') is null or length(p_content)>20000
       or p_created_at is null or not isfinite(p_created_at) then
        raise exception 'invalid QQ chat export' using errcode='22023';
    end if;
    v_hash := md5(jsonb_build_array(p_assistant_id,p_conversation_id,p_role,p_content,
                                   extract(epoch from p_created_at))::text);
    perform pg_advisory_xact_lock(hashtextextended(p_message_key,0));
    select * into v_old from public.qqbot_chat_exports where message_key=p_message_key;
    if found then
        if v_old.payload_hash<>v_hash then
            raise exception 'QQ export key payload mismatch' using errcode='22023';
        end if;
        return v_old.chat_message_id;
    end if;
    insert into public.chat_messages(assistant_id,conversation_id,role,content,created_at)
    values(p_assistant_id,p_conversation_id,p_role,p_content,p_created_at at time zone 'Asia/Shanghai')
    returning id into v_id;
    insert into public.qqbot_chat_exports(message_key,payload_hash,chat_message_id)
    values(p_message_key,v_hash,v_id);
    return v_id;
end;
$$;

revoke all on function public.qqbot_publish_chat_message(text,text,text,text,text,timestamptz) from public, anon, authenticated;
grant execute on function public.qqbot_publish_chat_message(text,text,text,text,text,timestamptz) to service_role;
notify pgrst, 'reload schema';
commit;
