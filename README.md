# qi-gateway

统一网关 — Eventide + 记忆 + 待办

## 当前状态

记忆系统、连续感总结、Eventide、待办读写闭环、橘瓣工具和客户端主动唤醒时的待办上下文注入已实现。旧积温、网关标签定时器和旧主动消息库存投递链已经退役。

橘瓣原生主动消息请求会由网关保留原始 system prompt 和完整历史，不再被当作真人新消息处理，也不会注入普通聊天上下文。网关仅追加一条独立的内部触发说明，明确程序生成的最后一条 `user` 消息不是真人发言，禁止重复回答和虚构用户信息；是否发送以及如何拒绝仍遵循客户端原始提示词。普通聊天的网关上下文同样使用独立的补充 system 消息，原 system prompt 内容始终保持不变。

## 架构

```
橘瓣 → qi-gateway(/v1/chat/completions) → 上游 LLM → qi-gateway → 橘瓣
```

当前保留能力：
- Eventide 身体状态卡注入
- 记忆检索、连续感总结和审核
- 待办工具及客户端主动请求中的待办上下文
- 客户端原生主动请求识别；发送或拒绝协议由客户端原始 system prompt 决定

## 部署

### 环境变量

| 变量 | 说明 |
|------|------|
| `GATEWAY_TOKEN` | 网关鉴权 token（橘瓣填的 API Key） |
| `UPSTREAM_BASE_URL` | 聊天上游地址，默认 `https://api.deepseek.com/v1` |
| `UPSTREAM_API_KEY` | DeepSeek API Key，只通过部署环境变量配置 |
| `UPSTREAM_MODEL` | 默认 `deepseek-v4-pro` |
| `SUPABASE_URL` | Supabase 项目地址 |
| `SUPABASE_SECRET_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | 仅服务端使用的 Supabase 写入密钥 |
| `SUPABASE_KEY` | 兼容用 publishable/anon key，不用于主动记忆写入 |
| `MEMORY_PLUGIN_TOKEN` | 橘瓣记忆申请插件的独立鉴权 Token |
| `MEMORY_REQUEST_RATE_LIMIT` | 每个 assistant 每分钟最多提交的记忆申请数，默认 6 |
| `TODO_PLUGIN_TOKEN` | 橘瓣待办插件的独立鉴权 Token |
| `TODO_REQUEST_RATE_LIMIT` | 单实例每分钟最多处理的待办插件请求数，默认 60 |
| `PORT` | 端口（默认 8000） |

### Zeabur 部署

1. 连接 GitHub 仓库 `yizisala3-cloud/qi-gateway`
2. 配置环境变量
3. 部署后访问 `/health` 确认运行

### 橘瓣连接

- 提供商格式：OpenAI
- API Base URL：`https://你的域名/v1`
- API Key：填 `GATEWAY_TOKEN` 的值
- 模型名：填 `UPSTREAM_MODEL` 的值

## 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | 核心聊天接口，OpenAI 兼容 |
| `/v1/models` | GET | 模型列表 |
| `/v1/memory-requests` | POST | 橘瓣插件提交 pending 记忆申请（插件专用 Token） |
| `/v1/todos` | POST | 创建当前用户与角色范围内的待办（待办插件 Token） |
| `/v1/todos/query` | POST | 查询今日、逾期或全部开放待办（待办插件 Token） |
| `/v1/todos/{id}/complete` | POST | 标记待办完成（待办插件 Token） |
| `/v1/todos/{id}/snooze` | POST | 延后待办（待办插件 Token） |
| `/v1/todos/{id}/cancel` | POST | 软隐藏取消待办（待办插件 Token） |
| `/admin/api/memory-requests/{id}/review` | POST | Dashboard 通过或拒绝记忆申请（网关 Token） |
| `/health` | GET | 健康检查（无需鉴权） |
| `/status` | GET | 网关状态（需鉴权） |

## 橘瓣记忆与待办整合插件

