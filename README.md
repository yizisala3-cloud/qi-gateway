# qi-gateway

统一网关 — Eventide + 记忆 + 待办

## 当前状态

记忆系统、连续感总结、Eventide、待办读写闭环和 MCP 记忆工具已实现。旧积温、网关标签定时器、旧主动消息库存投递链和 OrangeChat 客户端兼容插件层已经退役；`/v1/memory-requests*`、`/v1/todos*` 端点与对应 Token 继续保留。

普通聊天的网关上下文使用独立的补充 system 消息追加，原 system prompt 内容始终保持不变。

QQBot主动评估退避、故障停止／手动恢复及日志接入已通过[独立复审](codex/审查日志/qqbot接入/审查摘要-26.10.25.02.34/审查报告.md)。本轮按用户授权提交推送，实际结果见[合并与同步记录](codex/代码修改日志/qqbot接入/合并与同步-26.10.25.02.45.md)；服务升级尚未执行，日志收集总开关保持关闭。现用生产版本仍见下方部署记录。

## 架构

```
手机客户端 → qi-gateway(/v1/chat/completions) → 上游 LLM → qi-gateway → 手机客户端
```

当前保留能力：
- Eventide 身体状态卡注入
- 记忆检索、连续感总结和审核
- 待办工具（`/v1/todos*` 端点保持不变）

另有默认关闭的 QQ 私聊入口（`/qqbot/onebot`）和主动消息运行模块：仅处理指定主人私聊，QQ 跳过 Eventide，复用人格及长期记忆，支持本地图片／GIF表情目录。配置页提供 QQBot 总开关和主动消息开关，保存后即时生效并在重启后保留；关闭总开关会一起关闭主动消息。设置存于 `QQBOT_SETTINGS_PATH`（默认 `data/qqbot-settings.json`），保存后覆盖两个启用环境变量的初始值。配置与接入顺序见 [QQBot 说明](qqbot/README.md)，配置示例为 `qqbot/config.env.example`。首次部署须先应用 `supabase/migrations/20261021010000_qqbot_chat_export.sql`；本地账本与开关文件目录均须持久化，一个机器人使用一个主动网关实例。

## 部署

### 现用部署：云悠 VPS + Coolify（2026-10-24）

云端网关由云悠 VPS 的 Coolify 托管；NapCat 已在同一 VPS 作为独立 Docker Compose 容器运行，已扫码登录并通过容器重启自动恢复。正式入口：

| 用途 | 地址 |
| --- | --- |
| 管理页 | https://yiziwanguan.com/admin/ |
| 手机客户端 API Base URL | `https://yiziwanguan.com/v1` |
| MCP（独立 Bearer Token） | `https://yiziwanguan.com/mcp` |
| NapCat 反向 WebSocket | `wss://yiziwanguan.com/qqbot/onebot` |

原网关 Token、MCP Token 和 OneBot Token 保持各自原值。临时入口
`https://qi-gateway.64-83-26-173.sslip.io` 保留。HTTPS、鉴权、Supabase 和 MCP
已验证；QQ 已连接、表情 168 项、总开关开启、主动消息关闭。10.24 的 NapCat 登录与重启恢复已通过，用户确认私聊正常回复，OneBot 成功回执及两条原文在 Supabase 的正文／角色／身份／顺序已核验。设备身份持久化及冷备情况见[本轮记录](codex/代码修改日志/qqbot接入/NapCat迁至服务器-26.10.24.08.46.md)；表情实际发送本轮未单独复测。

