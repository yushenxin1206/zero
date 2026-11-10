"""日志脚手架：轮转、脱敏、handler 级别、幂等。

全部用临时目录，结束后恢复 root handler —— 不污染其他测试的 caplog。
"""
import inspect
import logging

import pytest

from logging.handlers import RotatingFileHandler

from zero.utils.log_setup import FILE_FORMAT, setup_logging


@pytest.fixture
def isolated_root():
    """接管 root logger：setup_logging 会清空 handler，这里先摘走 pytest 的
    捕获 handler，测试结束再原样装回。"""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    try:
        yield root
    finally:
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def flush(root: logging.Logger) -> None:
    for handler in root.handlers:
        handler.flush()


def test_rotates_by_size(tmp_path, isolated_root):
    setup_logging(tmp_path, max_bytes=200, backup_count=1)
    logger = logging.getLogger("zero.test.rotation")
    for _ in range(20):
        logger.info("%s", "x" * 60)
    assert (tmp_path / "debug.log").exists()
    assert (tmp_path / "debug.log.1").exists()
    assert (tmp_path / "debug.log").stat().st_size < 200 * 2


def test_redacts_secrets(tmp_path, isolated_root):
    setup_logging(tmp_path)
    logger = logging.getLogger("zero.test.secrets")
    key = "sk-abcdefgh12345678"

    # %s 参数拼接路径也必须覆盖（过滤器在 getMessage 之后做替换）
    logger.error("Authorization: Bearer %s", key)
    logger.error('provider config apiKey="%s" token: tok_abc123', key)
    logger.error("bare key leaked=%s", key)
    flush(isolated_root)

    text = (tmp_path / "debug.log").read_text(encoding="utf-8")
    assert key not in text
    assert "Bearer ***" in text
    assert 'apiKey="***"' in text
    assert "token: ***" in text


def test_handler_levels_and_format(tmp_path, isolated_root):
    setup_logging(tmp_path)
    handlers = isolated_root.handlers
    file_handler = next(h for h in handlers if isinstance(h, RotatingFileHandler))
    console_handler = next(
        h for h in handlers if type(h) is logging.StreamHandler)
    assert file_handler.level == logging.DEBUG
    assert file_handler.formatter._fmt == FILE_FORMAT
    assert console_handler.level == logging.ERROR
    assert isolated_root.level == logging.DEBUG


def test_console_only_outputs_errors(tmp_path, isolated_root, capsys):
    setup_logging(tmp_path)
    logger = logging.getLogger("zero.test.console")
    logger.warning("warning stays in file")
    error_line = inspect.currentframe().f_lineno + 1
    logger.error("error reaches terminal")
    captured = capsys.readouterr()
    assert "warning stays in file" not in captured.err
    assert f"zero.test.console:{error_line}: error reaches terminal" in captured.err


def test_log_includes_source_location(tmp_path, isolated_root):
    """文件行带 logger 名:行号，可直接定位到发出日志的代码。"""
    setup_logging(tmp_path)
    logger = logging.getLogger("zero.test.location")
    info_line = inspect.currentframe().f_lineno + 1
    logger.info("locate me")
    flush(isolated_root)

    text = (tmp_path / "debug.log").read_text(encoding="utf-8")
    assert f"zero.test.location:{info_line}: locate me" in text


def test_third_party_debug_noise_is_suppressed(tmp_path, isolated_root):
    """第三方库的 DEBUG 噪音不进文件；本项目 DEBUG 照常保留。"""
    setup_logging(tmp_path)
    logging.getLogger("markdown_it.rules_block.hr").debug("entering hr")
    logging.getLogger("zero.test.noise").debug("project detail")
    flush(isolated_root)

    text = (tmp_path / "debug.log").read_text(encoding="utf-8")
    assert "entering hr" not in text
    assert "project detail" in text


def test_setup_is_idempotent(tmp_path, isolated_root):
    setup_logging(tmp_path)
    setup_logging(tmp_path)
    assert len(isolated_root.handlers) == 2
