"""基于 CDP 的有界 JSON 响应体捕获、结构识别和私有导出。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from witty_browser_auto.browser.session import CdpTargetSession
from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkCaptureConfig
from witty_browser_auto.domain.errors import ConfigurationError
from witty_browser_auto.domain.network_data import (
    NetworkDataExportResult,
    sanitize_network_inspection,
)
from witty_browser_auto.memory.url import normalize_url
from witty_browser_auto.network.routing import CdpNetworkRouter, NetworkRouteRule

logger = logging.getLogger(__name__)
_COLLECTION_NAME = re.compile(r"^[\w\u4e00-\u9fff-]{1,100}$", re.UNICODE)
_JSON_RESOURCE_TYPES = {"XHR", "Fetch"}
_TOTAL_KEYS = {"total", "totalcount", "recordcount", "totalrecords", "总数", "总条数"}
_TOTAL_PAGE_KEYS = {"pages", "totalpage", "totalpages", "pagecount", "总页数"}
_CURRENT_PAGE_KEYS = {"page", "pagenumber", "currentpage", "pageindex", "当前页"}


@dataclass(frozen=True, slots=True)
class _PendingResponse:
    request_id: str
    session_id: str
    endpoint: str
    method: str
    status: int
    mime_type: str
    resource_type: str


@dataclass(slots=True)
class _ObservedRequest:
    """请求侧观察：只保留方法、URL、开始时间和请求体结构，不保存请求体值。"""

    method: str
    url: str
    started: float | None = None
    post_shape: dict[str, Any] | None = None
    status: int | None = None
    mime_type: str = ""


@dataclass(slots=True)
class _ResponseWaiter:
    url_substring: str
    future: asyncio.Future[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _CapturedResponse:
    candidate_id: str
    endpoint: str
    method: str
    status: int
    mime_type: str
    resource_type: str
    body: bytes
    json_value: Any
    body_sha256: str
    json_shape: dict[str, Any]
    score: int
    request_shape: dict[str, Any] | None = None
    duration_ms: int | None = None
    captured_at: float = 0.0


class CdpNetworkCapture:
    """只捕获任务授权来源内的 JSON 接口响应，不发送额外网络请求。"""

    def __init__(
        self,
        config: NetworkCaptureConfig,
        artifact_root: Path,
        *,
        allowed_origins: Sequence[str],
    ) -> None:
        self.config = config
        self.artifact_root = artifact_root
        self.allowed_origins = frozenset(self._normalize_origins(allowed_origins))
        self._requests: dict[tuple[str, str], _ObservedRequest] = {}
        self._pending: dict[tuple[str, str], _PendingResponse] = {}
        self._captured: deque[_CapturedResponse] = deque(maxlen=config.max_responses)
        self._waiters: list[_ResponseWaiter] = []
        self._route_rules: tuple[NetworkRouteRule, ...] = ()
        self._route_router: CdpNetworkRouter | None = None
        self._route_lock = asyncio.Lock()

    async def bind_router(self, router: CdpNetworkRouter) -> None:
        async with self._route_lock:
            await router.set_rules(self._route_rules)
            self._route_router = router

    async def unbind_router(self, router: CdpNetworkRouter) -> None:
        async with self._route_lock:
            if self._route_router is router:
                self._route_router = None

    async def manage_route(self, operation: str, config: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation == "list":
            async with self._route_lock:
                return {"rules": [rule.public_dict() for rule in self._route_rules]}
        if operation == "add":
            rule = NetworkRouteRule.from_config(
                config,
                allowed_origins=tuple(self.allowed_origins),
                rule_id=f"route-{uuid.uuid4().hex[:12]}",
            )
            async with self._route_lock:
                if len(self._route_rules) >= 8:
                    raise ValueError("网络路由最多保留 8 条规则")
                rules = (*self._route_rules, rule)
                if self._route_router is not None:
                    await self._route_router.set_rules(rules)
                self._route_rules = rules
            logger.info(
                "已添加任务网络路由", extra={"rule_id": rule.rule_id, "action": rule.action}
            )
            return {"rule": rule.public_dict(), "rules": [item.public_dict() for item in rules]}
        if operation == "remove":
            rule_id = config.get("rule_id")
            if not isinstance(rule_id, str) or not rule_id.strip():
                raise ValueError("移除网络路由必须提供 rule_id")
            async with self._route_lock:
                rules = tuple(item for item in self._route_rules if item.rule_id != rule_id)
                if len(rules) == len(self._route_rules):
                    raise ValueError("网络路由不存在或已经移除")
                if self._route_router is not None:
                    await self._route_router.set_rules(rules)
                self._route_rules = rules
            logger.info("已移除任务网络路由", extra={"rule_id": rule_id})
            return {"removed_rule_id": rule_id, "rules": [item.public_dict() for item in rules]}
        raise ValueError("网络路由 operation 必须是 list、add 或 remove")

    def observe_request(self, event: CdpEvent) -> None:
        """暂存请求方法、URL、开始时间和请求体结构，不保存请求体原始值。"""

        request = event.params.get("request")
        request_id = event.params.get("requestId")
        session_id = event.session_id
        if (
            not isinstance(request, Mapping)
            or not isinstance(request_id, str)
            or not isinstance(session_id, str)
        ):
            return
        timestamp = event.params.get("timestamp")
        if len(self._requests) >= 4096:
            self._requests.pop(next(iter(self._requests)))
        self._requests[(session_id, request_id)] = _ObservedRequest(
            method=str(request.get("method", "GET")),
            url=str(request.get("url", "")),
            started=float(timestamp) if isinstance(timestamp, (int, float)) else None,
            post_shape=self._describe_request_payload(request),
        )

    def observe_response(self, event: CdpEvent) -> None:
        response = event.params.get("response")
        request_id = event.params.get("requestId")
        session_id = event.session_id
        resource_type = event.params.get("type")
        if (
            not isinstance(response, Mapping)
            or not isinstance(request_id, str)
            or not isinstance(session_id, str)
        ):
            return
        url = response.get("url")
        status = response.get("status")
        mime_type = str(response.get("mimeType", "")).lower()
        observed = self._requests.get((session_id, request_id))
        if observed is not None and isinstance(status, (int, float)):
            observed.status = int(status)
            observed.mime_type = mime_type
        if not self.config.enabled or resource_type not in _JSON_RESOURCE_TYPES:
            return
        if (
            not isinstance(url, str)
            or self._origin(url) not in self.allowed_origins
            or not isinstance(status, (int, float))
            or not 200 <= int(status) < 300
            or not self._is_json_mime(mime_type)
        ):
            return
        method = observed.method if observed is not None else "GET"
        endpoint = self._endpoint(url)
        self._pending[(session_id, request_id)] = _PendingResponse(
            request_id=request_id,
            session_id=session_id,
            endpoint=endpoint,
            method=method,
            status=int(status),
            mime_type=mime_type,
            resource_type=str(resource_type),
        )

    async def capture_finished(self, session: CdpTargetSession, event: CdpEvent) -> None:
        request_id = event.params.get("requestId")
        session_id = event.session_id
        if not isinstance(request_id, str) or not isinstance(session_id, str):
            return
        key = (session_id, request_id)
        pending = self._pending.pop(key, None)
        observed = self._requests.pop(key, None)
        duration_ms: int | None = None
        finished_at = event.params.get("timestamp")
        if (
            observed is not None
            and observed.started is not None
            and isinstance(finished_at, (int, float))
            and float(finished_at) >= observed.started
        ):
            duration_ms = round((float(finished_at) - observed.started) * 1000)
        candidate: _CapturedResponse | None = None
        if pending is not None:
            candidate = await self._try_capture(
                session,
                event,
                pending,
                request_shape=observed.post_shape if observed is not None else None,
                duration_ms=duration_ms,
            )
        self._resolve_waiters(observed, candidate, duration_ms=duration_ms)

    async def _try_capture(
        self,
        session: CdpTargetSession,
        event: CdpEvent,
        pending: _PendingResponse,
        *,
        request_shape: dict[str, Any] | None,
        duration_ms: int | None,
    ) -> _CapturedResponse | None:
        encoded_size = event.params.get("encodedDataLength")
        if isinstance(encoded_size, (int, float)) and encoded_size > self.config.max_body_bytes:
            logger.info(
                "网络响应体超过配置上限，跳过正文读取",
                extra={"endpoint": pending.endpoint, "encoded_bytes": int(encoded_size)},
            )
            return None
        try:
            result = await session.call(
                "Network.getResponseBody",
                {"requestId": pending.request_id},
            )
            body = self._decode_body(result)
            if len(body) > self.config.max_body_bytes:
                logger.info(
                    "网络响应体实际大小超过配置上限，已丢弃",
                    extra={"endpoint": pending.endpoint, "body_bytes": len(body)},
                )
                return None
            json_value = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError, binascii.Error) as exc:
            logger.info(
                "网络响应体不是可处理的 UTF-8 JSON，已跳过",
                extra={"endpoint": pending.endpoint, "reason": type(exc).__name__},
            )
            return None
        except Exception as exc:
            logger.warning(
                "读取网络响应体失败，任务继续执行",
                extra={"endpoint": pending.endpoint, "reason": type(exc).__name__},
            )
            return None
        body_sha256 = hashlib.sha256(body).hexdigest()
        candidate_id = hashlib.sha256(
            f"{pending.session_id}\0{pending.request_id}\0{pending.endpoint}\0{time.time_ns()}".encode()
        ).hexdigest()[:24]
        shape = self._describe_json(json_value)
        candidate = _CapturedResponse(
            candidate_id=candidate_id,
            endpoint=pending.endpoint,
            method=pending.method,
            status=pending.status,
            mime_type=pending.mime_type,
            resource_type=pending.resource_type,
            body=body,
            json_value=json_value,
            body_sha256=body_sha256,
            json_shape=shape,
            score=self._score_candidate(json_value, pending),
            request_shape=request_shape,
            duration_ms=duration_ms,
            captured_at=time.monotonic(),
        )
        self._captured.append(candidate)
        logger.info(
            "已捕获可复用的 JSON 接口响应",
            extra={"endpoint": pending.endpoint, "body_bytes": len(body)},
        )
        return candidate

    def discard_request(self, event: CdpEvent) -> None:
        request_id = event.params.get("requestId")
        if isinstance(request_id, str) and isinstance(event.session_id, str):
            key = (event.session_id, request_id)
            self._pending.pop(key, None)
            observed = self._requests.pop(key, None)
            self._resolve_waiters(observed, None, duration_ms=None, failed=True)

    def discard_session(self, session_id: str) -> None:
        for key in tuple(self._pending):
            if key[0] == session_id:
                self._pending.pop(key, None)
        for key in tuple(self._requests):
            if key[0] == session_id:
                self._requests.pop(key, None)

    async def wait_for_response(
        self,
        url_substring: str,
        *,
        timeout_seconds: float = 30.0,
        accept_recent_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """等待浏览器已触发的请求返回匹配响应；只返回脱敏元数据，不返回正文。"""

        substring = url_substring.strip()
        if not substring or len(substring) > 500 or any(ord(c) < 32 for c in substring):
            raise ValueError("等待网络响应的 url_substring 不能为空且不能包含控制字符")
        if not 1 <= timeout_seconds <= 300:
            raise ValueError("等待网络响应的超时必须在 1 到 300 秒之间")
        recent_cutoff = time.monotonic() - max(accept_recent_seconds, 0.0)
        for item in reversed(self._captured):
            if item.captured_at >= recent_cutoff and substring in item.endpoint:
                return self._candidate_wait_payload(item)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        waiter = _ResponseWaiter(url_substring=substring, future=future)
        self._waiters.append(waiter)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            return {
                "matched": False,
                "url_substring": substring,
                "waited_seconds": timeout_seconds,
            }
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def _resolve_waiters(
        self,
        observed: _ObservedRequest | None,
        candidate: _CapturedResponse | None,
        *,
        duration_ms: int | None,
        failed: bool = False,
    ) -> None:
        if observed is None or not observed.url or not self._waiters:
            return
        payload: dict[str, Any] | None = None
        for waiter in tuple(self._waiters):
            if waiter.future.done() or waiter.url_substring not in observed.url:
                continue
            if payload is None:
                if candidate is not None:
                    payload = self._candidate_wait_payload(candidate)
                else:
                    payload = {
                        "matched": True,
                        "captured": False,
                        "endpoint": self._endpoint(observed.url),
                        "method": observed.method,
                        "status": observed.status,
                        "mime_type": observed.mime_type,
                        "duration_ms": duration_ms,
                    }
                    if failed:
                        payload["failed"] = True
                    else:
                        payload["note"] = (
                            "响应已完成但未进入捕获，可能不是授权 origin 内的 2xx JSON"
                        )
            waiter.future.set_result(dict(payload))

    @staticmethod
    def _candidate_wait_payload(candidate: _CapturedResponse) -> dict[str, Any]:
        return {
            "matched": True,
            "captured": True,
            "candidate_id": candidate.candidate_id,
            "endpoint": candidate.endpoint,
            "method": candidate.method,
            "status": candidate.status,
            "mime_type": candidate.mime_type,
            "body_bytes": len(candidate.body),
            "duration_ms": candidate.duration_ms,
        }

    @staticmethod
    def _describe_request_payload(request: Mapping[str, Any]) -> dict[str, Any] | None:
        """把 POST 体归纳为字段结构，绝不保留字段值。"""

        post_data = request.get("postData")
        if not isinstance(post_data, str) or not post_data:
            return None
        if len(post_data) > 65_536:
            return {"type": "text", "length": len(post_data)}
        stripped = post_data.strip()
        if stripped.startswith(("{", "[")):
            try:
                return CdpNetworkCapture._describe_json(json.loads(stripped))
            except (ValueError, TypeError):
                pass
        if "=" in post_data and "\n" not in post_data:
            fields = [name for name, _ in parse_qsl(post_data, keep_blank_values=True)]
            if fields:
                return {"type": "form", "keys": [str(name)[:100] for name in fields[:50]]}
        return {"type": "text", "length": len(post_data)}

    async def inspect(self, *, max_candidates: int = 20) -> dict[str, Any]:
        if isinstance(max_candidates, bool) or not 1 <= max_candidates <= 50:
            raise ValueError("网络接口候选数量必须在 1 到 50 之间")
        ordered = sorted(self._captured, key=lambda item: (-item.score, item.endpoint))
        raw = {
            "captured_count": len(self._captured),
            "transport": "current_browser_cdp",
            "session_reused": True,
            "active_request_count": 0,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "endpoint": item.endpoint,
                    "method": item.method,
                    "status": item.status,
                    "mime_type": item.mime_type,
                    "resource_type": item.resource_type,
                    "body_bytes": len(item.body),
                    "score": item.score,
                    "json_shape": item.json_shape,
                    "request_shape": item.request_shape,
                    "duration_ms": item.duration_ms,
                }
                for item in ordered[:max_candidates]
            ],
        }
        return sanitize_network_inspection(raw, max_candidates=max_candidates)

    async def export(self, candidate_id: str, collection_name: str) -> NetworkDataExportResult:
        if not _COLLECTION_NAME.fullmatch(collection_name):
            raise ValueError("网络数据集合名称只能包含中英文、数字、下划线或连字符")
        captured = next(
            (item for item in reversed(self._captured) if item.candidate_id == candidate_id),
            None,
        )
        if captured is None:
            raise ValueError("网络接口候选不存在或已经过期，请重新观察")
        return await asyncio.to_thread(self._export_sync, captured, collection_name)

    async def export_many(
        self,
        candidate_ids: Sequence[str],
        collection_name: str,
    ) -> NetworkDataExportResult:
        if not _COLLECTION_NAME.fullmatch(collection_name):
            raise ValueError("网络数据集合名称只能包含中英文、数字、下划线或连字符")
        if not 2 <= len(candidate_ids) <= 50 or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("批量网络导出必须包含 2 到 50 个不重复候选")
        by_id = {item.candidate_id: item for item in self._captured}
        missing = [candidate_id for candidate_id in candidate_ids if candidate_id not in by_id]
        if missing:
            raise ValueError("网络接口候选不存在或已经过期，请重新观察")
        captured = tuple(by_id[candidate_id] for candidate_id in candidate_ids)
        endpoints = {item.endpoint for item in captured}
        record_paths = {tuple(self._find_record_path(item.json_value)) for item in captured}
        if len(endpoints) != 1 or len(record_paths) != 1:
            raise ValueError("批量网络导出只支持记录结构相同的同一接口响应")
        if not next(iter(record_paths)):
            raise ValueError("批量网络候选中没有可聚合的记录数组")
        return await asyncio.to_thread(self._export_many_sync, captured, collection_name)

    def _export_sync(
        self,
        captured: _CapturedResponse,
        collection_name: str,
    ) -> NetworkDataExportResult:
        output_dir = self.artifact_root / "network-data"
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        stem = f"{collection_name}-{time.time_ns()}"
        json_path = output_dir / f"{stem}.json"
        json_bytes = json.dumps(captured.json_value, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_private(json_path, json_bytes)
        records = self._find_record_collection(captured.json_value)
        csv_path: Path | None = None
        if records:
            csv_path = output_dir / f"{stem}.csv"
            self._write_private(csv_path, self._records_csv(records).encode("utf-8-sig"))
        result = NetworkDataExportResult(
            candidate_id=captured.candidate_id,
            collection_name=collection_name,
            endpoint=captured.endpoint,
            byte_count=len(captured.body),
            body_sha256=captured.body_sha256,
            record_count=len(records) if records is not None else None,
            json_path=json_path,
            csv_path=csv_path,
        )
        logger.info(
            "网络响应数据已由代码导出",
            extra={
                "endpoint": captured.endpoint,
                "body_bytes": len(captured.body),
                "record_count": result.record_count,
                "csv_exported": csv_path is not None,
            },
        )
        return result

    def _export_many_sync(
        self,
        captured: Sequence[_CapturedResponse],
        collection_name: str,
    ) -> NetworkDataExportResult:
        records: list[Mapping[str, Any]] = []
        seen_records: set[str] = set()
        for response in captured:
            response_records = self._find_record_collection(response.json_value)
            if not response_records:
                raise ValueError("批量网络候选中存在没有记录数组的响应")
            for record in response_records:
                identity = json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if identity not in seen_records:
                    seen_records.add(identity)
                    records.append(record)

        totals = [
            value
            for response in captured
            if (value := self._pagination_value(response.json_value, _TOTAL_KEYS)) is not None
        ]
        total_pages = [
            value
            for response in captured
            if (value := self._pagination_value(response.json_value, _TOTAL_PAGE_KEYS)) is not None
        ]
        current_pages = [
            value
            for response in captured
            if (value := self._pagination_value(response.json_value, _CURRENT_PAGE_KEYS))
            is not None
        ]
        declared_total = totals[0] if totals and len(set(totals)) == 1 else None
        declared_pages = total_pages[0] if total_pages and len(set(total_pages)) == 1 else None
        visited_pages = tuple(sorted(set(current_pages)))
        completion_evidence: list[str] = []
        failure_reasons: list[str] = []
        if declared_total is not None and len(records) == declared_total:
            completion_evidence.append(f"接口声明总数 {declared_total} 与聚合去重记录数一致")
        elif declared_total is not None:
            failure_reasons.append(
                f"接口声明总数为 {declared_total}，当前聚合去重后只有 {len(records)} 条"
            )
        if declared_pages is not None:
            expected_pages = set(range(1, declared_pages + 1))
            if visited_pages and set(visited_pages) == expected_pages:
                completion_evidence.append(f"接口页码字段证明已覆盖全部 {declared_pages} 页")
            elif visited_pages:
                missing_pages = sorted(expected_pages - set(visited_pages))
                failure_reasons.append(f"接口页码仍缺少：{missing_pages[:20]}")
        if not completion_evidence:
            failure_reasons.append("批量响应缺少可闭合的接口总数或完整页码证据")

        output_dir = self.artifact_root / "network-data"
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(output_dir, 0o700)
        stem = f"{collection_name}-{time.time_ns()}"
        json_path = output_dir / f"{stem}.json"
        csv_path = output_dir / f"{stem}.csv"
        payload = {
            "records": records,
            "collection": {
                "endpoint": captured[0].endpoint,
                "captured_response_count": len(captured),
                "record_count": len(records),
                "declared_total": declared_total,
                "declared_pages": declared_pages,
                "visited_pages": list(visited_pages),
            },
        }
        json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_private(json_path, json_bytes)
        self._write_private(csv_path, self._records_csv(records).encode("utf-8-sig"))
        aggregate_id = (
            "batch-"
            + hashlib.sha256(
                "\0".join(response.candidate_id for response in captured).encode()
            ).hexdigest()[:24]
        )
        result = NetworkDataExportResult(
            candidate_id=aggregate_id,
            collection_name=collection_name,
            endpoint=captured[0].endpoint,
            byte_count=sum(len(response.body) for response in captured),
            body_sha256=hashlib.sha256(json_bytes).hexdigest(),
            record_count=len(records),
            json_path=json_path,
            csv_path=csv_path,
            complete=bool(completion_evidence) and not failure_reasons,
            captured_response_count=len(captured),
            visited_pages=visited_pages,
            declared_total=declared_total,
            declared_pages=declared_pages,
            completion_evidence=tuple(completion_evidence),
            failure_reasons=tuple(failure_reasons),
        )
        logger.info(
            "多个网络响应已完成代码聚合",
            extra={
                "endpoint": result.endpoint,
                "captured_response_count": result.captured_response_count,
                "record_count": result.record_count,
                "complete": result.complete,
            },
        )
        return result

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)

    @staticmethod
    def _decode_body(result: Mapping[str, Any]) -> bytes:
        raw = result.get("body")
        if not isinstance(raw, str):
            raise ValueError("CDP 未返回响应正文")
        if result.get("base64Encoded") is True:
            return base64.b64decode(raw, validate=True)
        return raw.encode("utf-8")

    @staticmethod
    def _describe_json(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            keys = [str(key)[:100] for key in list(value)[:50]]
            records = CdpNetworkCapture._find_record_collection(value)
            result: dict[str, Any] = {"type": "object", "keys": keys}
            if records:
                result["record_path"] = CdpNetworkCapture._find_record_path(value)
                result["length"] = len(records)
                result["item_type"] = "object"
                result["item_keys"] = [str(key)[:100] for key in list(records[0])[:50]]
            return result
        if isinstance(value, list):
            result = {"type": "array", "length": len(value)}
            if value:
                result["item_type"] = CdpNetworkCapture._value_type(value[0])
                if isinstance(value[0], Mapping):
                    result["item_keys"] = [str(key)[:100] for key in list(value[0])[:50]]
            return result
        return {"type": CdpNetworkCapture._value_type(value)}

    @staticmethod
    def _find_record_collection(value: Any, *, depth: int = 0) -> list[Mapping[str, Any]] | None:
        if depth > 5:
            return None
        if isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
            return value
        if isinstance(value, Mapping):
            candidates = [
                found
                for item in value.values()
                if (found := CdpNetworkCapture._find_record_collection(item, depth=depth + 1))
            ]
            return max(candidates, key=len) if candidates else None
        return None

    @staticmethod
    def _find_record_path(value: Any, *, depth: int = 0) -> list[str]:
        if depth > 5 or not isinstance(value, Mapping):
            return []
        for key, item in value.items():
            if isinstance(item, list) and item and all(isinstance(row, Mapping) for row in item):
                return [str(key)[:100]]
            nested = CdpNetworkCapture._find_record_path(item, depth=depth + 1)
            if nested:
                return [str(key)[:100], *nested]
        return []

    @staticmethod
    def _pagination_value(
        value: Any,
        accepted_keys: set[str],
        *,
        depth: int = 0,
    ) -> int | None:
        """只检查响应包装对象，不进入业务记录数组，避免把金额等字段误认成分页。"""

        if depth > 5 or not isinstance(value, Mapping):
            return None
        for key, item in value.items():
            normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(key).casefold()).replace("_", "")
            if normalized in accepted_keys and isinstance(item, int) and not isinstance(item, bool):
                if 0 < item <= 10_000_000:
                    return item
        for item in value.values():
            if isinstance(item, Mapping):
                found = CdpNetworkCapture._pagination_value(
                    item,
                    accepted_keys,
                    depth=depth + 1,
                )
                if found is not None:
                    return found
        return None

    @staticmethod
    def _records_csv(records: list[Mapping[str, Any]]) -> str:
        fields = list(dict.fromkeys(str(key) for record in records for key in record))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    field: (
                        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                        if isinstance(value, (Mapping, list))
                        else value
                    )
                    for field in fields
                    if (value := record.get(field)) is not None
                }
            )
        return output.getvalue()

    @staticmethod
    def _score_candidate(value: Any, pending: _PendingResponse) -> int:
        score = 60
        if pending.resource_type in _JSON_RESOURCE_TYPES:
            score += 15
        if CdpNetworkCapture._find_record_collection(value):
            score += 25
        return min(score, 100)

    @staticmethod
    def _value_type(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, Mapping):
            return "object"
        if isinstance(value, list):
            return "array"
        if isinstance(value, (int, float)):
            return "number"
        return "string"

    @staticmethod
    def _is_json_mime(mime_type: str) -> bool:
        media_type = mime_type.split(";", 1)[0].strip()
        return media_type in {"application/json", "text/json"} or media_type.endswith("+json")

    @staticmethod
    def _origin(url: str) -> str:
        try:
            return normalize_url(url).origin
        except (ConfigurationError, ValueError):
            return ""

    @staticmethod
    def _endpoint(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))

    @staticmethod
    def _normalize_origins(origins: Sequence[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        for origin in origins:
            try:
                normalized.append(normalize_url(f"{origin.rstrip('/')}/").origin)
            except (ConfigurationError, ValueError):
                continue
        return tuple(dict.fromkeys(normalized))