最近核验的网关镜像为 `localhost:5000/qi-gateway:qq-active-window-20261025`，基于原每日上限镜像仅追加本次6文件更新。主动消息默认北京时间`Asia/Shanghai`及`08:00–24:00`，配置页已提供时段修改／保存，旧文件兼容，保存立即生效并跨重启保留，主动开关可点击；现用每日上限30，主动消息仍关闭。[本次发布](codex/代码修改日志/qqbot接入/默认中国时区与发送时段入口-26.10.25.06.40.md)记录精确候选、测试、备份及线上核验。[每日上限发布](codex/代码修改日志/qqbot接入/主动消息每日上限配置入口-26.10.24.11.05.md)保持历史原貌；已推送的主动评估增强仍未随本次发布。
恢复或再次部署需核对实际运行镜像与候选差异；GitHub main包含主动评估增强，不能将代码推送当作服务器已升级。
容器监听 8000；`/data/qi-gateway/qqbot` 持久挂载至 `/data/qqbot`，只运行一个
网关 worker 和机器人实例。恢复时保留账本、开关、素材及账本命名空间。

NapCat 使用 `/data/qi-napcat/compose.yaml`，QQ 登录与 NapCat 配置持久化，固定主机名、网络 MAC 和只读 machine-id，设置 `unless-stopped` 自动重启；WebUI 只监听服务器 `127.0.0.1:6099`。电脑旧 NapCat 入口已停用并备份。网关持久卷继续沿用原路径，机器人不再依赖日常电脑开机。

### 环境变量

| 变量 | 说明 |
|------|------|
| `GATEWAY_TOKEN` | 网关鉴权 token（手机客户端填的 API Key） |
| `GATEWAY_LOG_ENABLED` | 未保存配置页设置时的日志收集默认值，默认 `false`；上线保持关闭 |
| `UPSTREAM_BASE_URL` | 聊天上游地址，默认 `https://api.deepseek.com/v1` |
| `UPSTREAM_API_KEY` | 未为用途选择运行时配置时使用的聊天 API Key |
| `UPSTREAM_MODEL` | 默认 `deepseek-v4-pro`；对应用途实际选中的模型覆盖客户端模型名 |
| `MODEL_CONFIG_PATH` | 服务端模型配置 SQLite 文件；默认复用 QQ 设置或账本目录，否则为 `data/model-config.sqlite3`；目录须持久化 |
| `GEMINI_BROWSER_TOOL_COMPAT_ENABLED` | Gemini 浏览器代理工具历史兼容，默认 `false`；仅对最终模型名以 `gemini-` 开头的请求生效 |
| `SUPABASE_URL` | Supabase 项目地址 |
| `SUPABASE_SECRET_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | 仅服务端使用的 Supabase 写入密钥 |
| `SUPABASE_KEY` | 兼容用 publishable/anon key，不用于主动记忆写入 |
| `MEMORY_PLUGIN_TOKEN` | 记忆申请插件的独立鉴权 Token（插件源码已移除，端点保留） |
| `MCP_MEMORY_TOKEN` | `/mcp` 远程记忆工具的独立 Bearer Token |
| `MEMORY_REQUEST_RATE_LIMIT` | 每个 assistant 每分钟最多提交的记忆申请数，默认 6 |
| `TODO_PLUGIN_TOKEN` | 待办插件的独立鉴权 Token（插件源码已移除，端点保留） |
| `TODO_REQUEST_RATE_LIMIT` | 单实例每分钟最多处理的待办插件请求数，默认 60 |
| `RUMINATION_BASE_URL` / `RUMINATION_API_KEY` / `RUMINATION_MODEL` | 反刍连续感路径的独立提取模型；留空回退复用 `CONTINUITY_*` |
| `RUMINATION_MAX_TOKENS` | 反刍文本提取最大输出 token 数，默认 8192 |
| `RUMINATION_DAILY_HOUR` | 反刍每日调度小时（Asia/Shanghai），默认 6 点 |
| `PORT` | 部署平台端口映射惯例保留；网关启动端口由 Dockerfile 固定为 8000，代码不读取 |

本仓库固定 `supabase==2.15.1`；本次迁移实际使用原项目的 legacy `service_role`
JWT，填入 `SUPABASE_SERVICE_ROLE_KEY`，`SUPABASE_SECRET_KEY` 留空。此 SDK
尚不接受新版 `sb_secret_` 格式，不用 publishable／anon key 代替服务端写入密钥。

### 旧平台 Zeabur 部署参考（非现用环境）

1. 连接 GitHub 仓库 `yizisala3-cloud/qi-gateway`
2. 配置环境变量
3. 部署后访问 `/health` 确认运行

### 备忘录详情与回收站清理

部署详情读取优化前，先应用 `supabase/migrations/20261016020000_memo_detail_read_and_cleanup.sql`，并确认 PostgREST 已识别 `memo_get_entry(bigint,timestamptz)`，仅服务端 `service_role` 可调用。迁移兼容旧后端；新后端依赖该函数，不能先发后端再补 SQL。

单条详情通过一次只读 RPC 获取正文与全部标签。物理清扫由后台串行执行，每轮完成后等待 60 秒，忙行留到下一轮；交互请求不等待清扫。删除满 72 小时的记录即使尚未物理清除，也不会出现在详情/回收站列表，且不能恢复。前端同条在途点击共用请求，切换或关闭取消旧读取并隔离迟到响应。

### 手机客户端连接

- 提供商格式：OpenAI
- API Base URL：现用 `https://yiziwanguan.com/v1`；独立部署时替换为自己的域名
- API Key：填 `GATEWAY_TOKEN` 的值
- 模型名：填 `UPSTREAM_MODEL` 的值

