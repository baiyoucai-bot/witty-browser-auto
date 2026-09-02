"""WebSocket 帧读取的单元测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.traffic import NetworkExchange, NetworkTrafficLog, WebSocketFrame


def _frame(direction: str, payload: str, *, opcode: str = "text", timestamp: float = 1.0):
    return WebSocketFrame(
        direction=direction,
        opcode=opcode,
        payload=payload,
        byte_length=len(payload.encode("utf-8")),
        truncated=False,
        timestamp=timestamp,
    )


def _setup(tmp_path: Path, *, is_websocket: bool = True) -> NetworkTrafficInspector:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)
    exchange = NetworkExchange(
        exchange_id="ex-1",
        request_id="req-1",
        session_id="s-1",
        url="wss://quote.test/stream?token=secret123",
        state="open",
    )
    exchange.is_websocket = is_websocket
    exchange.websocket_frames = [
        _frame("sent", '{"op": "subscribe", "channel": "ticker"}', timestamp=1.0),
        _frame("received", '{"channel": "ticker", "price": 101.5}', timestamp=2.0),
        _frame("received", '{"channel": "ticker", "price": 102.0}', timestamp=3.0),
        _frame("received", "not json at all", timestamp=4.0),
        _frame("received", "", opcode="ping", timestamp=5.0),
    ]
    log._exchanges["ex-1"] = exchange
    return NetworkTrafficInspector(log, tmp_path, config=config)


def test_frames_are_returned_with_payload_and_parsed_json(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    full, _ = asyncio.run(inspector.read_websocket_frames({"exchange_id": "ex-1"}))

    assert full["returned_count"] == 5
    assert full["frame_count"] == 5
    assert full["directions"] == {"sent": 1, "received": 4}
    assert full["opcodes"] == {"text": 4, "ping": 1}

    first = full["frames"][0]
    assert first["direction"] == "sent"
    assert first["payload"] == '{"op": "subscribe", "channel": "ticker"}'
    assert first["json"] == {"op": "subscribe", "channel": "ticker"}
    # 非 JSON 帧不应带上 json 字段。
    assert "json" not in full["frames"][3]


def test_direction_filter_selects_one_side(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    full, _ = asyncio.run(
        inspector.read_websocket_frames({"exchange_id": "ex-1", "direction": "sent"})
    )
    assert full["returned_count"] == 1
    assert full["matched_count"] == 1
    assert full["frames"][0]["direction"] == "sent"
    # 统计始终针对整条连接，而不是过滤后的子集。
    assert full["frame_count"] == 5


def test_contains_filter_narrows_to_matching_payloads(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    full, _ = asyncio.run(
        inspector.read_websocket_frames({"exchange_id": "ex-1", "contains": "price"})
    )
    assert full["returned_count"] == 2
    assert all("price" in item["payload"] for item in full["frames"])


def test_limit_keeps_the_newest_frames(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    full, _ = asyncio.run(inspector.read_websocket_frames({"exchange_id": "ex-1", "limit": 2}))
    assert full["returned_count"] == 2
    assert full["matched_count"] == 5
    assert [item["timestamp"] for item in full["frames"]] == [4.0, 5.0]


def test_model_view_hides_frame_payloads(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    _, model = asyncio.run(inspector.read_websocket_frames({"exchange_id": "ex-1"}))
    serialized = json.dumps(model, ensure_ascii=False)
    assert "frames" not in model
    assert "101.5" not in serialized
    assert "secret123" not in serialized
    # 统计必须保留，否则模型判断不出连接是否活跃。
    assert model["frame_count"] == 5
    assert model["directions"] == {"sent": 1, "received": 4}


def test_non_websocket_exchange_is_rejected(tmp_path: Path) -> None:
    inspector = _setup(tmp_path, is_websocket=False)
    with pytest.raises(ValueError, match="不是 WebSocket 连接"):
        asyncio.run(inspector.read_websocket_frames({"exchange_id": "ex-1"}))


def test_invalid_direction_is_rejected(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    with pytest.raises(ValueError, match="direction 必须是"):
        asyncio.run(inspector.read_websocket_frames({"exchange_id": "ex-1", "direction": "both"}))


def test_missing_exchange_is_reported(tmp_path: Path) -> None:
    inspector = _setup(tmp_path)
    with pytest.raises(ValueError, match="流量交换不存在"):
        asyncio.run(inspector.read_websocket_frames({"exchange_id": "ex-404"}))
