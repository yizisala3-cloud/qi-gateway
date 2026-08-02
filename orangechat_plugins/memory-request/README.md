# 橘瓣记忆申请插件

该插件只允许 AI 向 qi-gateway 提交 `pending` 记忆申请。申请必须经过用户审核，AI 不能自行批准、修改正式记忆或永久删除记忆。

## 安装前准备

1. 按文件名顺序应用 `supabase/migrations/` 中的记忆迁移；版本替代功能需要 `20260802070000_memory_supersession.sql`。
2. 在 qi-gateway 服务端生成并配置独立的 `MEMORY_PLUGIN_TOKEN`。
3. 部署包含 `/v1/memory-requests` 端点的新版本网关。
4. 将本目录作为橘瓣插件导入，填写：
   - `gateway_url`：网关的 HTTPS 地址。
   - `plugin_token`：与服务端 `MEMORY_PLUGIN_TOKEN` 相同。
   - `assistant_id`：当前橘瓣角色的 Assistant ID。

申请提交后，在 qi-dashboard 的“记忆申请”页面编辑并通过或拒绝。只有通过后的正式记忆才会参与召回。

不要把 Supabase `service_role`、`secret key`、`GATEWAY_TOKEN` 填入插件。

## 工具

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