### Gemini 浏览器代理工具历史兼容

部分 OpenAI 兼容的 Gemini 浏览器代理要求 `role=tool` 消息同时携带函数名，但手机客户端的标准工具历史通常只提供 `tool_call_id`。仅在确认上游存在此兼容问题时，设置 `GEMINI_BROWSER_TOOL_COMPAT_ENABLED=true`；网关会在最终选中的模型名以 `gemini-` 开头时，从前置 `assistant.tool_calls` 按 `tool_call_id` 补充或纠正 `tool.name`。

该兼容层默认关闭，不根据 `UPSTREAM_BASE_URL` 猜测供应商，不改变消息顺序、`tool_call_id`、`content`、`tool_calls`、参数或工具定义。无法匹配的工具结果保持原样，不会伪造 `unknown_function`。非 Gemini 模型和关闭开关时继续沿用原转发行为。

## 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 核心聊天接口，OpenAI 兼容 |
| `/v1/models` | GET | 模型列表 |
| `/v1/memory-requests` | POST | 手机客户端插件提交 pending 记忆申请（插件专用 Token） |
| `/v1/memory-requests/reviewable` | POST | 列出当前 assistant 下 AI 可审核的低权重申请 |
| `/v1/memory-requests/{id}/review` | POST | 手机客户端 AI 审核低权重申请；服务端再次限制分类 |
| `/mcp` | Streamable HTTP | 标准 MCP 记忆工具（独立 MCP Token） |
| `/v1/todos` | POST | 创建当前用户与角色范围内的待办（待办插件 Token） |
| `/v1/todos/query` | POST | 查询今日、逾期或全部开放待办（待办插件 Token） |
| `/v1/todos/{id}/complete` | POST | 标记待办完成（待办插件 Token） |
| `/v1/todos/{id}/snooze` | POST | 延后待办（待办插件 Token） |
| `/v1/todos/{id}/cancel` | POST | 软隐藏取消待办（待办插件 Token） |
| `/admin/api/memory-requests/{id}/review` | POST | Dashboard 通过或拒绝记忆申请（网关 Token） |
| `/admin/api/eventide/settings` | GET / PUT | Dashboard 读取/切换身体状态注入开关（网关 Token） |
| `/admin/api/eventide/body` | GET | Dashboard 读取 Eventide 身体状态（只读，网关 Token） |
| `/admin/api/context/settings` | GET / PUT | Dashboard 读取/设置上下文注入（近期对话开关与条数、时间戳开关）（网关 Token） |
| `/admin/api/logs/settings` | GET / PUT | 配置页持久化日志收集开关（网关 Token）；关闭仍可查历史日志 |
| `/health` | GET | 健康检查（无需鉴权） |
| `/status` | GET | 网关状态（需鉴权） |

## 模型 API 配置（本地实现，待部署）

