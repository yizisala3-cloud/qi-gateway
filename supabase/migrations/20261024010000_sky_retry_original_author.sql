-- Keep original ownership when server-side assistant discovery changes on retry.
-- Existing receipts/hashes and message pairs are not rewritten.
begin;
create or replace function public.sky_publish_chat_delivery(
    p_delivery_id uuid, p_assistant_id text, p_vm_index integer,
    p_user_text text, p_assistant_text text, p_confirmed_at timestamptz
) returns jsonb
language plpgsql security definer
set search_path = pg_catalog, public
as $$
declare
    v_hash text;
    v_old public.sky_chat_deliveries%rowtype;
    v_original_author text;
    v_user bigint;
    v_assistant bigint;
begin
    if p_delivery_id is null or nullif(trim(p_assistant_id),'') is null
       or length(p_assistant_id)>200 or p_vm_index is null or p_vm_index not between 0 and 9999
       or nullif(trim(p_user_text),'') is null or length(p_user_text)>20000
       or nullif(trim(p_assistant_text),'') is null or length(p_assistant_text)>100
       or lower(trim(p_assistant_text))='pass' or p_assistant_text ~ E'[\n\r]'
       or p_confirmed_at is null or not isfinite(p_confirmed_at) then
        raise exception 'invalid Sky delivery' using errcode='22023';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('sky:'||p_delivery_id::text,0));
    select * into v_old from public.sky_chat_deliveries where delivery_id=p_delivery_id;
    if found then
        select assistant_id into v_original_author from public.chat_messages
          where id=v_old.assistant_message_id and role='assistant'
            and conversation_id='sky:mumu:'||p_vm_index::text;
        if not found then
            raise exception 'Sky original delivery unavailable' using errcode='22023';
        end if;
        v_hash := md5(jsonb_build_array(v_original_author,p_vm_index,p_user_text,p_assistant_text,
                                       extract(epoch from p_confirmed_at))::text);
        if v_old.payload_hash<>v_hash then
            raise exception 'Sky delivery payload mismatch' using errcode='22023';
        end if;
        return jsonb_build_object('delivery_id',p_delivery_id,'user_message_id',v_old.user_message_id,
                                 'assistant_message_id',v_old.assistant_message_id,'duplicate',true);
    end if;
    v_hash := md5(jsonb_build_array(p_assistant_id,p_vm_index,p_user_text,p_assistant_text,
                                   extract(epoch from p_confirmed_at))::text);
    insert into public.chat_messages(assistant_id,conversation_id,role,content,created_at)
    values(p_assistant_id,'sky:mumu:'||p_vm_index::text,'user',p_user_text,
           (p_confirmed_at at time zone 'Asia/Shanghai')-interval '1 microsecond') returning id into v_user;
    insert into public.chat_messages(assistant_id,conversation_id,role,content,created_at)
    values(p_assistant_id,'sky:mumu:'||p_vm_index::text,'assistant',p_assistant_text,
           p_confirmed_at at time zone 'Asia/Shanghai') returning id into v_assistant;
    insert into public.sky_chat_deliveries(delivery_id,payload_hash,user_message_id,assistant_message_id)
    values(p_delivery_id,v_hash,v_user,v_assistant);
    return jsonb_build_object('delivery_id',p_delivery_id,'user_message_id',v_user,
                             'assistant_message_id',v_assistant,'duplicate',false);
end;
$$;
revoke all on function public.sky_publish_chat_delivery(uuid,text,integer,text,text,timestamptz)
    from public, anon, authenticated;
grant execute on function public.sky_publish_chat_delivery(uuid,text,integer,text,text,timestamptz) to service_role;
notify pgrst, 'reload schema';
commit;
