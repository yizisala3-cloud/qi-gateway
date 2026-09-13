-- Rebuild memory_digest_runs_active_batch_uidx so an empty-result pause can
-- be retried on the same batch, and different pipelines can process the same
-- message range.
--
-- The production index (assistant_id, source_first_message_id,
-- source_last_message_id) where mode='execute' and status in
-- ('running','succeeded') was created outside the repository migrations and
-- had two defects:
--   * 'succeeded' runs permanently occupied a window, so the documented
--     continuity_retry flow (paused_empty -> retry the blocked batch) always
--     died on a unique violation while initializing the claimed run;
--   * the key ignored `pipeline`, so a digest run and a continuity run could
--     never cover the same messages even though they own separate cursors.
--
-- The replacement keys on (assistant_id, pipeline, source_first_message_id,
-- source_last_message_id) and only guards genuinely active runs
-- ('claimed'/'running'): concurrent duplicate submissions of the same
-- pipeline window are still rejected, while succeeded runs release the
-- window. Runs in 'claimed' state carry NULL window columns, which a unique
-- btree treats as distinct, so pre-initialization rows never collide.
--
-- Idempotent: dropping the old index uses IF EXISTS, creating the new one
-- uses IF NOT EXISTS. Forward-only; the previous shape can be restored by
-- re-running the original CREATE UNIQUE INDEX statement shown above.

begin;

drop index if exists public.memory_digest_runs_active_batch_uidx;

create unique index if not exists memory_digest_runs_active_batch_uidx
    on public.memory_digest_runs
        (assistant_id, pipeline, source_first_message_id, source_last_message_id)
    where mode = 'execute'
      and status in ('claimed', 'running');

commit;
