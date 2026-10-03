-- 备忘录一期（2026-10-02 确认《备忘录一期完整需求/需求规范.md》§1–§8）。
--
-- 数据模型（正文只有一份，排序属于「分组与记录的关系」）：
-- * memo_entry：备忘录记录 = 稳定身份 + 标题（可空）+ Markdown 正文 +
--   用途（pinned 常驻备忘 / note 随笔）+ 生命周期（active/archived/deleted）
--   + content_version 乐观并发版本 + client_request_id 创建幂等键。
--   归档 / 删除 / 恢复都作用于整条记录（所有标签下同步生效），不做硬删除。
-- * memo_tag：用户自定义标签；position = 首页板块顺序（新增标签放末尾）。
-- * memo_group：排序分组 = 一个标签一个分组，tag_id 唯一；tag_id 为 NULL 的
--   单例行是「未分类」分组（迁移时插入，全库唯一），同样持久保存随笔排序
--   模式与手动顺序（需求 §3.2/§4.4）。
-- * memo_entry_tag：多标签关联（entry_id, tag_id）——同一条记录在各标签
--   板块显示的是同一份正文。
-- * memo_position：(分组, 记录) 的手动位置。常驻备忘在每个标签内独立手排；
--   随笔默认按创建时间倒序（不落位置），拖动 / 切手动模式后才落位置。
--   稀疏位置：新增成员放末尾（max+1）、删除关联不打乱其他记录相对顺序、
--   改正文不改变手排位置（需求 §4.2–§4.4）。
--
-- RPC（PostgREST 资源 API 无跨行 / 跨表原子写能力，沿用既有最小 RPC 模式；
-- 同一次业务操作的多行写入在单个函数事务内完成，任一失败整体回滚）：
--   memo_create_entry          创建（记录 + 多标签关联 + 各分组末尾位置，
--                              client_request_id 幂等防重复）
--   memo_update_entry          编辑（标题/正文/用途/标签集合）——行锁 +
--                              status=active + content_version 相等才生效，
--                              版本不符 ME002、生命周期冲突 ME003；陈旧自动
--                              保存不能静默覆盖更新的数据，也不能把已归档 /
--                              已删除记录改回正常（每次写都 content_version+1）
--   memo_set_entry_lifecycle   归档 / 删除 / 恢复（同样带版本门）
--   memo_reorder_group         标签板块内手排（常驻区或随笔区）；随笔区在
--                              latest 模式下拖动 = 进入手动模式；提交的全量
--                              id 集合与当前成员完全一致才生效（ME004，
--                              与规划重排同一防陈旧策略）
--   memo_set_note_mode         随笔排序模式切换；切回手动保留既有手动顺序、
--                              未定位成员按创建先后补末尾；从未手排过才按
--                              当前最新顺序落位（需求 §4.3）
--   memo_reorder_tags          首页标签板块顺序（全量一致才生效）
--   memo_create_tag            新建标签（末尾；重名返回既有行）
--   memo_delete_tag            删除标签只解除分类关系；失去最后一个标签的
--                              活跃记录转入「未分类」分组并按规则补位
--
-- 错误码（5 字符 SQLSTATE，应用层映射 HTTP 状态）：ME001 not_found → 404；
-- ME002 version_conflict → 409；ME003 lifecycle_conflict → 409；
-- ME004 concurrent_modified → 409；ME005 invalid_payload → 400。
-- 锁序纪律：入口行（entry/tag）→ 记录行（id 升序）→ 分组行 → 位置行；
-- 删除标签先锁受影响记录再级联删除，并在锁内复核成员关系；多个分组的
-- 差集/成员集合按 id 升序固化，任何两条写路径之间不形成反向锁序（F18），
-- 与规划 RPC 的「先身份行后成员行」一致。
-- RLS：全部启用但不出策略（单用户私人网关，服务端 key 绕过 RLS，与规划
-- 三表同惯例）。
-- 部署状态：全新表与全新函数（无 overload 风险）；增量迁移，不修改任何
-- 已发布迁移。

begin;

