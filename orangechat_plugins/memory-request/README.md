# 橘瓣记忆与待办插件

这是原“记忆申请”插件的原位升级版，保留相同插件 ID。它同时提供：

- 向 qi-gateway 提交 `pending` 记忆申请；申请必须经过用户审核，AI 不能自行批准。
- 创建、查看、完成、延后和取消当前用户与角色范围内的待办。

两个功能共用网关地址，但使用两个独立 Token。插件不直接连接 Supabase，也不会写入聊天记录。

## 安装前准备

1. 按文件名顺序应用 `supabase/migrations/` 中的记忆迁移；版本替代功能需要 `20260802070000_memory_supersession.sql`。
2. 在 qi-gateway 服务端分别生成并配置 `MEMORY_PLUGIN_TOKEN` 与 `TODO_PLUGIN_TOKEN`。
3. 部署包含 `/v1/memory-requests` 和 `/v1/todos` 系列端点的新版本网关。
4. 将本目录作为橘瓣插件导入，填写：
   - `gateway_url`：网关的 HTTPS 地址。
   - `plugin_token`：记忆 Token，与服务端 `MEMORY_PLUGIN_TOKEN` 相同。
   - `assistant_id`：当前橘瓣角色的 Assistant ID。
   - `todo_plugin_token`：待办 Token，与服务端 `TODO_PLUGIN_TOKEN` 相同。
   - `user_name`：`todos.user_name` 中当前用户的准确名称。
   - `ai_name`：`todos.ai_name` 中当前角色的准确名称。
   - `timezone_offset_minutes`：中国标准时间填写 `480`。

申请提交后，在 qi-dashboard 的“记忆申请”页面编辑并通过或拒绝。只有通过后的正式记忆才会参与召回。

不要把 Supabase `service_role`、secret key 或 `GATEWAY_TOKEN` 填入插件，也不要混用两个插件 Token。

## 记忆工具

`request_memory` 通过普通 HTTP POST 调用：

```text
POST {gateway_url}/v1/memory-requests
Authorization: Bearer {plugin_token}
```

重复内容会被幂等去重。成功响应只表示申请进入审核队列，不表示记忆已经生效。

对于进度、状态、位置等会变化的事实，使用：

- `update_mode=replace`
- 稳定且可复用的 `memory_key`，例如 `project.qi-gateway.progress`

同一事实后续更新必须沿用相同的 `memory_key`。用户审核通过后，新版本生效，旧版本软失效但仍保留审计与恢复关系。普通相似内容不要使用 `replace`；它们应作为独立申请或由用户决定是否合并。

## 待办工具

- `create_todo`：创建用户或 AI 待办。
- `list_today_todos`：读取今天、逾期和未排期的开放待办。
- `complete_todo`：标记完成。
- `snooze_todo`：修改提醒时间并恢复为开放状态。
- `cancel_todo`：软隐藏，不永久删除。

待办请求使用 `TODO_PLUGIN_TOKEN`，并始终带上已配置的 `user_name + ai_name`。时间必须使用带时区的 ISO 8601 格式，例如 `2026-08-03T09:00:00+08:00`。

当前整合仅把记忆与待办工具放进同一个插件；它不会自行唤醒橘瓣。后台主动提醒仍由橘瓣原生主动消息服务负责，后续再由网关在该请求中追加到期待办上下文。