控制台“配置”分为“常规配置”和“API配置”。API 配置集中管理供应商、API 地址、多个带别名的 Key 和模型列表；日常聊天、QQ、光遇、记忆提取与分析、连续感总结、反刍分别选择供应商／Key／模型，共用供应商库。总结页和 QQ／光遇配置区提供对应入口。新建供应商不会自动切换已有用途；选择“使用现有配置”恢复该用途的环境配置。保存后的新调用生效，已经开始的调用保留原配置快照。本批不增加 Key 自动轮换或故障切换。

向量模型独立列出且仅查看，继续使用原 `ANALYSIS_*` 地址／Key、`Qwen/Qwen3-Embedding-0.6B` 和1024维；调整文本分析服务不修改向量服务，不触发历史向量重建。非模型服务的插件 Token 仍在常规配置。

配置由服务端 SQLite 事务保存，版本冲突返回409，失败不伪装成功。`MODEL_CONFIG_PATH` 可指定持久卷内路径；留空时优先采用 `QQBOT_SETTINGS_PATH`、其次 `QQBOT_STORE_PATH` 的父目录，无上述配置时使用 `data/model-config.sqlite3`。所有 worker 必须共用此文件，独立服务器需另行共享配置存储。发布时确认文件所在目录已持久挂载，备份包含配置文件；它含真实供应商密钥，仅服务端保存。默认路径已排除 Git／Docker 构建。

管理接口 `GET /admin/api/model-config`、`PUT /admin/api/model-config/providers`、`PUT /admin/api/model-config/routes/{purpose}` 均要求已配置且匹配的网关 Bearer Token。写请求为 `{revision,value}`；读取和保存结果只返回 Key 别名／标识，不回传明文，响应不缓存。本功能无需新增 Supabase 迁移。

## 日志收集开关

配置页“功能开关 → 日志收集”默认关闭，保存到现有 `app_settings` 表的
`gateway_log.collect_enabled`，无需新增迁移。保存成功后当前实例立即生效，
其他实例每 30 秒同步一次；重启恢复已保存的选择。关闭停止接收新事件，已收集
事件按原有时限收尾，历史日志仍可查询。数据库不可用或保存值非法时关闭采集；
保存失败显示错误，不伪装成功。首次上线不设置 `GATEWAY_LOG_ENABLED=true`，
且不预先写入开启值。已登记的日志与密钥查看缺陷仍待后续修复，关闭采集不代表
这些缺陷已经解决。

## 身体状态注入开关

网关级应用设置存储在 Supabase `app_settings` 表（`key` / `value` jsonb / `updated_at`，由迁移 `20260920010000_create_app_settings.sql` 创建，并幂等种子 `eventide.inject_enabled=true`）。Dashboard 在配置页提供开关（`PUT /admin/api/eventide/settings`），情感页只读展示当前身体状态（`GET /admin/api/eventide/body`，只读接口不会推进状态或创建初始状态）。

开关语义：

- 开启（默认）：每次聊天构建上下文时按 60 秒进程内缓存读取开关，推进 Eventide 状态并注入身体状态卡；首次聊天自动创建初始状态。
- 关闭 = 彻底暂停：不再注入、不再推进状态、不创建初始状态，数值冻结在关闭那一刻；已存在的状态行原样保留。
- 重新开启：不回填关闭期间的时间，由 Eventide 原生机制按时间分段追赶（每段 ≤6 小时、最多 48 段），网关不写任何追赶逻辑。
- fail-open：开关读取异常或行缺失时一律按开启处理（保持注入现状），查询失败只记 warning；写库失败时 Dashboard 开关会报错并回滚 UI。

## 上下文注入设置

配置页“上下文注入”卡片对应 `/admin/api/context/settings`（网关 Token，GET 读取、PUT 可选更新任意键），设置同样存储在 `app_settings`（由迁移 `20260921010000_seed_context_injection_settings.sql` 幂等种子）。保存成功即清空 60 秒 TTL 缓存，下一次聊天请求立即生效，无需重启。