-- ── 记录表 ────────────────────────────────────────────────────────
create table public.memo_entry (
    id bigint generated always as identity primary key,
    -- 标题可空（§2.3）：空串 / 纯空白一律归一为 NULL，前端用正文首行兜底
    title text
        constraint memo_entry_title_shape
        check (title is null or char_length(title) <= 200),
    -- 正文必填（§2.3），普通 Markdown；50000 字符是长文编辑的宽裕上限
    content text not null
        constraint memo_entry_content_shape
        check (btrim(content) <> '' and char_length(content) <= 50000),
    -- 用途（§2.2）：pinned 常驻备忘 / note 随笔；由用户明确选择，永不自动切换
    kind text not null
        constraint memo_entry_kind_check
        check (kind in ('pinned', 'note')),
    -- 生命周期（§6.2）：归档 / 删除保留恢复入口，全部软状态
    status text not null default 'active'
        constraint memo_entry_status_check
        check (status in ('active', 'archived', 'deleted')),
    -- 乐观并发版本：每次业务写（含生命周期变化）+1；陈旧保存被拒绝
    content_version integer not null default 1
        constraint memo_entry_version_shape
        check (content_version >= 1),
    -- 创建幂等键：自动保存首次创建的重试不得产生重复记录
    client_request_id text
        constraint memo_entry_crid_shape
        check (client_request_id is null or char_length(client_request_id) <= 64),
    constraint memo_entry_client_request_id_key unique (client_request_id),
    archived_at timestamptz,
    deleted_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

-- ── 标签表 ────────────────────────────────────────────────────────
create table public.memo_tag (
    id bigint generated always as identity primary key,
    name text not null
        constraint memo_tag_name_shape
        check (btrim(name) <> '' and char_length(btrim(name)) <= 30)
        constraint memo_tag_name_key unique,
    -- 首页板块顺序；新增标签由 RPC 赋 max+1（末尾，§4.1）
    position integer not null default 0,
    created_at timestamptz not null default now()
);

-- ── 排序分组表（标签分组 + 未分类单例） ──────────────────────────
create table public.memo_group (
    id bigint generated always as identity primary key,
    tag_id bigint unique
        references public.memo_tag (id) on delete cascade,
    -- 随笔排序模式（§4.3）：latest 创建时间倒序 / manual 手动顺序；
    -- 按分组（即按标签）分别保存
    note_sort_mode text not null default 'latest'
        constraint memo_group_note_mode_check
        check (note_sort_mode in ('latest', 'manual')),
    created_at timestamptz not null default now()
);
comment on table public.memo_group is
    '排序分组：每标签一行（tag_id 唯一）；tag_id 为 NULL 的单例行是「未分类」分组。';

-- ── 多标签关联（正文只有一份） ───────────────────────────────────
create table public.memo_entry_tag (
    entry_id bigint not null
        references public.memo_entry (id) on delete cascade,
    tag_id bigint not null
        references public.memo_tag (id) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (entry_id, tag_id)
);
create index memo_entry_tag_tag_idx on public.memo_entry_tag (tag_id);

-- ── 手动位置（分组 × 记录） ──────────────────────────────────────
create table public.memo_position (
    group_id bigint not null
        references public.memo_group (id) on delete cascade,
    entry_id bigint not null
        references public.memo_entry (id) on delete cascade,
    position integer not null
        constraint memo_position_shape
        check (position >= 1),
    primary key (group_id, entry_id)
);
create index memo_position_group_idx on public.memo_position (group_id, position);

-- 「未分类」分组单例：全库唯一一行 tag_id is null，随迁移创建；
-- 所有「失去最后一个标签」的转入路径都直接引用这一行。
insert into public.memo_group (tag_id) values (null);

alter table public.memo_entry enable row level security;
alter table public.memo_tag enable row level security;
alter table public.memo_group enable row level security;
alter table public.memo_entry_tag enable row level security;
alter table public.memo_position enable row level security;

-- ── 序列化 ───────────────────────────────────────────────────────
create or replace function public.memo_serialize_entry(p_entry public.memo_entry)
returns jsonb
language sql
stable
as $$
    select jsonb_build_object(
        'id', p_entry.id,
        'title', p_entry.title,
        'content', p_entry.content,
        'kind', p_entry.kind,
        'status', p_entry.status,
        'content_version', p_entry.content_version,
        'created_at', p_entry.created_at,
        'updated_at', p_entry.updated_at,
        'archived_at', p_entry.archived_at,
        'deleted_at', p_entry.deleted_at,
        'tags', coalesce((
            select jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name) order by t.id)
              from public.memo_entry_tag et
              join public.memo_tag t on t.id = et.tag_id
             where et.entry_id = p_entry.id
        ), '[]'::jsonb)
    );
$$;

-- ── 分组定位 ─────────────────────────────────────────────────────
-- 标签分组按需创建（on conflict 目标 = tag_id 唯一约束，竞态下两个并发
-- 调用都返回同一行）；「未分类」单例行由本迁移创建，这里只读取。
create or replace function public.memo_ensure_group(p_tag_id bigint)
returns bigint
language sql
volatile
as $$
    insert into public.memo_group (tag_id) values (p_tag_id)
    on conflict (tag_id) do update set tag_id = excluded.tag_id
    returning id;
