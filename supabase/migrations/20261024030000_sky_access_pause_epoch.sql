-- One row holds both permission and the last successful disable generation.
-- Execute before deploying the versioned Sky access API/bridge.
begin;
insert into public.app_settings(key,value)
values('sky.enabled',jsonb_build_object('enabled',false,
       'pause_epoch','00000000-0000-0000-0000-000000000000'))
on conflict(key) do nothing;
update public.app_settings
set value=jsonb_build_object('enabled',value,
    'pause_epoch','00000000-0000-0000-0000-000000000000'),updated_at=clock_timestamp()
where key='sky.enabled' and jsonb_typeof(value)='boolean';

create or replace function public.sky_set_access(p_enabled boolean,p_pause_epoch uuid)
returns jsonb language plpgsql security definer
set search_path=pg_catalog,public
as $$
declare
    v_old jsonb;
    v_epoch uuid:='00000000-0000-0000-0000-000000000000';
    v_new jsonb;
begin
    if p_enabled is null or p_pause_epoch is null or p_pause_epoch=v_epoch then
        raise exception 'invalid Sky access request' using errcode='22023';
    end if;
    perform pg_advisory_xact_lock(hashtextextended('sky:access',0));
    select value into v_old from public.app_settings where key='sky.enabled' for update;
    if found then
        if jsonb_typeof(v_old) is distinct from 'object'
           or (v_old-'enabled'-'pause_epoch')<>'{}'::jsonb
           or jsonb_typeof(v_old->'enabled') is distinct from 'boolean'
           or jsonb_typeof(v_old->'pause_epoch') is distinct from 'string' then
            raise exception 'invalid stored Sky access state' using errcode='22023';
        end if;
        v_epoch:=(v_old->>'pause_epoch')::uuid;
    end if;
    if not p_enabled then
        if p_pause_epoch=v_epoch then
            raise exception 'reused Sky pause generation' using errcode='22023';
        end if;
        v_epoch:=p_pause_epoch;
    end if;
    v_new:=jsonb_build_object('enabled',p_enabled,'pause_epoch',v_epoch::text);
    insert into public.app_settings(key,value,updated_at) values('sky.enabled',v_new,clock_timestamp())
    on conflict(key) do update set value=excluded.value,updated_at=excluded.updated_at;
    return v_new;
end;
$$;
revoke all on function public.sky_set_access(boolean,uuid) from public,anon,authenticated;
grant execute on function public.sky_set_access(boolean,uuid) to service_role;
notify pgrst,'reload schema';
commit;
