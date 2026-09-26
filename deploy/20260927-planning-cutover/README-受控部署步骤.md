# 规划管理 · 受控部署步骤（Phase 0.5 门禁 + Phase 1A/1B 迁移上线）

- 制定日期：2026-09-27
- 目标库：Supabase 项目 `dlgunkklpjqletgahdtp`（https://dlgunkklpjqletgahdtp.supabase.co ，执行前请在 Supabase 控制台核对右上角项目 ref 一致）
- 执行方式：全部 SQL 由 user 在 **Supabase Dashboard → SQL Editor** 复制粘贴运行；本套件每个 `.sql` 文件 = 一次"整文件粘贴 → Run"。
- 预计停机窗口：旧网关停机约 20～40 分钟（以实际操作速度为准）。

---

## 〇、核验结论（为什么只需要这两个迁移）

经 2026-09-27 对线上库实查：

1. `schema_migrations` 表**不可作为依据**——历史迁移多为 SQL Editor 手工应用，未留记录（表里最后一条是 20260905 的 werewolf 迁移，而 rumination 链实际已应用）。
2. 以**真实 schema** 为准逐项核对的结果：
   - 基础迁移 `20260920010000_planning_tasks_occurrences.sql` **已应用**（planning_task / planning_occurrence / planning_recompute_state 均为旧 schema，索引与文件一致，`planning_occurrence_schedule_slot_uq` 存在待被 1a 删除）；
   - rumination / memory 链（至 `20260919010000`）**已应用**（`review_memory_request_v5` / `commit_rumination_batch` / `pause_memory_continuity_empty` 等末端标记函数全部存在）；
   - `app_settings` 表已存在（无 planning 键，属正常——新代码首次运行才写入）。
3. **待执行迁移有且只有 2 个**：
   - `20260924010000_planning_phase1a_domain_identity.sql`（Phase 1A 身份列 + CHECK + 身份守卫触发器 + 边界配置种子）
   - `20260924020000_planning_phase1b_refresh.sql`（Phase 1B 刷新/重排列 + 并发守卫触发器 + takeover/absorb RPC + 唯一索引 + 每日刷新开关种子）

### 迁移的两个硬约束（执行前必读）

- **不可重跑**：两个文件内的 `create trigger` / `create unique index` / `add constraint` 都没有 IF NOT EXISTS。失败或中断后**不得原样重跑**——每个文件自身是一个事务（begin/commit），要么整体成功要么整体回滚，不存在半应用；若 01 成功、02 失败，排查后**只重跑 02**。
- **必须先停旧写入源**：1a 的守卫函数引用了 1b 才添加的列；且旧网关的 planning 循环此刻仍在**每分钟写入**（实测 2026-09-26 16:17 UTC 仍在增长，旧实例已累计 12,339 行）。顺序必须是：停旧网关 → 迁移 → 清理 → 起新网关。

---

## 一、部署前置（数据库之外）

- [ ] **0-1 提交工作树**：当前仓库 dev 分支有 20+ 个修改与 13 个未跟踪文件（Phase 1R 全部成果 + 暂停刷新补丁，测试 1235 passed）。先按团队惯例提交（建议分批：planning 后端 / 前端 / 测试 / 部署套件），否则新网关镜像没有可靠构建来源。
- [ ] **0-2 确认部署方式**：按现有方式构建并部署新网关（仓库含 Dockerfile）。本步骤只要求：新镜像 = 提交后的 working tree 代码。
- [ ] **0-3 交叉核对项目**：Supabase 控制台确认当前项目 ref = `dlgunkklpjqletgahdtp`。

## 二、停旧写入源

- [ ] **1-1 停止旧网关**：按现有部署方式停掉旧网关进程/容器（`docker stop <容器>` 或停服务）。旧网关整体停机，记忆等其他功能同步暂停——这是唯一停机窗口。
- [ ] **1-2 停写证据**：停完后等 2～3 分钟（旧循环约 1 分钟一轮），在 SQL Editor 运行：

```sql
select now() as at_utc, max(updated_at) as last_write from planning_occurrence;
```

`last_write` 不再逼近 `now()`（相差 > 3 分钟）即为停写成立。留截图/记录备查。

## 三、备份

- [ ] **2-1 运行 `00_备份.sql`**：生成 `_backup_planning_task_20260927` / `_backup_planning_occurrence_20260927` / `_backup_planning_recompute_state_20260927` 三张备份表，并回显行数（应为 4 / 一万二千余 / 1）。
- [ ] **2-2（可选加固）**：Supabase Dashboard 的自动备份按其套餐计划执行；如需额外保险，可在此时手动触发一次 Supabase 备份（Database → Backups）。

