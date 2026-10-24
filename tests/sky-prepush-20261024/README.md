# 光遇推送前检查 2026-10-24

本目录记录实际暂存候选的验证，不读取游戏。

- candidate-results.xml：471 passed / 21 subtests passed，含最新4项独立权威状态探针。
- original-results.xml、rereview-results.xml：原两轮独立7+4通过。
- candidate-source-verification.json：13份业务源码逐文本匹配07:32复审。
- verification.json：综合结果；PostgreSQL13项在本轮工作区一次性隔离库重跑，迁移与候选逐文本一致，非生产游戏测试。
- production-migrations.json：迁移前后对象、权限、指纹和实际执行返回。
- live-interface.json：PostgREST RPC缓存识别及/status现用协议，验证请求均为无效参数，不新建正式对话。
- preserved-work.json：未选入的既有跟踪文件指纹；selected-files.json是初始范围，最终文件以git show --stat为准。

候选通过git checkout-index --all --prefix导出。解释器使用工作区.venv/Scripts/python.exe，入口tests/sky-review-20261024/run_isolated.py，清除外部凭证及dotenv；相关完整回归包含test_sky*、最新独立repro_new.py和authority_probes.py，以及QQ配置、聊天持久化、上下文、路由、日志的邻接回归。两份同名repro_cases.py分别独立执行，避免模块冲突。PostgreSQL命令：run_isolated.py tests/test_sky_pause_pgserver.py tests/test_sky_delivery_pgserver.py --db -q --tb=short。

未启动MuMu、未发送游戏消息、未启用主动消息；服务部署与实机验收独立记录。历史报告不改写。
