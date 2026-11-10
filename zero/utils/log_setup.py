"""日志脚手架：文件/控制台分离、按大小轮转、密钥脱敏、第三方噪音压制。

文件收 DEBUG（排障用，2MB × 保留 3 份），控制台只出 ERROR 及以上——CLI 是
rich 全屏渲染，WARNING 级别的行会打断面板，所以不往终端刷。两条格式都带
源码位置（logger 名 + 行号）——见 ``FILE_FORMAT`` 的注释。
"""
import logging
import re
import sys

from logging.handlers import RotatingFileHandler
from pathlib import Path

from zero.utils.log_context import current_session_id, current_turn_id

# 文件行格式：时间/级别/logger 名:源码行/轮次上下文/内容。
#
# 位置写成 ``%(name)s:%(lineno)d`` 而不是 ``%(filename)s:%(lineno)d``：
# 本仓 logger 全部用 ``__name__`` 命名，name 本身就是模块路径
# （``zero.core.loop``），已经唯一定位到文件；单加 filename 反而会在
# session/store.py 与 scheduler/store.py 之间产生同名歧义，pathname 又太长。
# 行号放在 name 之后，既有 grep 锚点（``[ERROR] zero.core.loop:``、
# ``turn status=``）仍原样命中。
#
# ``%(context)s`` 由 TurnContextFilter 注入：轮内是
# `` [turn=<id> session=<id>]``，轮外为空串，因此不改动既有行的可读性。
FILE_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d:%(context)s %(message)s")
# 控制台与文件刻意不同：不带时间戳（终端已有上下文），但同样带位置与轮次
# 前缀——控制台只出 ERROR，正是用户截图/粘贴给我们的那些行，位置直接可用。
CONSOLE_FORMAT = "%(levelname)s %(name)s:%(lineno)d:%(context)s %(message)s"

MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3

# 黑名单：本仓可能出现的三种密钥形态。保留前缀，日志里仍看得出
# 「这里本该有密钥、已被抹掉」；值统一换成 ***，不影响定位。
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+"), r"\1***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(
        r"""(?i)("?(?:api_?key|token)"?\s*[:=]\s*["']?)[^"'\s,}]+"""), r"\1***"),
)

# 自带 handler 或 DEBUG 级会刷屏的第三方 logger：统一汇入 root 并压到 WARNING。
# 按前缀生效：给父 logger 设级别，未单独设级别的子 logger 会继承，
# 例如 ``markdown_it.rules_block.*`` 全部收敛。
_NOISY_LOGGERS = (
    "Lark", "lark_oapi", "websocket", "websockets",
    "urllib3", "urllib3.connectionpool", "requests", "asyncio",
    "markdown_it", "pywebview",
)


class SecretFilter(logging.Filter):
    """把格式化后的正文里的密钥替换掉。

    作用在 handler 上：record 先渲染成完整字符串再正则替换，因此
    ``logger.info("... %s", key)`` 的参数拼接路径同样覆盖。traceback 由
    Formatter 在过滤之后生成，过滤器管不到——靠「异常里不拼请求体」的
    编码约定兜底（provider 永不打印 headers/payload）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern, replacement in _SECRET_PATTERNS:
            message = pattern.sub(replacement, message)
        record.msg = message
        record.args = ()
        return True


class TurnContextFilter(logging.Filter):
    """把当前轮次的 turn_id / session_id 写进每条 LogRecord。

    值来自 contextvars，轮内所有日志自动携带；轮外为空，前缀也随之为空。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        turn_id = current_turn_id()
        session_id = current_session_id()
        record.turn_id = turn_id
        record.session_id = session_id
        record.context = (
            f" [turn={turn_id} session={session_id}]" if turn_id else "")
        return True


def setup_logging(
        log_dir: Path,
        *,
        console_level: int = logging.ERROR,
        max_bytes: int = MAX_BYTES,
        backup_count: int = BACKUP_COUNT,
) -> None:
    """装配 root logger：文件 DEBUG + 控制台 ERROR，重复调用不叠 handler。

    ``log_dir`` 由调用方从 ``DataPaths.logs`` 取得，本函数只负责在缺失时
    创建目录并写入 ``debug.log``（轮转备份由 handler 放在同目录）。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    file_handler = RotatingFileHandler(
        log_dir / "debug.log", maxBytes=max_bytes,
        backupCount=backup_count, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))

    for handler in (file_handler, console_handler):
        handler.addFilter(TurnContextFilter())
        handler.addFilter(SecretFilter())
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    # 第三方自带 stdout handler 会绕过文件配置直接污染终端：一律清掉，
    # 改为经 root 输出，并把 DEBUG 期的连接/任务噪音压到 WARNING。
    for name in _NOISY_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.setLevel(logging.WARNING)
        lib_logger.propagate = True
