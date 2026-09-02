"""网络结构观察与私有导出的领域结果。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SHAPE_KEYS = {"type", "keys", "length", "item_type", "item_keys", "record_path"}


def _safe_text(value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.strip() if ord(character) >= 32)[:maximum]


def _safe_endpoint(value: Any) -> str:
    text = _safe_text(value, maximum=2048)
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


def _safe_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _SHAPE_KEYS:
        item = value.get(key)
        if key in {"keys", "item_keys", "record_path"}:
            if isinstance(item, list):
                result[key] = [text for raw in item[:50] if (text := _safe_text(raw, maximum=100))]
        elif key == "length":
            if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000_000:
                result[key] = item
        else:
            text = _safe_text(item, maximum=40)
            if text:
                result[key] = text
    return result


def sanitize_network_inspection(
    value: Mapping[str, Any], *, max_candidates: int = 50
) -> dict[str, Any]:
    """只保留接口元数据和 JSON 结构，拒绝实现层夹带响应样例值。"""

    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("网络数据观察缺少候选数组")
    candidates: list[dict[str, Any]] = []
    for raw in raw_candidates[:max_candidates]:
        if not isinstance(raw, Mapping):
            continue
        candidate_id = _safe_text(raw.get("candidate_id"), maximum=80)
        endpoint = _safe_endpoint(raw.get("endpoint"))
        if not _CANDIDATE_ID.fullmatch(candidate_id) or not endpoint:
            continue
        status = raw.get("status")
        body_bytes = raw.get("body_bytes")
        score = raw.get("score")
        duration_ms = raw.get("duration_ms")
        candidate: dict[str, Any] = {
            "candidate_id": candidate_id,
            "endpoint": endpoint,
            "method": _safe_text(raw.get("method"), maximum=16),
            "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
            "mime_type": _safe_text(raw.get("mime_type"), maximum=100),
            "resource_type": _safe_text(raw.get("resource_type"), maximum=40),
            "body_bytes": (
                body_bytes
                if isinstance(body_bytes, int)
                and not isinstance(body_bytes, bool)
                and body_bytes >= 0
                else 0
            ),
            "score": (
                score
                if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100
                else 0
            ),
            "json_shape": _safe_shape(raw.get("json_shape")),
            "duration_ms": (
                duration_ms
                if isinstance(duration_ms, int)
                and not isinstance(duration_ms, bool)
                and 0 <= duration_ms <= 3_600_000
                else None
            ),
        }
        request_shape = _safe_shape(raw.get("request_shape"))
        if request_shape:
            candidate["request_shape"] = request_shape
        candidates.append(candidate)
    captured_count = value.get("captured_count", len(candidates))
    if (
        not isinstance(captured_count, int)
        or isinstance(captured_count, bool)
        or captured_count < 0
    ):
        captured_count = len(candidates)
    return {
        "candidates": candidates,
        "captured_count": captured_count,
        "transport": (
            "current_browser_cdp" if value.get("transport") == "current_browser_cdp" else "unknown"
        ),
        "session_reused": value.get("session_reused") is True,
        "active_request_count": (0 if value.get("active_request_count") == 0 else None),
    }


@dataclass(frozen=True, slots=True)
class NetworkDataExportResult:
    candidate_id: str
    collection_name: str
    endpoint: str
    byte_count: int
    body_sha256: str
    record_count: int | None
    json_path: Path
    csv_path: Path | None = None
    complete: bool = False
    captured_response_count: int = 1
    visited_pages: tuple[int, ...] = ()
    failed_pages: tuple[int, ...] = ()
    declared_total: int | None = None
    declared_pages: int | None = None
    completion_evidence: tuple[str, ...] = ()
    failure_reasons: tuple[str, ...] = ("当前只导出了单个浏览器响应，未执行分页、去重和总数闭合",)

    @property
    def has_strong_completion_evidence(self) -> bool:
        return self.complete and bool(self.completion_evidence) and not self.failed_pages

    def model_summary(self) -> dict[str, Any]:
        """只返回计数、校验值和路径，不把响应正文放进模型上下文。"""

        return {
            "candidate_id": self.candidate_id,
            "collection_name": self.collection_name,
            "endpoint": self.endpoint,
            "byte_count": self.byte_count,
            "body_sha256": self.body_sha256,
            "record_count": self.record_count,
            "complete": self.complete,
            "captured_response_count": self.captured_response_count,
            "visited_pages": list(self.visited_pages),
            "failed_pages": list(self.failed_pages),
            "declared_total": self.declared_total,
            "declared_pages": self.declared_pages,
            "completion_evidence": list(self.completion_evidence),
            "failure_reasons": list(self.failure_reasons),
            "json_path": str(self.json_path),
            "csv_path": str(self.csv_path) if self.csv_path else None,
        }
