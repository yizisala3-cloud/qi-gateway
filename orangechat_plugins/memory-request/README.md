# 橘瓣记忆插件 3.1.0

这是面向 qi-gateway 的小体量 OrangeChat 插件，只负责两件事：

- `request_memory`：栖主动提交连续感记忆。
- `review_memory_requests`：查看和审核栖有权处理的普通待审记忆。

插件不包含待办、不直连 Supabase、不保存第二份记忆、不扫描聊天记录，也不会自动触发审核。

## 配置

安装后只需填写：

- `gateway_url`：qi-gateway 的 HTTP(S) 地址。
- `plugin_token`：记忆插件专用 Token。
- `assistant_id`：当前橘瓣角色的 assistant_id。

不要填写 Supabase `service_role`、secret key、`GATEWAY_TOKEN` 或任何数据库密钥。

## 记忆写入

`request_memory` 调用：

```text
POST {gateway_url}/v1/memory-requests
Authorization: Bearer {plugin_token}
Content-Type: application/json
```

可写六类连续感记忆：

- `moment`：近期但值得保留的具体片段。
- `thread`：尚未结束、未来需要继续的线索。
- `episode`：有起点、过程和阶段性结果的完整共同经历。
- `inside_joke`：双方可再次唤起的内部梗、称呼或玩法。
- `profile`：有直接证据的稳定资料、偏好或背景。
- `interaction_rule`：叶子明确提出或确认的长期互动规则。

审核策略按图片要求固定：

- `moment/thread/inside_joke` 请求 `auto_approve`。
- `episode/profile/interaction_rule` 请求 `user_review`，只能由叶子审核。
- `interaction_rule` 还必须带叶子明确指令摘要，并使用 `replace + memory_key`。

`continuity_data` 在 OrangeChat 工具参数中使用 JSON 对象字符串，插件解析后按对象发送给网关。详细结构由网关最终校验。时间没有把握时使用 `null`，不得编造。

网关返回什么状态，插件就如实返回，不会把所有成功改写成 pending，也不会在失败时假装保存成功。

## AI 审核

`review_memory_requests` 支持：

- `list`
- `approve`
- `reject`
- `merge`
- `duplicate`
- `conflict`

插件先读取当前 AI 可审核列表，再执行动作；本地还会再次过滤 `episode/profile/interaction_rule`。这些高权重分类不会出现在 AI 列表中，也不能通过猜测 ID 绕过列表进行审核。

## append 与 replace

- `append`：创建独立记忆，不提供 `memory_key`。
- `replace`：更新同一稳定对象，必须沿用稳定 `memory_key`。
- `interaction_rule` 固定使用 `replace`，便于以后撤销或替代同一规则。

## 主动记忆与自动总结

- 本插件的写入由栖结合完整人设、当前上下文和长期记忆主动判断。
- 插件自身不做自动总结，也不自动扫描聊天记录。
- qi-gateway 的自动总结仍是独立流程，不由本插件控制。

## 网关兼容行为

- 分类策略由网关按 `continuity_type` 强制决定，插件请求头不能改变审核边界。
- `moment/thread/inside_joke` 校验后原子写入正式记忆。
- `episode/profile/interaction_rule` 始终进入 pending，只能由叶子审核。
- AI 审核接口按 assistant、pending 状态和低权重分类再次校验。

## 已移除

本插件已完全移除待办配置、待办 Token、待办工具和待办 HTTP 请求。qi-gateway 内现有待办模块不受影响。

