-- Reproducible, server-only RPCs for P2 hybrid memory retrieval.
-- chat_messages remains an immutable read-only source and is not modified.

create or replace function public.match_memories(
    query_embedding extensions.vector,
    match_threshold double precision default 0.5,
    match_count integer default 20
)
returns table(
    id integer,
    content text,
    title text,
    tags text[],
    heat double precision,
    importance integer,
    created_at timestamptz,
    last_recalled_at timestamptz,
    similarity double precision
)
language sql
stable
set search_path to 'public', 'extensions'
as $function$
    select
        memory.id,
        memory.content,
        memory.title,
        memory.tags,
        memory.heat,
        memory.importance,
        memory.created_at,
        memory.last_recalled_at,
        1 - (memory.embedding <=> query_embedding) as similarity
    from public.memories as memory
    where memory.is_active = true
      and memory.verified = 'verified'
      and memory.embedding is not null
      and 1 - (memory.embedding <=> query_embedding)
          > least(greatest(coalesce(match_threshold, 0.5), 0.0), 1.0)
    order by memory.embedding <=> query_embedding
    limit least(greatest(coalesce(match_count, 20), 1), 50);
$function$;

revoke all on function public.match_memories(
    extensions.vector, double precision, integer
) from public, anon, authenticated;

grant execute on function public.match_memories(
    extensions.vector, double precision, integer
) to service_role;

create or replace function public.boost_memory_heat(
    memory_id integer,
    boost_amount double precision default 15,
    recalled_at timestamptz default now()
)
returns void
language sql
set search_path to 'public'
as $function$
    update public.memories
    set
        heat = least(heat + least(greatest(coalesce(boost_amount, 15), 0), 25), 100),
        recall_count = recall_count + 1,
        last_recalled_at = coalesce(recalled_at, now())
    where id = memory_id
      and is_active = true
      and verified = 'verified';
$function$;

revoke all on function public.boost_memory_heat(
    integer, double precision, timestamptz
) from public, anon, authenticated;

grant execute on function public.boost_memory_heat(
    integer, double precision, timestamptz
) to service_role;

