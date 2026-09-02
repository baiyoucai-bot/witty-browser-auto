from __future__ import annotations

import pytest

from witty_browser_auto.cdp.discovery import ensure_loopback_endpoint
from witty_browser_auto.domain.errors import PolicyViolationError


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "ws://[::1]:9222/devtools/browser/test",
    ],
)
def test_loopback_endpoint_is_allowed(endpoint: str) -> None:
    ensure_loopback_endpoint(endpoint)


def test_remote_endpoint_is_denied_by_default() -> None:
    with pytest.raises(PolicyViolationError, match="只允许连接本机回环"):
        ensure_loopback_endpoint("ws://192.168.1.8:9222/devtools/browser/test")


def test_loopback_endpoint_with_credentials_is_denied() -> None:
    with pytest.raises(PolicyViolationError, match="不得包含明文账号或密码"):
        ensure_loopback_endpoint("http://admin:secret@127.0.0.1:9222")
