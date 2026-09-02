from __future__ import annotations

from witty_browser_auto.security.redaction import (
    REDACTED,
    redact,
    redact_task_inputs,
    redact_url,
)


def test_redact_nested_secrets() -> None:
    value = {
        "Authorization": "Bearer secret",
        "nested": {"api_key": "abc", "name": "可保留"},
        "items": [{"password": "123456"}],
    }

    result = redact(value)

    assert result["Authorization"] == REDACTED
    assert result["nested"]["api_key"] == REDACTED
    assert result["nested"]["name"] == "可保留"
    assert result["items"][0]["password"] == REDACTED


def test_redact_url_removes_fragment_and_masks_sensitive_query() -> None:
    result = redact_url("https://example.com/path?token=abc&page=2#section")

    assert "abc" not in result
    assert "page=2" in result
    assert "section" not in result


def test_redact_url_removes_embedded_credentials() -> None:
    result = redact_url("https://admin:secret@example.com/path?page=2")

    assert result == "https://example.com/path?page=2"


def test_redact_hides_multimodal_image_data() -> None:
    result = redact(
        {
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,c2VjcmV0"},
                }
            ]
        }
    )

    assert result["content"][0]["image_url"]["url"] == REDACTED


def test_inline_and_blob_urls_are_omitted() -> None:
    assert redact_url("data:font/woff2;base64,secret") == "<内联数据 URL 已省略>"
    assert redact_url("blob:https://example.com/secret-id") == "<Blob URL 已省略>"


def test_redact_url_masks_business_query_and_known_task_input() -> None:
    result = redact_url(
        "https://example.com/order?keywords=account-value&ticket=temp-ticket&page=2",
        sensitive_values=("account-value",),
    )

    assert "account-value" not in result
    assert "temp-ticket" not in result
    assert "page=2" in result


def test_redact_task_inputs_recursively_scrubs_arbitrary_string_fields() -> None:
    result = redact_task_inputs(
        {
            "message": "账号 account-value 已完成",
            "url": "https://example.com/order?query=account-value",
            "items": ["保留", {"text": "account-value"}],
        },
        {"account": "account-value"},
    )

    assert "account-value" not in str(result)
    assert result["items"][0] == "保留"
