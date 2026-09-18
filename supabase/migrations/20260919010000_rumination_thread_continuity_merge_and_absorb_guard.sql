-- Rumination thread continuity merge + absorb-channel thread guard.
--
-- F3 (continuity merge): the model input projection (_compact_thread) only
-- exposes open_question/current_state/next_expected/closure_criteria, while a
-- thread's continuity_data legally holds 10 fields
-- (gateway/memory_continuity_schema.py). Both update paths (in-place and
-- versioned) used to overwrite continuity_data wholesale with the model
-- output, silently dropping opened_at and the retrieval hints on every
-- update/resolve and on adopt-with-body. When the op carries the gateway's
-- read-time baseline (continuity_baseline; the production gateway always
-- attaches it to thread ops), the stored value becomes a merge: projected
-- fields are taken from the model output (absent = cleared), non-projected
-- fields are preserved from the server's current row (the model cannot see
-- them, so it can never mean to change them; the model's own copy of a
-- non-projected field is overridden). Ops without a baseline keep the exact
-- legacy whole-replacement behavior (direct-caller compatibility).
-- validate_continuity_data still validates the MODEL's original
-- continuity_data: what the model may express is unchanged, only the stored
-- merge result changes. Closure-field safety: the target is an open/paused
-- thread (schema invariant), so its baseline can never contain
-- closure_summary/closure_reason/closed_at and a resolve's model-supplied
-- closure fields survive the merge.
-- The same-body check now compares the MERGED value with the target, so an
-- update whose visible fields did not change correctly degrades to an
-- evidence merge instead of being pushed into a structural rewrite by
-- invisible-field differences. adopt_thread's versioned path applies the
-- same merge before inserting the successor; adopt's in-place takeover does
-- not write continuity_data and is untouched.
--
-- F7 (thread is never an absorb target): rule 15 of the prompt forbids
-- disposing of threads via absorption, but the DB re-checks only rejected
-- CLOSED threads, so an open/paused fast-path thread listed as an absorption
-- target would be deactivated outside the thread state machine and version
-- chain. commit_rumination_batch (create_memory and create_request
-- re-checks) and review_memory_request_v5 (approval re-verification) now
-- reject ANY thread ("v_absorbed.continuity_type is not distinct from
-- 'thread'") with memory_rumination_absorb_target_invalid.
-- In-flight request compatibility: a pending request that already lists a
-- thread as an absorption target will be rejected with 409 on approval;
-- reviewers should reject such requests. No data correction is performed.
--
-- Deploy order: gateway FIRST, THEN this migration. The gateway tightens its
-- candidate list first (threads are never offered to the model), so under
-- any order the model can never emit a thread absorption the DB would
-- reject; tightening the DB first would leave a window in which the model
-- still sees thread candidates and its batch fails.
--
-- Forward-only follow-up to 20260918010000 (commit_rumination_batch is
-- copied verbatim from it) and 20260908010000 (review_memory_request_v5 is
-- copied verbatim from it); production may currently run either
-- 20260915010000 or 20260918010000 (byte-identical except the content-length
-- checks) — both are compatible with this rebuild. No table structure is
-- touched; the only DDL is the rebuild of these two functions.
--
-- security definer, fixed search_path, service_role only (unchanged).
-- Does NOT touch chat_messages, memories table structure, memory_requests, or
-- memory_relations. Not applied to production by this PR; production apply
-- happens separately after review approval.

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
    v_continuity_data_merged jsonb;
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
                   or v_absorbed.continuity_type is not distinct from 'thread' then
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
                -- 版本化接管同样合并不可见字段：插入值以合并结果为准，
                -- 结构校验仍针对模型原始 continuity_data（契约不变）。
                v_continuity_data_merged :=
                    case when v_op ? 'continuity_baseline' then
                        coalesce(jsonb_strip_nulls(v_continuity_data), '{}'::jsonb)
                        || coalesce(jsonb_strip_nulls((
                            select jsonb_object_agg(field.key, field.value)
                            from jsonb_each(coalesce(v_target.continuity_data, '{}'::jsonb))
                                as field(key, value)
                            where field.key not in (
                                'open_question', 'current_state', 'next_expected', 'closure_criteria'
                            )
                        )), '{}'::jsonb)
                    else v_continuity_data
                    end;
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
                    v_target.continuity_id, 1, v_continuity_data_merged,
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
            -- 网关随操作携带读取时基线（continuity_baseline）；模型输入只投影
            -- open_question/current_state/next_expected/closure_criteria 四个
            -- 字段，其余字段它不可见。合并规则：投影内字段以模型输出为准
            -- （未提供=清除），投影外字段以服务端当前行为准（模型即便输出也
            -- 被基线覆盖）；baseline 缺失的直接调用方保持整包替换的旧行为。
            v_continuity_data_merged :=
                case when v_op ? 'continuity_baseline' then
                    coalesce(jsonb_strip_nulls(v_continuity_data), '{}'::jsonb)
                    || coalesce(jsonb_strip_nulls((
                        select jsonb_object_agg(field.key, field.value)
                        from jsonb_each(coalesce(v_target.continuity_data, '{}'::jsonb))
                            as field(key, value)
                        where field.key not in (
                            'open_question', 'current_state', 'next_expected', 'closure_criteria'
                        )
                    )), '{}'::jsonb)
                else v_continuity_data
                end;
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
                   and jsonb_strip_nulls(coalesce(v_continuity_data_merged, '{}'::jsonb))
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
                set continuity_data = v_continuity_data_merged,
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
                v_target.continuity_id, 1, v_continuity_data_merged,
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
            -- fast path, verified, still active; a thread is never an absorb
            -- target, open or paused alike). The
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
                   or v_absorbed.continuity_type is not distinct from 'thread' then
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

