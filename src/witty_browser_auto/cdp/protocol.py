"""CDP 消息的最小类型定义。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CdpEvent:
    method: str
    params: dict[str, Any]
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrowserVersion:
    protocol_version: str
    product: str
    revision: str
    user_agent: str
    js_version: str

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> BrowserVersion:
        return cls(
            protocol_version=str(result.get("protocolVersion", "")),
            product=str(result.get("product", "")),
            revision=str(result.get("revision", "")),
            user_agent=str(result.get("userAgent", "")),
            js_version=str(result.get("jsVersion", "")),
        )
