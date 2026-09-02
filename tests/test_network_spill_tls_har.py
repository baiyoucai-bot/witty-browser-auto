"""超大响应落盘、TLS 证书详情与 SSE 写入 HAR 的单元测试。"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from witty_browser_auto.cdp.protocol import CdpEvent
from witty_browser_auto.config import NetworkTrafficConfig
from witty_browser_auto.network.har import build_har
from witty_browser_auto.network.inspection import NetworkTrafficInspector
from witty_browser_auto.network.traffic import (
    NetworkExchange,
    NetworkTrafficLog,
    ServerSentEvent,
)

SESSION = "s-1"


def _event(method: str, params: dict[str, Any]) -> CdpEvent:
    return CdpEvent(method=method, params=params, session_id=SESSION)


class _BodySession:
    """只回应正文读取的假会话；可模拟 CDP 失败。"""

    def __init__(self, body: str, *, base64_encoded: bool = False, fail: bool = False) -> None:
        self.body = body
        self.base64_encoded = base64_encoded
        self.fail = fail
        self.calls = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert method == "Network.getResponseBody"
        self.calls += 1
        if self.fail:
            raise RuntimeError("No resource with given identifier found")
        return {"body": self.body, "base64Encoded": self.base64_encoded}


def _run_exchange(
    log: NetworkTrafficLog,
    session: _BodySession,
    *,
    encoded_bytes: int,
    url: str = "https://export.test/api/orders.csv",
) -> NetworkExchange:
    log.on_request(
        _event(
            "Network.requestWillBeSent",
            {
                "requestId": "req-1",
                "request": {"url": url, "method": "GET", "headers": {}},
                "type": "XHR",
                "timestamp": 1000.0,
                "wallTime": 1_700_000_000.0,
            },
        )
    )
    log.on_response(
        _event(
            "Network.responseReceived",
            {
                "requestId": "req-1",
                "type": "XHR",
                "response": {
                    "status": 200,
                    "mimeType": "text/csv",
                    "headers": {"Content-Type": "text/csv"},
                },
            },
        )
    )
    asyncio.run(
        log.on_finished(
            session,  # type: ignore[arg-type]
            _event(
                "Network.loadingFinished",
                {"requestId": "req-1", "timestamp": 1001.0, "encodedDataLength": encoded_bytes},
            ),
        )
    )
    return log.ordered()[0]


# ----------------------------------------------------------------------
# 超大响应落盘
# ----------------------------------------------------------------------


def test_oversized_response_is_spilled_to_a_private_file(tmp_path: Path) -> None:
    payload = "id,name\n" + "\n".join(f"{index},row" for index in range(5000))
    config = NetworkTrafficConfig(max_body_bytes=1024)
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")
    session = _BodySession(payload)

    exchange = _run_exchange(log, session, encoded_bytes=len(payload.encode("utf-8")))
    body = exchange.response_body
    assert body is not None

    # 正文没进内存，但落了盘：路径可读、权限 0600、内容与服务端一致。
    assert body.text is None
    assert body.spill_path
    spilled = Path(body.spill_path)
    assert spilled.read_text(encoding="utf-8") == payload
    assert spilled.stat().st_mode & 0o777 == 0o600
    assert body.byte_length == len(payload.encode("utf-8"))
    assert "已落盘" in body.reason
    assert log.stats()["spilled_body_bytes"] == len(payload.encode("utf-8"))


def test_base64_response_is_spilled_as_raw_bytes(tmp_path: Path) -> None:
    raw = bytes(range(256)) * 20
    config = NetworkTrafficConfig(max_body_bytes=1024)
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")
    session = _BodySession(base64.b64encode(raw).decode("ascii"), base64_encoded=True)

    exchange = _run_exchange(log, session, encoded_bytes=len(raw))
    body = exchange.response_body
    assert body is not None
    # 二进制落盘要还原成原始字节，而不是把 base64 文本写进去。
    assert Path(body.spill_path).read_bytes() == raw


def test_spill_is_skipped_when_disabled(tmp_path: Path) -> None:
    payload = "x" * 5000
    config = NetworkTrafficConfig(max_body_bytes=1024, spill_body_bytes=0)
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")
    session = _BodySession(payload)

    exchange = _run_exchange(log, session, encoded_bytes=len(payload))
    body = exchange.response_body
    assert body is not None
    assert body.spill_path == ""
    assert body.reason == "响应超过单体正文上限"
    # 关闭落盘时不该白白把大正文取进内存。
    assert session.calls == 0
    assert log.stats()["spill_budget_bytes"] == 0


def test_spill_is_skipped_without_a_spill_root(tmp_path: Path) -> None:
    payload = "x" * 5000
    log = NetworkTrafficLog(NetworkTrafficConfig(max_body_bytes=1024))
    session = _BodySession(payload)

    exchange = _run_exchange(log, session, encoded_bytes=len(payload))
    assert exchange.response_body is not None
    assert exchange.response_body.spill_path == ""
    assert session.calls == 0


def test_response_above_the_spill_ceiling_is_not_written(tmp_path: Path) -> None:
    config = NetworkTrafficConfig(
        max_body_bytes=1024, spill_body_bytes=4096, max_total_spill_bytes=8192
    )
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")
    session = _BodySession("x" * 9000)

    exchange = _run_exchange(log, session, encoded_bytes=9000)
    body = exchange.response_body
    assert body is not None
    assert body.reason == "响应超过落盘单体上限"
    assert body.spill_path == ""
    assert session.calls == 0


def test_spill_budget_exhaustion_is_reported(tmp_path: Path) -> None:
    config = NetworkTrafficConfig(
        max_body_bytes=1024, spill_body_bytes=4096, max_total_spill_bytes=4096
    )
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")

    first = _BodySession("a" * 3000)
    log.on_request(
        _event(
            "Network.requestWillBeSent",
            {
                "requestId": "req-1",
                "request": {"url": "https://export.test/a", "method": "GET", "headers": {}},
                "type": "XHR",
                "timestamp": 1000.0,
            },
        )
    )
    asyncio.run(
        log.on_finished(
            first,  # type: ignore[arg-type]
            _event(
                "Network.loadingFinished",
                {"requestId": "req-1", "timestamp": 1001.0, "encodedDataLength": 3000},
            ),
        )
    )
    assert log.ordered()[0].response_body is not None
    assert log.ordered()[0].response_body.spill_path

    second = _BodySession("b" * 3000)
    log.on_request(
        _event(
            "Network.requestWillBeSent",
            {
                "requestId": "req-2",
                "request": {"url": "https://export.test/b", "method": "GET", "headers": {}},
                "type": "XHR",
                "timestamp": 1002.0,
            },
        )
    )
    asyncio.run(
        log.on_finished(
            second,  # type: ignore[arg-type]
            _event(
                "Network.loadingFinished",
                {"requestId": "req-2", "timestamp": 1003.0, "encodedDataLength": 3000},
            ),
        )
    )
    body = log.ordered()[1].response_body
    assert body is not None
    assert body.reason == "落盘全局预算已用尽"
    assert body.spill_path == ""


def test_cdp_failure_during_spill_is_reported(tmp_path: Path) -> None:
    config = NetworkTrafficConfig(max_body_bytes=1024)
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")
    session = _BodySession("", fail=True)

    exchange = _run_exchange(log, session, encoded_bytes=5000)
    body = exchange.response_body
    assert body is not None
    assert "CDP 未能返回响应正文" in body.reason
    assert body.spill_path == ""


def test_read_body_returns_the_spill_path_instead_of_failing(tmp_path: Path) -> None:
    payload = "x" * 5000
    config = NetworkTrafficConfig(max_body_bytes=1024)
    log = NetworkTrafficLog(config, body_spill_root=tmp_path / "network-bodies")
    exchange = _run_exchange(log, _BodySession(payload), encoded_bytes=len(payload))
    inspector = NetworkTrafficInspector(log, tmp_path, config=config)

    full, model = asyncio.run(inspector.read_body({"exchange_id": exchange.exchange_id}))

    assert full["available"] is False
    assert Path(full["spill_path"]).read_text(encoding="utf-8") == payload
    # 路径只回调用方；模型侧只知道正文落盘了，拿不到落盘位置。
    assert full["spill_path"] not in json.dumps(model, ensure_ascii=False)
    assert model["available"] is False


# ----------------------------------------------------------------------
# TLS 证书详情
# ----------------------------------------------------------------------


def test_security_details_are_captured_and_bounded() -> None:
    log = NetworkTrafficLog(NetworkTrafficConfig())
    log.on_request(
        _event(
            "Network.requestWillBeSent",
            {
                "requestId": "req-1",
                "request": {"url": "https://shop.test/api", "method": "GET", "headers": {}},
                "type": "XHR",
                "timestamp": 1000.0,
            },
        )
    )
    log.on_response(
        _event(
            "Network.responseReceived",
            {
                "requestId": "req-1",
                "type": "XHR",
                "response": {
                    "status": 200,
                    "securityState": "secure",
                    "securityDetails": {
                        "protocol": "TLS 1.3",
                        "keyExchange": "",
                        "keyExchangeGroup": "X25519",
                        "cipher": "AES_128_GCM",
                        "subjectName": "shop.test",
                        "issuer": "Example CA",
                        "validFrom": 1_700_000_000,
                        "validTo": 1_800_000_000,
                        "certificateTransparencyCompliance": "compliant",
                        "sanList": [f"host{index}.test" for index in range(30)],
                        "encryptedClientHello": False,
                    },
                },
            },
        )
    )
    details = log.ordered()[0].security_details

    assert details["protocol"] == "TLS 1.3"
    assert details["cipher"] == "AES_128_GCM"
    assert details["issuer"] == "Example CA"
    assert details["valid_to"] == 1_800_000_000.0
    assert details["certificate_transparency"] == "compliant"
    assert details["encrypted_client_hello"] is False
    # 空字符串字段不入结果，SAN 列表有界并标注截断。
    assert "key_exchange" not in details
    assert len(details["san_list"]) == 20
    assert details["san_truncated"] is True
    # 调用方视图带上证书信息，模型视图不需要这些噪声。
    assert log.ordered()[0].full_dict()["security_details"]["protocol"] == "TLS 1.3"
    assert "security_details" not in log.ordered()[0].model_dict()


def test_missing_security_details_stay_empty() -> None:
    log = NetworkTrafficLog(NetworkTrafficConfig())
    log.on_request(
        _event(
            "Network.requestWillBeSent",
            {
                "requestId": "req-1",
                "request": {"url": "http://plain.test/api", "method": "GET", "headers": {}},
                "type": "XHR",
                "timestamp": 1000.0,
            },
        )
    )
    log.on_response(
        _event(
            "Network.responseReceived",
            {"requestId": "req-1", "type": "XHR", "response": {"status": 200}},
        )
    )
    assert log.ordered()[0].security_details == {}


# ----------------------------------------------------------------------
# HAR
# ----------------------------------------------------------------------


def _sse_exchange() -> NetworkExchange:
    exchange = NetworkExchange(
        exchange_id="ex-1",
        request_id="req-1",
        session_id=SESSION,
        method="GET",
        url="https://llm.test/v1/stream",
        resource_type="EventSource",
        status=200,
        state="open",
    )
    exchange.is_event_source = True
    exchange.sse_messages = [
        ServerSentEvent("chunk", '{"delta": "hi"}', "1", 15, False, 1.0),
        ServerSentEvent("done", "[DONE]", "2", 6, False, 2.0),
    ]
    exchange.security_details = {"protocol": "TLS 1.3", "issuer": "Example CA"}
    return exchange


def test_har_carries_sse_messages_on_the_entry() -> None:
    document = build_har([_sse_exchange()])
    entries = document["log"]["entries"]

    # SSE 是普通 HTTP 请求，应该出现在 entries 里而不是被当成 WebSocket。
    assert len(entries) == 1
    assert document["log"]["_websockets"] == []
    messages = entries[0]["_serverSentEvents"]
    assert [item["event"] for item in messages] == ["chunk", "done"]
    assert messages[0]["data"] == '{"delta": "hi"}'
    assert messages[0]["byteLength"] == 15


def test_har_omits_sse_payloads_when_bodies_are_excluded() -> None:
    document = build_har([_sse_exchange()], include_bodies=False)
    messages = document["log"]["entries"][0]["_serverSentEvents"]

    assert "data" not in messages[0]
    # 元数据仍然保留，便于判断连接是否活跃。
    assert messages[0]["event"] == "chunk"


def test_har_includes_security_details() -> None:
    document = build_har([_sse_exchange()])
    assert document["log"]["entries"][0]["_securityDetails"]["protocol"] == "TLS 1.3"


def test_har_omits_security_details_for_plain_http() -> None:
    exchange = _sse_exchange()
    exchange.security_details = {}
    assert "_securityDetails" not in build_har([exchange])["log"]["entries"][0]


def test_har_export_summary_counts_sse_connections(tmp_path: Path) -> None:
    config = NetworkTrafficConfig()
    log = NetworkTrafficLog(config)
    log._exchanges["ex-1"] = _sse_exchange()
    inspector = NetworkTrafficInspector(log, tmp_path, config=config)

    full, _ = asyncio.run(inspector.export_har({"collection_name": "流式接口"}))

    assert full["sse_count"] == 1
    assert full["entry_count"] == 1
    document = json.loads(Path(full["har_path"]).read_text(encoding="utf-8"))
    assert document["log"]["entries"][0]["_serverSentEvents"][1]["data"] == "[DONE]"