drop function if exists public.review_memory_request_v5(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer,
    extensions.vector, text, text[], timestamptz, text
);

create or replace function public.review_memory_request_v5(
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
    p_related_memory_id integer default null,
    p_recall_embedding extensions.vector default null,
    p_recall_scene text default null,
    p_recall_tags text[] default null,
    p_evidence_end_time timestamptz default null,
    p_evidence_time_precision text default null
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_mode text;
    v_key text;
    v_id uuid;
    v_result jsonb;
    v_memory_id integer;
    v_absorbed_id bigint;
    v_absorbed public.memories%rowtype;
    v_snapshot jsonb;
begin
    select * into v_request
    from public.memory_requests
    where id = p_request_id
    for update;
    if not found then
        raise exception 'memory_request_not_found';
    end if;
    if lower(trim(p_action)) in ('approve','merge') and (
        v_request.continuity_schema_version <> 1
        or not public.validate_continuity_data(
            v_request.continuity_type,v_request.thread_state,v_request.continuity_data
        )
    ) then
        raise exception 'memory_request_unclassified_legacy';
    end if;

    if v_request.status in ('pending','conflict') and lower(trim(p_action)) = 'approve' then
        v_mode := lower(trim(coalesce(p_update_mode,v_request.update_mode,'append')));
        v_key := case
            when v_mode = 'replace'
            then nullif(lower(trim(coalesce(p_memory_key,v_request.memory_key,''))),'')
            else null
        end;
        if v_mode not in ('append','replace') or (v_mode = 'replace' and v_key is null) then
            raise exception 'memory_request_invalid_update_mode';
        end if;
        v_id := public.allocate_memory_continuity_id(v_request.assistant_id,v_mode,v_key);
        update public.memory_requests set
            continuity_id = v_id,
            update_mode = v_mode,
            memory_key = v_key,
            updated_at = now()
        where id = v_request.id
        returning * into v_request;
    elsif v_request.status in ('pending','conflict') and lower(trim(p_action)) = 'merge' then
        select continuity_id into v_id
        from public.memories
        where id = p_related_memory_id
          and verified = 'verified'
          and is_active = true
          and (assistant_id is null or assistant_id = v_request.assistant_id)
        for update;
        if not found then
            raise exception 'memory_request_related_memory_not_found';
        end if;
        if v_id is null then
            insert into public.memory_continuity_objects(assistant_id)
            values(v_request.assistant_id)
            returning continuity_id into v_id;
            update public.memories set continuity_id = v_id where id = p_related_memory_id;
        end if;
        update public.memory_requests set continuity_id = v_id, updated_at = now()
        where id = v_request.id
        returning * into v_request;
    end if;

    v_result := public.review_memory_request_v4(
        p_request_id,p_action,p_content,p_title,p_tags,p_importance,p_content_hash,
        p_reviewed_by,p_review_note,p_memory_key,p_update_mode,p_related_memory_id
    );

    -- The caller resolves the final recall values (review edits win, otherwise
    -- the request's own values) and derives the embedding from the final
    -- recall_scene only; the database never guesses one. Applied after the
    -- metadata trigger so the scene-presence constraint sees consistent final
    -- values in a single statement: a cleared scene always clears the vector.
    if lower(trim(p_action)) in ('approve','merge') then
        v_memory_id := nullif(v_result->'request'->>'memory_id','')::integer;
        -- Re-read the request: v4 filled in related_memory_id/memory_id after
        -- the snapshot above was taken.
        select * into v_request
        from public.memory_requests
        where id = v_request.id;
        if v_memory_id is not null then
            update public.memories
            set recall_scene = p_recall_scene,
                recall_tags = coalesce(p_recall_tags, '{}'::text[]),
                evidence_end_time = p_evidence_end_time,
                evidence_time_precision = p_evidence_time_precision,
                recall_embedding = p_recall_embedding
            where id = v_memory_id;
        end if;

        -- Rumination handoff: only requests that EXPLICITLY list fast-path
        -- absorption targets are processed, and only when this call actually
        -- transitioned the request (idempotent replays never re-handoff).
        -- Each target is re-verified under lock: same assistant, produced by
        -- the fast path, verified, still active, and never a thread. Any
        -- invalid target fails the whole approval transaction. The absorbed
        -- row keeps its full body, evidence and history: it only leaves the
        -- active truth via is_active=false, and the cross-semantic handoff is
        -- recorded in the audit table instead of the supersedes version chain.
        if coalesce((v_result->>'changed')::boolean, false)
           and v_memory_id is not null
           and coalesce(v_request.absorbed_fast_path_memory_ids, '{}'::bigint[]) <> '{}'::bigint[] then
            foreach v_absorbed_id in array v_request.absorbed_fast_path_memory_ids loop
                -- A merge's related target is already fully handled by the
                -- merge itself (superseded by the merge result inside this
                -- transaction): re-processing it here would reject on its
                -- now-inactive state or, in degenerate cases, deactivate the
                -- final result. Skip it; every other listed target is
                -- re-verified against its creation-time snapshot. The result
                -- memory itself is likewise never a handoff target.
                if v_absorbed_id = v_memory_id
                   or v_absorbed_id = v_request.related_memory_id then
                    continue;
                end if;
                select * into v_absorbed
                from public.memories
                where id = v_absorbed_id
                for update;
                if not found
                   or v_absorbed.assistant_id is distinct from v_request.assistant_id
                   or v_absorbed.producer_path is distinct from 'fast_path'
                   or v_absorbed.verified is distinct from 'verified'
                   or v_absorbed.is_active is not true
                   or v_absorbed.continuity_type is not distinct from 'thread' then
                    raise exception 'memory_rumination_absorb_target_invalid';
                end if;
                -- Snapshot re-verification: the target must be byte-for-byte
                -- the memory the request captured at creation time. Any edit
                -- to body, identity, class, key, state, evidence, producer,
                -- verified or active fails the whole approval transaction
                -- before any deactivation or handoff happens.
                v_snapshot := (
                    select snapshot
                    from jsonb_array_elements(
                        coalesce(v_request.absorbed_fast_path_memory_snapshots, '[]'::jsonb)
                    ) as s(snapshot)
                    where (snapshot->>'memory_id')::bigint = v_absorbed_id
                    limit 1
                );
                if v_snapshot is null
                   or v_snapshot->>'content_hash' is distinct from v_absorbed.content_hash
                   or v_snapshot->>'continuity_id' is distinct from v_absorbed.continuity_id::text
                   or v_snapshot->>'continuity_type' is distinct from v_absorbed.continuity_type
                   or v_snapshot->>'memory_key' is distinct from v_absorbed.memory_key
                   or v_snapshot->>'thread_state' is distinct from v_absorbed.thread_state
                   or v_snapshot->'evidence_message_ids'
                          is distinct from to_jsonb(v_absorbed.evidence_message_ids)
                   or v_snapshot->>'producer_path' is distinct from v_absorbed.producer_path
                   or v_snapshot->>'verified' is distinct from v_absorbed.verified
                   or (v_snapshot->>'is_active')::boolean is distinct from v_absorbed.is_active
                then
                    raise exception 'memory_rumination_absorb_target_changed';
                end if;

                update public.memories
                set is_active = false
                where id = v_absorbed.id;

                insert into public.memory_path_handoffs (
                    assistant_id, run_id, request_id, kind,
                    fast_path_memory_id, rumination_memory_id,
                    memory_key, continuity_id, note
                ) values (
                    v_request.assistant_id,
                    v_request.digest_run_id,
                    v_request.id,
                    'absorbed_by_request',
                    v_absorbed.id,
                    v_memory_id,
                    v_absorbed.memory_key,
                    v_absorbed.continuity_id,
                    left(
                        'absorbed by approved rumination request #'
                        || v_request.id::text,
                        500
                    )
                );
            end loop;
        end if;

        -- Final sanity before returning: the result memory must still be
        -- verified, active and agree with the request's memory pointer.
        if v_memory_id is not null then
            select * into v_absorbed
            from public.memories
            where id = v_memory_id
            for update;
            if not found
               or v_absorbed.verified is distinct from 'verified'
               or v_absorbed.is_active is not true
               or v_absorbed.id is distinct from (
                    select memory_id from public.memory_requests
                    where id = v_request.id
               ) then
                raise exception 'memory_rumination_absorb_result_invalid';
            end if;
        end if;
    end if;
    return v_result;
end;
$function$;

revoke all on function public.review_memory_request_v5(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer,
    extensions.vector, text, text[], timestamptz, text
) from public, anon, authenticated;

grant execute on function public.review_memory_request_v5(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer,
    extensions.vector, text, text[], timestamptz, text
) to service_role;

commit;
