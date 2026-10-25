# NapCat 迁至现有服务器

时间：2026-10-24，America/Los_Angeles。用户在只读可行性检查后明确授权执行迁移。

## 范围与起点

仅迁移日常电脑上的 NapCat／QQ 登录组件。网关、模型、记忆、QQ SQLite、持久化开关及 168 项表情已在 VPS；继续沿用这些数据和身份，不修改业务代码、不新建账本命名空间、不执行 Supabase SQL，不重启网关或其它应用。私聊恢复时启用 QQ 总开关，主动消息保持关闭。

迁移前实际状态：QQ 总开关关闭，运行／连接均为 false；电脑 NapCat 未运行。不要把 10.23 文档中的连接成功当作本次起始状态。日常电脑上的普通 QQ 客户端不属于迁移操作对象。

## 备份与部署配置

- 迁移前快照：`codex/环境变量备份/2026-10-24-NapCat迁移前/`，由 Git 忽略；服务器另存于 `/data/qi-napcat/backups/`。原连接配置、网关开关、在线 SQLite 备份及原 QQ 持久卷压缩包保留，下载逐项 SHA256 校验。
- SQLite 在线备份 `integrity_check=ok`，命名空间仅记录指纹；迁移前 history／inbox／outbox 均为 0。现有空账本继续使用，不能据此生成另一份空账本替代。
- 固定官方 Linux 镜像 `mlikiowa/napcat-docker:v4.18.41@sha256:69c5ca84ec226d658d0d5cd19997c6c89169da3206faa2948c2dc67d669b6406`，与原 NapCat 版本一致。
- 独立 Docker Compose 服务 `qi-napcat`，目录 `/data/qi-napcat`；QQ 登录、NapCat 配置及插件目录持久化。固定主机名／网络 MAC，新增持久 machine-id 只读挂载；网关仍由原 Coolify 应用运行。
- `restart=unless-stopped`，内存上限 1536 MiB、CPU 上限 2、PID 上限 512、共享内存 256 MiB；Docker 日志轮转 10 MiB × 3。未授予 privileged／SYS_ADMIN。
- WebUI 只映射 `127.0.0.1:6099`，使用独立随机令牌；不发布公网管理页。OneBot 仅保留原正式 WSS 反向客户端，保留 Token、数组格式与证书验证，不开放额外 OneBot 监听端口。

运维辅助文件位于被 Git 忽略的 `qqbot/private/napcat-server-20261024/`，SSH 绑定既有主机公钥。备份回传遇到 SFTP 预取停滞后改用 SSH 流式读取；最终按服务器 SHA256 核验，未跳过备份。

## 执行与验收

08:59 已启动容器并恢复网关 QQ 总开关；主动消息继续关闭。容器运行、无 OOM／重启，WebUI 仅绑定服务器环回地址，三个持久挂载及资源上限与配置一致。已停用电脑旧反向客户端和启动脚本并保留备份。

部署核验：账号／Token 与原网关匹配；只有一个正式反向客户端、数组消息、TLS 证书验证开启，无额外 OneBot 监听口；168 项表情和账本命名空间指纹不变。网关继续使用 `localhost:5000/qi-gateway:sky-delivery-20261023-1111-acb4e6`，未重启／重新部署。首次等待登录时 NapCat 约 272.4 MiB、CPU 1.29%，仅为该次瞬时采样。

09:07 用户首次扫码后，实际 QQ 为 `ready`、账号匹配，网关 `running=true / connected=true`。09:08 第一次离线冷备后停止／启动 NapCat，快速登录提示“登录态已失效”，回到二维码；该轮恢复失败。检查发现首份冷备中没有 `qq/global/nt_data/msf/machine-info`，重启后才生成，现有登录 DB 仍在；这些事实尚不足以唯一判定失效根因。

09:14 已将当前 machine-id 写入持久文件并只读挂载，固定当前网络 MAC，主机名保持不变；重建核验机器标识／MAC 未变，未改 QQ 业务代码或引入额外权限。网络 MAC 配置使用 Docker 官方支持的 `services.networks.mac_address`（[规范](https://docs.docker.com/reference/compose-file/services/#mac_address)）；当前版本的 NapCat 源码按 machine-id 与持久 machine-info 中的 MAC 计算 Linux GUID。用户随后再次扫码确认。

09:16 新登录状态离线冷备后再次停止／启动：**自动登录成功，无需扫码，网关自动重连**。新容器启动时间 `2026-10-24T16:16:43.694190875Z`，WebUI `isLogin=true / loginPhase=ready / coreReady=true`，账号匹配；网关 `running=true / connected=true / leader=true`，主动关闭。machine-id 与固定前指纹一致，machine-info 与离线备份字节一致，实际 MAC 匹配持久配置。SQLite 完整性正常、命名空间保持，表情 168 项；该次采样 NapCat 约 183 MiB、CPU 0.96%。网关自 `2026-10-23T18:20:17.273812544Z` 运行，本轮未重启。

当前状态：**MIGRATED, ONLINE, RESTART AND TEXT CHAT VERIFIED**。服务器完整冷备约 137.5 MB；另生成约 36.9 MB 的可恢复副本，仅省略 QQ 自带全局 Emoji 资源缓存，保留全部其它 QQ 数据、配置、插件、Compose、运行 env 与 machine-id。网关的 168 项私有表情位于原持久卷，仍完整保留。两份服务器冷备已记录 SHA256；可恢复副本已取回 `codex/环境变量备份/2026-10-24-NapCat服务器/`，下载字节数／SHA256、压缩包可读性、登录 DB／machine-info 存在及 machine-id 一致性均通过。该目录共九份配置／状态／恢复文件，另有逐项校验清单，包含迁移后真实对话账本；原八份迁移前文件及其清单另行保留。

用户自行向小号发起私聊，并确认“能正常回复”。聚合核验：history=2（user／assistant各1）、inbox=1、outbox=1且confirmed，两条原文均 exported=1。仅在服务器进程内比对实际 SQLite 和 Supabase：对应 `qqbot_chat_exports` 两份回执、`chat_messages` 两行均存在，正文／角色／助手／会话一致，远端 ID 顺序与本地一致；未向日志输出正文、账号或导出键。另制作包含这轮真实对话的迁移后 SQLite 在线备份，本机 SHA256／完整性／命名空间均通过。表情实际发送本轮未单独复测；没有通过运维工具发送测试 QQ 消息。

脱敏证据：[目录](../../临时验证/NapCat服务器迁移-26.10.24.08.46/)，含迁移前状态、部署匹配及最新登录／网关状态；不含账号、密钥或二维码 URL。

489 个已跟踪非 Markdown 文件逐一 SHA256 对比无改动，保留原有未提交业务修改；本轮仅部署配置／私人辅助文件及相关说明更新。三个私人运维／备份路径均由 Git 忽略。本机访问正式管理页 HTTP 200，未带 Token 的 `/status` 返回预期 401；鉴权后的 QQ 设置 API 为 200。网页插图及登录后数据加载的性能问题不因这次部署核验而视为解决。

本轮不提交／推送 Git。既有未提交业务改动保持原样。
