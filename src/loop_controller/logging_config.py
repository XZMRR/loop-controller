"""结构化日志配置（v0.18.0）。

每个 HTTP 请求分配 trace_id，通过 ContextVar 在异步上下文中传递。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str | None) -> None:
    """设置当前请求的 trace_id。"""
    trace_id_var.set(trace_id)


def get_trace_id() -> str | None:
    """获取当前请求的 trace_id。"""
    return trace_id_var.get()


class JsonFormatter(logging.Formatter):
    """JSON 格式日志，包含 trace_id。"""

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id()
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if trace_id:
            payload["trace_id"] = trace_id
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            payload.update(record.extra)
        return json.dumps(payload, ensure_ascii=False)


class ColoredFormatter(logging.Formatter):
    """终端彩色文本格式日志。"""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        trace_id = get_trace_id()
        color = self.COLORS.get(record.levelname, "")
        trace_part = f" [{trace_id}]" if trace_id else ""
        return (
            f"{color}[{record.levelname}]{self.RESET} "
            f"{datetime.now(UTC).isoformat()}{trace_part} "
            f"{record.getMessage()}"
        )


def configure_logging(*, json_format: bool = False, level: int = logging.INFO) -> None:
    """配置项目日志。"""
    handler = logging.StreamHandler(sys.stdout)
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(ColoredFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
