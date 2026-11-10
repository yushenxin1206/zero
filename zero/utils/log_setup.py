"""日志脚手架：文件/控制台分离、按大小轮转、密钥脱敏、第三方噪音压制。

文件收 DEBUG（排障用，2MB × 保留 3 份），控制台只出 ERROR 及以上——CLI 是
rich 全屏渲染，WARNING 级别的行会打断面板，所以不往终端刷。文件格式与
改造前逐字符一致，历史 grep 习惯不变。
"""
import logging
import re
import sys

from logging.handlers import RotatingFileHandler
from pathlib import Path

# 文件行格式：与 basicConfig 时代完全一致（时间/级别/logger 名/内容）
FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
CONSOLE_FORMAT = "%(levelname)s %(name)s: %(message)s"

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
_NOISY_LOGGERS = (
    "Lark", "lark_oapi", "websocket", "websockets",
    "urllib3", "urllib3.connectionpool", "requests", "asyncio", "pywebview",
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


def setup_logging(
        root: Path,
        *,
        console_level: int = logging.ERROR,
        max_bytes: int = MAX_BYTES,
        backup_count: int = BACKUP_COUNT,
) -> None:
    """装配 root logger：文件 DEBUG + 控制台 ERROR，重复调用不叠 handler。"""
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)

    file_handler = RotatingFileHandler(
        root / "debug.log", maxBytes=max_bytes,
        backupCount=backup_count, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))

    for handler in (file_handler, console_handler):
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
