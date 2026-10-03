# qi-gateway

统一网关 — Eventide + 记忆 + 待办

## 当前状态

记忆系统、连续感总结、Eventide、待办读写闭环和 MCP 记忆工具已实现。旧积温、网关标签定时器、旧主动消息库存投递链和 OrangeChat 客户端兼容插件层已经退役；`/v1/memory-requests*`、`/v1/todos*` 端点与对应 Token 继续保留。

普通聊天的网关上下文使用独立的补充 system 消息追加，原 system prompt 内容始终保持不变。

## 架构

```
手机客户端 → qi-gateway(/v1/chat/completions) → 上游 LLM → qi-gateway → 手机客户端
```

当前保留能力：
- Eventide 身体状态卡注入
- 记忆检索、连续感总结和审核
- 待办工具（`/v1/todos*` 端点保持不变）

## 部署

### 环境变量

| 变量 | 说明 |
|------|------|
| `GATEWAY_TOKEN` | 网关鉴权 token（手机客户端填的 API Key） |
| `UPSTREAM_BASE_URL` | 聊天上游地址，默认 `https://api.deepseek.com/v1` |
| `UPSTREAM_API_KEY` | DeepSeek API Key，只通过部署环境变量配置 |
| `UPSTREAM_MODEL` | 默认 `deepseek-v4-pro`；配置后统一覆盖客户端传入的模型名 |
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

### Zeabur 部署

1. 连接 GitHub 仓库 `yizisala3-cloud/qi-gateway`
2. 配置环境变量
3. 部署后访问 `/health` 确认运行

### 手机客户端连接

- 提供商格式：OpenAI
- API Base URL：`https://你的域名/v1`
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
| `/health` | GET | 健康检查（无需鉴权） |
| `/status` | GET | 网关状态（需鉴权） |

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

热度衰减由数据库 RPC 原子执行，并按 Asia/Shanghai 自然日保证幂等；网关重启或多实例不会让同一批记忆在一天内重复降温。只有已审核且仍启用、重要度低于 10 的记忆参与衰减；自动归档仅限长期未召回、低热度且低重要度的“碎片”，归档为可恢复的 `is_active=false`，不会物理删除。

注入阶段按“核心 / 场景 / 碎片”使用不同相关性门槛和数量上限，并设置 2400 字符硬预算。核心记忆优先完整注入，场景和碎片随相关性降级为标题线索或跳过；只有最终实际进入上下文的记忆会升温。记忆块只追加辅助信息，并明确不得覆盖现有人设、原始 system prompt 或用户当前表达。

## 开发

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入实际值
uvicorn gateway.main:app --reload --port 8000
```