$$;

create or replace function public.memo_untagged_group_id()
returns bigint
language sql
stable
as $$
    select id from public.memo_group where tag_id is null limit 1;
$$;

-- 某分组某用途区的「末尾位次」（max+1）。位次按用途区分段比较：
-- 常驻区与随笔区各自成序，互不混排（展示时先常驻后随笔，§3.2）。
-- p_exclude_entry_id：用途切换重定位时排除记录自身（其行 kind 已改为
-- 新用途，旧区遗留位次不得计入新区 max）。
create or replace function public.memo_next_section_position(
    p_group_id bigint,
    p_kind text,
    p_exclude_entry_id bigint default null
)
returns integer
language sql
stable
as $$
    select coalesce(max(p.position), 0) + 1
      from public.memo_position p
      join public.memo_entry e on e.id = p.entry_id
     where p.group_id = p_group_id
       and e.kind = p_kind
       and (p_exclude_entry_id is null or p.entry_id <> p_exclude_entry_id)
$$;

-- ── 创建（幂等） ─────────────────────────────────────────────────
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

    update public.memo_entry e
       set title = case when p_patch ? 'title' then v_new_title else e.title end,
           content = case when p_patch ? 'content' then v_new_content else e.content end,
           kind = case when v_kind_changed then v_new_kind else e.kind end,
           content_version = e.content_version + 1,
           updated_at = p_now
     where e.id = p_entry_id;

    -- 用途切换的重定位（§2.2：用途由用户明确选择）：所属分组从实际成员
    -- 关系枚举（F15：最新随笔没有位次行，不能从稀疏位次表推断成员，否则
    -- 转换后漏位次、后续新增越过它）；每个分组内移到新用途区末尾；
    -- note 落到 latest 模式分组时清除位置行。
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

