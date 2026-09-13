-- Unify the rumination content length contract at 5-3000 characters and
-- stop silently truncating bodies at insert time.
--
-- The parser (gateway) rejects content outside 5-3000 with per-op
-- diagnostics (op index, op type, raw value type, normalized length,
-- content_too_short/content_too_long/content_type_invalid). The commit RPC
-- previously stored left(content, 600) -- a silent truncation that also
-- diverged from the content_hash/embedding computed by the gateway over the
-- full normalized text. memory_requests.content and memories.content are
-- both unconstrained text, so the full normalized body is now stored as-is;
-- the RPC validates the same 5-3000 range explicitly (min checks existed in
-- both the shared path and the adopt_thread branch, max checks added).
-- Content_hash and embedding already correspond to the same normalized text
-- received here.
--
-- The review chain's shared content validation (review_memory_request_v2,
-- v3) and the admin lifecycle entries (create/edit/change_type) are
-- realigned to the same range so >600-char requests and memories can be
-- approved and edited without truncation.
-- memory_requests' content-length CHECK constraint is realigned to the same
-- 5-3000 range (constraint swap only; the column itself is unchanged), and
-- downstream review approval (review_memory_request_v5) stores the request
-- content without truncation, so >600-char approved requests persist fully.
--
-- Forward-only follow-up to 20260915010000. Only change from the previous
-- version: the content extraction line and the explicit max-length checks.
-- All other logic is byte-for-byte identical.
--
-- Apply order: after 20260915010000 (filename order already guarantees this).
-- Not applied to production by this PR; production apply happens separately
-- after review approval.
--
-- security definer, fixed search_path, service_role only (unchanged).
-- Does NOT touch chat_messages, memories table structure, memory_requests, or
-- memory_relations.

begin;

drop function if exists public.commit_rumination_batch(bigint, jsonb);

create or replace function public.commit_rumination_batch(
    p_run_id bigint,
    p_ops jsonb
)
returns jsonb
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_cursor public.memory_rumination_cursors%rowtype;
    v_ops jsonb;
    v_op jsonb;
    v_preview jsonb := '[]'::jsonb;
    v_counts jsonb;
    v_op_type text;
    v_reason text;
    v_requested_evidence bigint[];
    v_evidence bigint[];
    v_evidence_count integer;
    v_ev_start timestamptz;
    v_ev_end timestamptz;
    v_content text;
    v_content_hash text;
    v_title text;
    v_importance integer;
    v_confidence double precision;
    v_source_type text;
    v_recall_scene text;
    v_recall_tags text[];
    v_recall_embedding extensions.vector;
    v_embedding extensions.vector;
    v_memory_time timestamptz;
    v_time_precision text;
    v_continuity_type text;
    v_thread_state text;
    v_continuity_data jsonb;
    v_memory_key text;
    v_target_id integer;
    v_target public.memories%rowtype;
    v_new_memory_id integer;
    v_new_continuity_id uuid;
    v_conversation_id text;
    v_delta integer;
    v_op_count integer := 0;
    v_written integer := 0;
    v_dup_max_evidence bigint;
    v_prior_request_id bigint;
    v_idempotency_key text;
    v_ev_precision text := 'minute';
    v_update_mode text;
    v_absorbed_ids bigint[];
    v_absorbed_id bigint;
    v_absorbed public.memories%rowtype;
    v_absorbed_snapshots jsonb;
    v_snapshot jsonb;
