"""重新生成 docs/prompt.md（提示词文档快照，由代码生成，请勿手改）。

放在 core 之外：生成文档需要真实权限状态来结算本机沙箱能力，而核心层
不允许依赖权限模块。运行：``uv run python scripts/gen_prompt.py``。
"""
from pathlib import Path

from zero.core.context import Context
from zero.data_paths import DataPaths
from zero.permission.envelope import PermissionState
from zero.session.store import SessionStore

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    data_paths = DataPaths.default()
    store = SessionStore(data_paths.sessions)
    context = Context(
        session_id="doc", channel="cli",
        session_store=store, data_paths=data_paths,
        working_dir=ROOT,
        sandbox_available=PermissionState(data_paths, store).sandbox_available())
    out = ROOT / "docs" / "prompt.md"
    out.write_text(
        "<!-- 本文件由 `python scripts/gen_prompt.py` 生成，请勿手改 -->\n\n"
        + context.system_prompt + "\n",
        encoding="utf-8",
    )
    print(f"written: {out}")


if __name__ == "__main__":
    main()
