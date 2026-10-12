"""日志初始化：控制台 + 滚动文件，并捕获未处理异常与 print 输出。"""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _force_utf8(stream) -> None:
    """尽量把控制台流切到 UTF-8，避免 GBK 控制台因 superscript/中文而崩溃。"""
    if stream is None:
        return
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _StreamTee:
    """把写入 stdout/stderr 的文本转发到日志。不直接写回控制台，避免重复输出。"""

    def __init__(self, logger: logging.Logger, level: int, fallback):
        self.logger = logger
        self.level = level
        self.fallback = fallback
        self._buf = ""

    def write(self, text: str) -> None:
        if not text:
            return
        if self.fallback is not None:
            try:
                self.fallback.write(text)
                self.fallback.flush()
            except Exception:
                pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._safe_log(line.rstrip())

    def flush(self) -> None:
        if self._buf:
            if self.fallback is not None:
                try:
                    self.fallback.write(self._buf)
                except Exception:
                    pass
            if self._buf.strip():
                self._safe_log(self._buf.rstrip())
            self._buf = ""

    def _safe_log(self, message: str) -> None:
        try:
            self.logger.log(self.level, message)
        except Exception:
            pass

    def isatty(self) -> bool:
        return False


def setup_logging(log_dir: Optional[Path] = None, level: int = logging.INFO) -> logging.Logger:
    """初始化根日志器，返回名为 hapi_app 的 logger。"""
    log_dir = Path(log_dir) if log_dir else Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        _force_utf8(stream)

    logger = logging.getLogger("hapi_app")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # 控制台 handler：打包成无控制台 GUI 后 stdout 可能为 None
    console_stream = sys.__stdout__
    if console_stream is not None:
        console = logging.StreamHandler(console_stream)
        console.setFormatter(formatter)
        logger.addHandler(console)

    log_file = log_dir / time.strftime("run_%Y%m%d_%H%M%S.log")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "未捕获异常", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _excepthook
    return logger


def capture_print_output(logger: logging.Logger) -> None:
    """把后续所有 print/标准输出转发到日志，适用于 GUI 与 HAPI 的 banner 输出。

    不把内容直接写回控制台：控制台 handler 会负责输出，避免一行出现两次。
    """
    sys.stdout = _StreamTee(logger, logging.INFO, None)
    sys.stderr = _StreamTee(logger, logging.WARNING, None)
