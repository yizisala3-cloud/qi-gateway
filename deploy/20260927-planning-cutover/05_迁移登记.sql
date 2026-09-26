-- ============================================================
-- 05 · 迁移登记（04 之后执行，可选但强烈建议）
-- 两个迁移是经 SQL Editor 手工应用的，Supabase 不会自动记入
-- supabase_migrations.schema_migrations；手工登记后，将来用 MCP/CLI
-- 查询"哪些迁移已应用"才不会再出现这次的歧义。
-- ============================================================

insert into supabase_migrations.schema_migrations (version, name)
values
  ('20260924010000', 'planning_phase1a_domain_identity'),
  ('20260924020000', 'planning_phase1b_refresh')
on conflict (version) do nothing;

-- 确认登记成功（应返回这两行）
select version, name from supabase_migrations.schema_migrations
where version in ('20260924010000', '20260924020000')
order by version;
