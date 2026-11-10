"""架构约束：核心层不得访问 metadata 口袋。

metadata 在 models.py 中只作为第三方插件的扩展口袋保留。核心代码
（core/ channels/ scheduler/）一旦读写其中的字符串 key，契约就退回成
"注释约定"——拼错 key 或改名字段都不会有任何报错。本测试用 AST 静态
扫描，白名单为空：需要结构化信息时，先把它提升为类型化字段。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "zero"
CORE_DIRS = ("core", "channels", "scheduler")
WHITELIST: set[str] = set()


def _offenses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ROOT).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "metadata":
            found.append(f"{rel}:{node.lineno}: .metadata")
        elif isinstance(node, ast.keyword) and node.arg == "metadata":
            found.append(f"{rel}:{node.lineno}: metadata=<kwarg>")
    return found


def test_core_does_not_access_metadata():
    offenders: list[str] = []
    for name in CORE_DIRS:
        for path in sorted((ROOT / name).rglob("*.py")):
            if path.relative_to(ROOT).as_posix() in WHITELIST:
                continue
            offenders.extend(_offenses(path))
    assert not offenders, (
        "核心层禁止访问 metadata 口袋，请提升为类型化字段：\n"
        + "\n".join(offenders))


def test_data_paths_module_has_no_process_global():
    """路径模块只做推导：进程根必须显式构造并注入，不得重新引入全局根。"""
    import zero.data_paths as data_paths

    assert not hasattr(data_paths, "current"), (
        "不允许恢复进程级 current()：需要路径的组件应接收显式 DataPaths")
    assert not hasattr(data_paths, "configure"), (
        "不允许恢复隐式 configure()：组合根直接构造 DataPaths")