- 流式上下文（近期对话注入）：每轮聊天稳定注入数据库 `chat_messages` 最近 N 条（user/assistant 各算一行），与客户端本次请求发送多少条历史无关、也不去重；N 范围 1–100，默认 10。关闭后网关不查库、不注入 `[最近对话]` 块，persona / Eventide / 记忆检索不受影响。
- 时间戳注入：每轮请求实时生成 `[当前时间] YYYY-MM-DD HH:MM 星期X`（北京时间 UTC+8），默认开启，用于取代客户端提示词里手动维护的时间。
- 两个开关 fail-open：读取异常或行缺失按开启处理；注入条数非法时回退默认 10 并夹取到 1–100。
- `chat_messages` 对网关始终只读，相关迁移不触碰该表。

## MCP 记忆工具

推荐客户端通过官方 Python MCP SDK 提供的 Streamable HTTP 端点连接：URL 填 `https://你的域名/mcp`，自定义请求头填 `Authorization: Bearer <MCP_MEMORY_TOKEN>`。协议协商、初始化、ping、`tools/list`、`tools/call`、请求 ID、Content-Type/Accept 和标准工具错误由 SDK 处理。服务使用无持久会话模式，只暴露 `request_memory` 与 `review_memory_requests`。

旧版 OrangeChat 兼容插件源码已从仓库移除（原 `orangechat_plugins/` 目录）；`/v1/memory-requests*` 端点与 `MEMORY_PLUGIN_TOKEN` 鉴权保持不变，MCP 是当前推荐的客户端接入方式。

`moment/thread/inside_joke` 经完整校验、去重和版本处理后，在单个数据库事务中直接成为正式记忆；`episode/profile/interaction_rule` 强制进入 pending，由用户审核。客户端请求头不能改变这个分类边界。MCP 申请保留 `memory_requests.source=mcp_memory`，历史插件申请保留 `orangechat_plugin`（历史数据值），正式记忆按既有规则使用 `ai_tool_request`（自动总结审核结果保留 `daily_digest`）。

进度、状态、位置等可变事实可以使用 `update_mode=replace` 和稳定的 ASCII `memory_key`。审核通过后，新版本会原子启用，旧版本仅软失效，并通过 `supersedes_memory_id` / `superseded_by_memory_id` 保留双向替代关系；过期申请不得反向覆盖较新的已审核版本。普通相似内容默认仍是独立候选，不会仅凭相似度自动覆盖。

普通相似内容由 Dashboard 人工选择现有记忆后处理：`duplicate` 只把申请关联到已有记忆，不写入新内容；`conflict` 将申请保留在冲突待处理队列且不参与召回；`merge` 要求用户编辑最终合并内容，再原子创建新版本并软失效旧版本。每次操作都会写入私有的追加式审核事件，保留目标、结果、操作者和备注。

## 待办接口

待办能力通过 `/v1/todos*` 端点提供（需 `TODO_PLUGIN_TOKEN`）；旧待办插件源码已从仓库移除（原 `orangechat_plugins/todo/` 目录）。客户端只通过普通 HTTP 调用网关，不持有 Supabase 密钥。网关对每次读写同时约束 `user_name` 与 `ai_name`；取消操作只会设置 `is_hidden=true`，不会永久删除记录。

“今日待办”包含今天已排期、已逾期和未排期的开放事项，并排除已完成、已取消、空心占位和开始/结束标记。时间参数必须是带时区的 ISO 8601 字符串。写入和修改接口同时约束 `user_name` 与 `ai_name`。

正常聊天中，用户明确表达“完成了”“稍后再做”或“取消提醒”等已有待办状态变化时，网关会追加一段独立的待办反馈说明，引导模型先用 `list_today_todos` 定位原记录，再调用完成、延期或取消工具；不会用 `create_todo` 复制出新待办。指代不清或无法可靠确定新时间时应先询问用户。普通聊天不会因此自动注入整张待办表，原始 system prompt 仍保持不变。

