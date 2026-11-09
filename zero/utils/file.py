import os
import tempfile
from pathlib import Path


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


