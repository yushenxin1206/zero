#!/usr/bin/env python
"""plan skill 的内置脚本——会话 plan 状态的唯一写入口。

由 agent 经 shell 调用（包内脚本，shell_guard 免审批）。数据布局在
--dir 指定的**会话数据目录**下：

    plan.json   源真相：PlanState 序列化（camelCase）
    plan.md     人类可读视图，由本脚本渲染，不接受手工编辑

每个子命令都会：校验枚举 → 刷新 updated_at → 原子写 plan.json → 重渲染
plan.md。stdout 输出一行紧凑状态供 agent 转述。

用法（--dir 一律用绝对路径）：
    python plan.py init     --dir D --task T [--goal G] [--findings F] [--force]
    python plan.py set      --dir D [--task T] [--goal G] [--findings F]
                                     [--status S] [--note N]
    python plan.py add-step --dir D --text TXT
    python plan.py step     --dir D --index N [--status S] [--text TXT] [--note N]
    python plan.py log      --dir D --note N
    python plan.py show     --dir D
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# 开发态未安装 zero 包时也能 import（安装态下该插入是空操作）
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))


def setup_stdio():
    """强制 UTF-8 输出，避免 Windows GBK 控制台下中文报错/乱码。

    脚本属包内可信资源，shell_guard 只放行「python + 包内绝对路径」的裸调用；
    一旦给它加 `set PYTHONIOENCODING` / `chcp` 前缀，首 token 就不再是解释器，
    豁免失效会触发人工审批。故编码问题必须在脚本内部解决，不靠命令前缀。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

from zero.models import PlanChange, PlanState, PlanStep  # noqa: E402

STATUSES = ("draft", "reviewing", "approved", "executing", "done", "aborted")
STEP_STATUSES = ("pending", "in_progress", "done", "blocked", "skipped")


def _json_path(d: Path) -> Path:
    return d / "plan.json"


def _md_path(d: Path) -> Path:
    return d / "plan.md"


