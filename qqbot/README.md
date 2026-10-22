# QQ 私聊接入

NapCat 登录专用 QQ 小号，将 OneBot v11 反向 WebSocket 连接到网关的
`wss://你的网关域名/qqbot/onebot`。只处理配置的主人私聊，忽略群聊和其他联系人。
QQ 使用现有模型、人格和长期记忆，保留独立近期对话，不注入 Eventide 情感状态。

## 部署

1. 核对目标 Supabase 的 `chat_messages` 结构，在部署代码前应用
   `supabase/migrations/20261021010000_qqbot_chat_export.sql`。同步 RPC 仅限
   `service_role`；检查实际对象、参数、权限和 PostgREST 缓存。
2. 挂载持久目录，例如 `/data/qqbot`，保持一个网关 worker、一个机器人运行实例。
   参考 `config.env.example` 合并环境变量，保留现有模型和数据库配置。
3. 设置 `QQBOT_TOKEN`（至少24个非空白ASCII字符）、`QQBOT_ACCOUNT_ID`、
   `QQBOT_OWNER_ID`、`QQBOT_ASSISTANT_ID`。角色与现有 `MEMORY_ASSISTANT_ID` 一致。
   设置 `QQBOT_STORE_PATH=/data/qqbot/messages.sqlite3` 和
   `QQBOT_SETTINGS_PATH=/data/qqbot/settings.json`，两个启用环境变量先保持 `false`。
4. 部署后在网关配置页开启总开关。NapCat 建立 Universal 反向 WS 客户端，
   使用数组消息格式、相同 Token 和机器人 X-Self-ID。公网必须使用 WSS。
5. 从主人 QQ 私聊小号，核对回执、远端原文同步和重连。主动消息单独启用，
   启用前明确配置时区、发送时段与频率。

配置页双开关即时生效并原子写入持久文件。保存后以文件为准，环境变量只作为
首次初始值；关闭总开关同时关闭主动消息。读取失败显示错误并禁止修改。
`GET/PUT /admin/api/qqbot/settings` 和 `GET /admin/api/qqbot/status` 沿用网关鉴权，
不返回账号、令牌、聊天正文或服务器文件路径。

## 消息与记忆

SQLite 保存输入、发送队列、回执、近期对话和同步进度。只有 OneBot 成功回执中
含真实 message_id 的回复才进入已发送历史；结果不明的发送不自动重发。
远端同步独立运行，不阻塞私聊，按原文顺序及数据库幂等键发布到现有
`chat_messages`，供已有总结、审核和召回链使用。内部主动检查提示不作为用户原文。

主动检查可以选择静默。主人新消息、暂停、当日静默或关闭开关会取消尚未提交的
主动问候；重启和恢复不会使过期问候重新有效。QQ 支持“暂停主动消息”、
“恢复主动消息”、“今天先别找我”和“查看主动消息状态”。

## 表情

`stickers.example.json` 是合法空目录。实际 JSON 目录及 PNG/JPEG/GIF/WebP 素材
放私人目录，配置 `QQBOT_STICKER_MANIFEST`；路径相对目录文件且不可越界。
模型只选择现有编号，图片内容随消息发送，不要求 NapCat 和网关共享路径。
不设轮数间隔或同图冷却；自动每轮最多一张，明确索要多张时最多10张。
缺少个别文件只排除该素材。尚未提供素材时可以正常文字聊天。

## 运维与验证

迁移服务器前停止旧实例，安全备份整个 SQLite 账本及开关文件，不能用空账本
替代原实例恢复。账本、QQ 登录缓存、Token 和素材不要提交或打包到镜像。
本机 NapCat 关闭或电脑休眠、关机时机器人离线。

隔离测试使用 `python tools/run_qqbot_tests.py -q tests/test_qqbot*.py`，
追加 `--db` 时只启动一次性本地 PostgreSQL，不使用生产凭据。前端竞态测试
通过 `tests/test_qqbot_config_frontend.py` 执行，需要 PATH 中存在 Node。
本地测试不能替代真实 QQ 收发和生产记忆同步验收。