现有 `todos` 表没有幂等键，创建接口会对同角色、同内容、同时间的常规重试做尽力去重，但不能保证并发下的数据库级原子幂等。若后续需要强化，将单独提交 migration 并在应用前取得明确授权。

## 记忆检索

自动总结会先把手机客户端消息内的显示时间戳解析并统一为 Asia/Shanghai 时间，再从正文移除重复时间行。模型只输出最多 12 条结构化候选，并必须引用本批真实消息 ID；无有效证据的候选会被丢弃。总结预览区分证据时间、记忆实际发生时间及时间精度。应用 `20260804020000_auto_digest_memory_requests.sql` 后，执行总结会把候选原子写入记忆申请队列，只有审核通过后才成为可召回的正式记忆；分类、证据、时间、向量和总结批次来源会随审核结果保留。`inserted_count` 是本批写入的申请总数（含提交事务内自动转正的部分）；每条申请的真实状态可在 admin 运行详情中查看。

连续感总结成功提交后只设置 1 小时的自动冷却（`auto_cooldown_until`）；旧版"手动触发成功后再冷却 10 秒"的机制已在 `20260917010000_memory_continuity_stale_batch_guard.sql` 重建提交 RPC 时移除。

该 migration 还会在原子提交时检查已有记忆工具申请、正式记忆和开放待办。同一来源、同一稳定主题键且处于同一时间窗口，或内容已完全存在时直接跳过；已创建的待办不会被再写成目标记忆。仅靠相似度无法确定时不会自动覆盖，而是保留 `pending` 并在审核页标注“疑似重复”。同一主题的更晚状态更新仍会进入替代审核，不会被相似度误删。

召回使用关键词与向量双通道融合排序，相关性优先于热度和新鲜度；两条通道同时命中会获得一致性加权。向量服务未配置或暂时不可用时自动退化为关键词检索。只有 `is_active=true` 且 `verified='verified'` 的记忆会进入候选并在实际注入后升温。

聊天召回默认冷却 60 分钟，`profile` / `interaction_rule` 豁免；环境变量 `MEMORY_RECALL_COOLDOWN_MINUTES` 可设置 0 关闭，修改后重启生效。两个检索通道均在候选上限前过滤，实际注入后才由后台批次更新召回时间、次数和热度，并保存最多 150 条实际注入文本快照。记忆管理页的“召回记录”展示这些快照，允许重复记忆、查看当前原记忆，已删除的原记忆不会连带删除历史。后台写入不增加聊天同步请求；允许短时重复召回，不保证严格并发冷却或异常退出零丢失。升级须先执行 `supabase/migrations/20261020020000_memory_recall_cooldown_history.sql`，再部署代码；未执行时新检索参数和记录表不可用。完整需求和本地验证见 `codex/需求/10.20新增召回冷却-召回记录/`。

热度衰减由数据库 RPC 原子执行，并按 Asia/Shanghai 自然日保证幂等；网关重启或多实例不会让同一批记忆在一天内重复降温。只有已审核且仍启用、重要度低于 10 的记忆参与衰减；自动归档仅限长期未召回、低热度且低重要度的“碎片”，归档为可恢复的 `is_active=false`，不会物理删除。

注入阶段按“核心 / 场景 / 碎片”使用不同相关性门槛和数量上限，并设置 2400 字符硬预算。核心记忆优先完整注入，场景和碎片随相关性降级为标题线索或跳过；只有最终实际进入上下文的记忆会升温。记忆块只追加辅助信息，并明确不得覆盖现有人设、原始 system prompt 或用户当前表达。

## 规划待办备注

待办支持独立备注，不设字数上限。创建／编辑表单的备注位于内容输入框下方。手机详情采用底部面板，备注最多预览两行，点击查看全文和编辑；面板可从默认位置整块向下拖动，允许回拖至默认位置，剩屏幕五分之一时连续滑出。平板采用可拖动居中弹窗，备注在条目内编辑，最多显示三行，超出部分在输入框内滚动。PC 保留右侧详情栏。详情栏及其中间内容区均不滚动。