## 四、应用迁移（顺序不可颠倒，两个连续执行）

- [ ] **3-1 运行 `01_迁移A_phase1a_身份列.sql`**（= `supabase/migrations/20260924010000_...`，见文末校验和）
- [ ] **3-2 运行 `02_迁移B_phase1b_刷新与并发.sql`**（= `supabase/migrations/20260924020000_...`）
- [ ] **3-3 运行 `03_迁移后结构核验.sql`**：全部返回 true / 预期值。任何一项 false → 停止，按"回滚与故障处理"排查。

## 五、一次性清理（user 2026-09-27 确认：旧数据直接删掉，不迁移）

- [ ] **4-1 运行 `04_旧数据一次性清理.sql`**：删除全部旧实例（12,339 条）+ 4 条旧任务定义（做饭/洗澡/洗衣服/拿快递）+ 重置重算标记。末尾查询应返回 0 / 0。
- 备注：若临时改主意想保留某个旧任务定义，把它从 delete 语句中排除即可——但保留的旧任务 `refresh_mode` 为 NULL，新代码不会为其生成轮次，须另行分类（此路不通时建议干脆重建）。

## 六、迁移登记（可选但强烈建议）

- [ ] **5-1 运行 `05_迁移登记.sql`**：把两个版本手工写入 `supabase_migrations.schema_migrations`，消除本次发现的"记录不可靠"问题，后续 MCP/CLI 查询差集才准确。

## 七、部署新网关

- [ ] **6-1 构建并启动新网关**（0-1 提交后的代码）。启动后等 2～3 分钟让 `planning_loop` 跑过至少两轮。
- [ ] **6-2 运行 `06_部署后运行核验.sql`**：确认无 round_key 为空的行、无同轮重复。
- [ ] **6-3 管理台验收**：登录管理台 → 规划管理页：新建 1 个每日待办 + 1 个单次待办 → 当前列表立即出现 → 完成 / 此次不执行 / 延后 / 编辑 / 拆分 / 暂停刷新各点一遍 → 全部待办页签看得到记录。
- [ ] **6-4（稳定数日后）**：确认无需回滚后，删除三张 `_backup_planning_*_20260927` 备份表。

---

## 回滚与故障处理

| 故障点 | 处理 |
| --- | --- |
| 01 或 02 执行报错 | 该文件自身事务已整体回滚，库仍是执行前状态。排查报错原因后**只重跑失败的那个文件**（01 成功过则不重跑 01）。期间旧网关保持停机。 |
| 02 成功但 03 核验有 false | 旧网关保持停机，把核验输出发出来排查；必要时用备份表评估是否整体回退（见下行）。 |
| 清理后想回退到旧系统 | 新 schema 对旧代码兼容（新列均可空/带默认，守卫触发器对 round_key 为空的旧行直接放行）。恢复旧数据：`insert into planning_task select * from _backup_planning_task_20260927;`（occurrence 同理，若序列冲突先 `select setval(pg_get_serial_sequence('planning_task','id'), (select max(id) from planning_task));`）→ 重启**旧**网关。 |
| 新网关运行异常 | 停新网关 → 按上行恢复数据 → 启动旧网关。规划数据整体可由备份表重建，回滚成本 ≈ 0。 |

---

## 文件清单与校验和

| 文件 | 用途 | SHA-256 |
| --- | --- | --- |
| `01_迁移A_phase1a_身份列.sql` | = `supabase/migrations/20260924010000_planning_phase1a_domain_identity.sql` | `e431ed9a1b5b9e431716323e6c4da04880009d8c4160cb2da860b209d70dfb71` |
| `02_迁移B_phase1b_刷新与并发.sql` | = `supabase/migrations/20260924020000_planning_phase1b_refresh.sql` | `30eea48aa49a14603f4f046934572615fa02b5d4ef9d66120ae6f2398c7f0fa7` |

以仓库 `supabase/migrations/` 原件为准；校验和不一致说明套件副本过期，请以原件重新复制。

**完整文件夹地址**：
`C:\Users\Administrator\Documents\Codex\2026-08-01\che\qi-gateway-integrate\deploy\20260927-planning-cutover\`

迁移原件所在文件夹：
`C:\Users\Administrator\Documents\Codex\2026-08-01\che\qi-gateway-integrate\supabase\migrations\`
