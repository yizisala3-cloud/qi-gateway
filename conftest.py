"""pytest 全局配置：``--db`` 统一入口与测试分类 marker 自动标注。

只做两件事，不改变任何测试的行为与内容：

1. ``pytest --db`` 在启动阶段完成真实 PostgreSQL（pgserver）测试栈的
   全部前置依赖检查（pgserver、psycopg、Asia/Shanghai 时区数据），
   缺什么直接报错并给出解决方式，不允许进入测试后才因环境不足跳过；
   检查通过后把全部门控环境变量设为 1（等价于手动导出，已显式设置
   的变量优先）。
2. 按 pytest.ini 注册的 ``database`` / ``frontend`` marker 自动标注
   收集到的测试，供 ``pytest -m database`` / ``pytest -m frontend`` 筛选。
"""

import os
from pathlib import Path

import pytest

# 真实 PostgreSQL（pgserver）测试套件的门控环境变量。各套件历史上各自
# 独立命名；--db 统一开启，保持单个变量仍可单独控制对应套件。
PG_GATE_VARS = (
    "QIGATEWAY_PG_MIGRATION_TEST",
    "QIGATEWAY_PG_PLANNING_TEST",
    "QIGATEWAY_PG_MEMO_TEST",
    "QIGATEWAY_ADMIN_MEMORY_PG_TEST",
)

# 文件名含 pgserver 的测试文件在真实 PostgreSQL 上执行（既有命名惯例）。
DATABASE_MARKER_FILE_KEYWORD = "pgserver"

# 纯前端测试文件：所有测试类都加载并真实执行/解析前端 JavaScript
# （Node 或 quickjs）。没有统一命名惯例，按文件名维护；新增此类测试时
# 把文件名加进来。
FRONTEND_MARKER_FILES = frozenset({
    "test_absorb_display_js.py",
    "test_admin_memory_form_logic.py",
    "test_memo_bugfix_20261003.py",
    "test_memo_bugfix_20261004.py",
    "test_memo_editor_behavior.py",
    "test_memo_frontend_contract.py",
    "test_planning_adjustments_logic.py",
    "test_planning_frontend_modules.py",
})

# 混合职责文件：只有列出的测试类属于 frontend，文件里的其余测试类是
# 后端/领域/工具测试。必须按类标记——整文件标记会让
# ``pytest -m "not frontend"`` 错误跳过后端测试。
FRONTEND_MARKER_CLASSES = {
    "test_planning_delete_facts_interval.py": frozenset({
        "FrontendDurationDisplayTests",
    }),
    "test_planning_frontend_contract.py": frozenset({
        "PlanningPageContractTests",
        "PlanningNavigationContractTests",
        "PlanningAudioAssetTests",
    }),
}


def pytest_addoption(parser):
    parser.addoption(
        "--db",
        action="store_true",
        default=False,
        help="启用真实 PostgreSQL 测试（pgserver 一次性实例，绝不连接生产库；"
             "等价于设置全部 QIGATEWAY_* 门控环境变量，已设置的变量优先）",
    )


def _pg_timezone_ready():
    """pgserver 安装目录里是否已有 Asia/Shanghai 时区文件。

    迁移函数体使用 ``at time zone 'Asia/Shanghai'``，而 pgserver 自带的
    PostgreSQL 不含 IANA 时区文件。测试的 _ensure_pg_timezone_data 会从
    Python tzdata 复制一次并持久化在 pgserver 安装目录，之后不再需要
    tzdata——所以这里与运行时条件保持一致：文件已存在即就绪。
    """
    try:
        import pgserver  # noqa: F401
    except ImportError:
        return False
    tzfile = (Path(pgserver.__file__).parent / "pginstall" / "share"
              / "postgresql" / "timezone" / "Asia" / "Shanghai")
    return tzfile.exists()


def _db_dependency_problems():
    """--db 启动阶段前置依赖检查，返回 (缺少什么, 为什么需要, 如何解决)。"""
    problems = []
    try:
        import pgserver  # noqa: F401
    except ImportError:
        problems.append((
            "pgserver（Python 包）",
            "真库测试用它启动一次性 PostgreSQL + pgvector 实例",
            "pip install -r requirements-test-pg.txt",
        ))
    try:
        import psycopg  # noqa: F401
    except ImportError:
        problems.append((
            "psycopg（Python 包）",
            "真库测试的 PostgreSQL 连接驱动",
            "pip install -r requirements-test-pg.txt",
        ))
    if not _pg_timezone_ready():
        try:
            import tzdata  # noqa: F401
        except ImportError:
            problems.append((
                "tzdata（Python 包，或 pgserver 安装目录里已持久化的时区文件）",
                "pgserver 的 PostgreSQL 不带 IANA 时区文件，而迁移函数使用 "
                "Asia/Shanghai；测试启动时需要从 tzdata 复制时区数据",
                "pip install -r requirements-test-pg.txt（内含 tzdata）",
            ))
    return problems


def pytest_configure(config):
    if not config.getoption("--db"):
        return
    problems = _db_dependency_problems()
    if problems:
        lines = ["--db 前置依赖检查失败："]
        for what, why, how in problems:
            lines.append(f"  缺少什么：{what}")
            lines.append(f"  为什么需要：{why}")
            lines.append(f"  如何解决：{how}")
        raise pytest.UsageError("\n".join(lines))
    for var in PG_GATE_VARS:
        os.environ.setdefault(var, "1")


def pytest_collection_modifyitems(items):
    for item in items:
        filename = item.path.name
        if DATABASE_MARKER_FILE_KEYWORD in filename:
            item.add_marker(pytest.mark.database)
            continue
        if filename in FRONTEND_MARKER_FILES:
            item.add_marker(pytest.mark.frontend)
            continue
        classes = FRONTEND_MARKER_CLASSES.get(filename)
        if classes and getattr(item, "cls", None) is not None \
                and item.cls.__name__ in classes:
            item.add_marker(pytest.mark.frontend)
