# 视觉PASS输出约定-26.10.23.23.58

修改时间：2026-10-23 23:58:14，America/Los_Angeles。范围：`tools/sky_companion/bridge.py::Gateway.generate`、`tests/test_sky_companion_vision.py`。这是关机前已保存、先前尚未同步文档的代码阶段，不是本轮新增实机测试。

普通文字轮直接输出PASS；看图轮只能输出scene/reply对象，静默放入reply字段。两种协议在提示词入口分别定义，去掉“直接输出PASS”和“只输出JSON”同时存在的冲突。无效输出的安全诊断补充blank和JSON错误位置，不保存思考原文或图片数据。

真实预览原“哦”场景并非每次失败；修改后也曾出现14个空格的API输出，因此不能把提示词冲突当成已经证明的唯一原因，或声称已修复上游偶发空白。程序继续拒绝无效观察、按用户规则退回文字并保留视觉退避。该阶段相关359项／15子测试通过。私有原始预览证据为 `vision-pass-before.json`、`vision-pass-after.json`；23:56循环已停止，没有为该比较再次发送游戏文字。

用户随后反馈视觉“测试没问题”。现有规则保留，BUG-14／15的专项边界未据此全部清理。本阶段状态：**FIXED, PARTIALLY VERIFIED**；程序输出约定已修正，上游输出稳定性和其余视觉场景仍需专项核对。
