# 橘瓣待办插件

该插件让 AI 通过 qi-gateway 管理 Supabase 已有的 `todos` 表，不直接持有任何 Supabase 密钥，也不会写入聊天记录。

## 配置

1. 在网关环境变量中设置独立的 `TODO_PLUGIN_TOKEN`。
2. 部署包含 `/v1/todos` 系列端点的新版本网关。
3. 将本目录导入橘瓣，填写：
   - `gateway_url`：网关 HTTP(S) 地址。
   - `plugin_token`：与服务端 `TODO_PLUGIN_TOKEN` 相同。
   - `user_name`：`todos.user_name` 中当前用户的准确名称。
   - `ai_name`：`todos.ai_name` 中当前角色的准确名称。
   - `timezone_offset_minutes`：中国标准时间填写 `480`。

不要把 Supabase `service_role`、secret key、`GATEWAY_TOKEN` 或记忆插件 Token 填入本插件。

## 工具

- `create_todo`：创建用户或 AI 待办。
- `list_today_todos`：读取今天、逾期和未排期的开放待办。
- `complete_todo`：标记完成。
- `snooze_todo`：修改提醒时间并恢复为开放状态。
- `cancel_todo`：软隐藏，不永久删除。

所有请求都是普通 HTTP POST。服务端始终使用 `user_name + ai_name` 约束查询和修改；插件无法操作其他角色的待办。

当前版本基于既有表结构进行常规重试去重，并非数据库级原子幂等。若后续需要承受并发重复提交，应先新增幂等键 migration，再由用户明确授权应用。