备注与“部分完成备注”分开保存。修改或清空备注时，任务模板与所有未结束实例在同一数据库事务内同步；已完成、已删除、此次不执行和超时记录保留当时的备注。旧历史没有备注时保持为空，不能从当前模板回填。

部署此功能前依次应用 `supabase/migrations/20261014010000_planning_task_notes.sql`、`supabase/migrations/20261014020000_planning_once_task_notes.sql`，再更新网关与前端。第一份新增任务备注、实例备注快照及同步／历史保护触发器，不改写已有历史记录；第二份扩展单次待办守护编辑 RPC 的备注白名单与写入逻辑，保留行锁和实例存在性复核。已安装第一份的环境只需追加第二份；备注缺省保留、`null` 清空、文本更新。本地 `tools/preview_planning.cjs` 的创建与备注编辑只操作内存测试数据，重启预览会重置。

取消字数上限还需应用 `supabase/migrations/20261015010000_planning_task_notes_unlimited.sql`，移除两张表原有的 60 字约束后再更新网关与前端；备注同步和历史保留规则保持不变。

## 10.16 规划管理优化

详情名称可原位编辑，并在同一详情区域修改备注、耗时与可安排时段。
保存时明确显示生效范围：未开始的所选轮次与未来定义同步；已有执行事实时
名称备注仍同步开放记录，时间只影响未来；已关闭历史保持原值。中空阶段名称
可直接编辑，耗时与窗口按整轮校验。重复规则仍沿用原有未来轮次语义。

新建待办类型初始未选择，包括目标内新建入口。当前列表常驻拖动把手，
连续拖动后统一确认或取消。当前轮可以沉底并恢复：沉底退出排程、占用槽和
冲突计算，保留原截止、生命周期、执行事实及提醒设置，新轮次不继承沉底。

发布前按顺序应用以下增量迁移，再更新网关与前端：

1. `supabase/migrations/20261017010000_planning_detail_edit_and_parking.sql`
2. `supabase/migrations/20261017020000_planning_parked_recompute_guard.sql`
3. `supabase/migrations/20261017030000_planning_detail_once_absence_guard.sql`
4. `supabase/migrations/20261017040000_planning_detail_general_save.sql`

本地已使用完整迁移链的一次性 PostgreSQL 验证；本轮没有执行或核验生产迁移，
没有提交、推送或部署。所选轮次编辑通过 `GET/POST /admin/api/planning/tasks/{id}/detail-edit`
获取生效范围与并发 token，再携带 UUID `Idempotency-Key` 保存。模板、当前轮和
回执在同一事务提交；结果未知时重试原请求，冲突时保留草稿并明确重读范围。
原任务 PATCH 保留只编辑定义的兼容语义。

固定周期详情上下文先结清旧轴欠轮再冻结 token；单次空轮次在事务的任务锁内
复核，生成抢先完成时整体拒绝。沉底排程资格与预估时间所有权分别判断。
明确拒绝的未知保存结束恢复记录，未生成单次允许编辑合法耗时与窗口。

专项：`pytest tests/test_planning_optimization_pgserver.py --db`。
审查修复专项：`pytest tests/test_planning_detail_review_fixes_pgserver.py --db`；
前端专项：`python tests/run_planning_optimization_browser.py tests/planning_detail_review_fixes.browser.cjs`。
浏览器验收：`python tests/run_planning_optimization_browser.py`，自动启动并停止端口
8026 的隔离 API/PG；需要本机 Edge 与 Playwright。Windows 测试设置
`PYTHONUTF8=1`、`PGTZ=Asia/Shanghai`，避免旧用例的编码与时间字符串格式差异。

## 长期目标一期

规划管理的“长期目标”支持目标增删改、重复待办一对一关联、目标内新建并关联、
整轮完成自动计数与持久化手动修正。关联前已完成及解绑期间完成的轮次不补计；
中空轮须两个阶段完整完成；解绑、换绑、删除待办保留已有贡献，合法历史状态更正
仍调整原贡献。达标后继续累计，文字百分比可超过 100%。

