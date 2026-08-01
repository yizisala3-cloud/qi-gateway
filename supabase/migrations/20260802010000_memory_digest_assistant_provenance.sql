-- Preserve assistant provenance for every automatically extracted memory.
-- chat_messages remains an immutable source and is intentionally not altered.

alter table public.memories
    add column if not exists assistant_id text;

-- Backfill any digest memories created before this migration. Manual memories
-- (digest_run_id is null) are allowed to remain assistant-agnostic.
update public.memories as memory
set assistant_id = run.assistant_id
from public.memory_digest_runs as run
where memory.digest_run_id = run.id
  and memory.assistant_id is null;

alter table public.memories
    drop constraint if exists memories_digest_assistant_required;

alter table public.memories
    add constraint memories_digest_assistant_required
    check (digest_run_id is null or assistant_id is not null);

create index if not exists memories_assistant_review_idx
    on public.memories (assistant_id, verified, is_active);

comment on column public.memories.assistant_id is
    'Assistant whose read-only chat message range produced this memory.';

create or replace function public.commit_memory_digest_run(
    p_run_id bigint,
    p_memories jsonb default '[]'::jsonb
)
returns integer
language plpgsql
set search_path to 'public', 'extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_item jsonb;
    v_preview jsonb := '[]'::jsonb;
    v_inserted integer := 0;
    v_delta integer := 0;
begin
    if jsonb_typeof(coalesce(p_memories, '[]'::jsonb)) <> 'array' then
        raise exception 'p_memories must be a JSON array';
    end if;

    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;

    if not found then
        raise exception 'memory digest run % not found', p_run_id;
    end if;
    if v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory digest run % is not a running execute run', p_run_id;
    end if;
    if v_run.source_last_message_id is null then
        raise exception 'memory digest run % has no source range', p_run_id;
    end if;

    for v_item in
        select value
        from jsonb_array_elements(coalesce(p_memories, '[]'::jsonb))
    loop
        v_preview := v_preview || jsonb_build_array(
            v_item - 'embedding' - 'content_hash'
        );

        insert into public.memories (
            content,
            title,
            tags,
            heat,
            importance,
            layer,
            embedding,
            source,
            verified,
            is_active,
            emotion_weight,
            recall_count,
            assistant_id,
            digest_run_id,
            source_first_message_id,
            source_last_message_id,
            confidence,
            content_hash
        ) values (
            v_item->>'content',
            nullif(v_item->>'title', ''),
            array(
                select jsonb_array_elements_text(
                    coalesce(v_item->'tags', '[]'::jsonb)
                )
            ),
            least(
                greatest(coalesce((v_item->>'importance')::integer, 5) * 10.0, 0),
                100
            ),
            least(greatest(coalesce((v_item->>'importance')::integer, 5), 1), 10),
            case
                when coalesce((v_item->>'importance')::integer, 5) >= 8 then '场景'
                else '碎片'
            end,
            case
                when v_item ? 'embedding' and v_item->'embedding' <> 'null'::jsonb
                then (v_item->>'embedding')::extensions.vector
                else null
            end,
            'daily_digest',
            'pending',
            true,
            least(
                greatest(coalesce((v_item->>'emotion_weight')::double precision, 0.5), 0),
                1
            ),
            0,
            v_run.assistant_id,
            v_run.id,
            v_run.source_first_message_id,
            v_run.source_last_message_id,
            least(
                greatest(coalesce((v_item->>'confidence')::double precision, 0.5), 0),
                1
            ),
            nullif(v_item->>'content_hash', '')
        )
        on conflict (content_hash) do nothing;

        get diagnostics v_delta = row_count;
        v_inserted := v_inserted + v_delta;
    end loop;

    insert into public.memory_digest_cursors (
        assistant_id,
        last_processed_message_id,
        last_success_at,
        updated_at
    ) values (
        v_run.assistant_id,
        v_run.source_last_message_id,
        now(),
        now()
    )
    on conflict (assistant_id) do update set
        last_processed_message_id = greatest(
            public.memory_digest_cursors.last_processed_message_id,
            excluded.last_processed_message_id
        ),
        last_success_at = now(),
        updated_at = now();

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = jsonb_array_length(coalesce(p_memories, '[]'::jsonb)),
        inserted_count = v_inserted,
        preview_memories = v_preview,
        completed_at = now(),
        error_code = null,
        error_message = null
    where id = v_run.id;

    return v_inserted;
end;
$function$;