插件源码位于 `orangechat_plugins/memory-request/`。同一个插件同时暴露记忆申请和待办管理工具；保留原插件 ID，可作为旧“记忆申请”插件的升级版导入。记忆与待办共用网关地址，但分别使用 `MEMORY_PLUGIN_TOKEN` 和 `TODO_PLUGIN_TOKEN`。Supabase 服务端密钥始终留在网关环境变量中。

AI 调用 `request_memory` 后，重复申请由数据库原子去重，所有新申请均为 `pending`，不会进入正常记忆召回。管理员可以在 Dashboard 的“记忆申请”页面编辑后通过或拒绝；通过操作会在数据库事务内写入一条 `verified` 正式记忆，拒绝记录则留存审计。

进度、状态、位置等可变事实可以使用 `update_mode=replace` 和稳定的 ASCII `memory_key`。审核通过后，新版本会原子启用，旧版本仅软失效，并通过 `supersedes_memory_id` / `superseded_by_memory_id` 保留双向替代关系；过期申请不得反向覆盖较新的已审核版本。普通相似内容默认仍是独立候选，不会仅凭相似度自动覆盖。

普通相似内容由 Dashboard 人工选择现有记忆后处理：`duplicate` 只把申请关联到已有记忆，不写入新内容；`conflict` 将申请保留在冲突待处理队列且不参与召回；`merge` 要求用户编辑最终合并内容，再原子创建新版本并软失效旧版本。每次操作都会写入私有的追加式审核事件，保留目标、结果、操作者和备注。

## 橘瓣待办插件

整合插件已包含创建、查看今日待办、完成、延后和取消五个工具；`orangechat_plugins/todo/` 仍保留为只需要待办功能时使用的独立版本。插件只通过普通 HTTP 调用网关，不使用 WebSocket，也不持有 Supabase 密钥。网关对每次读写同时约束 `user_name` 与 `ai_name`；取消操作只会设置 `is_hidden=true`，不会永久删除记录。

“今日待办”包含今天已排期、已逾期和未排期的开放事项，并排除已完成、已取消、空心占位和开始/结束标记。时间参数必须是带时区的 ISO 8601 字符串。橘瓣原生主动消息触发时，网关会读取这些开放待办并作为独立辅助 system 消息追加，原始 system prompt 保持不变；查询失败时直接跳过，不会阻断主动回复。当前部署仅供一个用户与一个 AI 使用，因此主动提醒读取不增加身份环境变量，插件的写入和修改接口仍保留原有身份约束。

应用 `20260804010000_atomic_proactive_todo_claim.sql` 后，同一条待办至少间隔三小时才会再次进入客户端主动请求上下文。该机制只记录最近一次进入上下文的时间，不设置每日提醒次数或累计次数上限；数据库使用原子 claim 避免并发请求重复选中同一待办。若某个部署环境尚未应用迁移，代码会安全退回原有直接读取逻辑，不阻断客户端主动请求。

正常聊天中，用户明确表达“完成了”“稍后再做”或“取消提醒”等已有待办状态变化时，网关会追加一段独立的待办反馈说明，引导模型先用 `list_today_todos` 定位原记录，再调用完成、延期或取消工具；不会用 `create_todo` 复制出新待办。指代不清或无法可靠确定新时间时应先询问用户。普通聊天不会因此自动注入整张待办表，原始 system prompt 仍保持不变。

现有 `todos` 表没有幂等键，创建接口会对同角色、同内容、同时间的常规重试做尽力去重，但不能保证并发下的数据库级原子幂等。若后续需要强化，将单独提交 migration 并在应用前取得明确授权。

## 记忆检索

自动总结会先把橘瓣消息内的显示时间戳解析并统一为 Asia/Shanghai 时间，再从正文移除重复时间行。模型只输出最多 8 条结构化候选，并必须引用本批真实消息 ID；无有效证据的候选会被丢弃。总结预览区分证据时间、记忆实际发生时间及时间精度。应用 `20260804020000_auto_digest_memory_requests.sql` 后，执行总结会把候选原子写入记忆申请队列，只有审核通过后才成为可召回的正式记忆；分类、证据、时间、向量和总结批次来源会随审核结果保留。

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