部署前，在现有完整迁移链之后执行
`supabase/migrations/20261016010000_planning_goals.sql`，再更新网关和前端。
该迁移新增五张私有表、整轮事实/删除触发器与目标 RPC，不回填历史贡献；
不能用本地测试代替目标环境迁移和上线验收。目标写入需 UUID `Idempotency-Key`，
编辑/关联/修正/删除还需 `expected_version`；完成事实与命令在 READ COMMITTED
事务内排序，不能用前端列表重新计算次数。

`python -m tools.preview_planning_goals` 启动一次性本地 PostgreSQL 和实际目标 API
的预览（端口 8021、本地令牌 `local-goals-preview`）；后台生成/重算在该夹具中停用，
不使用生产客户端。专项测试为 `pytest tests/test_planning_goals_pgserver.py --db` 与
`pytest tests/test_planning_goals_frontend.py`，浏览器脚本为
`node tests/planning_goals.browser.cjs`，需要预览服务与本机 Edge/Playwright。

## 开发

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入实际值
uvicorn gateway.main:app --reload --port 8000
```

## 测试

依赖安装：

```bash
pip install -r requirements.txt -r requirements-test.txt   # 快速测试
pip install -r requirements-test-pg.txt                    # 追加真实 PostgreSQL 套件（可选）
```

三种运行方式：

```bash
pytest            # 快速测试：默认入口，约半分钟。真实 PostgreSQL 测试自动跳过
pytest --db       # 完整测试：追加真实 PostgreSQL 套件（等价于设置全部 QIGATEWAY_* 门控变量）
pytest -m "not database"        # 非数据库测试，无需安装真库栈
```

按分类筛选（marker 由 conftest.py 自动标注；纯前端文件按文件标注，混合文件按测试类标注，后端用例不会随前端筛选被排除）：

```bash
pytest -m database --db   # 只跑真实 PostgreSQL 测试
pytest -m frontend        # 前端行为、语法、契约与资源检查（行为测试使用 Node / quickjs）
pytest -m "not frontend"  # 其余测试
```

Node 行为测试需要 PATH 中的 `node`，或已安装的 `nodejs-bin`；缺少运行时会明确跳过。quickjs 由 `requirements-test.txt` 安装。`frontend` 包含源码契约与资源检查，不代表浏览器端到端验收。

真实 PostgreSQL 测试说明：

- 用 `pgserver` 包在临时目录启动一次性 PostgreSQL + pgvector 实例，连接 URI 来自该实例，不使用 Supabase 连接配置；导入网关模块仍会读取环境配置。首次补齐时区数据时会将 tzdata 文件复制到 pgserver 安装目录，后续运行复用。
- `--db` 等价于设置 `QIGATEWAY_PG_MIGRATION_TEST`、`QIGATEWAY_PG_PLANNING_TEST`、`QIGATEWAY_PG_MEMO_TEST`、`QIGATEWAY_ADMIN_MEMORY_PG_TEST` 四个门控变量；已显式导出的变量优先，可单独控制某个套件。
- `--db` 在启动阶段检查 pgserver、psycopg，以及已持久化的 Asia/Shanghai 时区文件或可导入的 tzdata；缺少这些前置依赖时直接报错并提示安装命令。显式设置为 `0` 的门控变量仍会让对应套件跳过。
- 跳过原因默认显示（pytest.ini 的 `-rs`）：不加 `--db` 的每次运行都会列出如何启用真库测试。

另有一个未接入 pytest 的浏览器验收脚本 `tests/frontend_planning_create_latency.browser.cjs`（导出 `runPlanningTests`，需外部 Playwright harness 与本地预览服务驱动）；本仓库未安装 Playwright，需要时另行接入，勿与 quickjs/Node 前端测试混淆。

规划详情支持按原样式无边框编辑，离开编辑位置自动保存；原提醒位置的通用“保存”将实际修改字段和提醒一起提交。该保存依赖新增迁移 `supabase/migrations/20261017040000_planning_detail_general_save.sql`，须在前三份规划详情迁移后执行。当前仅本地隔离验证，未发布。
