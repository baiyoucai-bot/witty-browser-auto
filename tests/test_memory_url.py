from __future__ import annotations

import pytest

from witty_browser_auto.domain.errors import ConfigurationError
from witty_browser_auto.memory.url import normalize_url, template_path


def test_normalize_url_removes_secrets_tracking_fragment_and_default_port() -> None:
    result = normalize_url(
        "HTTPS://Example.COM:443/orders/12345/?token=secret&utm_source=x&page=2#detail"
    )

    assert result.exact == "https://example.com/orders/12345?page=2"
    assert "secret" not in result.exact
    assert result.path_template == "/orders/{id}"


def test_template_path_replaces_uuid_and_long_identifier() -> None:
    assert (
        template_path("/projects/550e8400-e29b-41d4-a716-446655440000/items/abcdef1234567890")
        == "/projects/{id}/items/{id}"
    )


def test_normalize_url_rejects_non_http_scheme() -> None:
    with pytest.raises(ConfigurationError, match="只支持 http 或 https"):
        normalize_url("file:///tmp/demo.html")


def test_normalize_url_converts_unicode_hostname_to_idna() -> None:
    result = normalize_url("https://例子.测试/订单")

    assert result.origin == "https://xn--fsqu00a.xn--0zwm56d"
    assert result.path == "/%E8%AE%A2%E5%8D%95"


def test_normalize_url_never_retains_secrets_but_can_retain_tracking_key() -> None:
    retained = normalize_url(
        "https://example.com/tasks?token=secret&utm_source=campaign&page=2",
        retained_query_keys=frozenset({"token", "utm_source"}),
    )

    assert "token" not in retained.exact
    assert "secret" not in retained.exact
    assert retained.exact.endswith("?page=2&utm_source=campaign")
