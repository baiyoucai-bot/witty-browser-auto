"""元素拖放、PDF 导出与性能采集的执行层。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from witty_browser_auto.agent.locator_tools import locator_recipe
from witty_browser_auto.browser.page_content import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_LINKS,
    MAX_LINKS,
    markdown_options,
    select_links,
)
from witty_browser_auto.browser.page_content import MAX_CHARS as MAX_CHARS_LIMIT
from witty_browser_auto.browser.page_export import PAPER_SIZES, build_print_params
from witty_browser_auto.domain.models import DragRiskClass, Observation
from witty_browser_auto.domain.protocols import AutomationDriver

PAGE_TOOL_NAMES = frozenset(
    {
        "drag_to_element",
        "save_pdf",
        "measure_performance",
        "read_page_markdown",
        "list_page_links",
    }
)


@dataclass(frozen=True, slots=True)
class PageToolOutcome:
    success: bool
    message: str
    data: dict[str, Any]
    model_data: dict[str, Any] | None = None


def element_drag_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "element_drag", False)) and hasattr(driver, "drag_to_element")


def pdf_export_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "pdf_export", False)) and hasattr(driver, "save_page_pdf")


def performance_available(driver: AutomationDriver) -> bool:
    capabilities = getattr(driver, "capabilities", None)
    return bool(getattr(capabilities, "performance", False)) and hasattr(
        driver, "measure_performance"
    )


# ----------------------------------------------------------------------
# drag_to_element
# ----------------------------------------------------------------------


async def execute_drag_to_element(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
    observation: Observation | None = None,
) -> PageToolOutcome:
    if not element_drag_available(driver):
        raise ValueError("当前驱动不支持元素到元素拖放")
    unknown = set(arguments) - {
        "source_target_id",
        "source_locator",
        "target_target_id",
        "target_locator",
        "steps",
        "step_delay_ms",
    }
    if unknown:
        raise ValueError(f"drag_to_element 包含未知参数：{', '.join(sorted(unknown))}")

    source_id, source_locator = _endpoint(arguments, "source")
    target_id, target_locator = _endpoint(arguments, "target")
    _refuse_security_challenge(observation, source_id)
    steps = _bounded_int(arguments, "steps", default=12, low=4, high=60)
    step_delay_ms = _bounded_int(arguments, "step_delay_ms", default=16, low=0, high=200)

    outcome = await driver.drag_to_element(
        source_target_id=source_id,
        source_locator=source_locator,
        target_target_id=target_id,
        target_locator=target_locator,
        steps=steps,
        step_delay_ms=step_delay_ms,
    )
    channel = outcome.get("channel")
    detail = (
        f"走 HTML5 原生拖放通道，携带 {'、'.join(outcome.get('mime_types') or []) or '空'} 数据"
        if channel == "html5"
        else f"走鼠标事件通道，分 {outcome.get('steps')} 步移动"
    )
    return PageToolOutcome(
        success=True,
        message=f"已把「{outcome.get('source')}」拖到「{outcome.get('target')}」上；{detail}",
        data=dict(outcome),
    )


def _refuse_security_challenge(observation: Observation | None, source_id: str | None) -> None:
    """安全挑战必须走 drag / visual_drag，那里才有截图留证与尝试预算。

    判据放在页面与已分类候选两处，不看定位器解析出的风险——后者恒为 unknown，
    拿它当闸门会把本工具服务的看板、排序、拖入文件夹全部挡掉，
    而这类页面往往连一个观察候选都没有，根本无从分类。
    """

    if observation is None:
        return
    if observation.visual_drag_risk is DragRiskClass.SECURITY:
        raise ValueError("当前页面疑似安全挑战，元素拖放不处理挑战，请改用 drag 或 visual_drag")
    if source_id is None:
        return
    for candidate in observation.candidates:
        if candidate.target_id == source_id and candidate.drag_risk is DragRiskClass.SECURITY:
            raise ValueError("源元素疑似安全挑战控件，请改用 drag 或 visual_drag")


def _endpoint(arguments: Mapping[str, Any], side: str):
    raw_id = arguments.get(f"{side}_target_id")
    raw_locator = arguments.get(f"{side}_locator")
    has_id = bool(isinstance(raw_id, str) and raw_id.strip())
    has_locator = raw_locator is not None
    if has_id == has_locator:
        raise ValueError(f"{side} 端必须且只能给出 {side}_target_id 或 {side}_locator 之一")
    if has_locator:
        return None, locator_recipe({"locator": raw_locator})
    return str(raw_id).strip(), None


# ----------------------------------------------------------------------
# save_pdf
# ----------------------------------------------------------------------


async def execute_save_pdf(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
) -> PageToolOutcome:
    if not pdf_export_available(driver):
        raise ValueError("当前驱动不支持 PDF 导出")
    unknown = set(arguments) - {
        "label",
        "paper",
        "landscape",
        "print_background",
        "scale",
        "margin_inches",
        "page_ranges",
        "prefer_css_page_size",
    }
    if unknown:
        raise ValueError(f"save_pdf 包含未知参数：{', '.join(sorted(unknown))}")

    label = arguments.get("label", "page")
    if not isinstance(label, str) or len(label) > 60:
        raise ValueError("label 必须是不超过 60 个字符的字符串")
    params = build_print_params(
        paper=str(arguments.get("paper", "a4")).lower(),
        landscape=_flag(arguments, "landscape", False),
        print_background=_flag(arguments, "print_background", True),
        scale=_number(arguments, "scale", 1.0),
        margin_inches=_number(arguments, "margin_inches", 0.4),
        page_ranges=str(arguments.get("page_ranges", "")),
        prefer_css_page_size=_flag(arguments, "prefer_css_page_size", False),
    )
    outcome = await driver.save_page_pdf(label=label, params=params)
    size_kb = round(outcome["bytes"] / 1024, 1)
    return PageToolOutcome(
        success=True,
        message=f"已导出 PDF 到 {outcome['pdf_path']}，{size_kb} KB",
        data=dict(outcome),
    )


# ----------------------------------------------------------------------
# measure_performance
# ----------------------------------------------------------------------


async def execute_measure_performance(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
) -> PageToolOutcome:
    if not performance_available(driver):
        raise ValueError("当前驱动不支持性能采集")
    unknown = set(arguments) - {"reload", "settle_seconds"}
    if unknown:
        raise ValueError(f"measure_performance 包含未知参数：{', '.join(sorted(unknown))}")
    reload_page = _flag(arguments, "reload", False)
    settle_seconds = _number(arguments, "settle_seconds", 0.5)
    if not 0 <= settle_seconds <= 30:
        raise ValueError("settle_seconds 必须在 0 到 30 秒之间")

    metrics = await driver.measure_performance(
        reload_page=reload_page, settle_seconds=settle_seconds
    )
    vitals = metrics.get("core_web_vitals", {})
    ratings = metrics.get("ratings", {})
    parts = [
        f"{name.upper()} {value}{'' if name == 'cls' else 'ms'} 评级 {ratings.get(name, 'unknown')}"
        for name, value in (
            ("lcp", vitals.get("lcp_ms")),
            ("cls", vitals.get("cls")),
            ("ttfb", vitals.get("ttfb_ms")),
        )
        if value is not None
    ]
    message = "；".join(parts) if parts else "已采集性能数据"
    if not reload_page and vitals.get("lcp_ms") is None:
        # 探测确认：导航后才挂观察器时 buffered 也补不回 LCP，必须说清楚而不是返回 0。
        message += "。LCP 缺席是因为采集器晚于本次导航安装，需要 reload=true 才能测到"
    return PageToolOutcome(success=True, message=message, data=dict(metrics))


# ----------------------------------------------------------------------


async def execute_read_page_markdown(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
) -> PageToolOutcome:
    """把当前页面主内容转成 Markdown，供调用方的模型直接阅读。"""

    _reject_unknown(
        "read_page_markdown",
        arguments,
        {"only_main_content", "selector", "include_images", "include_links", "max_chars"},
    )
    selector = arguments.get("selector")
    if selector is not None and not isinstance(selector, str):
        raise ValueError("selector 必须是文本")
    options = markdown_options(
        only_main_content=_flag(arguments, "only_main_content", True),
        selector=selector,
        include_images=_flag(arguments, "include_images", False),
        include_links=_flag(arguments, "include_links", True),
        max_chars=_bounded_int(
            arguments, "max_chars", default=DEFAULT_MAX_CHARS, low=1000, high=MAX_CHARS_LIMIT
        ),
    )
    result = await driver.read_page_markdown(options)  # type: ignore[attr-defined]
    markdown = str(result.get("markdown", ""))
    truncated = bool(result.get("truncated"))
    payload = {
        "markdown": markdown,
        "truncated": truncated,
        "char_count": int(result.get("char_count", len(markdown))),
        "total_char_count": int(result.get("total_char_count", len(markdown))),
        "title": str(result.get("title", "")),
        "url": str(result.get("url", "")),
        "content_root": str(result.get("root", "")),
        "only_main_content": options["onlyMainContent"],
    }
    return PageToolOutcome(
        success=bool(markdown),
        message=(
            f"已提取 {payload['char_count']} 字符 Markdown"
            + ("，已按上限截断" if truncated else "")
            if markdown
            else "页面主内容为空，可能仍在加载或内容由非文本元素承载"
        ),
        data=payload,
        # 正文本身就是调用方要读的东西，因此模型视图同样给出；上限与截断标记保持一致。
        model_data=payload,
    )


async def execute_list_page_links(
    arguments: Mapping[str, Any],
    *,
    driver: AutomationDriver,
) -> PageToolOutcome:
    """列出页面链接与可选图片，供调用方自行编排站内遍历。"""

    _reject_unknown(
        "list_page_links",
        arguments,
        {"same_origin_only", "contains", "include_images", "limit"},
    )
    contains = arguments.get("contains")
    if contains is not None and (not isinstance(contains, str) or not contains.strip()):
        raise ValueError("contains 必须是非空文本")
    limit = _bounded_int(arguments, "limit", default=DEFAULT_MAX_LINKS, low=1, high=MAX_LINKS)
    include_images = _flag(arguments, "include_images", False)
    result = await driver.read_page_links(  # type: ignore[attr-defined]
        include_images=include_images,
        scan_limit=MAX_LINKS,
    )
    page_url = str(result.get("url", ""))
    raw_links = result.get("links")
    links = select_links(
        raw_links if isinstance(raw_links, list) else [],
        page_url=page_url,
        same_origin_only=_flag(arguments, "same_origin_only", False),
        contains=contains,
        limit=limit,
    )
    images = result.get("images") if include_images else []
    payload: dict[str, Any] = {
        "url": page_url,
        "title": str(result.get("title", "")),
        "links": [dict(item) for item in links],
        "returned_count": len(links),
        "scanned_count": len(raw_links) if isinstance(raw_links, list) else 0,
    }
    if include_images:
        payload["images"] = images if isinstance(images, list) else []
    return PageToolOutcome(
        success=bool(links),
        message=(f"已列出 {len(links)} 个链接" if links else "当前过滤条件下没有匹配的链接"),
        data=payload,
        model_data=payload,
    )


def _reject_unknown(name: str, arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"{name} 包含未知参数：{'、'.join(unknown)}")


def _flag(arguments: Mapping[str, Any], key: str, default: bool) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _number(arguments: Mapping[str, Any], key: str, default: float) -> float:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{key} 必须是数字")
    return float(value)


def _bounded_int(
    arguments: Mapping[str, Any], key: str, *, default: int, low: int, high: int
) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    if not low <= value <= high:
        raise ValueError(f"{key} 必须在 {low} 到 {high} 之间")
    return value


__all__ = [
    "PAGE_TOOL_NAMES",
    "PAPER_SIZES",
    "PageToolOutcome",
    "element_drag_available",
    "execute_drag_to_element",
    "execute_list_page_links",
    "execute_measure_performance",
    "execute_read_page_markdown",
    "execute_save_pdf",
    "pdf_export_available",
    "performance_available",
]
