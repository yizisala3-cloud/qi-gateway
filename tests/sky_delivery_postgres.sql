-- Run only in an isolated PostgreSQL container with a representative old row.
create function public.sky_test_fail_assistant() returns trigger language plpgsql as $$
begin
    if new.role='assistant' and new.content='__force_assistant_failure__' then
        raise exception 'injected assistant insert failure' using errcode='22003';
    end if;
    return new;
end $$;
create trigger sky_test_failure before insert on public.chat_messages
    for each row execute function public.sky_test_fail_assistant();

do $check$
declare v_id uuid:=gen_random_uuid(); v_time timestamptz:=clock_timestamp();
    a jsonb; b jsonb; v_failed uuid:=gen_random_uuid(); v_count bigint;
begin
    if (select count(*) from public.chat_messages where content='existing immutable row')<>1 then
        raise exception 'legacy row changed';
    end if;
    a:=public.sky_publish_chat_delivery(v_id,'isolated-test',10,'question','answer',v_time);
    b:=public.sky_publish_chat_delivery(v_id,'isolated-test',10,'question','answer',v_time);
    if (a->>'duplicate')::boolean or not (b->>'duplicate')::boolean
       or a->>'user_message_id'<>b->>'user_message_id'
       or a->>'assistant_message_id'<>b->>'assistant_message_id' then
        raise exception 'duplicate changed the pair';
    end if;
    begin
        perform public.sky_publish_chat_delivery(v_id,'isolated-test',10,'question','changed',v_time);
        raise exception 'payload conflict accepted';
    exception when sqlstate '22023' then null;
    end;
    select count(*) into v_count from public.chat_messages;
    begin
        perform public.sky_publish_chat_delivery(v_failed,'isolated-test',10,'rollback question',
                                                '__force_assistant_failure__',v_time);
        raise exception 'injected failure was not raised';
    exception when sqlstate '22003' then null;
    end;
    if (select count(*) from public.chat_messages)<>v_count
       or exists(select 1 from public.sky_chat_deliveries where delivery_id=v_failed) then
        raise exception 'partial pair or receipt survived rollback';
    end if;
    if has_function_privilege('anon','public.sky_publish_chat_delivery(uuid,text,integer,text,text,timestamptz)','execute')
       or has_function_privilege('authenticated','public.sky_publish_chat_delivery(uuid,text,integer,text,text,timestamptz)','execute')
       or not has_function_privilege('service_role','public.sky_publish_chat_delivery(uuid,text,integer,text,text,timestamptz)','execute') then
        raise exception 'execution permissions changed';
    end if;
end $check$;
drop trigger sky_test_failure on public.chat_messages;
drop function public.sky_test_fail_assistant();
