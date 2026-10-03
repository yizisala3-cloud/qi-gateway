-- 备忘录并发修复（BUG-04，2026-10-03）：统一分类 / 排序锁协议与存量坏位次清理。
--
-- 背景：20261002060000_memo_phase1.sql 的 RPC 在真实双连接并发下暴露三类
-- 缺陷（审查报告 R06/R07/R08）：
--   1) 重排 / manual 物化只锁分组并按一次成员快照写位置：等锁期间成员被
--      移出后仍写出非成员幽灵位次，之后再次关联 / 移除末标签持续 23505；
--   2) 创建（group→tag 外键）与删除标签（tag→级联 group）、用途转换
--      （entry→position）与重排（group→entry 外键）存在反向锁序，真实
--      触发 40P01；
--   3) 用途转换不锁分组即计算 max+1，两条记录并发转常驻取得相同末尾
--      位次，转换顺序不再持久。
--
-- 本迁移不修改任何表结构，只整体替换相关 RPC 并清理已产生的坏位次；
-- 对尚未应用 phase1 的新实例与已应用的存量实例都收敛到同一最终状态。
--
-- 全模块统一锁序（任何两条写路径之间不形成环）：
--       记录行（entry，id 升序）→ 标签行（id 升序）→ 分组行（id 升序）
--       → 位置行
--   - memo_create_entry：先取标签行 KEY SHARE 再插入记录、锁分组；
--   - memo_update_entry：入口行锁 → 新增标签 KEY SHARE → 汇总涉及的
--     既有分组按 id 升序加锁（含用途转换分组）→ 写位置；
--   - memo_reorder_group / memo_set_note_mode：先按 id 升序锁成员记录
--     行，再锁分组行，锁内复核成员后才写位置（不再按陈旧快照写出非
--     成员位次）；
--   - memo_delete_tag：记录行（升序）→ 标签行 → 级联 → 未分类分组；
--     与编辑（entry→tag→group）同序。
--   等锁期间成员关系变化一律通过「锁内复核」暴露：重排 / 模式物化复核
--   成员集合（不一致返回 ME004 让客户端基于最新数据重试），编辑 / 删除
--   标签复核孤儿集合与标签存在性。

begin;