-- ── 生命周期（归档 / 删除 / 恢复） ───────────────────────────────
create or replace function public.memo_set_entry_lifecycle(
    p_entry_id bigint,
    p_action text,
    p_expected_version integer,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_entry public.memo_entry;
begin
    if p_action not in ('archive', 'delete', 'restore') then
        raise exception 'memo_set_entry_lifecycle: unknown action'
            using errcode = 'ME005';
    end if;
    if p_expected_version is null then
        raise exception 'memo_set_entry_lifecycle: expected_version is required'
            using errcode = 'ME005';
    end if;

    select * into v_entry
      from public.memo_entry e
     where e.id = p_entry_id
     for update;
    if not found then
        raise exception 'memo_set_entry_lifecycle: entry not found'
            using errcode = 'ME001';
    end if;
    if v_entry.content_version is distinct from p_expected_version then
        raise exception 'memo_set_entry_lifecycle: stale content_version'
            using errcode = 'ME002';
    end if;

    if p_action = 'archive' then
        if v_entry.status <> 'active' then
            raise exception 'memo_set_entry_lifecycle: cannot archive from %', v_entry.status
                using errcode = 'ME003';
        end if;
        update public.memo_entry e
           set status = 'archived', archived_at = p_now,
               content_version = e.content_version + 1, updated_at = p_now
         where e.id = p_entry_id;
    elsif p_action = 'delete' then
        if v_entry.status not in ('active', 'archived') then
            raise exception 'memo_set_entry_lifecycle: cannot delete from %', v_entry.status
                using errcode = 'ME003';
        end if;
        update public.memo_entry e
           set status = 'deleted', deleted_at = p_now,
               content_version = e.content_version + 1, updated_at = p_now
         where e.id = p_entry_id;
    else
        if v_entry.status not in ('archived', 'deleted') then
            raise exception 'memo_set_entry_lifecycle: cannot restore from %', v_entry.status
                using errcode = 'ME003';
        end if;
        update public.memo_entry e
           set status = 'active', archived_at = null, deleted_at = null,
               content_version = e.content_version + 1, updated_at = p_now
         where e.id = p_entry_id;
    end if;

    return public.memo_serialize_entry(
        (select e from public.memo_entry e where e.id = p_entry_id));
end;
$$;

-- ── 分组成员查询（重排 / 模式切换共用） ──────────────────────────
-- 返回指定分组指定用途区的当前活跃成员 id（未分类 = 不带任何标签的活跃记录）。
create or replace function public.memo_group_member_ids(
    p_group_id bigint,
    p_section text
)
returns bigint[]
language plpgsql
stable
as $$
declare
    v_tag_id bigint;
begin
    select g.tag_id into v_tag_id
      from public.memo_group g
     where g.id = p_group_id;
    if v_tag_id is not null then
        return array(
            select e.id
              from public.memo_entry_tag et
              join public.memo_entry e on e.id = et.entry_id
             where et.tag_id = v_tag_id
               and e.status = 'active'
               and e.kind = p_section
             order by e.id);
    end if;
    return array(
        select e.id
          from public.memo_entry e
         where e.status = 'active'
           and e.kind = p_section
           and not exists (
                select 1 from public.memo_entry_tag et
                 where et.entry_id = e.id)
         order by e.id);
end;
$$;

-- ── 分组内重排（常驻区 / 随笔区） ────────────────────────────────
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
        v_group_id := public.memo_untagged_group_id();
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
        v_group_id := public.memo_ensure_group(v_tag_id);
    end if;

    perform 1 from public.memo_group g where g.id = v_group_id for update;

    v_members := public.memo_group_member_ids(v_group_id, p_section);
    -- 全量一致才生效（与规划重排同一防陈旧策略）：成员集合不一致 =
    -- 有并发增删，让客户端基于最新数据重排，不静默覆盖。
    if coalesce(array_length(v_members, 1), 0) <> coalesce(array_length(v_submitted, 1), 0)
       or exists (select unnest(v_members) except select unnest(v_submitted))
       or exists (select unnest(v_submitted) except select unnest(v_members)) then
        raise exception 'memo_reorder_group: member set changed (concurrent change)'
            using errcode = 'ME004';
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

-- ── 随笔排序模式切换 ─────────────────────────────────────────────
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
        v_group_id := public.memo_untagged_group_id();
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
        v_group_id := public.memo_ensure_group(v_tag_id);
    end if;

    perform 1 from public.memo_group g where g.id = v_group_id for update;

    if p_mode = 'manual' then
        v_members := public.memo_group_member_ids(v_group_id, 'note');
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

    update public.memo_group g
       set note_sort_mode = p_mode
     where g.id = v_group_id;

    return jsonb_build_object(
        'group_id', v_group_id,
        'note_sort_mode', p_mode);
end;
$$;

-- ── 标签板块顺序 ─────────────────────────────────────────────────
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

-- ── 标签管理 ─────────────────────────────────────────────────────
create or replace function public.memo_create_tag(
    p_name text,
    p_now timestamptz
)
returns jsonb
language plpgsql
as $$
declare
    v_name text;
    v_row public.memo_tag;
begin
    v_name := btrim(coalesce(p_name, ''));
    if v_name = '' or char_length(v_name) > 30 then
        raise exception 'memo_create_tag: name must be 1-30 characters'
            using errcode = 'ME005';
    end if;

    select * into v_row from public.memo_tag t where t.name = v_name;
    if found then
        return jsonb_build_object(
            'id', v_row.id, 'name', v_row.name, 'position', v_row.position,
            'created_at', v_row.created_at, 'existed', true);
    end if;

    begin
        insert into public.memo_tag (name, position, created_at)
        values (v_name,
                (select coalesce(max(t.position), 0) + 1 from public.memo_tag t),
                p_now)
        returning * into v_row;
    exception
        when unique_violation then
            -- 并发重名：返回既有行，不再创建第二条同名标签。
            select * into v_row from public.memo_tag t where t.name = v_name;
            if not found then
                raise exception 'memo_create_tag: tag name conflict'
                    using errcode = 'ME005';
            end if;
            return jsonb_build_object(
                'id', v_row.id, 'name', v_row.name, 'position', v_row.position,
                'created_at', v_row.created_at, 'existed', true);
    end;

    return jsonb_build_object(
        'id', v_row.id, 'name', v_row.name, 'position', v_row.position,
        'created_at', v_row.created_at, 'existed', false);
end;
$$;

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
    perform 1 from public.memo_tag t where t.id = p_tag_id for update;
    if not found then
        raise exception 'memo_delete_tag: tag not found'
            using errcode = 'ME001';
    end if;

    -- 锁序纪律（F18）：入口行（标签）→ 记录行（id 升序）→ 级联删除 →
    -- 未分类分组 → 位次。先按 id 升序锁住仍挂在本标签下的记录，再删标签；
    -- 与 memo_update_entry 的「先锁 entry、后动关联/分组」同序，两个事务
    -- 不会各自持有对方需要的锁（原实现级联先锁关联行、后锁未分类分组，
    -- 与编辑事务形成反向锁序，真实触发 40P01 死锁）。
    for v_entry_id in
        select et.entry_id
          from public.memo_entry_tag et
         where et.tag_id = p_tag_id
         order by et.entry_id
    loop
        perform 1 from public.memo_entry e where e.id = v_entry_id for update;
    end loop;

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

commit;
