# 快速开关取消与姓名碎片修复证据

源码修改：2026-10-24 02:58–03:11，America/Los_Angeles。测试完成03:18；中断后06:53回读核对结果及源码时间，继续归档。所有请求均为测试替身或本机一次性数据库；未连接生产、启动MuMu或发送游戏消息。

- before.xml：02:37复审的四项原用例，修复前4失败。
- after.xml：最终相关454项、21子测试通过（JUnit共475个结果）；包含桥接、视觉、开关、交付及相邻聊天／上下文／QQ配置；配置页12个真实JavaScript交互场景包含在前端测试内。
- rereview.xml：未修改的02:37原复审用例，4通过。
- original.xml：未修改的00:10原独立七断言，7通过。
- postgres.xml：一次性真实PostgreSQL共13通过（暂停代次5项、旧交付8项），无跳过。含旧布尔升级、重复迁移保留代次、进程缓存重建、等待提交的并发开启、并发关闭、回滚、角色权限以及既有交付幂等。
- source-sha256.json：本轮源代码、迁移、相关测试及两份原复现文件的最终指纹。

执行入口统一为`.venv\Scripts\python.exe -X utf8 tests/sky-review-20261024/run_isolated.py`，它禁用dotenv并移除服务凭证环境变量。原两份repro_cases.py同名，分别运行，避免pytest模块名冲突；未改原审查报告或原用例。

最终相关文件参数：

```text
tests/test_sky_companion.py tests/test_sky_companion_duplicates.py
tests/test_sky_companion_wrapped.py tests/test_sky_companion_vision.py
tests/test_sky_companion_five_fixes.py tests/test_sky_settings.py
tests/test_sky_delivery.py tests/test_sky_companion_review_regressions.py
tests/test_sky_pause_epoch.py tests/test_sky_config_frontend.py
tests/test_qqbot_config_frontend.py tests/test_qqbot_settings.py
tests/test_chat_message_persistence.py tests/test_request_context.py
tests/test_model_routing.py tests/test_gateway_log_chat_gaps.py
-q --tb=short
```

独立复现分别使用`tests/sky-rereview-20261024/repro_cases.py`和`tests/sky-review-20261024/repro_cases.py`。数据库使用`tests/test_sky_pause_pgserver.py tests/test_sky_delivery_pgserver.py --db -q --tb=short`；pgserver创建独立临时实例并停止清理，不连接Supabase。

当前状态：**FIXED, PARTIALLY VERIFIED**。本地关键场景已覆盖；新迁移／配套服务未发布，真实OCR拆框、游戏发送以及线上快速关开待后续验收。此目录不包含真实聊天、Token或游戏截图。