-- ── 创建（幂等）：标签行 KEY SHARE 先于分组锁 ────────────────────
create or replace function public.memo_create_entry(
    p_payload jsonb,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_kind text;
    v_title text;
    v_content text;
    v_tag_ids bigint[] := array[]::bigint[];
    v_crid text;
    v_existing bigint;
    v_id bigint;
    v_tag_id bigint;
    v_group_id bigint;
begin
    if p_payload is null or jsonb_typeof(p_payload) is distinct from 'object' then
        raise exception 'memo_create_entry: payload must be an object'
            using errcode = 'ME005';
    end if;
    if exists (
        select 1 from jsonb_object_keys(p_payload) as k
        where k not in ('kind', 'title', 'content', 'tag_ids', 'client_request_id')
    ) then
        raise exception 'memo_create_entry: unknown payload key'
            using errcode = 'ME005';
    end if;

    v_kind := p_payload->>'kind';
    if jsonb_typeof(p_payload->'kind') is distinct from 'string'
       or v_kind not in ('pinned', 'note') then
        raise exception 'memo_create_entry: kind must be pinned or note'
            using errcode = 'ME005';
    end if;

    if jsonb_typeof(p_payload->'content') is distinct from 'string' then
        raise exception 'memo_create_entry: content must be a string'
            using errcode = 'ME005';
    end if;
    v_content := p_payload->>'content';
    if btrim(v_content) = '' then
        raise exception 'memo_create_entry: content is required'
            using errcode = 'ME005';
    end if;
    if char_length(v_content) > 50000 then
        raise exception 'memo_create_entry: content too long'
            using errcode = 'ME005';
    end if;

    if p_payload ? 'title' and jsonb_typeof(p_payload->'title') is distinct from 'string'
       and jsonb_typeof(p_payload->'title') is distinct from 'null' then
        raise exception 'memo_create_entry: title must be a string or null'
            using errcode = 'ME005';
    end if;
    v_title := p_payload->>'title';
    if v_title is not null then
        v_title := btrim(v_title);
        if v_title = '' then
            v_title := null;
        elsif char_length(v_title) > 200 then
            raise exception 'memo_create_entry: title too long'
                using errcode = 'ME005';
        end if;
    end if;

    v_crid := p_payload->>'client_request_id';
    if v_crid is not null then
        v_crid := btrim(v_crid);
        if v_crid = '' then
            v_crid := null;
        elsif char_length(v_crid) > 64 then
            raise exception 'memo_create_entry: client_request_id too long'
                using errcode = 'ME005';
        end if;
    end if;

    -- 创建重试幂等：同一 client_request_id 直接返回首次记录，不再写入。
    if v_crid is not null then
        select e.id into v_existing
          from public.memo_entry e
         where e.client_request_id = v_crid
         limit 1;
        if v_existing is not null then
            return public.memo_serialize_entry(
                (select e from public.memo_entry e where e.id = v_existing));
        end if;
    end if;

    if p_payload ? 'tag_ids' then
        if jsonb_typeof(p_payload->'tag_ids') is distinct from 'array' then
            raise exception 'memo_create_entry: tag_ids must be an array'
                using errcode = 'ME005';
        end if;
        if exists (
            select 1 from jsonb_array_elements(p_payload->'tag_ids') as e
            where jsonb_typeof(e.value) is distinct from 'number'
               or e.value::text !~ '^-?[0-9]+$'
        ) then
            raise exception 'memo_create_entry: tag_ids must be integers'
                using errcode = 'ME005';
        end if;
        v_tag_ids := array(
            select distinct (e.value::text)::bigint
              from jsonb_array_elements(p_payload->'tag_ids') as e);
        if (select count(*) from public.memo_tag where id = any(v_tag_ids))
           <> coalesce(array_length(v_tag_ids, 1), 0) then
            raise exception 'memo_create_entry: unknown tag id'
                using errcode = 'ME005';
        end if;
    end if;

    -- 锁序（BUG-04）：标签行（id 升序）→ 记录行 → 分组行 → 位置行。
    -- 先持有标签 KEY SHARE 再触碰分组：删除标签（tag FOR UPDATE → 级联
    -- 删分组）与创建事务不再形成 tag↔group 反向锁序（原 40P01）。
    -- v_tag_ids 固化为 id 升序，多标签分组的加锁 / 写入顺序确定。
    if coalesce(array_length(v_tag_ids, 1), 0) > 0 then
        v_tag_ids := array(
            select t.id from public.memo_tag t
             where t.id = any(v_tag_ids)
             order by t.id);
        perform 1 from public.memo_tag t
         where t.id = any(v_tag_ids)
         order by t.id
         for key share;
        -- 锁内复核：等待期间标签可能被并发删除
        if (select count(*) from public.memo_tag where id = any(v_tag_ids))
           <> coalesce(array_length(v_tag_ids, 1), 0) then
            raise exception 'memo_create_entry: unknown tag id'
                using errcode = 'ME005';
        end if;
    end if;

    begin
        insert into public.memo_entry (title, content, kind, client_request_id,
                                       created_at, updated_at)
        values (v_title, v_content, v_kind, v_crid, p_now, p_now)
        returning id into v_id;
    exception
        when unique_violation then
            -- 同幂等键并发重试（F17）：首个事务先查不到、提交后本事务才到达
            -- 唯一索引。此时首次记录已提交，取同一条返回即幂等语义，不再
            -- 23505。memo_entry 上唯一约束只有 client_request_id 一处；
            -- 无键（或查不到对应行）说明不是这个竞态，原样上抛。
            if v_crid is null then
                raise;
            end if;
            select e.id into v_existing
              from public.memo_entry e
             where e.client_request_id = v_crid
             limit 1;
            if v_existing is null then
                raise;
            end if;
            return public.memo_serialize_entry(
                (select e from public.memo_entry e where e.id = v_existing));
    end;

    -- 多标签关联 + 各分组末尾补位（§4.2/§4.3/§4.4）：常驻 → 常驻区末尾；
    -- 随笔 → 手动模式分组末尾、最新模式分组不落位。分组行按 id 升序加锁。
    foreach v_tag_id in array v_tag_ids loop
        v_group_id := public.memo_ensure_group(v_tag_id);
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        insert into public.memo_entry_tag (entry_id, tag_id)
        values (v_id, v_tag_id);
        if v_kind = 'pinned'
           or (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'manual' then
            insert into public.memo_position (group_id, entry_id, position)
            values (v_group_id, v_id,
                    public.memo_next_section_position(v_group_id, v_kind));
        end if;
    end loop;

    -- 无标签创建同样落「未分类」分组并按同一规则补位（F14）：否则后续
    -- 移除/删除末标签转入的记录会获得位置，越过既有未分类记录。
    if coalesce(array_length(v_tag_ids, 1), 0) = 0 then
        v_group_id := public.memo_untagged_group_id();
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        if v_kind = 'pinned'
           or (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'manual' then
            insert into public.memo_position (group_id, entry_id, position)
            values (v_group_id, v_id,
                    public.memo_next_section_position(v_group_id, v_kind));
        end if;
    end if;

    return public.memo_serialize_entry(
        (select e from public.memo_entry e where e.id = v_id));
end;
$$;

-- ── 编辑（标题 / 正文 / 用途 / 标签集合，原子 + 乐观并发） ────────
create or replace function public.memo_update_entry(
    p_entry_id bigint,
    p_expected_version integer,
    p_patch jsonb,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_entry public.memo_entry;
    v_new_title text;
    v_new_content text;
    v_new_kind text;
    v_tag_ids bigint[] := array[]::bigint[];
    v_current_tags bigint[] := array[]::bigint[];
    v_to_add bigint[] := array[]::bigint[];
    v_to_remove bigint[] := array[]::bigint[];
    v_tag_id bigint;
    v_group_id bigint;
    v_group_ids bigint[] := array[]::bigint[];
    v_lock_groups bigint[] := array[]::bigint[];
    v_field_changed boolean := false;
    v_kind_changed boolean := false;
    v_tags_changed boolean := false;
    v_mode text;
begin
    if p_expected_version is null then
        raise exception 'memo_update_entry: expected_version is required'
            using errcode = 'ME005';
    end if;
    if p_patch is null or jsonb_typeof(p_patch) is distinct from 'object'
       or p_patch = '{}'::jsonb then
        raise exception 'memo_update_entry: patch must be a non-empty object'
            using errcode = 'ME005';
    end if;
    if exists (
        select 1 from jsonb_object_keys(p_patch) as k
        where k not in ('title', 'content', 'kind', 'tag_ids')
    ) then
        raise exception 'memo_update_entry: unknown patch key'
            using errcode = 'ME005';
    end if;

    -- 形状校验先行（违规零写入）。
    if p_patch ? 'title' then
        if jsonb_typeof(p_patch->'title') is distinct from 'string'
           and jsonb_typeof(p_patch->'title') is distinct from 'null' then
            raise exception 'memo_update_entry: title must be a string or null'
                using errcode = 'ME005';
        end if;
        v_new_title := p_patch->>'title';
        if v_new_title is not null then
            v_new_title := btrim(v_new_title);
            if v_new_title = '' then
                v_new_title := null;
            elsif char_length(v_new_title) > 200 then
                raise exception 'memo_update_entry: title too long'
                    using errcode = 'ME005';
            end if;
        end if;
    end if;
    if p_patch ? 'content' then
        if jsonb_typeof(p_patch->'content') is distinct from 'string' then
            raise exception 'memo_update_entry: content must be a string'
                using errcode = 'ME005';
        end if;
        v_new_content := p_patch->>'content';
        if btrim(v_new_content) = '' then
            raise exception 'memo_update_entry: content is required'
                using errcode = 'ME005';
        end if;
        if char_length(v_new_content) > 50000 then
            raise exception 'memo_update_entry: content too long'
                using errcode = 'ME005';
        end if;
    end if;
    if p_patch ? 'kind' then
        v_new_kind := p_patch->>'kind';
        if jsonb_typeof(p_patch->'kind') is distinct from 'string'
           or v_new_kind not in ('pinned', 'note') then
            raise exception 'memo_update_entry: kind must be pinned or note'
                using errcode = 'ME005';
        end if;
    end if;
    if p_patch ? 'tag_ids' then
        if jsonb_typeof(p_patch->'tag_ids') is distinct from 'array' then
            raise exception 'memo_update_entry: tag_ids must be an array'
                using errcode = 'ME005';
        end if;
        if exists (
            select 1 from jsonb_array_elements(p_patch->'tag_ids') as e
            where jsonb_typeof(e.value) is distinct from 'number'
               or e.value::text !~ '^-?[0-9]+$'
        ) then
            raise exception 'memo_update_entry: tag_ids must be integers'
                using errcode = 'ME005';
        end if;
        v_tag_ids := array(
            select distinct (e.value::text)::bigint
              from jsonb_array_elements(p_patch->'tag_ids') as e);
        if (select count(*) from public.memo_tag where id = any(v_tag_ids))
           <> coalesce(array_length(v_tag_ids, 1), 0) then
            raise exception 'memo_update_entry: unknown tag id'
                using errcode = 'ME005';
        end if;
    end if;

    -- 行锁 → 生命周期门 → 版本门：陈旧自动保存既不能覆盖新数据，
    -- 也不能把已归档 / 已删除记录改回正常（需求 §6.2）。
    select * into v_entry
      from public.memo_entry e
     where e.id = p_entry_id
     for update;
    if not found then
        raise exception 'memo_update_entry: entry not found'
            using errcode = 'ME001';
    end if;
    if v_entry.status <> 'active' then
        raise exception 'memo_update_entry: entry is %', v_entry.status
            using errcode = 'ME003';
    end if;
    if v_entry.content_version is distinct from p_expected_version then
        raise exception 'memo_update_entry: stale content_version'
            using errcode = 'ME002';
    end if;

    -- 合法的部分 PATCH 可以不提交 kind（F16）：有效用途取记录当前用途，
    -- 后续所有落位分支都使用有效值，不得使用未初始化的 NULL。
    if not (p_patch ? 'kind') then
        v_new_kind := v_entry.kind;
    end if;

    if p_patch ? 'title' and v_new_title is distinct from v_entry.title then
        v_field_changed := true;
    end if;
    if p_patch ? 'content' and v_new_content is distinct from v_entry.content then
        v_field_changed := true;
    end if;
    if p_patch ? 'kind' and v_new_kind is distinct from v_entry.kind then
        v_kind_changed := true;
        v_field_changed := true;
    end if;

    -- 标签集合差异（提交完整目标集合）；差集按 id 升序固化，多个分组
    -- 的加锁/写入顺序稳定，不与其他事务形成反向锁序（F18）。
    v_current_tags := array(
        select et.tag_id from public.memo_entry_tag et
         where et.entry_id = p_entry_id);
    if p_patch ? 'tag_ids' then
        v_to_add := array(
            select u from (
                select unnest(v_tag_ids) as u
                except
                select unnest(v_current_tags)
            ) s order by u);
        v_to_remove := array(
            select u from (
                select unnest(v_current_tags) as u
                except
                select unnest(v_tag_ids)
            ) s order by u);
        v_tags_changed := coalesce(array_length(v_to_add, 1), 0) > 0
            or coalesce(array_length(v_to_remove, 1), 0) > 0;
    else
        v_to_add := array[]::bigint[];
        v_to_remove := array[]::bigint[];
    end if;

    if not v_field_changed and not v_tags_changed then
        -- 无有效变化：不 bump 版本、不写 updated_at（连续输入场景下
        -- 相同快照重放不应制造版本漂移）。
        return public.memo_serialize_entry(v_entry);
    end if;

    -- ── 统一锁序（BUG-04）：记录行 → 标签行 → 分组行 → 位置行 ──────
    -- 用途转换涉及的分组从实际成员关系枚举（F15：最新随笔没有位次行，
    -- 不能从稀疏位次表推断成员）；记录行锁已冻结本记录的成员关系，
    -- 枚举结果是稳定的。
    if v_kind_changed then
        v_group_ids := array(
            select s.id from (
                select g.id
                  from public.memo_entry_tag et
                  join public.memo_group g on g.tag_id = et.tag_id
                 where et.entry_id = p_entry_id
                union
                select ug.id
                  from public.memo_group ug
                 where ug.tag_id is null
                   and not exists (
                        select 1 from public.memo_entry_tag et2
                         where et2.entry_id = p_entry_id)
            ) s
            order by s.id);
    end if;

    -- 新增标签：先取标签行 KEY SHARE（id 升序）。持有分组锁后再等标签
    -- 外键是创建/删除标签反向锁序（40P01）的成因之一，这里把标签等待
    -- 提前到任何分组锁之前。
    if coalesce(array_length(v_to_add, 1), 0) > 0 then
        perform 1 from public.memo_tag t
         where t.id = any(v_to_add)
         order by t.id
         for key share;
    end if;

    -- 汇总本事务要写 / 删位次的全部既有分组（用途转换 ∪ 移除 ∪ 新增的
    -- 既有分组 ∪ 进入/离开未分类），按 id 升序一次加锁：用途转换 max+1
    -- 由此获得分组串行化（并发转换不再取得相同末尾位次），并与重排 /
    -- 模式物化（成员行 → 分组行）保持同向。
    v_lock_groups := array(
        select s.id from (
            select unnest(v_group_ids) as id
            union
            select g.id
              from public.memo_group g
             where g.tag_id = any(v_to_remove)
                or g.tag_id = any(v_to_add)
            union
            select ug.id
              from public.memo_group ug
             where ug.tag_id is null
               and v_tags_changed
               and (coalesce(array_length(v_current_tags, 1), 0) = 0
                    or coalesce(array_length(v_tag_ids, 1), 0) = 0)
        ) s
        order by s.id);
    foreach v_group_id in array v_lock_groups loop
        perform 1 from public.memo_group g where g.id = v_group_id for update;
    end loop;
    -- 新增标签的分组行可能尚不存在：在既有分组锁之后创建（insert 自带
    -- 行锁；此刻本事务不持任何他人需要的未提交分组锁，不构成环）。
    if v_tags_changed then
        foreach v_tag_id in array v_to_add loop
            if not exists (
                select 1 from public.memo_group g where g.tag_id = v_tag_id) then
                perform public.memo_ensure_group(v_tag_id);
            end if;
        end loop;
    end if;

    update public.memo_entry e
       set title = case when p_patch ? 'title' then v_new_title else e.title end,
           content = case when p_patch ? 'content' then v_new_content else e.content end,
           kind = case when v_kind_changed then v_new_kind else e.kind end,
           content_version = e.content_version + 1,
           updated_at = p_now
     where e.id = p_entry_id;

    -- 用途切换的重定位（§2.2：用途由用户明确选择）：v_group_ids 已在锁
    -- 阶段按成员关系枚举并加锁；每个分组内移到新用途区末尾；note 落到
    -- latest 模式分组时清除位置行。
    if v_kind_changed then
        foreach v_group_id in array v_group_ids loop
            if v_new_kind = 'note' then
                v_mode := (select g.note_sort_mode from public.memo_group g where g.id = v_group_id);
                if v_mode = 'latest' then
                    delete from public.memo_position p
                     where p.group_id = v_group_id and p.entry_id = p_entry_id;
                else
                    update public.memo_position p
                       set position = public.memo_next_section_position(
                               v_group_id, 'note', p_entry_id)
                     where p.group_id = v_group_id and p.entry_id = p_entry_id;
                end if;
            else
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, p_entry_id,
                        public.memo_next_section_position(v_group_id, 'pinned', p_entry_id))
                on conflict (group_id, entry_id)
                do update set position = excluded.position;
            end if;
        end loop;
    end if;

    -- 标签集合变更（§4.4）：移除不打乱他人相对顺序（直接删位次行）；
    -- 新增按新分组规则补位；失去最后一个标签转入未分类。
    if v_tags_changed then
        foreach v_tag_id in array v_to_remove loop
            delete from public.memo_entry_tag et
             where et.entry_id = p_entry_id and et.tag_id = v_tag_id;
            delete from public.memo_position p
             where p.entry_id = p_entry_id
               and p.group_id = (select g.id from public.memo_group g where g.tag_id = v_tag_id);
        end loop;
        foreach v_tag_id in array v_to_add loop
            v_group_id := public.memo_ensure_group(v_tag_id);
            perform 1 from public.memo_group g where g.id = v_group_id for update;
            insert into public.memo_entry_tag (entry_id, tag_id)
            values (p_entry_id, v_tag_id);
            if v_new_kind = 'pinned'
               or (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'manual' then
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, p_entry_id,
                        public.memo_next_section_position(v_group_id, v_new_kind));
            end if;
        end loop;
        -- 离开未分类（0 → ≥1 个标签）：清除未分类分组中的位次行。
        if coalesce(array_length(v_current_tags, 1), 0) = 0
           and coalesce(array_length(v_tag_ids, 1), 0) > 0 then
            delete from public.memo_position p
             where p.entry_id = p_entry_id
               and p.group_id = public.memo_untagged_group_id();
        end if;
        -- 失去最后一个标签（≥1 → 0）：转入未分类并按规则补位。
        if coalesce(array_length(v_current_tags, 1), 0) > 0
           and coalesce(array_length(v_tag_ids, 1), 0) = 0 then
            v_group_id := public.memo_untagged_group_id();
            perform 1 from public.memo_group g where g.id = v_group_id for update;
            if v_new_kind = 'pinned'
               or (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'manual' then
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, p_entry_id,
                        public.memo_next_section_position(v_group_id, v_new_kind));
            end if;
        end if;
    end if;

    return public.memo_serialize_entry(
        (select e from public.memo_entry e where e.id = p_entry_id));
end;
$$;

-- ── 分组内重排（常驻区 / 随笔区）：锁内复核成员再写位置 ───────────
create or replace function public.memo_reorder_group(
    p_scope jsonb,
    p_section text,
    p_ordered_entry_ids jsonb,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_tag_id bigint;
    v_untagged boolean;
    v_group_id bigint;
    v_members bigint[];
    v_submitted bigint[];
    v_entry_id bigint;
    v_i integer;
begin
    if p_scope is null or jsonb_typeof(p_scope) is distinct from 'object' then
        raise exception 'memo_reorder_group: scope must be an object'
            using errcode = 'ME005';
    end if;
    if p_section not in ('pinned', 'note') then
        raise exception 'memo_reorder_group: section must be pinned or note'
            using errcode = 'ME005';
    end if;
    if jsonb_typeof(p_ordered_entry_ids) is distinct from 'array'
       or exists (
            select 1 from jsonb_array_elements(p_ordered_entry_ids) as e
            where jsonb_typeof(e.value) is distinct from 'number'
               or e.value::text !~ '^-?[0-9]+$'
       ) then
        raise exception 'memo_reorder_group: ordered_entry_ids must be an array of integers'
            using errcode = 'ME005';
    end if;
    -- 提交顺序即业务语义：dedupe 保留首次出现顺序（select distinct 不保序，
    -- 必须 with ordinality 固化提交顺序）。
    v_submitted := array(
        select s.id
          from (
            select (e.value::text)::bigint as id, min(e.ord) as first_ord
              from jsonb_array_elements(p_ordered_entry_ids) with ordinality as e(value, ord)
             group by 1
          ) s
         order by s.first_ord);
    if coalesce(array_length(v_submitted, 1), 0) <> jsonb_array_length(p_ordered_entry_ids) then
        raise exception 'memo_reorder_group: duplicate entry ids'
            using errcode = 'ME005';
    end if;

    v_untagged := coalesce((p_scope->>'untagged')::boolean, false);
    if v_untagged then
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id is null
         limit 1;
        if v_group_id is null then
            raise exception 'memo_reorder_group: untagged group missing'
                using errcode = 'ME001';
        end if;
    else
        v_tag_id := (p_scope->>'tag_id')::bigint;
        if v_tag_id is null then
            raise exception 'memo_reorder_group: scope.tag_id is required'
                using errcode = 'ME005';
        end if;
        if not exists (select 1 from public.memo_tag t where t.id = v_tag_id) then
            raise exception 'memo_reorder_group: tag not found'
                using errcode = 'ME001';
        end if;
        -- 只读定位分组，不在此处 ensure：分组锁必须晚于成员记录锁
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id = v_tag_id;
    end if;

    -- 统一锁序（BUG-04）：成员记录行（id 升序）→ 分组行 → 位置行。
    -- 成员行锁先于分组行锁，与 memo_update_entry（entry → tag → group）
    -- 同向；等锁期间的关联变化无法逃过锁内复核，不再按陈旧快照为非
    -- 成员写出幽灵位次（原 23505 链）。
    if v_group_id is not null then
        v_members := public.memo_group_member_ids(v_group_id, p_section);
        foreach v_entry_id in array v_members loop
            perform 1 from public.memo_entry e where e.id = v_entry_id for update;
        end loop;
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        -- 锁内复核：全部相关行锁在手后重新读取成员集合
        v_members := public.memo_group_member_ids(v_group_id, p_section);
    else
        -- 标签尚无分组行 = 该分组没有成员
        v_members := array[]::bigint[];
    end if;

    -- 全量一致才生效（与规划重排同一防陈旧策略）：成员集合不一致 =
    -- 有并发增删，让客户端基于最新数据重排，不静默覆盖。
    if coalesce(array_length(v_members, 1), 0) <> coalesce(array_length(v_submitted, 1), 0)
       or exists (select unnest(v_members) except select unnest(v_submitted))
       or exists (select unnest(v_submitted) except select unnest(v_members)) then
        raise exception 'memo_reorder_group: member set changed (concurrent change)'
            using errcode = 'ME004';
    end if;

    -- 提交空集合且分组行尚未创建：此路径不会写任何位置，成员行锁阶段
    -- 无成员可锁（不构成锁序环），此处创建分组行。
    if v_group_id is null then
        v_group_id := public.memo_ensure_group(v_tag_id);
        perform 1 from public.memo_group g where g.id = v_group_id for update;
    end if;

    if p_section = 'note'
       and (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'latest' then
        -- 随笔区在最新模式下被拖动 = 进入手动排序模式（§4.3），
        -- 提交的顺序即手动顺序的落位。
        update public.memo_group g
           set note_sort_mode = 'manual'
         where g.id = v_group_id;
    end if;

    v_i := 1;
    foreach v_entry_id in array v_submitted loop
        insert into public.memo_position (group_id, entry_id, position)
        values (v_group_id, v_entry_id, v_i)
        on conflict (group_id, entry_id)
        do update set position = excluded.position;
        v_i := v_i + 1;
    end loop;

    return jsonb_build_object(
        'group_id', v_group_id,
        'section', p_section,
        'note_sort_mode', (select g.note_sort_mode from public.memo_group g where g.id = v_group_id),
        'order', to_jsonb(v_submitted));
end;
$$;

-- ── 随笔排序模式切换：同一锁协议，锁内复核成员再物化 ─────────────
create or replace function public.memo_set_note_mode(
    p_scope jsonb,
    p_mode text,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_tag_id bigint;
    v_untagged boolean;
    v_group_id bigint;
    v_members bigint[];
    v_positioned bigint[];
    v_entry_id bigint;
    v_next integer;
begin
    if p_mode not in ('latest', 'manual') then
        raise exception 'memo_set_note_mode: mode must be latest or manual'
            using errcode = 'ME005';
    end if;
    if p_scope is null or jsonb_typeof(p_scope) is distinct from 'object' then
        raise exception 'memo_set_note_mode: scope must be an object'
            using errcode = 'ME005';
    end if;

    v_untagged := coalesce((p_scope->>'untagged')::boolean, false);
    if v_untagged then
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id is null
         limit 1;
        if v_group_id is null then
            raise exception 'memo_set_note_mode: untagged group missing'
                using errcode = 'ME001';
        end if;
    else
        v_tag_id := (p_scope->>'tag_id')::bigint;
        if v_tag_id is null then
            raise exception 'memo_set_note_mode: scope.tag_id is required'
                using errcode = 'ME005';
        end if;
        if not exists (select 1 from public.memo_tag t where t.id = v_tag_id) then
            raise exception 'memo_set_note_mode: tag not found'
                using errcode = 'ME001';
        end if;
        select g.id into v_group_id
          from public.memo_group g
         where g.tag_id = v_tag_id;
    end if;

    -- 统一锁序（BUG-04）：与 memo_reorder_group 相同——成员记录行
    -- （id 升序）→ 分组行 → 位置行；锁内复核成员后再物化。
    if v_group_id is not null then
        v_members := public.memo_group_member_ids(v_group_id, 'note');
        foreach v_entry_id in array v_members loop
            perform 1 from public.memo_entry e where e.id = v_entry_id for update;
        end loop;
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        v_members := public.memo_group_member_ids(v_group_id, 'note');
    else
        v_members := array[]::bigint[];
    end if;

    if p_mode = 'manual' then
        v_positioned := array(
            select p.entry_id
              from public.memo_position p
              join public.memo_entry e on e.id = p.entry_id
             where p.group_id = v_group_id
               and e.kind = 'note'
               and e.status = 'active'
               and p.entry_id = any(v_members));
        if coalesce(array_length(v_positioned, 1), 0) = 0
           and coalesce(array_length(v_members, 1), 0) > 0 then
            -- 从未手排过：按当前最新顺序（创建时间倒序）落位。
            v_next := 1;
            for v_entry_id in
                select e.id
                  from public.memo_entry e
                 where e.id = any(v_members)
                 order by e.created_at desc, e.id desc
            loop
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, v_entry_id, v_next)
                on conflict (group_id, entry_id)
                do update set position = excluded.position;
                v_next := v_next + 1;
            end loop;
        else
            -- 切回手动：原手动顺序继续保留（§4.3）；未定位成员（手动模式
            -- 期间新增等）按创建先后补在末尾。
            v_next := coalesce((
                select max(p.position)
                  from public.memo_position p
                  join public.memo_entry e on e.id = p.entry_id
                 where p.group_id = v_group_id
                   and e.kind = 'note'
                   and e.status = 'active'
                   and p.entry_id = any(v_members)
            ), 0) + 1;
            for v_entry_id in
                select e.id
                  from public.memo_entry e
                 where e.id = any(v_members)
                   and not (e.id = any(v_positioned))
                 order by e.created_at asc, e.id asc
            loop
                insert into public.memo_position (group_id, entry_id, position)
                values (v_group_id, v_entry_id, v_next)
                on conflict (group_id, entry_id)
                do update set position = excluded.position;
                v_next := v_next + 1;
            end loop;
        end if;
    end if;
    -- 切回 latest 只改模式：位次行原样保留，供再次切回手动时恢复（§4.3）。

    if v_group_id is null then
        -- 标签尚无分组行且没有成员：仅落分组行并设置模式
        v_group_id := public.memo_ensure_group(v_tag_id);
        perform 1 from public.memo_group g where g.id = v_group_id for update;
    end if;

    update public.memo_group g
       set note_sort_mode = p_mode
     where g.id = v_group_id;

    return jsonb_build_object(
        'group_id', v_group_id,
        'note_sort_mode', p_mode);
end;
$$;

-- ── 标签板块顺序：全量一致 + 锁内复核 ────────────────────────────
create or replace function public.memo_reorder_tags(
    p_ordered_tag_ids jsonb,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_existing bigint[];
    v_submitted bigint[];
    v_tag_id bigint;
    v_i integer;
begin
    if jsonb_typeof(p_ordered_tag_ids) is distinct from 'array'
       or exists (
            select 1 from jsonb_array_elements(p_ordered_tag_ids) as e
            where jsonb_typeof(e.value) is distinct from 'number'
               or e.value::text !~ '^-?[0-9]+$'
       ) then
        raise exception 'memo_reorder_tags: ordered_tag_ids must be an array of integers'
            using errcode = 'ME005';
    end if;
    v_submitted := array(
        select s.id
          from (
            select (e.value::text)::bigint as id, min(e.ord) as first_ord
              from jsonb_array_elements(p_ordered_tag_ids) with ordinality as e(value, ord)
             group by 1
          ) s
         order by s.first_ord);
    if coalesce(array_length(v_submitted, 1), 0) <> jsonb_array_length(p_ordered_tag_ids) then
        raise exception 'memo_reorder_tags: duplicate tag ids'
            using errcode = 'ME005';
    end if;

    v_existing := array(select t.id from public.memo_tag t order by t.id);
    if coalesce(array_length(v_existing, 1), 0) <> coalesce(array_length(v_submitted, 1), 0)
       or exists (select unnest(v_existing) except select unnest(v_submitted))
       or exists (select unnest(v_submitted) except select unnest(v_existing)) then
        raise exception 'memo_reorder_tags: tag set changed (concurrent change)'
            using errcode = 'ME004';
    end if;

    -- 稳定锁序（id 升序）后写板块位置。
    perform 1 from public.memo_tag t
     where t.id = any(v_submitted)
     order by t.id
     for update;

    -- 锁内复核（BUG-04）：等锁期间标签可能被并发创建 / 删除，集合已
    -- 变化时不得按陈旧快照写位置（避免被删标签更新零行后的顺序缺口）。
    v_existing := array(select t.id from public.memo_tag t order by t.id);
    if coalesce(array_length(v_existing, 1), 0) <> coalesce(array_length(v_submitted, 1), 0)
       or exists (select unnest(v_existing) except select unnest(v_submitted))
       or exists (select unnest(v_submitted) except select unnest(v_existing)) then
        raise exception 'memo_reorder_tags: tag set changed (concurrent change)'
            using errcode = 'ME004';
    end if;

    v_i := 1;
    foreach v_tag_id in array v_submitted loop
        update public.memo_tag t
           set position = v_i
         where t.id = v_tag_id;
        v_i := v_i + 1;
    end loop;

    return jsonb_build_object('order', to_jsonb(v_submitted));
end;
$$;

-- ── 标签管理：记录行先于标签行（与编辑同序，消除反向锁序） ────────
create or replace function public.memo_delete_tag(
    p_tag_id bigint,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_orphans bigint[];
    v_entry_id bigint;
    v_kind text;
    v_group_id bigint;
begin
    -- 无锁预检：标签不存在时直接返回 ME001，不做无谓的记录加锁。
    if not exists (select 1 from public.memo_tag t where t.id = p_tag_id) then
        raise exception 'memo_delete_tag: tag not found'
            using errcode = 'ME001';
    end if;

    -- 锁序纪律（BUG-04 统一）：记录行（id 升序）→ 标签行 → 级联删除 →
    -- 未分类分组 → 位次。记录行先于标签行与 memo_update_entry
    -- （entry → tag KEY SHARE → group）同向：编辑加标签与删除标签、
    -- 创建（tag KEY SHARE → group）与删除标签都不再形成
    -- tag↔entry / tag↔group 反向锁序（原 40P01）。
    for v_entry_id in
        select et.entry_id
          from public.memo_entry_tag et
         where et.tag_id = p_tag_id
         order by et.entry_id
    loop
        perform 1 from public.memo_entry e where e.id = v_entry_id for update;
    end loop;

    perform 1 from public.memo_tag t where t.id = p_tag_id for update;
    if not found then
        raise exception 'memo_delete_tag: tag not found'
            using errcode = 'ME001';
    end if;

    -- 记录行已在锁内（F06）：重新确认「将失去最后一个标签」的成员，替代
    -- 删除前的陈旧孤儿快照——否则并发为记录新增第二标签后，删除标签仍会
    -- 给它写未分类幽灵位次，后续移除末标签再撞 memo_position_pkey。
    v_orphans := array(
        select et.entry_id
          from public.memo_entry_tag et
         where et.tag_id = p_tag_id
           and not exists (
                select 1 from public.memo_entry_tag et2
                 where et2.entry_id = et.entry_id
                   and et2.tag_id <> p_tag_id)
         order by et.entry_id);

    delete from public.memo_tag t where t.id = p_tag_id;

    -- 失去最后一个标签的活跃记录转入未分类分组并按规则补位。
    -- 插入带 on conflict do nothing：与「移除最后标签」路径对同一条记录
    -- 的未分类补位互为幂等（成员与位次不变），不掩盖任何数据不一致。
    if coalesce(array_length(v_orphans, 1), 0) > 0 then
        v_group_id := public.memo_untagged_group_id();
        perform 1 from public.memo_group g where g.id = v_group_id for update;
        foreach v_entry_id in array v_orphans loop
            select e.kind into v_kind
              from public.memo_entry e
             where e.id = v_entry_id
               and e.status = 'active';
            if v_kind is not null
               and not exists (
                    select 1 from public.memo_entry_tag et
                     where et.entry_id = v_entry_id) then
                if v_kind = 'pinned'
                   or (select g.note_sort_mode from public.memo_group g where g.id = v_group_id) = 'manual' then
                    insert into public.memo_position (group_id, entry_id, position)
                    values (v_group_id, v_entry_id,
                            public.memo_next_section_position(v_group_id, v_kind))
                    on conflict (group_id, entry_id) do nothing;
                end if;
            end if;
        end loop;
    end if;

    return jsonb_build_object('deleted', true, 'affected', coalesce(array_length(v_orphans, 1), 0));
end;
$$;

-- ── 存量坏位次清理（BUG-04）：非成员位次一次性移除 ────────────────
-- 此前并发窗口可能已写入「记录不属于该分组」的 memo_position 行（幽灵
-- 位次），让后续关联 / 移除末标签持续 23505。安全恢复 = 只删除
-- 「分组有标签但记录未关联该标签」与「未分类分组中已有任一标签」的
-- 位次行；合法成员位次（含归档 / 回收站记录的位次）原样保留。
delete from public.memo_position p
 using public.memo_group g
 where g.id = p.group_id
   and (
        (g.tag_id is not null and not exists (
            select 1 from public.memo_entry_tag et
             where et.entry_id = p.entry_id
               and et.tag_id = g.tag_id))
     or (g.tag_id is null and exists (
            select 1 from public.memo_entry_tag et2
             where et2.entry_id = p.entry_id))
       );

commit;
