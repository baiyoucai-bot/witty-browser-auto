"""页面 Markdown 与链接清单的执行层回归。

HTML→Markdown 的转换保真度由真实浏览器集成测试证明，见
`tests/integration/test_real_browser_page_content.py`；这里覆盖参数校验、筛选去重与两路视图。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from witty_browser_auto.agent import page_tools
from witty_browser_auto.browser.page_content import (
    DEFAULT_MAX_CHARS,
    MAX_CHARS,
    MAX_LINKS,
    markdown_options,
    select_links,
)

PAGE_URL = "https://docs.test/guide/"


class _ContentDriver:
    """只回应两个内容读取方法的假驱动。"""

    def __init__(
        self,
        *,
        markdown: str = "# 标题\n\n正文",
        links: list[dict[str, Any]] | None = None,
        images: list[dict[str, Any]] | None = None,
    ) -> None:
        self.markdown = markdown
        self.links = links if links is not None else []
        self.images = images if images is not None else []
        self.options: dict[str, Any] | None = None
        self.link_calls: list[dict[str, Any]] = []

    async def read_page_markdown(self, options: dict[str, Any]) -> dict[str, Any]:
        self.options = options
        clipped = self.markdown[: options["maxChars"]]
        return {
            "markdown": clipped,
            "truncated": len(clipped) < len(self.markdown),
            "char_count": len(clipped),
            "total_char_count": len(self.markdown),
            "title": "指南",
            "url": PAGE_URL,
            "root": "main",
        }

    async def read_page_links(self, *, include_images: bool, scan_limit: int) -> dict[str, Any]:
        self.link_calls.append({"include_images": include_images, "scan_limit": scan_limit})
        return {
            "links": self.links,
            "images": self.images if include_images else [],
            "url": PAGE_URL,
            "title": "指南",
        }


# ----------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------


def test_markdown_options_are_bounded() -> None:
    options = markdown_options()
    assert options == {
        "onlyMainContent": True,
        "selector": "",
        "includeImages": False,
        "includeLinks": True,
        "maxChars": DEFAULT_MAX_CHARS,
    }
    with pytest.raises(ValueError, match="Markdown 上限"):
        markdown_options(max_chars=10)
    with pytest.raises(ValueError, match="Markdown 上限"):
        markdown_options(max_chars=MAX_CHARS + 1)
    with pytest.raises(ValueError, match="内容选择器"):
        markdown_options(selector="  ")


def test_markdown_tool_rejects_unknown_arguments() -> None:
    driver = _ContentDriver()
    with pytest.raises(ValueError, match="未知参数"):
        asyncio.run(page_tools.execute_read_page_markdown({"bogus": 1}, driver=driver))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only_main_content 必须是布尔值"):
        asyncio.run(
            page_tools.execute_read_page_markdown({"only_main_content": "yes"}, driver=driver)  # type: ignore[arg-type]
        )


def test_markdown_tool_passes_options_through() -> None:
    driver = _ContentDriver()
    asyncio.run(
        page_tools.execute_read_page_markdown(
            {
                "only_main_content": False,
                "selector": "#content",
                "include_images": True,
                "include_links": False,
                "max_chars": 5000,
            },
            driver=driver,  # type: ignore[arg-type]
        )
    )
    assert driver.options == {
        "onlyMainContent": False,
        "selector": "#content",
        "includeImages": True,
        "includeLinks": False,
        "maxChars": 5000,
    }


def test_markdown_result_reports_truncation_honestly() -> None:
    driver = _ContentDriver(markdown="正" * 4000)
    outcome = asyncio.run(
        page_tools.execute_read_page_markdown({"max_chars": 1000}, driver=driver)  # type: ignore[arg-type]
    )
    assert outcome.success is True
    assert outcome.data["truncated"] is True
    assert outcome.data["char_count"] == 1000
    # 截断只影响返回内容，页面真实总长必须如实报告，否则调用方以为自己读全了。
    assert outcome.data["total_char_count"] == 4000
    assert "截断" in outcome.message
    # 正文就是调用方要读的东西，模型视图同样给出。
    assert outcome.model_data is not None
    assert outcome.model_data["markdown"] == outcome.data["markdown"]


def test_empty_main_content_is_a_business_failure() -> None:
    driver = _ContentDriver(markdown="")
    outcome = asyncio.run(page_tools.execute_read_page_markdown({}, driver=driver))  # type: ignore[arg-type]
    assert outcome.success is False
    assert "主内容为空" in outcome.message


# ----------------------------------------------------------------------
# 链接
# ----------------------------------------------------------------------


def _entries() -> list[dict[str, Any]]:
    return [
        {"href": "https://docs.test/guide/a", "text": "甲", "rel": "", "target": ""},
        {"href": "https://docs.test/guide/a", "text": "甲重复", "rel": "", "target": ""},
        {"href": "https://other.test/out", "text": "外站", "rel": "nofollow", "target": "_blank"},
        {"href": "https://docs.test/api/b", "text": "接口乙", "rel": "", "target": ""},
    ]


def test_links_are_deduped_in_page_order() -> None:
    links = select_links(_entries(), page_url=PAGE_URL)
    assert [item["href"] for item in links] == [
        "https://docs.test/guide/a",
        "https://other.test/out",
        "https://docs.test/api/b",
    ]
    # 去重保留首次出现的那条文本。
    assert links[0]["text"] == "甲"
    assert links[1]["rel"] == "nofollow"
    assert links[1]["target"] == "_blank"


def test_same_origin_filter_and_substring_filter() -> None:
    same_origin = select_links(_entries(), page_url=PAGE_URL, same_origin_only=True)
    assert all(item["same_origin"] for item in same_origin)
    assert "https://other.test/out" not in [item["href"] for item in same_origin]

    # 子串同时匹配地址与链接文本。
    by_url = select_links(_entries(), page_url=PAGE_URL, contains="/api/")
    assert [item["href"] for item in by_url] == ["https://docs.test/api/b"]
    by_text = select_links(_entries(), page_url=PAGE_URL, contains="外站")
    assert [item["href"] for item in by_text] == ["https://other.test/out"]


def test_link_limit_is_bounded() -> None:
    assert len(select_links(_entries(), page_url=PAGE_URL, limit=1)) == 1
    with pytest.raises(ValueError, match="链接数量上限"):
        select_links(_entries(), page_url=PAGE_URL, limit=MAX_LINKS + 1)


def test_links_tool_reports_counts_and_scans_at_the_hard_cap() -> None:
    images = [{"src": "https://docs.test/x.png", "alt": "图"}]
    driver = _ContentDriver(links=_entries(), images=images)
    outcome = asyncio.run(
        page_tools.execute_list_page_links({"include_images": True}, driver=driver)  # type: ignore[arg-type]
    )

    assert outcome.success is True
    assert outcome.data["returned_count"] == 3
    assert outcome.data["scanned_count"] == 4
    assert outcome.data["images"][0]["alt"] == "图"
    # 页面侧始终按硬上限扫描，筛选与截断都在调用方进程内完成。
    assert driver.link_calls == [{"include_images": True, "scan_limit": MAX_LINKS}]


def test_links_tool_without_matches_is_a_business_failure() -> None:
    driver = _ContentDriver(links=_entries())
    outcome = asyncio.run(
        page_tools.execute_list_page_links({"contains": "不存在的路径"}, driver=driver)  # type: ignore[arg-type]
    )
    assert outcome.success is False
    assert "没有匹配" in outcome.message
    assert outcome.data["links"] == []


def test_links_tool_rejects_unknown_arguments() -> None:
    driver = _ContentDriver()
    with pytest.raises(ValueError, match="未知参数"):
        asyncio.run(page_tools.execute_list_page_links({"depth": 2}, driver=driver))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contains 必须是非空文本"):
        asyncio.run(page_tools.execute_list_page_links({"contains": " "}, driver=driver))  # type: ignore[arg-type]