def _fail(msg: str) -> None:
    print(f"plan.py 错误: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load(d: Path) -> PlanState:
    p = _json_path(d)
    if not p.exists():
        _fail(f"{p} 不存在，请先执行 init")
    try:
        return PlanState.model_validate_json(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _fail(f"{p} 解析失败: {e}")


def render_md(st: PlanState) -> str:
    lines = [f"# Plan: {st.task or '(未命名)'}", ""]
    lines.append(f"- status: {st.status}")
    lines.append(f"- updated: {st.updated_at:%Y-%m-%d %H:%M}")
    lines += ["", "## 目标", st.goal or "-"]
    lines += ["", "## 调研结论", st.findings or "-"]
    lines += ["", "## 步骤"]
    if st.steps:
        for s in st.steps:
            note = f" — {s.note}" if s.note else ""
            lines.append(f"{s.index}. [{s.status}] {s.text}{note}")
    else:
        lines.append("（无）")
    lines += ["", "## 变更记录"]
    if st.changes:
        for c in st.changes:
            lines.append(f"- {c.at:%m-%d %H:%M} {c.note}")
    else:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def summary(st: PlanState) -> str:
    counts: dict[str, int] = {}
    for s in st.steps:
        counts[s.status] = counts.get(s.status, 0) + 1
    parts = ", ".join(f"{k}={counts[k]}" for k in STEP_STATUSES if k in counts)
    cur = next((s for s in st.steps if s.status == "in_progress"), None)
    cur_txt = f" | 当前: {cur.index}. {cur.text}" if cur else ""
    return (f"plan[{st.status}] {st.task or '(未命名)'}{cur_txt} | "
            f"步骤 {len(st.steps)} ({parts or '空'})")


def save(d: Path, st: PlanState) -> None:
    """原子写 plan.json 并同步渲染 plan.md。"""
    d.mkdir(parents=True, exist_ok=True)
    st.updated_at = datetime.now()
    p = _json_path(d)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(st.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    tmp.replace(p)
    _md_path(d).write_text(render_md(st), encoding="utf-8")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_init(d: Path, a) -> None:
    if _json_path(d).exists() and not a.force:
        _fail(f"{_json_path(d)} 已存在；如需覆盖请加 --force "
              "（或先归档旧计划）")
    now = datetime.now()
    st = PlanState(task=a.task, goal=a.goal, findings=a.findings,
                   status="draft", updated_at=now)
    st.changes.append(PlanChange(at=now, note="创建计划（draft）"))
    save(d, st)
    print(summary(st))


def cmd_set(d: Path, a) -> None:
    st = load(d)
    changed: list[str] = []
    for field in ("task", "goal", "findings"):
        value = getattr(a, field)
        if value is not None:
            setattr(st, field, value)
            changed.append(field)
    if a.status is not None:
        st.status = a.status
        changed.append(f"status={a.status}")
    if not changed:
        _fail("set 未提供任何字段")
    st.changes.append(PlanChange(
        at=datetime.now(), note=a.note or "更新: " + ", ".join(changed)))
    save(d, st)
    print(summary(st))


def cmd_add_step(d: Path, a) -> None:
    st = load(d)
    idx = max((s.index for s in st.steps), default=0) + 1
    st.steps.append(PlanStep(index=idx, text=a.text))
    st.changes.append(PlanChange(
        at=datetime.now(), note=f"新增步骤 {idx}: {a.text}"))
    save(d, st)
    print(summary(st))


def cmd_step(d: Path, a) -> None:
    st = load(d)
    target = next((s for s in st.steps if s.index == a.index), None)
    if target is None:
        _fail(f"步骤 {a.index} 不存在")
    bits: list[str] = []
    if a.status is not None:
        target.status = a.status
        bits.append(f"status={a.status}")
    if a.text is not None:
        target.text = a.text
        bits.append("text")
    if a.note is not None:
        target.note = a.note
        bits.append("note")
    if not bits:
        _fail("step 未提供任何字段")
    note = f"步骤 {a.index}: " + ", ".join(bits)
    if a.status in ("blocked", "skipped") and a.note:
        note += f" — {a.note}"
    st.changes.append(PlanChange(at=datetime.now(), note=note))
    save(d, st)
    print(summary(st))


def cmd_log(d: Path, a) -> None:
    st = load(d)
    st.changes.append(PlanChange(at=datetime.now(), note=a.note))
    save(d, st)
    print(summary(st))


def cmd_show(d: Path, a) -> None:
    st = load(d)
    print(summary(st))
    print()
    sys.stdout.write(render_md(st))


def cmd_archive(d: Path, a) -> None:
    """把活跃计划归档为 plan-archive-<ts>.{json,md}，并移除活跃文件。"""
    st = load(d)
    base = f"plan-archive-{datetime.now():%Y%m%d-%H%M%S}"
    _md_path(d).write_text(render_md(st), encoding="utf-8")
    _json_path(d).replace(d / f"{base}.json")
    _md_path(d).replace(d / f"{base}.md")
    print(f"已归档 → {base}.json / {base}.md")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_dir(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--dir", required=True,
                    help="会话数据目录（绝对路径，取环境块 Session dir）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plan.py", description="会话 plan 状态维护（唯一写入口）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="创建计划（status=draft）")
    _add_dir(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--goal", default="")
    sp.add_argument("--findings", default="")
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("set", help="更新标量字段 / 整体状态")
    _add_dir(sp)
    sp.add_argument("--task")
    sp.add_argument("--goal")
    sp.add_argument("--findings")
    sp.add_argument("--status", choices=STATUSES)
    sp.add_argument("--note", default="", help="变更记录；缺省按字段自动生成")

    sp = sub.add_parser("add-step", help="追加一个步骤")
    _add_dir(sp)
    sp.add_argument("--text", required=True)

    sp = sub.add_parser("step", help="更新某步骤的状态/文本/备注")
    _add_dir(sp)
    sp.add_argument("--index", type=int, required=True)
    sp.add_argument("--status", choices=STEP_STATUSES)
    sp.add_argument("--text")
    sp.add_argument("--note")

    sp = sub.add_parser("log", help="追加一条变更记录")
    _add_dir(sp)
    sp.add_argument("--note", required=True)

    sp = sub.add_parser("show", help="打印当前计划视图")
    _add_dir(sp)

    sp = sub.add_parser("archive", help="归档活跃计划（plan-archive-<ts>.*）")
    _add_dir(sp)
    return p


_HANDLERS = {
    "init": cmd_init, "set": cmd_set, "add-step": cmd_add_step,
    "step": cmd_step, "log": cmd_log, "show": cmd_show,
    "archive": cmd_archive,
}


def main(argv: list[str] | None = None) -> None:
    setup_stdio()
    args = build_parser().parse_args(argv)
    _HANDLERS[args.cmd](Path(args.dir).expanduser(), args)


if __name__ == "__main__":
    main()