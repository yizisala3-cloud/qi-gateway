# 旧请求取消隔离与视觉原子提交

2026-10-24，America/Los_Angeles。07:12复现、07:13修改桥接、07:16完成相关测试。无生产请求、模拟器操作或真实游戏发送。

- before.xml：07:07原独立3项，修复前3失败。
- after.xml：最终467项、21子测试通过，JUnit记录488个结果，包含本轮原独立3项及新增10项，前端12交互场景包含在原前端测试内。
- original.xml：00:10原7项通过。
- rereview.xml：02:37原4项通过。
- source-sha256.json：最终业务／测试及独立原文件指纹。
- unchanged-business.json：对07:07复审的13份业务文件比对，本轮只有bridge.py变化，其他网关／SQL／前端／OCR布局实现保持一致。

全部测试通过`.venv\Scripts\python.exe -X utf8 tests/sky-review-20261024/run_isolated.py`执行，禁用dotenv并移除外部服务凭证。

完整回归文件参数：

```text
tests/test_sky_companion.py tests/test_sky_companion_duplicates.py
tests/test_sky_companion_wrapped.py tests/test_sky_companion_vision.py
tests/test_sky_companion_five_fixes.py tests/test_sky_settings.py
tests/test_sky_delivery.py tests/test_sky_companion_review_regressions.py
tests/test_sky_pause_epoch.py tests/test_sky_cancellation_order.py
tests/sky-final-rereview-20261024/repro_new.py
tests/test_sky_config_frontend.py tests/test_qqbot_config_frontend.py
tests/test_qqbot_settings.py tests/test_chat_message_persistence.py
tests/test_request_context.py tests/test_model_routing.py
tests/test_gateway_log_chat_gaps.py -q --tb=short
```

两轮旧独立复现分别使用`tests/sky-review-20261024/repro_cases.py`及`tests/sky-rereview-20261024/repro_cases.py`，避免同名模块冲突。三份原独立测试及历史审查报告未修改。

数据库层无改动，07:07复审13项隔离PostgreSQL结果未覆盖；本轮未重跑。没有新一轮浏览器展示。网络错误和输入／回显／交付均由本地测试替身完成，真实双线程测试验证状态同步，不代表实机出现频率。

**FIXED, PARTIALLY VERIFIED**：本地关键场景通过，原有生产迁移与发布仍未执行；实机按用户要求暂不测试。
