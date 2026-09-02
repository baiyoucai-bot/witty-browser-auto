"""中文结构化日志配置。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from witty_browser_auto.security.redaction import redact

_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class ChineseJsonFormatter(logging.Formatter):
    """输出稳定 JSON，并在序列化前统一脱敏。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "时间": datetime.now(UTC).isoformat(),
            "级别": record.levelname,
            "模块": record.name,
            "消息": record.getMessage(),
        }
        context = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_")
        }
        if context:
            payload["上下文"] = redact(context)
        if record.exc_info:
            payload["异常"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """配置根日志；`stream` 省略时写 stderr。

    以 stdio 传输运行 MCP 服务端时 stdout 是协议通道，日志必须留在 stderr，否则会破坏
    JSON-RPC 分帧。`StreamHandler` 的默认值本就是 stderr，这里把它显式化以免被改错。
    """

    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(ChineseJsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)