begin
    v_counts := jsonb_build_object(
        'ignored', 0,
        'created_memories', 0,
        'created_threads', 0,
        'adopted_threads', 0,
        'evidence_only', 0,
        'updated_versions', 0,
        'paused', 0,
        'resumed', 0,
        'resolved', 0,
        'created_requests', 0,
        'skipped_duplicates', 0
    );

    if jsonb_typeof(coalesce(p_ops, '{}'::jsonb)) <> 'object'
       or jsonb_typeof(coalesce(p_ops->'operations', '[]'::jsonb)) <> 'array' then
        raise exception 'memory_rumination_ops_invalid';
    end if;
    v_ops := p_ops->'operations';

    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;
    if not found or v_run.pipeline <> 'rumination'
       or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_rumination_invalid_run';
    end if;
    if v_run.source_first_message_id is null or v_run.source_last_message_id is null
       or coalesce(v_run.message_count, 0) < 1 then
        raise exception 'memory_rumination_missing_batch';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id, 7));

    select * into v_cursor
    from public.memory_rumination_cursors
    where assistant_id = v_run.assistant_id
    for update;
    if not found then
        raise exception 'memory_rumination_cursor_missing';
    end if;
    if v_cursor.initialized and v_run.source_first_message_id <= v_cursor.last_processed_message_id then
        raise exception 'memory_rumination_batch_stale';
    end if;

    for v_op in select value from jsonb_array_elements(v_ops) loop
        v_op_count := v_op_count + 1;
        v_op_type := v_op->>'op';
        if v_op_type not in (
            'ignore', 'create_memory', 'create_tracked_thread', 'adopt_thread',
            'evidence_only', 'update_thread', 'pause_thread', 'resume_thread',
            'resolve_thread', 'create_request'
        ) then
            raise exception 'memory_rumination_invalid_op';
        end if;
        v_reason := nullif(left(trim(coalesce(v_op->>'reason', '')), 500), '');

        -- Evidence: every referenced id must really exist inside this batch
        -- window for this assistant. Hallucinated or out-of-batch ids abort
        -- the whole batch (nothing is committed, cursor never moves).
        v_requested_evidence := coalesce(array_agg(distinct value::bigint order by value::bigint), '{}'::bigint[])
            from jsonb_array_elements_text(coalesce(v_op->'evidence_message_ids', '[]'::jsonb)) as entry(value)
            where entry.value ~ '^[0-9]+$';
        v_evidence_count := coalesce(cardinality(v_requested_evidence), 0);
        if v_evidence_count <> jsonb_array_length(coalesce(v_op->'evidence_message_ids', '[]'::jsonb))
           or v_evidence_count not between 1 and 960 then
            raise exception 'memory_rumination_invalid_evidence';
        end if;

        select coalesce(array_agg(message.id order by message.id), '{}'::bigint[]),
               min(message.created_at),
               max(message.created_at)
        into v_evidence, v_ev_start, v_ev_end
        from public.chat_messages as message
        where message.id = any(v_requested_evidence)
          and message.assistant_id = v_run.assistant_id
          and message.id between v_run.source_first_message_id and v_run.source_last_message_id;
        if cardinality(v_evidence) <> v_evidence_count then
            raise exception 'memory_rumination_invalid_evidence';
        end if;

        select message.conversation_id into v_conversation_id
        from public.chat_messages as message
        where message.id = v_evidence[1];

        if v_op_type = 'ignore' then
            v_counts := jsonb_set(v_counts, '{ignored}', (coalesce((v_counts->>'ignored')::integer, 0) + 1)::text::jsonb);
            v_preview := v_preview || jsonb_build_object(
                'op', 'ignore', 'commit_status', 'ignored',
                'reason', v_reason,
                'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        -- Shared metadata extraction for content-bearing ops.
        v_content := trim(coalesce(v_op->>'content', ''));
        v_content_hash := lower(trim(coalesce(v_op->>'content_hash', '')));
        v_title := nullif(left(trim(coalesce(v_op->>'title', '')), 100), '');
        v_importance := least(greatest(coalesce((v_op->>'importance')::integer, 5), 1), 10);
        v_confidence := least(greatest(coalesce((v_op->>'confidence')::double precision, 0.6), 0), 1);
        v_source_type := nullif(trim(coalesce(v_op->>'source_type', '')), '');
        if v_source_type is not null and v_source_type not in (
            'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
            'roleplay', 'tool_result', 'system_meta', 'unknown'
        ) then
            raise exception 'memory_rumination_invalid_source_type';
        end if;
        v_recall_scene := nullif(btrim(coalesce(v_op->>'recall_scene', '')), '');
        v_recall_tags := case
            when jsonb_typeof(coalesce(v_op->'recall_tags', '[]'::jsonb)) = 'array'
            then array(
                select t.value
                from jsonb_array_elements_text(v_op->'recall_tags') as t(value)
                where nullif(btrim(t.value), '') is not null
            )
            else null
        end;
        -- A non-empty recall_scene must always arrive with its own vector: the
        -- recall embedding serves the scene alone, and a scene saved without a
        -- vector is a permanently incomplete write. The op-level failure
        -- aborts the whole batch transaction, so no memory row, no continuity
        -- object, no handoff row and no cursor advance can survive it.
        if v_recall_scene is not null
           and (not (v_op ? 'recall_embedding') or v_op->'recall_embedding' = 'null'::jsonb) then
            raise exception 'memory_rumination_missing_recall_embedding';
        end if;
        v_recall_embedding := case
            when v_recall_scene is not null
                 and v_op ? 'recall_embedding'
                 and v_op->'recall_embedding' <> 'null'::jsonb
            then (v_op->>'recall_embedding')::extensions.vector
            else null
        end;
        v_time_precision := case
            when v_op->>'time_precision' in ('minute', 'hour', 'day', 'approximate', 'unknown')
            then v_op->>'time_precision'
            else 'unknown'
        end;
        v_memory_time := case
            when nullif(v_op->>'memory_time', '') is null then null
            when v_time_precision = 'day' and (v_op->>'memory_time') ~ '^\d{4}-\d{2}-\d{2}$'
            then ((v_op->>'memory_time')::date::timestamp at time zone 'Asia/Shanghai')
            else (v_op->>'memory_time')::timestamptz
        end;
        v_embedding := case
            when v_op ? 'embedding' and v_op->'embedding' <> 'null'::jsonb
            then (v_op->>'embedding')::extensions.vector
            else null
        end;

        -- adopt_thread may be contentless (pure in-place takeover); all other
        -- content-bearing ops require a valid body and hash.
        if v_op_type in ('create_memory', 'create_tracked_thread', 'update_thread', 'pause_thread', 'resume_thread', 'resolve_thread', 'create_request') then
            if char_length(v_content) < 5 or char_length(v_content) > 3000
                or v_content_hash !~ '^[0-9a-f]{64}$' then
                raise exception 'memory_rumination_invalid_content';
            end if;
            v_continuity_type := v_op->>'continuity_type';
            v_thread_state := nullif(trim(coalesce(v_op->>'thread_state', '')), '');
            v_continuity_data := v_op->'continuity_data';
            if v_continuity_data is not null and jsonb_typeof(v_continuity_data) <> 'object' then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
        elsif v_op_type = 'adopt_thread' then
            v_continuity_type := 'thread';
            v_thread_state := nullif(trim(coalesce(v_op->>'thread_state', '')), '');
            v_continuity_data := v_op->'continuity_data';
            if v_continuity_data is not null and jsonb_typeof(v_continuity_data) <> 'object' then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if char_length(v_content) >= 5 or char_length(trim(coalesce(v_op->>'content', ''))) > 0 then
                if char_length(v_content) < 5 or char_length(v_content) > 3000
                    or v_content_hash !~ '^[0-9a-f]{64}$' then
                    raise exception 'memory_rumination_invalid_content';
                end if;
            end if;
        end if;

        -- Thread-target ops share target resolution. Any unfinished thread is
        -- addressable; acting on a fast_path thread implicitly takes it over
        -- (maintained_by flips and an audit row is written in this transaction).
        -- A takeover must end with a stable memory_key: ops other than
        -- adopt_thread refuse a keyless fast_path target unless the op itself
        -- carries the key to fill in.
        if v_op_type in ('adopt_thread', 'evidence_only', 'update_thread', 'pause_thread', 'resume_thread', 'resolve_thread') then
            v_target_id := nullif(trim(coalesce(v_op->>'target_memory_id', '')), '')::integer;
            if v_target_id is null then
                raise exception 'memory_rumination_target_required';
            end if;
            select * into v_target
            from public.memories
            where id = v_target_id
              and assistant_id = v_run.assistant_id
              and continuity_type = 'thread'
              and verified = 'verified'
              and is_active = true
            for update;
            if not found then
                raise exception 'memory_rumination_invalid_target';
            end if;
            if v_target.thread_state not in ('open', 'paused') then
                raise exception 'memory_rumination_target_closed';
            end if;
            if v_op_type <> 'adopt_thread'
               and v_target.maintained_by = 'fast_path'
               and v_target.memory_key is null
               and nullif(trim(coalesce(v_op->>'memory_key', '')), '') is null then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            -- Optimistic target consistency: the model must echo the exact
            -- snapshot it read from the input. Any concurrent change between
            -- model input and commit aborts the whole batch.
            if nullif(btrim(coalesce(v_op->>'target_memory_key', '')), '')
                   is distinct from v_target.memory_key then
                raise exception 'memory_rumination_target_changed';
            end if;
            if nullif(btrim(coalesce(v_op->>'target_continuity_id', '')), '')::uuid
                   is distinct from v_target.continuity_id then
                raise exception 'memory_rumination_target_changed';
            end if;
            if lower(nullif(btrim(coalesce(v_op->>'target_content_hash', '')), ''))
                   is distinct from v_target.content_hash then
                raise exception 'memory_rumination_target_changed';
            end if;
            if nullif(btrim(coalesce(v_op->>'target_thread_state', '')), '')
                   is distinct from v_target.thread_state then
                raise exception 'memory_rumination_target_changed';
            end if;
        end if;

        if v_op_type = 'create_memory' then
            -- Terminal and ordinary direct writes: moment and inside_joke only.
            -- episode/profile/interaction_rule must go through create_request;
            -- thread has its own ops and is never a terminal class.
            if v_continuity_type not in ('moment', 'inside_joke') then
                raise exception 'memory_rumination_invalid_direct_type';
            end if;
            if v_thread_state is not null then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;
            if not public.validate_continuity_data(v_continuity_type, null, v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;

            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.content_hash = v_content_hash
                  and request.status in ('pending', 'approved', 'merged')
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_memory', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.is_active = true
                  and memory.verified = 'verified'
                  and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
                  and memory.content_hash = v_content_hash
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_memory', 'commit_status', 'skipped_active_memory',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            insert into public.memory_continuity_objects (assistant_id)
            values (v_run.assistant_id)
            returning continuity_id into v_new_continuity_id;

            insert into public.memories (
                content, title, tags, heat, importance, embedding,
                source, verified, is_active, recall_count,
                assistant_id, digest_run_id,
                source_first_message_id, source_last_message_id,
                confidence, content_hash,
                continuity_id, continuity_schema_version, continuity_data,
                continuity_type, thread_state,
                memory_time, time_precision,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision,
                recall_scene, recall_tags, recall_embedding,
                source_type, producer_path, maintained_by
            ) values (
                v_content, v_title, array[v_continuity_type],
                least(greatest(v_importance * 10.0, 0), 100), v_importance, v_embedding,
                'rumination', 'verified', true, 0,
                v_run.assistant_id, v_run.id,
                v_evidence[1], v_evidence[cardinality(v_evidence)],
                v_confidence, v_content_hash,
                v_new_continuity_id, 1, v_continuity_data,
                v_continuity_type, null,
                v_memory_time, v_time_precision,
                v_evidence, v_ev_start, v_ev_end,
                v_ev_precision,
                v_recall_scene, coalesce(v_recall_tags, '{}'::text[]), v_recall_embedding,
                v_source_type, 'rumination', 'rumination'
            )
            returning id into v_new_memory_id;

            -- Explicit absorption: the listed fast-path formal memories leave
            -- the active truth in this same transaction and an audit row is
            -- written. Structural re-checks under lock; the new rumination
            -- memory and the deactivation commit or roll back together.
            v_absorbed_ids := coalesce(array_agg(distinct value::bigint order by value::bigint), '{}'::bigint[])
                from jsonb_array_elements_text(coalesce(v_op->'absorbed_fast_path_memory_ids', '[]'::jsonb)) as entry(value)
                where entry.value ~ '^[0-9]+$';
            if coalesce(cardinality(v_absorbed_ids), 0)
                   <> jsonb_array_length(coalesce(v_op->'absorbed_fast_path_memory_ids', '[]'::jsonb))
               or coalesce(cardinality(v_absorbed_ids), 0) > 8 then
                raise exception 'memory_rumination_invalid_absorb_target';
            end if;
            foreach v_absorbed_id in array v_absorbed_ids loop
                select * into v_absorbed
                from public.memories
                where id = v_absorbed_id
                for update;
                if not found
                   or v_absorbed.assistant_id is distinct from v_run.assistant_id
                   or v_absorbed.producer_path is distinct from 'fast_path'
                   or v_absorbed.verified is distinct from 'verified'
                   or v_absorbed.is_active is not true
                   or (
                        v_absorbed.continuity_type = 'thread'
                        and v_absorbed.thread_state in ('resolved', 'dissolved', 'abandoned')
                   ) then
                    raise exception 'memory_rumination_absorb_target_invalid';
                end if;

                update public.memories
                set is_active = false
                where id = v_absorbed.id;

                insert into public.memory_path_handoffs (
                    assistant_id, run_id, kind,
                    fast_path_memory_id, rumination_memory_id,
                    memory_key, continuity_id, note
                ) values (
                    v_run.assistant_id, v_run.id, 'absorbed_by_direct_memory',
                    v_absorbed.id, v_new_memory_id,
                    v_absorbed.memory_key, v_absorbed.continuity_id,
                    left('absorbed by direct memory; snapshot ' || v_absorbed.content_hash, 500)
                );
            end loop;

            v_counts := jsonb_set(v_counts, '{created_memories}',
                ((v_counts->>'created_memories')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'create_memory', 'commit_status', 'inserted_memory',
                'memory_id', v_new_memory_id,
                'continuity_type', v_continuity_type,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'create_tracked_thread' then
            if v_continuity_type is not null and v_continuity_type <> 'thread' then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            v_memory_key := lower(trim(coalesce(v_op->>'memory_key', '')));
            if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            if v_thread_state is null or v_thread_state <> 'open' then
                raise exception 'memory_rumination_invalid_thread_state';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;
            if not public.validate_continuity_data('thread', 'open', v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.memory_key = v_memory_key
                  and memory.is_active = true
                  and memory.verified = 'verified'
            ) then
                raise exception 'memory_rumination_memory_key_conflict';
            end if;
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.content_hash = v_content_hash
                  and request.status in ('pending', 'approved', 'merged')
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_tracked_thread', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.is_active = true
                  and memory.verified = 'verified'
                  and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
                  and memory.content_hash = v_content_hash
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_tracked_thread', 'commit_status', 'skipped_active_memory',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            insert into public.memory_continuity_objects (assistant_id)
            values (v_run.assistant_id)
            returning continuity_id into v_new_continuity_id;

            insert into public.memories (
                content, title, tags, heat, importance, embedding,
                source, verified, is_active, recall_count,
                assistant_id, digest_run_id,
                source_first_message_id, source_last_message_id,
                confidence, content_hash,
                memory_key,
                continuity_id, continuity_schema_version, continuity_data,
                continuity_type, thread_state,
                memory_time, time_precision,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision,
                recall_scene, recall_tags, recall_embedding,
                source_type, producer_path, maintained_by
            ) values (
                v_content, v_title, array['thread'],
                least(greatest(v_importance * 10.0, 0), 100), v_importance, v_embedding,
                'rumination', 'verified', true, 0,
                v_run.assistant_id, v_run.id,
                v_evidence[1], v_evidence[cardinality(v_evidence)],
                v_confidence, v_content_hash,
                v_memory_key,
                v_new_continuity_id, 1, v_continuity_data,
                'thread', 'open',
                v_memory_time, v_time_precision,
                v_evidence, v_ev_start, v_ev_end,
                v_ev_precision,
                v_recall_scene, coalesce(v_recall_tags, '{}'::text[]), v_recall_embedding,
                v_source_type, 'rumination', 'rumination'
            )
            returning id into v_new_memory_id;

            v_counts := jsonb_set(v_counts, '{created_threads}',
                ((v_counts->>'created_threads')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'create_tracked_thread', 'commit_status', 'inserted_thread',
                'memory_id', v_new_memory_id,
                'memory_key', v_memory_key,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'adopt_thread' then
            -- Takeover is only meaningful for fast-path threads; rumination's
            -- own threads are addressed with evidence_only/update/... ops.
            if v_target.maintained_by <> 'fast_path' then
                raise exception 'memory_rumination_not_fast_path';
            end if;
            v_memory_key := lower(trim(coalesce(v_op->>'memory_key', v_target.memory_key, '')));
            if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.memory_key = v_memory_key
                  and memory.is_active = true
                  and memory.verified = 'verified'
                  and memory.id <> v_target.id
            ) then
                raise exception 'memory_rumination_memory_key_conflict';
            end if;
            if nullif(trim(coalesce(v_op->>'thread_state', '')), '') is not null
               and nullif(trim(coalesce(v_op->>'thread_state', '')), '') <> v_target.thread_state then
                raise exception 'memory_rumination_state_change_forbidden';
            end if;

            if char_length(v_content) >= 5
               and v_content_hash !~ '^[0-9a-f]{64}$' then
                raise exception 'memory_rumination_invalid_content';
            end if;

            if char_length(v_content) >= 5 and v_content_hash <> v_target.content_hash then
                -- Versioned takeover. Soft-deactivate first, then insert the
                -- successor and link both directions: the whole sequence is
                -- one transaction, so a failure restores the old row while
                -- the partial unique index never sees two active versions of
                -- the same memory_key.
                if v_embedding is null then
                    raise exception 'memory_rumination_missing_embedding';
                end if;
                if v_continuity_data is null
                   or not public.validate_continuity_data('thread', v_target.thread_state, v_continuity_data) then
                    raise exception 'memory_rumination_invalid_continuity_data';
                end if;
                update public.memories
                set is_active = false,
                    superseded_at = now(),
                    superseded_by_memory_id = null
                where id = v_target.id;
                insert into public.memories (
                    content, title, tags, heat, importance, embedding,
                    source, verified, is_active, recall_count,
                    assistant_id, digest_run_id,
                    source_first_message_id, source_last_message_id,
                    confidence, content_hash,
                    memory_key, supersedes_memory_id,
                    continuity_id, continuity_schema_version, continuity_data,
                    continuity_type, thread_state,
                    memory_time, time_precision,
                    evidence_message_ids, evidence_start_time, evidence_end_time,
                    evidence_time_precision,
                    recall_scene, recall_tags, recall_embedding,
                    source_type, producer_path, maintained_by
                ) values (
                    v_content, coalesce(v_title, v_target.title), v_target.tags,
                    v_target.heat, v_importance, v_embedding,
                    'rumination', 'verified', true, 0,
                    v_run.assistant_id, v_run.id,
                    v_evidence[1], v_evidence[cardinality(v_evidence)],
                    v_confidence, v_content_hash,
                    v_memory_key, v_target.id,
                    v_target.continuity_id, 1, v_continuity_data,
                    'thread', v_target.thread_state,
                    v_memory_time, v_time_precision,
                    (select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                     from unnest(v_target.evidence_message_ids || v_evidence) as id),
                    least(coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                    greatest(coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                    v_ev_precision,
                    coalesce(v_recall_scene, v_target.recall_scene),
                    coalesce(v_recall_tags, v_target.recall_tags, '{}'::text[]),
                    coalesce(v_recall_embedding, v_target.recall_embedding),
                    coalesce(v_source_type, v_target.source_type),
                    'rumination', 'rumination'
                )
                returning id into v_new_memory_id;

                update public.memories
                set superseded_by_memory_id = v_new_memory_id
                where id = v_target.id;
            else
                -- In-place takeover: keep the fast-path row and its evidence;
                -- only the maintainer marker and the stable key change.
                v_new_memory_id := v_target.id;
                update public.memories
                set memory_key = v_memory_key,
                    maintained_by = 'rumination',
                    evidence_message_ids = (
                        select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                        from unnest(v_target.evidence_message_ids || v_evidence) as id
                    ),
                    evidence_start_time = least(
                        coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                    evidence_end_time = greatest(
                        coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                    evidence_time_precision = case
                        when v_ev_start is null then v_target.evidence_time_precision
                        else 'minute'
                    end
                where id = v_target.id;
            end if;

            insert into public.memory_path_handoffs (
                assistant_id, run_id, kind,
                fast_path_memory_id, rumination_memory_id,
                memory_key, continuity_id, note
            ) values (
                v_run.assistant_id, v_run.id, 'adopt_thread',
                v_target.id, v_new_memory_id,
                v_memory_key, v_target.continuity_id,
                left(coalesce(v_reason, ''), 500)
            );

            v_counts := jsonb_set(v_counts, '{adopted_threads}',
                ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'adopt_thread', 'commit_status', 'adopted',
                'memory_id', v_new_memory_id,
                'fast_path_memory_id', v_target.id,
                'memory_key', v_memory_key,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'evidence_only' then
            -- Repeated expression: merge evidence into the current active
            -- version, never rewrite the body, never create a version. An op
            -- (or merge) carrying a key fills in a keyless fast_path takeover.
            v_memory_key := coalesce(
                nullif(lower(trim(coalesce(v_op->>'memory_key', ''))), ''),
                v_target.memory_key
            );
            if v_memory_key is not null
               and v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            update public.memories
            set maintained_by = 'rumination',
                memory_key = v_memory_key,
                evidence_message_ids = (
                    select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                    from unnest(v_target.evidence_message_ids || v_evidence) as id
                ),
                evidence_start_time = least(
                    coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                evidence_end_time = greatest(
                    coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                evidence_time_precision = case
                    when v_ev_start is null then v_target.evidence_time_precision
                    else 'minute'
                end
            where id = v_target.id;

            if v_target.maintained_by = 'fast_path' then
                insert into public.memory_path_handoffs (
                    assistant_id, run_id, kind,
                    fast_path_memory_id, rumination_memory_id,
                    memory_key, continuity_id, note
                ) values (
                    v_run.assistant_id, v_run.id, 'adopt_thread',
                    v_target.id, v_target.id,
                    v_memory_key, v_target.continuity_id,
                    left(coalesce(v_reason, 'evidence merge takeover'), 500)
                );
                v_counts := jsonb_set(v_counts, '{adopted_threads}',
                    ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
            end if;
            v_counts := jsonb_set(v_counts, '{evidence_only}',
                ((v_counts->>'evidence_only')::integer + 1)::text::jsonb);
            v_preview := v_preview || jsonb_build_object(
                'op', 'evidence_only', 'commit_status', 'evidence_merged',
                'memory_id', v_target.id,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type in ('update_thread', 'pause_thread', 'resume_thread', 'resolve_thread') then
            v_thread_state := case v_op_type
                when 'update_thread' then coalesce(nullif(trim(coalesce(v_op->>'thread_state', '')), ''), v_target.thread_state)
                when 'pause_thread' then 'paused'
                when 'resume_thread' then 'open'
                else 'resolved'
            end;
            if v_op_type = 'update_thread' and v_thread_state <> v_target.thread_state then
                raise exception 'memory_rumination_state_change_forbidden';
            end if;
            if v_op_type = 'pause_thread' and v_target.thread_state <> 'open' then
                raise exception 'memory_rumination_invalid_state_transition';
            end if;
            if v_op_type = 'resume_thread' and v_target.thread_state <> 'paused' then
                raise exception 'memory_rumination_invalid_state_transition';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;
            -- 结构基线一致性：网关随操作携带读取时的结构（服务端保留
            -- 的基线，模型无法伪造——parse 未知字段白名单拒绝）。持行锁
            -- 后与当前行核对，比较语义与原地更新的结构比较一致；读取之
            -- 后结构被修改过的旧请求整批拒绝，游标不推进。基线缺失时跳
            -- 过（直接调用 RPC 的既有调用方兼容；生产网关恒注入）。
            if v_op ? 'continuity_baseline'
               and jsonb_strip_nulls(coalesce(v_op->'continuity_baseline', '{}'::jsonb))
                   is distinct from
                   jsonb_strip_nulls(coalesce(v_target.continuity_data, '{}'::jsonb)) then
                raise exception 'memory_rumination_target_changed';
            end if;
            if v_content_hash = v_target.content_hash then
                -- 正文未变：content_hash 的唯一约束不允许插入同哈希新版本
                -- 行，结构变化只能在当前版本行上原地更新。仅当正文与结构
                -- 规范形（jsonb_strip_nulls：键序无关、显式 null 视为未
                -- 提供）都未变时才"只补证据"；结构或状态确有变化时必须先
                -- 过 validate_continuity_data，再原地更新，不得被提前返回
                -- 绕过。普通字段（title/importance/confidence/source_type/
                -- memory_time/time_precision/recall_*）无模型基线，不参与
                -- 变化判定，也不在原地更新中修改（随版本化路径正常刷新）。
                if v_op_type = 'update_thread'
                   and jsonb_strip_nulls(coalesce(v_continuity_data, '{}'::jsonb))
                       is not distinct from
                       jsonb_strip_nulls(coalesce(v_target.continuity_data, '{}'::jsonb)) then
                    -- 接管完整化：key 回填、格式与唯一冲突校验、维护归属
                    -- 切换，全部与版本化路径同规则；同正文同结构也可能完
                    -- 成一次 fast_path 接管。
                    v_memory_key := coalesce(
                        case when v_target.memory_key is null
                             then nullif(lower(trim(coalesce(v_op->>'memory_key', ''))), '') end,
                        v_target.memory_key
                    );
                    if v_memory_key is not null then
                        if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                            raise exception 'memory_rumination_invalid_memory_key';
                        end if;
                        if exists (
                            select 1 from public.memories as memory
                            where memory.memory_key = v_memory_key
                              and memory.is_active = true
                              and memory.verified = 'verified'
                              and memory.id <> v_target.id
                        ) then
                            raise exception 'memory_rumination_memory_key_conflict';
                        end if;
                    end if;
                    update public.memories
                    set evidence_message_ids = (
                            select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                            from unnest(v_target.evidence_message_ids || v_evidence) as id
                        ),
                        evidence_start_time = least(
                            coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                        evidence_end_time = greatest(
                            coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                        memory_key = v_memory_key,
                        maintained_by = 'rumination'
                    where id = v_target.id;
                    if v_target.maintained_by = 'fast_path' then
                        insert into public.memory_path_handoffs (
                            assistant_id, run_id, kind,
                            fast_path_memory_id, rumination_memory_id,
                            memory_key, continuity_id, note
                        ) values (
                            v_run.assistant_id, v_run.id, 'adopt_thread',
                            v_target.id, v_target.id,
                            v_memory_key, v_target.continuity_id,
                            left('evidence merge takeover; state=' || v_thread_state
                                 || '; ' || coalesce(v_reason, ''), 500)
                        );
                        v_counts := jsonb_set(v_counts, '{adopted_threads}',
                            ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
                    end if;
                    v_counts := jsonb_set(v_counts, '{evidence_only}',
                        ((v_counts->>'evidence_only')::integer + 1)::text::jsonb);
                    v_preview := v_preview || jsonb_build_object(
                        'op', 'update_thread', 'commit_status', 'evidence_merged_unchanged',
                        'memory_id', v_target.id,
                        'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                    );
                    continue;
                end if;
                if not public.validate_continuity_data('thread', v_thread_state, v_continuity_data) then
                    raise exception 'memory_rumination_invalid_continuity_data';
                end if;
                -- 接管完整化：同规则回填 key、切换维护归属；交接日志记录
                -- 实际生效的 key 与目标状态。反刍维护目标保持原归属，
                -- 不产生虚假接管记录。
                v_memory_key := coalesce(
                    case when v_target.memory_key is null
                         then nullif(lower(trim(coalesce(v_op->>'memory_key', ''))), '') end,
                    v_target.memory_key
                );
                if v_memory_key is not null then
                    if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                        raise exception 'memory_rumination_invalid_memory_key';
                    end if;
                    if exists (
                        select 1 from public.memories as memory
                        where memory.memory_key = v_memory_key
                          and memory.is_active = true
                          and memory.verified = 'verified'
                          and memory.id <> v_target.id
                    ) then
                        raise exception 'memory_rumination_memory_key_conflict';
                    end if;
                end if;
                update public.memories
                set continuity_data = v_continuity_data,
                    thread_state = v_thread_state,
                    memory_key = v_memory_key,
                    maintained_by = 'rumination',
                    evidence_message_ids = (
                        select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                        from unnest(v_target.evidence_message_ids || v_evidence) as id
                    ),
                    evidence_start_time = least(
                        coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                    evidence_end_time = greatest(
                        coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end)
                where id = v_target.id;
                -- Takeover bookkeeping: acting on a fast_path thread in place
                -- flips it to rumination maintenance just like the versioned
                -- path; the audit row records the effective key and the
                -- resulting target state.
                if v_target.maintained_by = 'fast_path' then
                    insert into public.memory_path_handoffs (
                        assistant_id, run_id, kind,
                        fast_path_memory_id, rumination_memory_id,
                        memory_key, continuity_id, note
                    ) values (
                        v_run.assistant_id, v_run.id, 'adopt_thread',
                        v_target.id, v_target.id,
                        v_memory_key, v_target.continuity_id,
                        left('structure update takeover; state=' || v_thread_state
                             || '; ' || coalesce(v_reason, ''), 500)
                    );
                    v_counts := jsonb_set(v_counts, '{adopted_threads}',
                        ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
                end if;
                v_counts := jsonb_set(v_counts, '{updated_versions}',
                    ((v_counts->>'updated_versions')::integer + 1)::text::jsonb);
                v_written := v_written + 1;
                v_preview := v_preview || jsonb_build_object(
                    'op', v_op_type, 'commit_status', 'structure_updated_in_place',
                    'memory_id', v_target.id,
                    'thread_state', v_thread_state,
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;
            if not public.validate_continuity_data('thread', v_thread_state, v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;

            -- The version keeps the thread's stable key; an op-provided key
            -- only fills in the key of a keyless fast_path takeover, and it
            -- never re-keys an existing rumination thread.
            v_memory_key := coalesce(
                case when v_target.memory_key is null
                     then nullif(lower(trim(coalesce(v_op->>'memory_key', ''))), '') end,
                v_target.memory_key
            );
            if v_memory_key is not null then
                if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                    raise exception 'memory_rumination_invalid_memory_key';
                end if;
                if exists (
                    select 1 from public.memories as memory
                    where memory.memory_key = v_memory_key
                      and memory.is_active = true
                      and memory.verified = 'verified'
                      and memory.id <> v_target.id
                ) then
                    raise exception 'memory_rumination_memory_key_conflict';
                end if;
            end if;

            -- Soft-deactivate first, then insert the successor and link both
            -- directions: one transaction, so a failure restores the old row
            -- while the partial unique index never sees two active versions
            -- of the same memory_key. The version keeps the same memory_key
            -- and continuity_id.
            update public.memories
            set is_active = false,
                superseded_at = now(),
                superseded_by_memory_id = null
            where id = v_target.id;

            insert into public.memories (
                content, title, tags, heat, importance, embedding,
                source, verified, is_active, recall_count,
                assistant_id, digest_run_id,
                source_first_message_id, source_last_message_id,
                confidence, content_hash,
                memory_key, supersedes_memory_id,
                continuity_id, continuity_schema_version, continuity_data,
                continuity_type, thread_state,
                memory_time, time_precision,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision,
                recall_scene, recall_tags, recall_embedding,
                source_type, producer_path, maintained_by
            ) values (
                v_content, coalesce(v_title, v_target.title), v_target.tags,
                v_target.heat, v_importance, v_embedding,
                'rumination', 'verified', true, 0,
                v_run.assistant_id, v_run.id,
                v_evidence[1], v_evidence[cardinality(v_evidence)],
                v_confidence, v_content_hash,
                v_memory_key, v_target.id,
                v_target.continuity_id, 1, v_continuity_data,
                'thread', v_thread_state,
                v_memory_time, v_time_precision,
                (select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                 from unnest(v_target.evidence_message_ids || v_evidence) as id),
                least(coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                greatest(coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                v_ev_precision,
                coalesce(v_recall_scene, v_target.recall_scene),
                coalesce(v_recall_tags, v_target.recall_tags, '{}'::text[]),
                coalesce(v_recall_embedding, v_target.recall_embedding),
                coalesce(v_source_type, v_target.source_type),
                'rumination', 'rumination'
            )
            returning id into v_new_memory_id;

            update public.memories
            set superseded_by_memory_id = v_new_memory_id
            where id = v_target.id;

            -- Takeover bookkeeping: the acted-on fast-path thread is now
            -- rumination-maintained; the audit row references both versions.
            if v_target.maintained_by = 'fast_path' then
                insert into public.memory_path_handoffs (
                    assistant_id, run_id, kind,
                    fast_path_memory_id, rumination_memory_id,
                    memory_key, continuity_id, note
                ) values (
                    v_run.assistant_id, v_run.id, 'adopt_thread',
                    v_target.id, v_new_memory_id,
                    v_memory_key, v_target.continuity_id,
                    left(coalesce(v_reason, 'thread op takeover'), 500)
                );
                v_counts := jsonb_set(v_counts, '{adopted_threads}',
                    ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
            end if;

            v_counts := jsonb_set(
                v_counts,
                (case v_op_type
                    when 'update_thread' then '{updated_versions}'
                    when 'pause_thread' then '{paused}'
                    when 'resume_thread' then '{resumed}'
                    else '{resolved}'
                end)::text[],
                (
                    coalesce((
                        case v_op_type
                            when 'update_thread' then v_counts->>'updated_versions'
                            when 'pause_thread' then v_counts->>'paused'
                            when 'resume_thread' then v_counts->>'resumed'
                            else v_counts->>'resolved'
                        end
                    )::integer, 0) + 1
                )::text::jsonb
            );
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', v_op_type,
                'commit_status', 'version_created',
                'memory_id', v_new_memory_id,
                'previous_memory_id', v_target.id,
                'thread_state', v_thread_state,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'create_request' then
            -- episode / profile / interaction_rule enter the review queue as
            -- rumination requests; thread is never a request class here.
            if v_continuity_type not in ('moment', 'inside_joke', 'episode', 'profile', 'interaction_rule') then
                raise exception 'memory_rumination_invalid_request_type';
            end if;
            if v_thread_state is not null then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if not public.validate_continuity_data(v_continuity_type, null, v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if char_length(trim(coalesce(v_op->>'reason', ''))) < 3 then
                raise exception 'memory_rumination_invalid_reason';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;

            if v_continuity_type = 'interaction_rule' then
                v_update_mode := 'replace';
                v_memory_key := lower(trim(coalesce(v_op->>'memory_key', '')));
                if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                    raise exception 'memory_rumination_invalid_memory_key';
                end if;
            else
                v_update_mode := 'append';
                if nullif(trim(coalesce(v_op->>'memory_key', '')), '') is not null then
                    raise exception 'memory_rumination_invalid_memory_key';
                end if;
                v_memory_key := null;
            end if;

            -- The content is already in flight or finalized by a request from
            -- ANY lane. A pending row from another source shares the partial
            -- unique index (assistant_id, content_hash), and an approved or
            -- merged request has already produced its formal result: in both
            -- cases a second rumination request must not be created.
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.content_hash = v_content_hash
                  and request.status in ('pending', 'approved', 'merged')
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- The content already exists as a formal memory: a new request
            -- would duplicate an active truth.
            if exists (
                select 1 from public.memories as memory
                where memory.is_active = true
                  and memory.verified = 'verified'
                  and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
                  and memory.content_hash = v_content_hash
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_active_memory',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- Same-content pending rumination request already exists: never a
            -- second one.
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.source = 'rumination'
                  and request.content_hash = v_content_hash
                  and request.status = 'pending'
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- Semantically identical pending rumination request: skip.
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.source = 'rumination'
                  and request.status = 'pending'
                  and public.memory_dedupe_text_similarity(request.content, v_content) >= 0.86
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_similar_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- A rejected/duplicate/conflict rumination request with the same
            -- content may only be re-filed with at least one piece of new raw
            -- evidence that postdates every evidence the prior request saw.
            -- Batch windows are strictly id-ordered, so evidence inside this
            -- batch is automatically newer; the explicit check keeps the rule
            -- enforced even if batch ordering ever changes.
            select max(evidence_id) into v_dup_max_evidence
            from public.memory_requests as request
            cross join unnest(request.evidence_message_ids) as evidence_id
            where request.assistant_id = v_run.assistant_id
              and request.source = 'rumination'
              and request.content_hash = v_content_hash
              and request.status in ('rejected', 'duplicate', 'conflict');
            if v_dup_max_evidence is not null
               and (select max(value) from unnest(v_evidence) as value) <= v_dup_max_evidence then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_without_new_evidence',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- Explicit absorption intent: only structurally valid fast-path
            -- formal memories may be listed (same assistant, produced by the
            -- fast path, verified, still active, not a closed thread). The
            -- semantic relation is judged by the model and the human
            -- reviewer; it is never auto-derived from evidence overlap.
            -- Snapshots are reset per request: two create_request ops in one
            -- batch must never inherit each other's targets.
            v_absorbed_ids := '{}'::bigint[];
            v_absorbed_snapshots := '[]'::jsonb;
            v_absorbed_ids := coalesce(array_agg(distinct value::bigint order by value::bigint), '{}'::bigint[])
                from jsonb_array_elements_text(coalesce(v_op->'absorbed_fast_path_memory_ids', '[]'::jsonb)) as entry(value)
                where entry.value ~ '^[0-9]+$';
            if coalesce(cardinality(v_absorbed_ids), 0)
                   <> jsonb_array_length(coalesce(v_op->'absorbed_fast_path_memory_ids', '[]'::jsonb))
               or coalesce(cardinality(v_absorbed_ids), 0) > 8 then
                raise exception 'memory_rumination_invalid_absorb_target';
            end if;
            foreach v_absorbed_id in array v_absorbed_ids loop
                select * into v_absorbed
                from public.memories
                where id = v_absorbed_id
                  and assistant_id = v_run.assistant_id
                  and producer_path = 'fast_path'
                  and verified = 'verified'
                  and is_active = true
                for update;
                if not found
                   or (
                        v_absorbed.continuity_type = 'thread'
                        and v_absorbed.thread_state in ('resolved', 'dissolved', 'abandoned')
                   ) then
                    raise exception 'memory_rumination_absorb_target_invalid';
                end if;
                -- Immutable snapshot taken from the locked database row at
                -- request-creation time; the model never supplies it.
                v_snapshot := public.build_absorb_target_snapshot(v_absorbed);
                v_absorbed_snapshots := v_absorbed_snapshots || v_snapshot;
            end loop;

            -- Snapshot integrity: the id list and the snapshot array must
            -- agree one-to-one with no duplicates, every snapshot must carry
            -- the full field set, and empty ids mean empty snapshots.
            if not public.validate_absorb_snapshots(v_absorbed_ids, v_absorbed_snapshots) then
                raise exception 'memory_rumination_absorb_snapshot_mismatch';
            end if;

            v_idempotency_key := 'rumination-' || v_run.id::text || '-' || v_content_hash;
            select id into v_prior_request_id
            from public.memory_requests as request
            where request.assistant_id = v_run.assistant_id
              and request.idempotency_key = v_idempotency_key
            limit 1;
            if found then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'request_id', v_prior_request_id,
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            insert into public.memory_requests (
                assistant_id, conversation_id, source_message_id,
                content, title, tags, importance, reason, content_hash,
                idempotency_key, status, source, producer_path,
                memory_key, update_mode, confidence,
                evidence_message_ids, source_time, memory_time, time_precision,
                digest_run_id, embedding,
                dedupe_state, dedupe_reason,
                continuity_type, thread_state, continuity_id,
                continuity_schema_version, continuity_data,
                source_type,
                evidence_start_time, evidence_end_time, evidence_time_precision,
                recall_scene, recall_tags, absorbed_fast_path_memory_ids,
                absorbed_fast_path_memory_snapshots
            ) values (
                v_run.assistant_id, nullif(trim(v_conversation_id), ''), v_evidence[1],
                v_content, v_title, array[v_continuity_type], v_importance,
                left(trim(v_op->>'reason'), 500), v_content_hash,
                v_idempotency_key, 'pending', 'rumination', 'rumination',
                v_memory_key, v_update_mode, v_confidence,
                v_evidence, v_ev_end, v_memory_time, v_time_precision,
                v_run.id, v_embedding,
                'none', null,
                v_continuity_type, null, null,
                1, v_continuity_data,
                v_source_type,
                v_ev_start, v_ev_end, v_ev_precision,
                v_recall_scene, coalesce(v_recall_tags, '{}'::text[]), v_absorbed_ids,
                v_absorbed_snapshots
            )
            on conflict (idempotency_key) do nothing
            returning id into v_prior_request_id;

            if v_prior_request_id is null then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            v_counts := jsonb_set(v_counts, '{created_requests}',
                ((v_counts->>'created_requests')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'create_request', 'commit_status', 'inserted_request',
                'request_id', v_prior_request_id,
                'continuity_type', v_continuity_type,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        raise exception 'memory_rumination_invalid_op';
    end loop;

    -- The batch is fully validated and applied: advance the independent
    -- cursor to the last message of this batch and close the run.
    update public.memory_rumination_cursors set
        initialized = true,
        last_processed_message_id = greatest(
            last_processed_message_id, v_run.source_last_message_id),
        status = 'ready',
        last_success_at = now(),
        last_batch_first_message_id = v_run.source_first_message_id,
        last_batch_last_message_id = v_run.source_last_message_id,
        last_batch_message_count = v_run.message_count,
        last_scheduled_date = case
            when v_run.trigger = 'rumination_scheduled'
            then (now() at time zone 'Asia/Shanghai')::date
            else last_scheduled_date
        end,
        updated_at = now()
    where assistant_id = v_run.assistant_id
    returning * into v_cursor;

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = v_op_count,
        inserted_count = v_written,
        preview_memories = v_preview,
        op_counts = v_counts,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = v_run.id;

    return jsonb_build_object(
        'run_id', v_run.id,
        'cursor', to_jsonb(v_cursor),
        'op_counts', v_counts,
        'preview', v_preview,
        'inserted_count', v_written
    );
end;
$function$;

revoke all on function public.commit_rumination_batch(bigint, jsonb)
    from public, anon, authenticated;
grant execute on function public.commit_rumination_batch(bigint, jsonb)
    to service_role;

-- 数据库侧长度校验与解析器/提交 RPC 对齐：600 → 3000。
-- （这是本迁移唯一的表级变更：替换 CHECK 约束，不改列。）
alter table public.memory_requests
    drop constraint if exists memory_requests_content_length;
alter table public.memory_requests
    add constraint memory_requests_content_length
        check (char_length(content) between 5 and 3000);

-- 管理编辑/新建/换型入口的正文校验同步对齐（基于 20260902010000 的现定义
-- 逐字重建，仅改范围），避免长记忆无法在管理后台编辑保存。

create or replace function public.create_admin_memory_v1(
    p_assistant_id text,
    p_content text,
    p_content_hash text,
    p_title text,
    p_tags text[],
    p_importance integer,
    p_source_type text,
    p_memory_time text,
    p_time_precision text,
    p_recall_scene text,
    p_recall_tags text[],
    p_recall_embedding extensions.vector,
    p_continuity_type text,
    p_thread_state text,
    p_continuity_data jsonb,
    p_evidence_message_ids bigint[]
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_assistant_id text := nullif(btrim(coalesce(p_assistant_id, '')), '');
    v_content text := btrim(coalesce(p_content, ''));
    v_title text := nullif(left(btrim(coalesce(p_title, '')), 100), '');
    v_recall_scene text := nullif(btrim(coalesce(p_recall_scene, '')), '');
    v_continuity_id uuid;
    v_evidence jsonb;
    v_memory public.memories%rowtype;
    v_memory_time timestamptz;
    v_time_precision text;
begin
    if v_assistant_id is null then
        raise exception 'admin_memory_assistant_required';
    end if;
    if char_length(v_content) not between 5 and 3000 then
        raise exception 'admin_memory_invalid_content';
    end if;
    if p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'admin_memory_invalid_content_hash';
    end if;
    if p_importance is null or p_importance not between 1 and 10 then
        raise exception 'admin_memory_invalid_importance';
    end if;
    if not public.admin_memory_tags_ok(p_tags)
       or not public.admin_memory_tags_ok(p_recall_tags) then
        raise exception 'admin_memory_invalid_tags';
    end if;
    if p_continuity_type not in (
        'moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'
    ) then
        raise exception 'admin_memory_invalid_type';
    end if;
    -- Six-class memories always carry a fully validated v1 structure; the
    -- admin console never creates unclassified rows.
    if not public.validate_continuity_data(p_continuity_type, p_thread_state, p_continuity_data) then
        raise exception 'admin_memory_invalid_continuity_data';
    end if;
    if p_time_precision is not null
       and p_time_precision not in ('minute', 'hour', 'day', 'approximate', 'unknown') then
        raise exception 'admin_memory_invalid_time_precision';
    end if;
    if p_source_type is not null
       and p_source_type not in (
           'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
           'roleplay', 'tool_result', 'system_meta', 'unknown'
       ) then
        raise exception 'admin_memory_invalid_source_type';
    end if;
    -- A scene-carrying write must arrive with its vector: the gateway embeds
    -- before this transaction, so a null vector under a live scene means the
    -- caller tried to skip generation.
    if v_recall_scene is not null and p_recall_embedding is null then
        raise exception 'admin_memory_recall_vector_missing';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_assistant_id, 0));

    -- content_hash carries a table-wide unique constraint: refuse a
    -- duplicate with the stable business code before anything is written,
    -- so a rejected create never leaves rows or continuity objects behind.
    -- Other rows' hashes are never cleared here: hash release belongs
    -- exclusively to the confirmed same-chain type-change flow.
    if exists (
        select 1 from public.memories as other
        where other.content_hash = p_content_hash
    ) then
        raise exception 'admin_memory_content_exists';
    end if;

    insert into public.memory_continuity_objects (assistant_id)
    values (v_assistant_id)
    returning continuity_id into v_continuity_id;

    v_evidence := public.admin_memory_resolve_evidence(v_assistant_id, p_evidence_message_ids);

    -- Empty event time always stores precision 'unknown': claiming minute,
    -- hour, or day accuracy for a time that does not exist is inconsistent.
    v_memory_time := public.admin_memory_normalize_event_time(p_memory_time, p_time_precision);
    v_time_precision := coalesce(nullif(btrim(coalesce(p_time_precision, '')), ''), 'unknown');
    if v_memory_time is null then
        v_time_precision := 'unknown';
    end if;

    insert into public.memories (
        content, title, tags, heat, importance, embedding,
        source, verified, is_active, recall_count,
        assistant_id, digest_run_id, confidence, content_hash,
        memory_key, supersedes_memory_id, superseded_by_memory_id, superseded_at,
        continuity_id, continuity_type, continuity_schema_version, continuity_data,
        source_type, thread_state,
        evidence_message_ids, source_time,
        memory_time, time_precision,
        evidence_start_time, evidence_end_time, evidence_time_precision,
        recall_scene, recall_tags, recall_embedding
    ) values (
        v_content, v_title,
        coalesce(p_tags, '{}'::text[]),
        50.0, p_importance, null,
        'manual', 'verified', true, 0,
        v_assistant_id, null, 1.0, p_content_hash,
        null, null, null, null,
        v_continuity_id, p_continuity_type, 1, p_continuity_data,
        nullif(btrim(coalesce(p_source_type, '')), ''),
        case when p_continuity_type = 'thread' then p_thread_state else null end,
        public.admin_memory_ids_from_evidence(v_evidence),
        -- source_time is the AI pipeline's own evidence clock; the admin
        -- console has no AI source, so it always stays NULL.
        null,
        v_memory_time,
        v_time_precision,
        (v_evidence->>'start')::timestamptz,
        (v_evidence->>'end')::timestamptz,
        (v_evidence->>'precision'),
        v_recall_scene,
        p_recall_tags,
        case when v_recall_scene is null then null else p_recall_embedding end
    )
    returning * into v_memory;

    return jsonb_build_object(
        'memory_id', v_memory.id,
        'continuity_id', v_memory.continuity_id,
        'source', v_memory.source,
        'verified', v_memory.verified,
        'is_active', v_memory.is_active,
        'heat', v_memory.heat
    );
end;
$function$;

revoke all on function public.create_admin_memory_v1(text, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) from public, anon, authenticated;
grant execute on function public.create_admin_memory_v1(text, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) to service_role;

create or replace function public.edit_admin_memory_v1(
    p_memory_id integer,
    p_patch jsonb,
    p_content_hash text,
    p_recall_embedding extensions.vector,
    p_assistant_id text default null
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_memory public.memories%rowtype;
    v_new_type text;
    v_new_thread_state text;
    v_continuity_data jsonb;
    v_write_continuity boolean := false;
    v_content text;
    v_evidence jsonb;
    v_memory_time timestamptz;
    v_time_precision text;
    v_updated public.memories%rowtype;
    v_new_continuity_id uuid;
begin
    if p_patch is null or jsonb_typeof(p_patch) <> 'object' then
        raise exception 'admin_memory_invalid_patch';
    end if;

    select * into v_memory
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if not public.admin_memory_is_current(v_memory) then
        raise exception 'admin_memory_not_editable';
    end if;

    v_new_type := nullif(btrim(coalesce(p_patch->>'continuity_type', '')), '');
    if v_new_type is not null
       and v_new_type not in (
           'moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'
       ) then
        raise exception 'admin_memory_invalid_type';
    end if;
    if v_new_type is not null and v_memory.continuity_type is not null
       and v_new_type <> v_memory.continuity_type then
        -- Switching an existing class must go through the versioned
        -- type-change flow, never through an in-place edit.
        raise exception 'admin_memory_type_change_forbidden';
    end if;
    v_new_type := coalesce(v_new_type, v_memory.continuity_type);

    if v_new_type is null then
        -- Unclassified legacy row: ordinary-field edits never trigger
        -- continuity validation, and continuity structure cannot be
        -- attached without adopting a class first.
        if p_patch ? 'continuity_data' or p_patch ? 'thread_state' then
            raise exception 'admin_memory_class_required';
        end if;
    elsif p_patch ? 'continuity_data'
       or p_patch ? 'thread_state'
       or (p_patch ? 'continuity_type' and v_memory.continuity_type is null) then
        -- The continuity structure is being written (same-type edit or a
        -- one-time class adoption), so the complete type validation runs.
        v_write_continuity := true;
        v_new_thread_state := coalesce(
            nullif(btrim(coalesce(p_patch->>'thread_state', '')), ''),
            v_memory.thread_state
        );
        if p_patch ? 'continuity_data' then
            v_continuity_data := p_patch->'continuity_data';
        elsif v_memory.continuity_data is not null then
            v_continuity_data := v_memory.continuity_data;
        else
            raise exception 'admin_memory_invalid_continuity_data';
        end if;
        if not public.validate_continuity_data(v_new_type, v_new_thread_state, v_continuity_data) then
            raise exception 'admin_memory_invalid_continuity_data';
        end if;
    end if;
    -- else: ordinary fields only -- continuity fields are left untouched and
    -- unvalidated, so incomplete legacy rows are never forced to complete.

    if p_patch ? 'content' then
        v_content := btrim(coalesce(p_patch->>'content', ''));
        if char_length(v_content) not between 5 and 3000 then
            raise exception 'admin_memory_invalid_content';
        end if;
        if p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$' then
            raise exception 'admin_memory_invalid_content_hash';
        end if;
        -- An in-place edit must not steal another memory's content_hash
        -- (table-wide unique constraint); refuse with a stable code.
        if exists (
            select 1 from public.memories as other
            where other.content_hash = p_content_hash
              and other.id <> v_memory.id
        ) then
            raise exception 'admin_memory_content_exists';
        end if;
    elsif p_content_hash is not null then
        raise exception 'admin_memory_invalid_content_hash';
    end if;

    if p_patch ? 'recall_scene' then
        -- The patch is authoritative for the whole recall pair: an empty
        -- scene clears the vector, a live scene must arrive embedded.
        if nullif(btrim(coalesce(p_patch->>'recall_scene', '')), '') is not null
           and p_recall_embedding is null then
            raise exception 'admin_memory_recall_vector_missing';
        end if;
    elsif p_recall_embedding is not null then
        raise exception 'admin_memory_recall_vector_missing';
    end if;

    if p_patch ? 'importance'
       and (p_patch->>'importance')::integer not between 1 and 10 then
        raise exception 'admin_memory_invalid_importance';
    end if;
    if p_patch ? 'time_precision'
       and nullif(btrim(coalesce(p_patch->>'time_precision', '')), '') is not null
       and p_patch->>'time_precision' not in ('minute', 'hour', 'day', 'approximate', 'unknown') then
        raise exception 'admin_memory_invalid_time_precision';
    end if;
    if p_patch ? 'source_type'
       and nullif(btrim(coalesce(p_patch->>'source_type', '')), '') is not null
       and p_patch->>'source_type' not in (
           'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
           'roleplay', 'tool_result', 'system_meta', 'unknown'
       ) then
        raise exception 'admin_memory_invalid_source_type';
    end if;
    if (p_patch ? 'tags'
        and not public.admin_memory_tags_ok((
            select array_agg(t.value order by t.value)
            from jsonb_array_elements_text(p_patch->'tags') as t(value)
       )))
       or (p_patch ? 'recall_tags'
        and not public.admin_memory_tags_ok((
            select array_agg(t.value order by t.value)
            from jsonb_array_elements_text(p_patch->'recall_tags') as t(value)
       ))) then
        raise exception 'admin_memory_invalid_tags';
    end if;

    -- Event time and precision are decided together: clearing the time
    -- always stores 'unknown', and a time-less row can never claim
    -- minute/hour/day accuracy, on every write path alike.
    v_memory_time := v_memory.memory_time;
    v_time_precision := v_memory.time_precision;
    if p_patch ? 'memory_time' then
        v_memory_time := public.admin_memory_normalize_event_time(
            p_patch->>'memory_time',
            case when p_patch ? 'time_precision'
                 then nullif(btrim(coalesce(p_patch->>'time_precision', '')), '')
                 else v_memory.time_precision end);
        if v_memory_time is null then
            v_time_precision := 'unknown';
        elsif p_patch ? 'time_precision' then
            v_time_precision := coalesce(
                nullif(btrim(coalesce(p_patch->>'time_precision', '')), ''), 'unknown');
        end if;
    elsif p_patch ? 'time_precision' then
        v_time_precision := coalesce(
            nullif(btrim(coalesce(p_patch->>'time_precision', '')), ''), 'unknown');
        if v_memory_time is null then
            v_time_precision := 'unknown';
        end if;
    end if;

    if p_patch ? 'evidence_message_ids' then
        v_evidence := public.admin_memory_resolve_evidence(
            coalesce(v_memory.assistant_id, nullif(btrim(coalesce(p_assistant_id, '')), '')),
            (select array_agg((value)::bigint order by (value)::bigint)
             from jsonb_array_elements_text(p_patch->'evidence_message_ids') as entry(value)
             where entry.value ~ '^[0-9]+$')
        );
        -- Re-derived evidence replaces the old window wholesale; an empty
        -- list leaves no times behind.
    else
        v_evidence := null;
    end if;

    update public.memories as memory set
        title = case
            when p_patch ? 'title'
            then nullif(left(btrim(coalesce(p_patch->>'title', '')), 100), '')
            else memory.title end,
        content = case
            when p_patch ? 'content' then btrim(p_patch->>'content')
            else memory.content end,
        content_hash = case
            when p_patch ? 'content' then p_content_hash
            else memory.content_hash end,
        tags = case
            when p_patch ? 'tags'
            then coalesce((
                    select array_agg(distinct t.value)
                    from jsonb_array_elements_text(p_patch->'tags') as t(value)
                    where nullif(btrim(t.value), '') is not null
                 ), '{}'::text[])
            else memory.tags end,
        importance = case
            when p_patch ? 'importance' then (p_patch->>'importance')::integer
            else memory.importance end,
        source_type = case
            when p_patch ? 'source_type'
            then nullif(btrim(coalesce(p_patch->>'source_type', '')), '')
            else memory.source_type end,
        memory_time = v_memory_time,
        time_precision = v_time_precision,
        recall_scene = case
            when p_patch ? 'recall_scene'
            then nullif(btrim(coalesce(p_patch->>'recall_scene', '')), '')
            else memory.recall_scene end,
        recall_tags = case
            when p_patch ? 'recall_tags'
            then coalesce((
                    select array_agg(distinct t.value)
                    from jsonb_array_elements_text(p_patch->'recall_tags') as t(value)
                    where nullif(btrim(t.value), '') is not null
                 ), '{}'::text[])
            else memory.recall_tags end,
        recall_embedding = case
            when p_patch ? 'recall_scene'
            then case
                when nullif(btrim(coalesce(p_patch->>'recall_scene', '')), '') is null
                then null
                else p_recall_embedding end
            else memory.recall_embedding end,
        evidence_message_ids = case
            when p_patch ? 'evidence_message_ids' then public.admin_memory_ids_from_evidence(v_evidence)
            else memory.evidence_message_ids end,
        evidence_start_time = case
            when p_patch ? 'evidence_message_ids' then (v_evidence->>'start')::timestamptz
            else memory.evidence_start_time end,
        evidence_end_time = case
            when p_patch ? 'evidence_message_ids' then (v_evidence->>'end')::timestamptz
            else memory.evidence_end_time end,
        evidence_time_precision = case
            when p_patch ? 'evidence_message_ids' then (v_evidence->>'precision')
            else memory.evidence_time_precision end,
        thread_state = case
            when not v_write_continuity then memory.thread_state
            when v_new_type is distinct from 'thread' then null
            else nullif(btrim(coalesce(p_patch->>'thread_state', '')), '') end,
        continuity_type = case
            when v_write_continuity then v_new_type
            else memory.continuity_type end,
        continuity_schema_version = case
            when v_write_continuity then 1
            else memory.continuity_schema_version end,
        continuity_data = case
            when v_write_continuity then v_continuity_data
            else memory.continuity_data end
    where memory.id = v_memory.id
    returning * into v_updated;

    -- A newly adopted class needs a continuity identity allocated here. The
    -- gateway resolves the assistant for this purpose; nothing is invented.
    if v_write_continuity and v_updated.continuity_id is null then
        insert into public.memory_continuity_objects (assistant_id)
        values (coalesce(
            v_updated.assistant_id,
            nullif(btrim(coalesce(p_assistant_id, '')), ''),
            ''
        ))
        returning continuity_id into v_new_continuity_id;
        update public.memories
            set continuity_id = v_new_continuity_id
            where id = v_updated.id
            returning * into v_updated;
    end if;

    if v_updated.continuity_id is not null then
        update public.memory_continuity_objects
            set updated_at = now()
            where continuity_id = v_updated.continuity_id;
    end if;

    return jsonb_build_object('memory', to_jsonb(v_updated));
end;
$function$;

revoke all on function public.edit_admin_memory_v1(integer, jsonb, text, extensions.vector, text) from public, anon, authenticated;
grant execute on function public.edit_admin_memory_v1(integer, jsonb, text, extensions.vector, text) to service_role;

create or replace function public.change_memory_type_v1(
    p_memory_id integer,
    p_content text,
    p_content_hash text,
    p_title text,
    p_tags text[],
    p_importance integer,
    p_source_type text,
    p_memory_time text,
    p_time_precision text,
    p_recall_scene text,
    p_recall_tags text[],
    p_recall_embedding extensions.vector,
    p_continuity_type text,
    p_thread_state text,
    p_continuity_data jsonb,
    p_evidence_message_ids bigint[]
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_source public.memories%rowtype;
    v_earlier public.memories%rowtype;
    v_new public.memories%rowtype;
    v_continuity_id uuid;
    v_content text := btrim(coalesce(p_content, ''));
    v_recall_scene text := nullif(btrim(coalesce(p_recall_scene, '')), '');
    v_evidence jsonb;
    v_removed_earlier boolean := false;
    v_same_hash boolean := false;
    v_memory_time timestamptz;
    v_time_precision text;
begin
    if char_length(v_content) not between 5 and 3000 then
        raise exception 'admin_memory_invalid_content';
    end if;
    if p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'admin_memory_invalid_content_hash';
    end if;
    if p_importance is null or p_importance not between 1 and 10 then
        raise exception 'admin_memory_invalid_importance';
    end if;
    if not public.admin_memory_tags_ok(p_tags)
       or not public.admin_memory_tags_ok(p_recall_tags) then
        raise exception 'admin_memory_invalid_tags';
    end if;
    if p_continuity_type not in (
        'moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'
    ) then
        raise exception 'admin_memory_invalid_type';
    end if;
    if not public.validate_continuity_data(p_continuity_type, p_thread_state, p_continuity_data) then
        raise exception 'admin_memory_invalid_continuity_data';
    end if;
    if p_time_precision is not null
       and p_time_precision not in ('minute', 'hour', 'day', 'approximate', 'unknown') then
        raise exception 'admin_memory_invalid_time_precision';
    end if;
    if p_source_type is not null
       and p_source_type not in (
           'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
           'roleplay', 'tool_result', 'system_meta', 'unknown'
       ) then
        raise exception 'admin_memory_invalid_source_type';
    end if;
    if v_recall_scene is not null and p_recall_embedding is null then
        raise exception 'admin_memory_recall_vector_missing';
    end if;

    select * into v_source
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if not public.admin_memory_is_current(v_source) then
        raise exception 'admin_memory_not_editable';
    end if;
    if v_source.continuity_type is null then
        raise exception 'admin_memory_source_unclassified';
    end if;
    if p_continuity_type = v_source.continuity_type then
        raise exception 'admin_memory_type_unchanged';
    end if;

    -- Serialize same-identity operations (change/undo/restore) so two
    -- concurrent flows can never both believe they own the single active
    -- slot for this continuity identity.
    if v_source.continuity_id is null then
        insert into public.memory_continuity_objects (assistant_id)
        values (coalesce(v_source.assistant_id, ''))
        returning continuity_id into v_continuity_id;
        update public.memories
            set continuity_id = v_continuity_id
            where id = v_source.id;
    else
        v_continuity_id := v_source.continuity_id;
    end if;
    perform pg_advisory_xact_lock(hashtextextended(v_continuity_id::text, 0));

    -- content_hash carries a table-wide unique constraint. A type change may
    -- keep the content untouched, so the new version legitimately reuses the
    -- source hash. Occupation by unrelated memories is still refused; the
    -- source itself and the to-be-deleted parent are handled below inside
    -- this same transaction.
    v_same_hash := (p_content_hash = v_source.content_hash);
    if exists (
        select 1 from public.memories as other
        where other.content_hash = p_content_hash
          and other.id <> v_source.id
          and (v_source.supersedes_memory_id is null
               or other.id <> v_source.supersedes_memory_id)
    ) then
        raise exception 'admin_memory_content_exists';
    end if;

    v_evidence := public.admin_memory_resolve_evidence(
        v_source.assistant_id, p_evidence_message_ids
    );

    -- Empty event time always stores precision 'unknown', matching create.
    v_memory_time := public.admin_memory_normalize_event_time(p_memory_time, p_time_precision);
    v_time_precision := coalesce(nullif(btrim(coalesce(p_time_precision, '')), ''), 'unknown');
    if v_memory_time is null then
        v_time_precision := 'unknown';
    end if;

    -- Retire the source version FIRST: the one-active-per-identity index
    -- must never see two active rows, even inside this transaction.
    update public.memories
        set is_active = false,
            superseded_at = now()
        where id = v_source.id;

    -- Same-content type change: the retired source temporarily releases the
    -- hash so the new version can hold the real value; the undo hands it
    -- back. Any failure rolls the whole release back with the transaction.
    if v_same_hash then
        update public.memories
            set content_hash = null
            where id = v_source.id;
    end if;

    -- Keep exactly two generations, and free the parent BEFORE the insert:
    -- deleting it releases a chain-internal hash occupation (A -> B -> C
    -- with unchanged content) and nulls every dangling reference through
    -- the on-delete set null foreign keys.
    if v_source.supersedes_memory_id is not null then
        select * into v_earlier
        from public.memories as memory
        where memory.id = v_source.supersedes_memory_id
        for update;
        if found then
            if v_earlier.is_active is true then
                raise exception 'admin_memory_chain_conflict';
            end if;
            delete from public.memories where id = v_earlier.id;
            v_removed_earlier := true;
            if v_earlier.continuity_id is not null
               and v_earlier.continuity_id <> v_continuity_id then
                perform public.admin_memory_reap_continuity_object(v_earlier.continuity_id);
            end if;
        end if;
    end if;

    insert into public.memories (
        content, title, tags, heat, importance, embedding,
        source, verified, is_active, recall_count,
        assistant_id, digest_run_id, confidence, content_hash,
        memory_key, supersedes_memory_id, superseded_by_memory_id, superseded_at,
        continuity_id, continuity_type, continuity_schema_version, continuity_data,
        source_type, thread_state,
        evidence_message_ids, source_time,
        memory_time, time_precision,
        evidence_start_time, evidence_end_time, evidence_time_precision,
        recall_scene, recall_tags, recall_embedding
    ) values (
        v_content,
        nullif(left(btrim(coalesce(p_title, '')), 100), ''),
        coalesce(p_tags, '{}'::text[]),
        50.0, p_importance, null,
        'manual', 'verified', true, 0,
        v_source.assistant_id, null, 1.0, p_content_hash,
        null, v_source.id, null, null,
        v_continuity_id, p_continuity_type, 1, p_continuity_data,
        nullif(btrim(coalesce(p_source_type, '')), ''),
        case when p_continuity_type = 'thread' then p_thread_state else null end,
        public.admin_memory_ids_from_evidence(v_evidence),
        null,
        v_memory_time,
        v_time_precision,
        (v_evidence->>'start')::timestamptz,
        (v_evidence->>'end')::timestamptz,
        (v_evidence->>'precision'),
        v_recall_scene,
        p_recall_tags,
        case when v_recall_scene is null then null else p_recall_embedding end
    )
    returning * into v_new;

    update public.memories
        set superseded_by_memory_id = v_new.id
        where id = v_source.id;

    update public.memory_continuity_objects
        set updated_at = now()
        where continuity_id = v_continuity_id;

    return jsonb_build_object(
        'memory', to_jsonb(v_new),
        'previous_version_id', v_source.id,
        'removed_version_id', case when v_removed_earlier then v_earlier.id else null end
    );
end;
$function$;

revoke all on function public.change_memory_type_v1(integer, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) from public, anon, authenticated;
grant execute on function public.change_memory_type_v1(integer, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) to service_role;

-- 审核链的共享内容校验同样对齐到 5–3000（v2 是链底门禁，v3 有第二处
-- 检查；两处均基于 20260831010000 的现定义逐字重建，仅改范围）。
-- v4/v5 仅透传，无正文长度校验，不需要重建。

create or replace function public.review_memory_request_v2(
    p_request_id bigint,
    p_action text,
    p_content text default null,
    p_title text default null,
    p_tags text[] default null,
    p_importance integer default null,
    p_content_hash text default null,
    p_reviewed_by text default 'gateway_admin',
    p_review_note text default null,
    p_memory_key text default null,
    p_update_mode text default null
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_memory_id integer;
    v_existing_content_id integer;
    v_current_memory_id integer;
    v_superseded_memory_id integer;
    v_content text;
    v_title text;
    v_tags text[];
    v_importance integer;
    v_content_hash text;
    v_memory_key text;
    v_update_mode text;
    v_status text := 'approved';
begin
    if p_action not in ('approve', 'reject') then
        raise exception 'memory_request_invalid_action';
    end if;

    select * into v_request
    from public.memory_requests
    where id = p_request_id
    for update;

    if not found then
        raise exception 'memory_request_not_found';
    end if;

    if (p_action = 'approve' and v_request.status in ('approved', 'merged'))
       or (p_action = 'reject' and v_request.status = 'rejected') then
        return jsonb_build_object(
            'changed', false,
            'superseded_memory_id', null,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'memory_id', v_request.memory_id,
                'reviewed_at', v_request.reviewed_at
            )
        );
    end if;

    if v_request.status <> 'pending' then
        raise exception 'memory_request_not_pending';
    end if;

    if p_action = 'reject' then
        update public.memory_requests set
            status = 'rejected',
            reviewed_at = now(),
            reviewed_by = left(coalesce(nullif(trim(p_reviewed_by), ''), 'gateway_admin'), 120),
            review_note = nullif(left(trim(coalesce(p_review_note, '')), 500), ''),
            updated_at = now()
        where id = v_request.id
        returning * into v_request;

        return jsonb_build_object(
            'changed', true,
            'superseded_memory_id', null,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'memory_id', v_request.memory_id,
                'reviewed_at', v_request.reviewed_at
            )
        );
    end if;

    v_content := trim(coalesce(nullif(p_content, ''), v_request.content));
    v_title := nullif(left(trim(coalesce(p_title, v_request.title, '')), 100), '');
    v_tags := coalesce(p_tags, v_request.tags, '{}'::text[]);
    v_importance := coalesce(p_importance, v_request.importance, 5);
    v_content_hash := coalesce(nullif(trim(p_content_hash), ''), v_request.content_hash);
    v_memory_key := nullif(lower(trim(coalesce(p_memory_key, v_request.memory_key, ''))), '');
    v_update_mode := lower(trim(coalesce(p_update_mode, v_request.update_mode, 'append')));

    if char_length(v_content) not between 5 and 3000 then
        raise exception 'memory_request_invalid_content';
    end if;
    if cardinality(v_tags) > 5 then
        raise exception 'memory_request_invalid_tags';
    end if;
    if v_importance not between 1 and 10 then
        raise exception 'memory_request_invalid_importance';
    end if;
    if char_length(v_content_hash) <> 64 then
        raise exception 'memory_request_invalid_hash';
    end if;
    if v_update_mode not in ('append', 'replace') then
        raise exception 'memory_request_invalid_update_mode';
    end if;
    if v_memory_key is not null
       and v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
        raise exception 'memory_request_invalid_memory_key';
    end if;
    if (v_update_mode = 'replace' and v_memory_key is null)
       or (v_update_mode = 'append' and v_memory_key is not null) then
        raise exception 'memory_request_invalid_replace_key';
    end if;

    if v_update_mode = 'replace' then
        perform pg_advisory_xact_lock(
            hashtextextended('memory-key:' || v_memory_key, 0)
        );

        select memory.id into v_current_memory_id
        from public.memories as memory
        where memory.memory_key = v_memory_key
          and memory.is_active = true
          and memory.verified = 'verified'
        for update;

        if v_current_memory_id is not null and exists (
            select 1
            from public.memory_requests as newer_request
            where newer_request.memory_id = v_current_memory_id
              and newer_request.status in ('approved', 'merged')
              and newer_request.created_at > v_request.created_at
        ) then
            raise exception 'memory_request_stale_update';
        end if;
    end if;

    select memory.id into v_existing_content_id
    from public.memories as memory
    where memory.content_hash = v_content_hash
    for update;

    if v_update_mode = 'replace'
       and v_current_memory_id is not null
       and v_current_memory_id is distinct from v_existing_content_id then
        update public.memories
        set
            is_active = false,
            superseded_at = now(),
            superseded_by_memory_id = null
        where id = v_current_memory_id;
    end if;

    insert into public.memories (
        content,
        title,
        tags,
        heat,
        importance,
        embedding,
        source,
        verified,
        is_active,
        recall_count,
        assistant_id,
        digest_run_id,
        source_first_message_id,
        source_last_message_id,
        confidence,
        content_hash,
        memory_key,
        supersedes_memory_id,
        superseded_by_memory_id,
        superseded_at
    ) values (
        v_content,
        v_title,
        v_tags,
        least(greatest(v_importance * 10.0, 0), 100),
        v_importance,
        null,
        'ai_tool_request',
        'verified',
        true,
        0,
        v_request.assistant_id,
        null,
        v_request.source_message_id,
        v_request.source_message_id,
        1.0,
        v_content_hash,
        case when v_update_mode = 'replace' then v_memory_key else null end,
        case
            when v_update_mode = 'replace'
             and v_current_memory_id is not null
             and v_existing_content_id is null
            then v_current_memory_id
            else null
        end,
        null,
        null
    )
    on conflict (content_hash) do update set
        content = excluded.content,
        title = excluded.title,
        tags = excluded.tags,
        heat = greatest(public.memories.heat, excluded.heat),
        importance = excluded.importance,
        source = excluded.source,
        verified = 'verified',
        is_active = true,
        assistant_id = coalesce(public.memories.assistant_id, excluded.assistant_id),
        source_first_message_id = coalesce(
            public.memories.source_first_message_id,
            excluded.source_first_message_id
        ),
        source_last_message_id = coalesce(
            public.memories.source_last_message_id,
            excluded.source_last_message_id
        ),
        confidence = greatest(public.memories.confidence, excluded.confidence),
        memory_key = case
            when v_update_mode = 'replace' then excluded.memory_key
            else public.memories.memory_key
        end,
        superseded_by_memory_id = null,
        superseded_at = null
    returning id into v_memory_id;

    if v_update_mode = 'replace'
       and v_current_memory_id is not null
       and v_current_memory_id <> v_memory_id then
        v_superseded_memory_id := v_current_memory_id;
        update public.memories
        set
            is_active = false,
            superseded_at = now(),
            superseded_by_memory_id = v_memory_id
        where id = v_current_memory_id;
    end if;

    if exists (
        select 1
        from public.memory_requests as existing
        where existing.id <> v_request.id
          and existing.assistant_id = v_request.assistant_id
          and existing.content_hash = v_content_hash
          and existing.status in ('pending', 'approved', 'merged')
    ) then
        v_status := 'merged';
    end if;

    update public.memory_requests set
        content = v_content,
        title = v_title,
        tags = v_tags,
        importance = v_importance,
        content_hash = v_content_hash,
        memory_key = case when v_update_mode = 'replace' then v_memory_key else null end,
        update_mode = v_update_mode,
        status = v_status,
        memory_id = v_memory_id,
        reviewed_at = now(),
        reviewed_by = left(coalesce(nullif(trim(p_reviewed_by), ''), 'gateway_admin'), 120),
        review_note = nullif(left(trim(coalesce(p_review_note, '')), 500), ''),
        updated_at = now()
    where id = v_request.id
    returning * into v_request;

    return jsonb_build_object(
        'changed', true,
        'superseded_memory_id', v_superseded_memory_id,
        'request', jsonb_build_object(
            'id', v_request.id,
            'status', v_request.status,
            'memory_id', v_request.memory_id,
            'reviewed_at', v_request.reviewed_at,
            'memory_key', v_request.memory_key,
            'update_mode', v_request.update_mode
        )
    );
end;
$function$;

revoke all on function public.review_memory_request_v2(
    bigint, text, text, text, text[], integer, text, text, text, text, text
) from public, anon, authenticated;

grant execute on function public.review_memory_request_v2(
    bigint, text, text, text, text[], integer, text, text, text, text, text
) to service_role;

create or replace function public.review_memory_request_v3(
    p_request_id bigint,
    p_action text,
    p_content text default null,
    p_title text default null,
    p_tags text[] default null,
    p_importance integer default null,
    p_content_hash text default null,
    p_reviewed_by text default 'gateway_admin',
    p_review_note text default null,
    p_memory_key text default null,
    p_update_mode text default null,
    p_related_memory_id integer default null
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_target public.memories%rowtype;
    v_result jsonb;
    v_result_memory_id integer;
    v_from_status text;
    v_action text := lower(trim(coalesce(p_action, '')));
    v_content text;
    v_title text;
    v_tags text[];
    v_importance integer;
    v_content_hash text;
    v_target_memory_key text;
    v_reviewed_by text := left(
        coalesce(nullif(trim(p_reviewed_by), ''), 'gateway_admin'),
        120
    );
    v_review_note text := nullif(left(trim(coalesce(p_review_note, '')), 500), '');
begin
    if v_action not in ('approve', 'reject', 'merge', 'duplicate', 'conflict') then
        raise exception 'memory_request_invalid_action';
    end if;

    select * into v_request
    from public.memory_requests
    where id = p_request_id
    for update;

    if not found then
        raise exception 'memory_request_not_found';
    end if;

    v_from_status := v_request.status;

    -- Approval and rejection retain the proven v2 behavior. A conflict is an
    -- unresolved review state, so the user may later resolve it either way.
    if v_action in ('approve', 'reject') then
        if (v_action = 'approve' and v_request.status in ('approved', 'merged'))
           or (v_action = 'reject' and v_request.status = 'rejected') then
            return jsonb_build_object(
                'changed', false,
                'related_memory_id', v_request.related_memory_id,
                'request', jsonb_build_object(
                    'id', v_request.id,
                    'status', v_request.status,
                    'memory_id', v_request.memory_id,
                    'reviewed_at', v_request.reviewed_at
                )
            );
        end if;

        if v_request.status not in ('pending', 'conflict') then
            raise exception 'memory_request_not_pending';
        end if;

        if v_request.status = 'conflict' then
            update public.memory_requests
            set status = 'pending', updated_at = now()
            where id = v_request.id;
        end if;

        v_result := public.review_memory_request_v2(
            p_request_id,
            v_action,
            p_content,
            p_title,
            p_tags,
            p_importance,
            p_content_hash,
            v_reviewed_by,
            v_review_note,
            p_memory_key,
            p_update_mode
        );

        if coalesce((v_result->>'changed')::boolean, false) then
            insert into public.memory_request_review_events (
                request_id,
                action,
                from_status,
                to_status,
                target_memory_id,
                result_memory_id,
                reviewed_by,
                review_note,
                review_snapshot
            ) values (
                v_request.id,
                v_action,
                v_from_status,
                v_result->'request'->>'status',
                v_request.related_memory_id,
                nullif(v_result->'request'->>'memory_id', '')::integer,
                v_reviewed_by,
                v_review_note,
                jsonb_strip_nulls(jsonb_build_object(
                    'content', p_content,
                    'title', p_title,
                    'tags', p_tags,
                    'importance', p_importance,
                    'memory_key', p_memory_key,
                    'update_mode', p_update_mode
                ))
            );
        end if;

        return v_result || jsonb_build_object(
            'related_memory_id', v_request.related_memory_id
        );
    end if;

    if v_request.status not in ('pending', 'conflict') then
        raise exception 'memory_request_not_pending';
    end if;

    if p_related_memory_id is null then
        raise exception 'memory_request_related_memory_required';
    end if;

    -- Use the same memory-key lock order as mutable-fact replacement before
    -- taking the target lock, avoiding cross-flow races and deadlocks.
    select memory_key into v_target_memory_key
    from public.memories
    where id = p_related_memory_id;

    if not found then
        raise exception 'memory_request_related_memory_not_found';
    end if;

    if v_target_memory_key is not null then
        perform pg_advisory_xact_lock(
            hashtextextended('memory-key:' || v_target_memory_key, 0)
        );
    end if;

    -- Serialize every relational decision around the selected durable memory.
    perform pg_advisory_xact_lock(
        hashtextextended('memory-review-target:' || p_related_memory_id::text, 0)
    );

    select * into v_target
    from public.memories
    where id = p_related_memory_id
    for update;

    if not found then
        raise exception 'memory_request_related_memory_not_found';
    end if;
    if v_target.verified <> 'verified' or v_target.is_active is not true then
        raise exception 'memory_request_related_memory_inactive';
    end if;

    if v_action in ('duplicate', 'conflict') then
        if p_content is not null
           or p_title is not null
           or p_tags is not null
           or p_importance is not null
           or p_content_hash is not null
           or p_memory_key is not null
           or p_update_mode is not null then
            raise exception 'memory_request_relation_disallows_edits';
        end if;

        if v_action = 'conflict'
           and v_request.status = 'conflict'
           and v_request.related_memory_id = v_target.id then
            return jsonb_build_object(
                'changed', false,
                'related_memory_id', v_target.id,
                'request', jsonb_build_object(
                    'id', v_request.id,
                    'status', v_request.status,
                    'memory_id', v_request.memory_id,
                    'reviewed_at', v_request.reviewed_at
                )
            );
        end if;

        update public.memory_requests
        set
            status = v_action,
            memory_id = case when v_action = 'duplicate' then v_target.id else null end,
            related_memory_id = v_target.id,
            reviewed_at = now(),
            reviewed_by = v_reviewed_by,
            review_note = v_review_note,
            updated_at = now()
        where id = v_request.id
        returning * into v_request;

        insert into public.memory_request_review_events (
            request_id,
            action,
            from_status,
            to_status,
            target_memory_id,
            result_memory_id,
            reviewed_by,
            review_note,
            review_snapshot
        ) values (
            v_request.id,
            v_action,
            v_from_status,
            v_request.status,
            v_target.id,
            case when v_action = 'duplicate' then v_target.id else null end,
            v_reviewed_by,
            v_review_note,
            jsonb_build_object(
                'request_content_hash', v_request.content_hash,
                'target_content_hash', v_target.content_hash
            )
        );

        return jsonb_build_object(
            'changed', true,
            'related_memory_id', v_target.id,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'memory_id', v_request.memory_id,
                'reviewed_at', v_request.reviewed_at
            )
        );
    end if;

    -- A merge is never inferred. The user supplies the final merged wording,
    -- while the selected target is soft-deactivated and linked to the result.
    v_content := trim(coalesce(p_content, ''));
    v_title := nullif(left(trim(coalesce(p_title, '')), 100), '');
    v_tags := coalesce(p_tags, '{}'::text[]);
    v_importance := coalesce(p_importance, 5);
    v_content_hash := trim(coalesce(p_content_hash, ''));

    if char_length(v_content) not between 5 and 3000 then
        raise exception 'memory_request_invalid_content';
    end if;
    if cardinality(v_tags) > 5 then
        raise exception 'memory_request_invalid_tags';
    end if;
    if v_importance not between 1 and 10 then
        raise exception 'memory_request_invalid_importance';
    end if;
    if char_length(v_content_hash) <> 64 then
        raise exception 'memory_request_invalid_hash';
    end if;
    if p_memory_key is not null or p_update_mode is not null then
        raise exception 'memory_request_merge_disallows_update_mode';
    end if;
    if v_content_hash = v_target.content_hash then
        raise exception 'memory_request_merge_unchanged';
    end if;
    if exists (
        select 1 from public.memories
        where content_hash = v_content_hash
          and id <> v_target.id
    ) then
        raise exception 'memory_request_merge_content_exists';
    end if;

    update public.memories
    set
        is_active = false,
        superseded_at = now(),
        superseded_by_memory_id = null
    where id = v_target.id;

    insert into public.memories (
        content,
        title,
        tags,
        heat,
        importance,
        embedding,
        source,
        verified,
        is_active,
        recall_count,
        assistant_id,
        digest_run_id,
        source_first_message_id,
        source_last_message_id,
        confidence,
        content_hash,
        memory_key,
        supersedes_memory_id,
        superseded_by_memory_id,
        superseded_at
    ) values (
        v_content,
        v_title,
        v_tags,
        greatest(coalesce(v_target.heat, 0), least(greatest(v_importance * 10.0, 0), 100)),
        v_importance,
        null,
        'ai_tool_request',
        'verified',
        true,
        coalesce(v_target.recall_count, 0),
        coalesce(v_target.assistant_id, v_request.assistant_id),
        null,
        case
            when v_target.source_first_message_id is null then v_request.source_message_id
            when v_request.source_message_id is null then v_target.source_first_message_id
            else least(v_target.source_first_message_id, v_request.source_message_id)
        end,
        case
            when v_target.source_last_message_id is null then v_request.source_message_id
            when v_request.source_message_id is null then v_target.source_last_message_id
            else greatest(v_target.source_last_message_id, v_request.source_message_id)
        end,
        greatest(coalesce(v_target.confidence, 0), 1.0),
        v_content_hash,
        v_target.memory_key,
        v_target.id,
        null,
        null
    )
    returning id into v_result_memory_id;

    update public.memories
    set superseded_by_memory_id = v_result_memory_id
    where id = v_target.id;

    update public.memory_requests
    set
        content = v_content,
        title = v_title,
        tags = v_tags,
        importance = v_importance,
        content_hash = v_content_hash,
        status = 'merged',
        memory_id = v_result_memory_id,
        related_memory_id = v_target.id,
        reviewed_at = now(),
        reviewed_by = v_reviewed_by,
        review_note = v_review_note,
        updated_at = now()
    where id = v_request.id
    returning * into v_request;

    insert into public.memory_request_review_events (
        request_id,
        action,
        from_status,
        to_status,
        target_memory_id,
        result_memory_id,
        reviewed_by,
        review_note,
        review_snapshot
    ) values (
        v_request.id,
        'merge',
        v_from_status,
        v_request.status,
        v_target.id,
        v_result_memory_id,
        v_reviewed_by,
        v_review_note,
        jsonb_build_object(
            'content', v_content,
            'title', v_title,
            'tags', v_tags,
            'importance', v_importance,
            'content_hash', v_content_hash
        )
    );

    return jsonb_build_object(
        'changed', true,
        'related_memory_id', v_target.id,
        'request', jsonb_build_object(
            'id', v_request.id,
            'status', v_request.status,
            'memory_id', v_request.memory_id,
            'reviewed_at', v_request.reviewed_at
        )
    );
end;
$function$;

revoke all on function public.review_memory_request_v3(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer
) from public, anon, authenticated;

grant execute on function public.review_memory_request_v3(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer
) to service_role;

commit;
