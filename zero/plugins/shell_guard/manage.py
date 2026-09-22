"""
zero-guard：shell_guard 规则管理命令。

用法：
    zero-guard list                      列出全部规则（用户 + 内置）
    zero-guard add allow "git fetch*"    添加允许规则（支持 glob）
    zero-guard add deny "rm -rf *"       添加禁止规则
    zero-guard remove allow 0            按序号删除用户规则

用户规则存于 ~/.zero/shell_guard.json；内置规则在 policy.py，不可删改。
"""
import sys

from zero.plugins.shell_guard import policy, store

USAGE = __doc__


def _list() -> None:
    user = store.load()
    print("用户规则（~/.zero/shell_guard.json）:")
    for kind in ("allow", "deny"):
        print(f"  {kind}:")
        for i, r in enumerate(user[kind]):
            print(f"    [{i}] {r}")
        if not user[kind]:
            print("    (空)")
    print("内置规则（policy.py，不可删改）:")
    print(f"  allow: {len(policy.DEFAULT_ALLOW)} 条（只读命令，如 ls / git status*）")
    print(f"  deny:  {len(policy.DEFAULT_DENY)} 条（灾难性命令，如 rm -rf /*）")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return

    cmd, rest = args[0], args[1:]

    if cmd == "list":
        _list()
        return

    if cmd == "add" and len(rest) == 2 and rest[0] in ("allow", "deny"):
        if store.add(rest[0], rest[1]):
            print(f"已添加 {rest[0]} 规则: {rest[1]}")
        else:
            print("规则已存在")
        return

    if cmd == "remove" and len(rest) == 2 and rest[0] in ("allow", "deny"):
        try:
            index = int(rest[1])
        except ValueError:
            print("序号必须是整数")
            raise SystemExit(2)
        removed = store.remove(rest[0], index)
        if removed is None:
            print("序号越界")
            raise SystemExit(2)
        print(f"已删除: {removed}")
        return

    print(USAGE)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
