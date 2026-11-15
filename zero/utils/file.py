import os
import tempfile
from pathlib import Path


def require_absolute_path(raw: str) -> Path:
    """把工具参数里的路径规范成绝对路径；相对路径直接拒绝。

    只做 ``expanduser`` + 绝对性校验，不做「相对谁」的拼接：会话工作目录
    不参与路径解析，权限守卫与工具因此看到的是同一条路径。``~`` 展开后
    是绝对路径，允许使用。
    """
    value = os.path.expanduser(str(raw or "").strip())
    if not value:
        raise ValueError("路径不能为空")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"只支持绝对路径（可用 ~）：{raw!r}")
    return path


def save_or_create_file(file_path: Path, content: str | bytes = '') -> None:
    """原子写入：先写同目录临时文件，再 os.replace 覆盖目标。

    直接 open('w') 会在写入中途崩溃时留下半截文件；配置读失败会直接
    让程序起不来，所以这里走临时文件 + 原子替换。
    """
    encoding = 'utf-8' if isinstance(content, str) else None

    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=file_path.parent, prefix=f'.{file_path.name}-', suffix='.tmp')
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'w' if encoding else 'wb', encoding=encoding) as f:
            f.write(content)
        tmp.replace(file_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


