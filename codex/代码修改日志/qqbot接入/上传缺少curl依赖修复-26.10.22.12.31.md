# Zeabur文件上传缺少curl依赖修复

2026-10-22 12:31（America/Los_Angeles）。延续用户已授权的QQ上线和猫咪表情接入。用户在正确的`/data/qqbot`区域拖入ZIP后，截图明确报错`wget: not found`、`curl: not found`，随后gzip/tar因无下载内容失败；不是拖错目录，也没有证据表明ZIP损坏。

## 根因与改动

生产镜像来自`python:3.12-slim`，项目Dockerfile此前只额外安装git，未提供Zeabur容器文件上传所需的下载程序。检查完整Dockerfile及上传错误链后，最小修复为在原apt层追加**curl、ca-certificates**；保留git、原启动命令、构建上下文私密排除与其他配置。平台可使用curl下载，不需同时安装wget。

仅Dockerfile进入提交；私人表情资源、账本、密钥、codex文档库、6个原有无内容差异文件状态和`tools/preview_planning.cjs`无关预览修改均原位保留未提交。

## 发布与前置条件

- 基线及发布前实际远端：`a338c0c6c4afadd38c52153aba40e384806654a9`。
- 正常提交、推送并复核实际远端：`592166738e6d2b52b76338bbc75682467edfa240`，提交内容仅Dockerfile（2行新增、1行删除）。
- `git diff --check`通过，Git索引发布前／后为空。未强推、未修改账号权限或续费套餐。
- 本次没有新增数据库引用、DDL或RPC，不需新SQL；原QQ记忆前置`20261021010000`已于10月21日映射为远端`20261021202243_qqbot_chat_export`执行核验，来源与依赖未改变。本次未回放迁移。
- 本机无Docker，未声称本地镜像构建通过。通过Zeabur自动构建实际镜像并部署；平台显示本修复版本运行中。

## 实际验证

1. 服务终端实测`curl --version`成功，版本8.14.1，输出协议支持HTTPS／SSL。
2. 真实新容器只读检查确认`/usr/bin/curl`、`/usr/bin/tar`、`/usr/bin/gzip`均存在。
3. 通过容器内鉴权请求实际QQ状态接口：enabled／leader／connected均true，proactive_enabled=false，confirmed=2、stickers=0；没有输出Token、消息正文或篡改账本。
4. `/data/qqbot`仍有原settings.json、SQLite主文件及WAL／SHM；源设置为开启私聊／关闭主动的持久文件继续生效。新镜像未造成消息账本丢失。
5. 原目录尚无cloud-cat-v1.zip或表情库，本次没有假报上传／加载成功。用户需在修复后的容器重新拖入同一个整理包，随后继续解压与目录配置。

证据：`tests/qqbot-stickers-20261022/upload-dependency-fix.json`、`curl-runtime-check.png`；原错误为用户本次提供截图。业务Python／前端代码未改，复用已验证的3087项候选回归；本次必要验证为真实Docker构建与容器工具／QQ重连，没有增写镜像字符串式单元测试。

平台终端使用Canvas，不能依赖DOM文本获取输出；长串逐字键入中断后已取消未完成命令，改用可见终端剪贴板粘贴并执行检查，实际结果以截图为据。

## 当前结论

**FIXED, PARTIALLY VERIFIED**：缺少下载程序的根因已修复并部署，工具和QQ运行已实测；原上传流程仍待一次用户重传，随后还需资源包哈希、168项云端加载与QQ动图验收。主动消息继续关闭。仅代码推送／构建及上述运行验证通过，不将它们替代表情业务验收。
